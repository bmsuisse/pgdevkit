from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pgdevkit import migrate, migrate_mariadb
from pgdevkit.backends import get_backend
from pgdevkit.cli import app
from pgdevkit.connection import build_mariadb_conninfo
from pgdevkit.dialect import MARIADB, resolve_dialect

runner = CliRunner()


def test_mariadb_dialect_is_registered_but_has_no_introspection_backend():
    assert resolve_dialect("mariadb") is MARIADB
    with pytest.raises(ValueError, match="no introspection backend"):
        get_backend("mariadb")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("schema_migrations", (None, "schema_migrations")),
        ("ops.migrations", ("ops", "migrations")),
        ("`my db`.`my``table`", ("my db", "my`table")),
    ],
)
def test_split_name(name: str, expected: tuple[str | None, str]):
    assert migrate_mariadb.split_name(name) == expected


@pytest.mark.parametrize("name", ["", "a.b.c", "x`; drop table y;--", "a b"])
def test_split_name_rejects_anything_else(name: str):
    with pytest.raises(ValueError, match="tracking_table"):
        migrate_mariadb.split_name(name)


def test_default_tracking_table_has_no_schema_and_its_own_config_key(tmp_path: Path):
    assert migrate.default_tracking_table(tmp_path, dialect="mariadb") == "schema_migrations"
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pgdevkit]\nmigrations_table = 'public.schema_migrations'\nmariadb_migrations_table = 'ops.migrations'\n"
    )
    assert migrate.default_tracking_table(tmp_path, dialect="mariadb") == "ops.migrations"
    assert migrate.default_tracking_table(tmp_path) == "public.schema_migrations"


def test_strip_comments_keeps_literals():
    sql = "-- gone\nCREATE TABLE a (c text DEFAULT '-- kept', d int) # gone\n/* gone */ -- gone\n"
    out = migrate_mariadb.strip_comments(sql)
    assert "'-- kept'" in out
    assert "gone" not in out


def test_created_table_names_skips_comments_temporary_and_handles_quoting():
    stmts = [
        "CREATE TABLE IF NOT EXISTS `app`.`widgets` (id int)",
        "CREATE TABLE b (id int)",
        "CREATE TEMPORARY TABLE tmp (id int)",
        "-- CREATE TABLE commented (id int)\nSELECT 1",
    ]
    assert migrate_mariadb.created_table_names(stmts) == ["`app`.`widgets`", "b"]


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("CREATE TABLE IF NOT EXISTS `t` (id int)", ("relation", "`t`")),
        ("CREATE VIEW app.v AS SELECT 1", ("relation", "app.v")),
        ("CREATE OR REPLACE VIEW v AS SELECT 1", None),
        ("CREATE DATABASE IF NOT EXISTS app", ("schema", "app")),
        ("ALTER TABLE t ADD COLUMN IF NOT EXISTS c int", ("column", "t", "c")),
        ("ALTER TABLE t ADD `c` int NOT NULL DEFAULT 0", ("column", "t", "c")),
        ("ALTER TABLE t ADD a int, ADD b int", None),
        ("ALTER TABLE t ADD PRIMARY KEY (id)", None),
        ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id)", None),
        ("CREATE INDEX i ON t (c)", None),
        ("INSERT INTO t VALUES (1)", None),
        ("/* CREATE TABLE x (id int) */ CREATE DATABASE app", ("schema", "app")),
    ],
)
def test_idempotent_target(statement: str, expected: tuple[str, ...] | None):
    assert migrate_mariadb.idempotent_target(statement) == expected


def test_missing_privileges_is_postgres_only():
    with pytest.raises(NotImplementedError):
        migrate.missing_privileges("conn", "role", ["t"], dialect="mariadb")


def test_connect_rejects_non_mysql_urls():
    with pytest.raises(ValueError, match="mysql://"):
        migrate_mariadb.connect("postgresql://u:p@h/db")


def test_build_mariadb_conninfo_rejects_entra_user():
    assert build_mariadb_conninfo("mysql://u:p@h/db") == "mysql://u:p@h/db"
    with pytest.raises(ValueError, match="entra-user"):
        build_mariadb_conninfo("mysql://u:p@h/db", "a@b.c")


def test_cli_rejects_entra_user_with_mariadb(tmp_path: Path):
    result = runner.invoke(
        app, ["migrate", "check", str(tmp_path), "--url", "mysql://u:p@h/db", "--dialect", "mariadb", "--entra-user", "a@b.c"]
    )
    assert result.exit_code == 2


@pytest.mark.parametrize("command", ["check", "apply"])
@pytest.mark.parametrize("flag", ["--schema", "--exclude-schema"])
def test_cli_rejects_schema_filters_with_mariadb(tmp_path: Path, command: str, flag: str):
    result = runner.invoke(
        app, ["migrate", command, str(tmp_path), "--url", "mysql://u:p@h/db", "--dialect", "mariadb", flag, "app", "-y"]
    )
    assert result.exit_code == 2


def test_cli_compare_rejects_mariadb(tmp_path: Path):
    result = runner.invoke(app, ["compare", str(tmp_path), "--url", "mysql://u:p@h/db", "--dialect", "mariadb"])
    assert result.exit_code == 2


def test_confirmation_prompt_never_shows_mariadb_credentials(tmp_path: Path):
    result = runner.invoke(
        app,
        ["migrate", "apply", str(tmp_path), "--dialect", "mariadb", "--url", "mysql://user:s3cret@db.example:3306/app"],
        input="n\n",
    )
    assert "s3cret" not in result.output
    assert "db.example:3306/app" in result.output
