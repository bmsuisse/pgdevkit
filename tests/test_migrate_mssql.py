from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pgdevkit import migrate, migrate_mssql
from pgdevkit.cli import app

runner = CliRunner()


class FakeCursor:
    def __init__(self, con: FakeConnection) -> None:
        self.con = con
        self._row: tuple | None = None
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.con.log.append((sql, params))
        if self.con.fail_on and self.con.fail_on in sql:
            raise RuntimeError("boom")
        self._row, self._rows = None, []
        if sql.startswith("select object_id"):
            name = params[0]
            self._row = (1,) if name in self.con.objects else (None,)
        elif sql.startswith("select schema_id"):
            self._row = (1,) if params[0] in self.con.schemas else (None,)
        elif sql.startswith("select col_length"):
            self._row = (4,) if params in self.con.columns else (None,)
        elif sql.startswith("select filename"):
            self._rows = self.con.applied_rows

    def nextset(self) -> bool:
        self.con.log.append(("<nextset>", ()))
        if self.con.fail_on_nextset:
            raise RuntimeError("late boom")
        return False

    def fetchone(self) -> tuple | None:
        return self._row

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, **kw: Any) -> None:
        self.log: list[tuple[str, tuple]] = []
        self.objects: set[str] = kw.get("objects", set())
        self.schemas: set[str] = kw.get("schemas", set())
        self.columns: set[tuple] = kw.get("columns", set())
        self.applied_rows: list[tuple] = kw.get("applied_rows", [])
        self.fail_on: str | None = kw.get("fail_on")
        self.fail_on_nextset: bool = kw.get("fail_on_nextset", False)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass

    def sql(self) -> list[str]:
        return [s for s, _ in self.log]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeConnection:
    con = FakeConnection()
    monkeypatch.setattr(migrate_mssql, "connect", lambda conninfo: con)
    return con


def test_created_table_names_handles_brackets_and_skips_temp_tables():
    batches = ["CREATE TABLE [app].[widgets] (id int)", "CREATE TABLE #tmp (id int)", "CREATE TABLE dbo.b (id int)"]
    assert migrate_mssql.created_table_names(batches) == ["[app].[widgets]", "dbo.b"]


def test_created_table_names_ignores_comments():
    assert migrate_mssql.created_table_names(["-- CREATE TABLE x (id int)\nSELECT 1"]) == []


@pytest.mark.parametrize(
    ("batch", "expected"),
    [
        ("CREATE TABLE app.widgets (id int)", ("relation", "app.widgets")),
        ("CREATE VIEW [app].[v] AS SELECT 1 AS a", ("relation", "[app].[v]")),
        ("CREATE SCHEMA app", ("schema", "app")),
        ("CREATE SCHEMA [app]", ("schema", "app")),
        ("ALTER TABLE app.widgets ADD name nvarchar(10) NULL", ("column", "app.widgets", "name")),
        ("ALTER TABLE app.widgets ADD [name] decimal(10,2)", ("column", "app.widgets", "name")),
        ("ALTER TABLE app.widgets ADD a int, b int", None),
        ("ALTER TABLE app.widgets ADD CONSTRAINT pk PRIMARY KEY (id)", None),
        ("CREATE OR ALTER VIEW app.v AS SELECT 1 AS a", None),
        ("CREATE INDEX ix ON app.widgets (id)", None),
        ("CREATE TABLE a (id int); INSERT INTO a VALUES (1)", None),
        ("INSERT INTO app.widgets (id) VALUES (1)", None),
    ],
)
def test_idempotent_target(batch: str, expected: tuple | None):
    assert migrate_mssql.idempotent_target(batch) == expected


def test_default_tracking_table_for_mssql_is_dbo(tmp_path: Path):
    assert migrate.default_tracking_table(tmp_path, dialect="mssql") == "dbo.schema_migrations"
    assert migrate.default_tracking_table(tmp_path) == "public.schema_migrations"


def test_apply_migration_splits_on_go_runs_one_transaction_and_records(fake: FakeConnection, tmp_path: Path):
    fake.objects = {"app.widgets", "[dbo].[schema_migrations]"}
    path = tmp_path / "001_widgets.sql"
    path.write_text(
        "CREATE TABLE app.widgets (id int);\nGO\nCREATE VIEW app.v AS SELECT id FROM app.widgets;\nGO\n",
        encoding="utf-8",
    )

    result = migrate.apply_migration("conn", path, "dbo.schema_migrations", dialect="mssql")

    assert result.executed and result.verified_tables == ["app.widgets"]
    statements = [q for q in fake.sql() if q != "<nextset>"]
    assert statements[0] == "CREATE TABLE app.widgets (id int);"
    assert statements[1].startswith("CREATE VIEW app.v")
    assert fake.commits >= 2  # migration transaction + tracking insert
    insert = next(s for s in statements if "insert into" in s)
    assert "[dbo].[schema_migrations]" in insert
    assert "not exists" in insert  # idempotent record


def test_apply_migration_rolls_back_on_failure_and_does_not_record(fake: FakeConnection, tmp_path: Path):
    fake.fail_on = "BAD"
    path = tmp_path / "001_bad.sql"
    path.write_text("SELECT 1\nGO\nBAD STATEMENT\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="boom"):
        migrate.apply_migration("conn", path, "dbo.schema_migrations", dialect="mssql")

    assert fake.rollbacks == 1 and fake.commits == 0
    assert not any("insert into" in s for s in fake.sql())


def test_apply_migration_raises_when_created_table_missing(fake: FakeConnection, tmp_path: Path):
    path = tmp_path / "001_widgets.sql"
    path.write_text("CREATE TABLE app.widgets (id int)\n", encoding="utf-8")
    with pytest.raises(migrate.MigrationVerificationError, match="app.widgets"):
        migrate.apply_migration("conn", path, "dbo.schema_migrations", dialect="mssql")


def test_apply_migration_tolerates_missing_tracking_table(fake: FakeConnection, tmp_path: Path):
    path = tmp_path / "001_tracking.sql"
    path.write_text("CREATE TABLE dbo.schema_migrations (filename nvarchar(450))\n", encoding="utf-8")
    fake.objects = {"dbo.schema_migrations"}  # exists for verification, not as [dbo].[schema_migrations]
    result = migrate.apply_migration("conn", path, "dbo.schema_migrations", dialect="mssql")
    assert result.executed
    assert not any("insert into" in s for s in fake.sql())


def test_applied_migrations_raises_tracking_table_missing(fake: FakeConnection):
    with pytest.raises(migrate.TrackingTableMissing):
        migrate.applied_migrations("conn", "dbo.schema_migrations", "mssql")


def test_applied_migrations_reads_rows(fake: FakeConnection):
    fake.objects = {"[dbo].[schema_migrations]"}
    fake.applied_rows = [("001_a.sql", "2026-01-01", "sa")]
    assert migrate.applied_migrations("conn", "dbo.schema_migrations", "mssql") == {
        "001_a.sql": ("2026-01-01", "sa")
    }


def test_already_fully_applied(fake: FakeConnection, tmp_path: Path):
    path = tmp_path / "001.sql"
    path.write_text("CREATE SCHEMA app\nGO\nCREATE TABLE app.t (id int)\nGO\n", encoding="utf-8")
    assert not migrate.already_fully_applied("conn", path, "mssql")
    fake.schemas, fake.objects = {"app"}, {"app.t"}
    assert migrate.already_fully_applied("conn", path, "mssql")


def test_already_fully_applied_false_for_unrecognized_batch(fake: FakeConnection, tmp_path: Path):
    path = tmp_path / "001.sql"
    path.write_text("CREATE TABLE app.t (id int)\nGO\nINSERT INTO app.t VALUES (1)\n", encoding="utf-8")
    fake.objects = {"app.t"}
    assert not migrate.already_fully_applied("conn", path, "mssql")


def test_tracking_table_must_be_schema_dot_table(fake: FakeConnection):
    with pytest.raises(ValueError):
        migrate.applied_migrations("conn", "x]; drop table y;--", "mssql")


def test_missing_privileges_is_postgres_only():
    with pytest.raises(NotImplementedError):
        migrate.missing_privileges("conn", "role", ["a.b"], dialect="mssql")


def test_cli_check_and_apply_with_mssql_dialect(fake: FakeConnection, tmp_path: Path):
    (tmp_path / "001_a.sql").write_text("CREATE SCHEMA app\n", encoding="utf-8")
    fake.objects = {"[dbo].[schema_migrations]"}
    fake.applied_rows = []

    result = runner.invoke(app, ["migrate", "check", str(tmp_path), "--url", "Server=x", "--dialect", "mssql"])
    assert result.exit_code == 0, result.output
    assert "1 pending" in result.output

    result = runner.invoke(app, ["migrate", "apply", str(tmp_path), "--url", "Server=x", "--dialect", "mssql", "-y"])
    assert result.exit_code == 0, result.output
    assert "CREATE SCHEMA app" in fake.sql()
    assert any("insert into [dbo].[schema_migrations]" in s for s in fake.sql())


def test_build_mssql_conninfo():
    from pgdevkit.connection import build_mssql_conninfo

    assert build_mssql_conninfo("Server=x;Database=d") == "Server=x;Database=d"
    assert (
        build_mssql_conninfo("Server=x;Database=d;", "a@b.c")
        == "Server=x;Database=d;Authentication=ActiveDirectoryDefault"
    )
    with pytest.raises(ValueError):
        build_mssql_conninfo("Server=x;authentication = ActiveDirectoryMSI", "a@b.c")


def test_cli_entra_user_with_mssql_adds_authentication(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seen: list[str] = []
    con = FakeConnection(objects={"[dbo].[schema_migrations]"})
    monkeypatch.setattr(migrate_mssql, "connect", lambda conninfo: (seen.append(conninfo), con)[1])
    result = runner.invoke(
        app, ["migrate", "check", str(tmp_path), "--url", "Server=x", "--dialect", "mssql", "--entra-user", "a@b.c"]
    )
    assert result.exit_code == 0, result.output
    assert seen == ["Server=x;Authentication=ActiveDirectoryDefault"]


def test_cli_rejects_conflicting_authentication_and_unknown_dialect(tmp_path: Path):
    result = runner.invoke(
        app,
        ["migrate", "check", str(tmp_path), "--url", "Server=x;Authentication=ActiveDirectoryMSI",
         "--dialect", "mssql", "--entra-user", "a@b.c"],
    )
    assert result.exit_code == 2
    result = runner.invoke(app, ["migrate", "check", str(tmp_path), "--url", "x", "--dialect", "oracle"])
    assert result.exit_code == 2


def test_late_statement_error_surfaced_by_draining_result_sets_rolls_back(fake: FakeConnection, tmp_path: Path):
    fake.fail_on_nextset = True
    path = tmp_path / "001_multi.sql"
    path.write_text("UPDATE t SET a = 1; SELECT 1/0;\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="late boom"):
        migrate.apply_migration("conn", path, "dbo.schema_migrations", dialect="mssql")
    assert fake.commits == 0 and fake.rollbacks == 1
    assert not any("insert into" in s for s in fake.sql())


def test_strip_comments_handles_block_nested_line_and_literals():
    sql = "/* CREATE TABLE a (id int) /* nested */ still comment */ CREATE TABLE b (id int) -- CREATE TABLE c\n" \
          "SELECT '/* not a comment */', '--nope', [we--ird]"
    out = migrate_mssql.strip_comments(sql)
    assert "CREATE TABLE a" not in out and "CREATE TABLE c" not in out
    assert "CREATE TABLE b" in out
    assert "'/* not a comment */'" in out and "'--nope'" in out and "[we--ird]" in out


def test_block_commented_create_table_is_not_a_verification_target():
    batches = ["/* old: CREATE TABLE dbo.legacy (id int) */\nCREATE TABLE dbo.real (id int)"]
    assert migrate_mssql.created_table_names(batches) == ["dbo.real"]
    assert migrate_mssql.idempotent_target("/* CREATE TABLE x (id int) */ CREATE SCHEMA app") == ("schema", "app")


def test_mssql_tracking_table_ignores_postgres_migrations_table_key(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pgdevkit]\nmigrations_table = 'public.schema_migrations'\nmssql_migrations_table = 'ops.migrations'\n"
    )
    assert migrate.default_tracking_table(tmp_path, dialect="mssql") == "ops.migrations"
    assert migrate.default_tracking_table(tmp_path) == "public.schema_migrations"

    (tmp_path / "pyproject.toml").write_text("[tool.pgdevkit]\nmigrations_table = 'public.schema_migrations'\n")
    assert migrate.default_tracking_table(tmp_path, dialect="mssql") == "dbo.schema_migrations"


def test_confirmation_prompt_never_shows_mssql_credentials(fake: FakeConnection, tmp_path: Path):
    (tmp_path / "001_a.sql").write_text("CREATE SCHEMA app\n", encoding="utf-8")
    fake.objects = {"[dbo].[schema_migrations]"}
    result = runner.invoke(
        app,
        ["migrate", "apply", str(tmp_path), "--dialect", "mssql",
         "--url", "Server=h,1433;Database=d;UID=u;PWD=s3@cret"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "h,1433/d" in result.output
    assert "s3@cret" not in result.output and "PWD" not in result.output


def test_postgres_target_description_unchanged():
    from pgdevkit.cli import _describe_target

    assert _describe_target("postgresql://u:p@host:5432/db", "postgres") == "host:5432/db"


def test_missing_mssql_extra_gives_install_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def boom() -> None:
        raise ImportError("MSSQL support needs the mssql extra: pip install pgdevkit[mssql]")

    monkeypatch.setattr(migrate_mssql, "require_driver", boom)
    result = runner.invoke(app, ["migrate", "apply", str(tmp_path), "--url", "Server=x", "--dialect", "mssql", "-y"])
    assert result.exit_code == 2
    assert "pgdevkit[mssql]" in result.output


def test_compare_entra_user_with_mssql_uses_authentication_keyword(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seen: list[str] = []

    class StubBackend:
        from pgdevkit.dialect import MSSQL as dialect

        def introspect(self, conninfo: str):
            seen.append(conninfo)
            from pgdevkit.models import DatabaseSchema

            return DatabaseSchema()

    monkeypatch.setattr("pgdevkit.cli.get_backend", lambda d: StubBackend())
    result = runner.invoke(
        app,
        ["compare", "--url", "Server=x", "--dialect", "mssql", "--entra-user", "a@b.c", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert seen == ["Server=x;Authentication=ActiveDirectoryDefault"]
