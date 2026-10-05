from __future__ import annotations

import json
from pathlib import Path

import psycopg
from typer.testing import CliRunner

from pgdevkit.cli import app
from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

runner = CliRunner()

TEST_DB = f"pgdevkit_stats_cli_selftest_{RUN_SUFFIX}"


def test_get_stats_reads_json_without_db(tmp_path: Path):
    stats = tmp_path / "_stats"
    stats.mkdir()
    (stats / "_tables.json").write_text(json.dumps({"public.a": {"row_count": 3}, "public.b": {"row_count": 1}}))
    (stats / "public.a.json").write_text(json.dumps({"id": {"data_type": "integer"}}))

    r = runner.invoke(app, ["get-stats", str(tmp_path), "public.a"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {"public.a": {"row_count": 3, "columns": {"id": {"data_type": "integer"}}}}

    r = runner.invoke(app, ["get-stats", str(tmp_path), "--no-columns"])
    assert list(json.loads(r.stdout)) == ["public.a", "public.b"]

    assert runner.invoke(app, ["get-stats", str(tmp_path), "public.nope"]).exit_code == 2


@requires_podman
def test_update_stats_then_get_stats(tmp_path: Path):
    ensure_container()
    admin = constants.conninfo("postgres")
    dsn = constants.conninfo(TEST_DB)
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    try:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute("CREATE TABLE public.zeta (id int PRIMARY KEY, note text)")
            con.execute("CREATE TABLE public.alpha (id int PRIMARY KEY)")
            con.execute("INSERT INTO public.zeta SELECT g, NULL FROM generate_series(1, 10) g")

        r = runner.invoke(app, ["update-stats", str(tmp_path), "--url", dsn, "--exact", "--analyze"])
        assert r.exit_code == 0, r.output
        tables = json.loads((tmp_path / "_stats" / "_tables.json").read_text())
        assert list(tables) == ["public.alpha", "public.zeta"]
        assert tables["public.zeta"]["row_count"] == 10
        cols = json.loads((tmp_path / "_stats" / "public.zeta.json").read_text())
        assert cols["note"]["null_fraction"] == 1

        # A partial update keeps other tables' entries.
        r = runner.invoke(app, ["update-stats", str(tmp_path), "--url", dsn, "--table", "public.alpha"])
        assert r.exit_code == 0, r.output
        assert "public.zeta" in json.loads((tmp_path / "_stats" / "_tables.json").read_text())

        assert runner.invoke(app, ["update-stats", str(tmp_path), "--url", dsn, "--table", "public.nope"]).exit_code == 2

        # A full run prunes dropped tables.
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute("DROP TABLE public.alpha")
        assert runner.invoke(app, ["update-stats", str(tmp_path), "--url", dsn]).exit_code == 0
        assert list(json.loads((tmp_path / "_stats" / "_tables.json").read_text())) == ["public.zeta"]
        assert not (tmp_path / "_stats" / "public.alpha.json").exists()

        got = json.loads(runner.invoke(app, ["get-stats", str(tmp_path), "public.zeta"]).stdout)
        assert got["public.zeta"]["columns"]["id"]["data_type"] == "integer"
    finally:
        with psycopg.connect(admin, autocommit=True) as con:
            con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


class _FakeMssqlCursor:
    def __init__(self, conn):
        self.conn, self.description, self.rows = conn, None, []

    def execute(self, sql, params=()):
        self.conn.executed.append(sql)
        if "sys.dm_db_partition_stats" in sql:
            self.description = [(n,) for n in ("schema", "name", "estimated_rows", "table_bytes", "index_bytes", "total_bytes")]
            self.rows = [("dbo", "zeta", 10, 8192, 16384, 24576), ("sys", "junk", 1, 0, 0, 0)]
        elif "FROM sys.columns" in sql:
            self.description = [(n,) for n in ("name", "base_type", "max_length", "precision", "scale")]
            self.rows = [("id", "int", 4, 10, 0), ("note", "nvarchar", 100, 0, 0)]
        elif "COUNT_BIG(*)" in sql:
            names = ("n", "nn0", "nd0", "w0", "nn1", "nd1", "w1")
            self.description, self.rows = [(n,) for n in names], [(10, 10, 10, 4.0, 0, 0, None)]

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _FakeMssqlConn:
    def __init__(self):
        self.executed: list[str] = []

    def cursor(self):
        return _FakeMssqlCursor(self)

    def execute(self, sql):
        self.executed.append(sql)

    def close(self):
        pass


def test_update_stats_mssql(tmp_path: Path, monkeypatch):
    import mssql_python

    conn = _FakeMssqlConn()
    monkeypatch.setattr(mssql_python, "connect", lambda *a, **k: conn)

    r = runner.invoke(app, ["update-stats", str(tmp_path), "--url", "x", "--dialect", "mssql", "--exact", "--analyze"])
    assert r.exit_code == 0, r.output
    tables = json.loads((tmp_path / "_stats" / "_tables.json").read_text())
    assert list(tables) == ["dbo.zeta"]  # system schema skipped
    assert tables["dbo.zeta"]["row_count"] == 10 and tables["dbo.zeta"]["row_count_exact"] is True
    assert tables["dbo.zeta"]["total_bytes"] == 24576
    cols = json.loads((tmp_path / "_stats" / "dbo.zeta.json").read_text())
    assert cols["note"]["data_type"] == "nvarchar(50)" and cols["note"]["null_fraction"] == 1
    assert cols["id"]["n_distinct"] == 10 and cols["id"]["avg_width"] == 4
    assert "UPDATE STATISTICS [dbo].[zeta]" in conn.executed

    assert runner.invoke(app, ["update-stats", str(tmp_path), "--url", "x", "--dialect", "mssql", "--table", "dbo.nope"]).exit_code == 2
    assert runner.invoke(app, ["update-stats", str(tmp_path), "--url", "x", "--dialect", "oracle"]).exit_code == 2
