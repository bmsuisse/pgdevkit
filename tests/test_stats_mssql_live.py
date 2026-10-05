from __future__ import annotations

import json
from pathlib import Path

import mssql_python
import pytest
from typer.testing import CliRunner

from pgdevkit.cli import app
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, status
from tests.testdb.conftest import _make_project, requires_mssql

# Selected only by the dedicated `mssql-test` CI job; see test_compare_mssql_live.py.
pytestmark = pytest.mark.mssql

runner = CliRunner()


def _exec(dsn: str, sql: str) -> None:
    conn = mssql_python.connect(dsn, autocommit=True)
    try:
        conn.cursor().execute(sql)
    finally:
        conn.close()


@requires_mssql
def test_update_stats_mssql_live(tmp_path: Path):
    project = _make_project(tmp_path, "mssqlstats", "main", engine="mssql")
    out = tmp_path / "out"
    out.mkdir()
    try:
        ensure_testdb(project)
        dsn = status(project)["dsn"]
        _exec(dsn, "INSERT INTO app.widget (id, name, tags) VALUES (2, N'gear', NULL), (3, N'cog', NULL)")

        r = runner.invoke(app, ["update-stats", str(out), "--url", dsn, "--dialect", "mssql", "--exact", "--analyze"])
        assert r.exit_code == 0, r.output
        tables = json.loads((out / "_stats" / "_tables.json").read_text())
        assert "app.widget" in tables and not any(k.startswith("sys.") for k in tables)
        assert tables["app.widget"]["row_count"] == 3
        assert tables["app.widget"]["row_count_exact"] is True
        assert tables["app.widget"]["total_bytes"] > 0
        cols = json.loads((out / "_stats" / "app.widget.json").read_text())
        assert cols["name"]["data_type"] == "nvarchar(100)"
        assert cols["id"]["n_distinct"] == 3 and cols["id"]["null_fraction"] == 0
        assert cols["tags"]["null_fraction"] == 1

        # Without --exact: engine-maintained row count, no column measurements.
        r = runner.invoke(app, ["update-stats", str(out), "--url", dsn, "--dialect", "mssql", "--table", "app.widget"])
        assert r.exit_code == 0, r.output
        assert json.loads((out / "_stats" / "_tables.json").read_text())["app.widget"]["row_count"] == 3
        assert json.loads((out / "_stats" / "app.widget.json").read_text())["id"]["n_distinct"] is None

        bad = ["update-stats", str(out), "--url", dsn, "--dialect", "mssql", "--table", "app.nope"]
        assert runner.invoke(app, bad).exit_code == 2
    finally:
        clean_testdb(project)
