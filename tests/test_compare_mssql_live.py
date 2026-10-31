from __future__ import annotations

import json
from pathlib import Path

import pytest

from pgdevkit.backends import get_backend
from pgdevkit.diff import compute_diff
from pgdevkit.parser import parse_directory
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, status
# _make_project (not the project_factory fixture) -- project_factory lives
# in tests/testdb/conftest.py, whose fixtures pytest only injects into tests
# under tests/testdb/ itself. This file sits outside that directory, so it
# calls the same helper directly with its own tmp_path instead.
from tests.testdb.conftest import _make_project, requires_mssql

# See tests/testdb/test_api_mssql_live.py's module docstring for why this is
# marked `mssql` (selected only by the dedicated CI job) rather than run
# everywhere. This test in particular is the main way the `sys.*` catalog
# queries in pgdevkit/mssql_introspect.py get validated against a real SQL
# Server at all -- there's no way to check their syntactic correctness
# offline.
pytestmark = pytest.mark.mssql


@requires_mssql
def test_introspection_matches_applied_scripts_with_no_diff(tmp_path: Path):
    project = _make_project(tmp_path, "mssqlcompare", "main", engine="mssql")
    try:
        ensure_testdb(project)
        conninfo = status(project)["dsn"]

        scripts = parse_directory(project / "database", dialect="mssql")
        db = get_backend("mssql").introspect(conninfo)
        diffs = compute_diff(scripts, db, dialect="mssql")

        assert diffs == [], diffs
    finally:
        clean_testdb(project)


_NAV_TABLES = ["Company$Detailed Cust_ Ledg_ Entry", "Compañía$Kunde é Posten", "Company$Item Ledger Entry"]


def _write_nav_style_database(project: Path) -> None:
    """A NAV-export-like database/ layout: names with spaces, `$` and accents,
    a composite primary key with a varbinary(900) column, every identifier
    bracket-quoted, and a view over each table."""
    (project / "database").unlink()  # _make_project symlinks the shared fixtures
    tables = project / "database" / "dbo" / "tables"
    views = project / "database" / "dbo" / "views"
    tables.mkdir(parents=True)
    views.mkdir(parents=True)
    for name in _NAV_TABLES:
        (tables / f"{name}.sql").write_text(
            f"""CREATE TABLE dbo.[{name}] (
    [timestamp] timestamp NOT NULL,
    [Entry No_] int NOT NULL,
    [Key Bin] varbinary(900) NOT NULL,
    [Description] nvarchar(100) NOT NULL DEFAULT '',
    [Amount] decimal(38,20) NOT NULL DEFAULT 0,
    [Posting Date] datetime NOT NULL DEFAULT '1753-01-01',
    [Guid] uniqueidentifier NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    [Flag] tinyint NOT NULL DEFAULT 0,
    CONSTRAINT [{name}$0] PRIMARY KEY CLUSTERED ([Entry No_], [Key Bin])
);
CREATE INDEX [{name}$1] ON dbo.[{name}] ([Posting Date], [Flag]);
""",
            encoding="utf-8",
        )
        (views / f"{name} View.sql").write_text(
            f"CREATE VIEW dbo.[{name} View] AS SELECT [Entry No_], [Amount] FROM dbo.[{name}];\n", encoding="utf-8"
        )


@requires_mssql
def test_nav_style_object_names_apply_introspect_and_compare_without_diffs(tmp_path: Path):
    project = _make_project(tmp_path, "mssqlnav", "main", engine="mssql")
    _write_nav_style_database(project)
    try:
        ensure_testdb(project)
        conninfo = status(project)["dsn"]

        scripts = parse_directory(project / "database", dialect="mssql")
        db = get_backend("mssql").introspect(conninfo)

        assert set(db.tables) == {f"dbo.{n}" for n in _NAV_TABLES}
        assert set(db.views) == {f"dbo.{n} View" for n in _NAV_TABLES}
        assert compute_diff(scripts, db, dialect="mssql") == []
    finally:
        clean_testdb(project)


@requires_mssql
def test_nav_style_object_names_are_quoted_by_update_stats(tmp_path: Path):
    from typer.testing import CliRunner

    from pgdevkit.cli import app

    project = _make_project(tmp_path, "mssqlnavstats", "main", engine="mssql")
    _write_nav_style_database(project)
    out = tmp_path / "out"
    out.mkdir()
    try:
        ensure_testdb(project)
        r = CliRunner().invoke(
            app, ["update-stats", str(out), "--url", status(project)["dsn"], "--dialect", "mssql", "--exact"]
        )
        assert r.exit_code == 0, r.output
        tables = json.loads((out / "_stats" / "_tables.json").read_text())
        assert {f"dbo.{n}" for n in _NAV_TABLES} <= set(tables)
    finally:
        clean_testdb(project)
