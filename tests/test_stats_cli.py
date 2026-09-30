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
