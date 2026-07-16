from __future__ import annotations

from pathlib import Path

import psycopg
from typer.testing import CliRunner

from pgdevkit.cli import app
from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

runner = CliRunner()

TEST_DB = f"pgdevkit_fetchmissing_cli_selftest_{RUN_SUFFIX}"


def _admin_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/postgres"


def _db_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/{TEST_DB}"


@requires_podman
def test_fetch_missing_dry_run_then_write(tmp_path: Path):
    ensure_container()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    with psycopg.connect(_db_dsn(), autocommit=True) as con:
        con.execute("CREATE TABLE public.orphan (id serial PRIMARY KEY, name text NOT NULL)")

    scripts_dir = tmp_path / "database"
    scripts_dir.mkdir()

    try:
        dry = runner.invoke(app, ["fetch-missing", str(scripts_dir), "--url", _db_dsn()])
        assert dry.exit_code == 0, dry.output
        assert "public.orphan" in dry.output
        assert "Dry run" in dry.output
        assert not any(scripts_dir.rglob("*.sql"))

        written = runner.invoke(app, ["fetch-missing", str(scripts_dir), "--url", _db_dsn(), "--write"])
        assert written.exit_code == 0, written.output
        dest = scripts_dir / "public" / "tables" / "orphan.sql"
        assert dest.exists()
        assert "orphan" in dest.read_text(encoding="utf-8")
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as con:
            con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
