from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from pgdevkit.fetch_missing import find_missing_objects, layer_folder_for, reconstruct_ddl
from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

TEST_DB = f"pgdevkit_fetchmissing_selftest_{RUN_SUFFIX}"


def _admin_dsn() -> str:
    return constants.conninfo("postgres")


def _db_dsn() -> str:
    return constants.conninfo(TEST_DB)


def test_layer_folder_for_matches_stripped_sort_prefix(tmp_path: Path):
    (tmp_path / "1_reference_data").mkdir()
    (tmp_path / "2_app").mkdir()
    assert layer_folder_for(tmp_path, "reference_data") == tmp_path / "1_reference_data"
    assert layer_folder_for(tmp_path, "app") == tmp_path / "2_app"


def test_layer_folder_for_falls_back_to_schema_name(tmp_path: Path):
    assert layer_folder_for(tmp_path, "unmatched") == tmp_path / "unmatched"


@pytest.fixture
def fetchmissing_db(tmp_path: Path):
    ensure_container()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    with psycopg.connect(_db_dsn(), autocommit=True) as con:
        con.execute("CREATE SCHEMA app")
        con.execute("CREATE TABLE app.tracked (id serial PRIMARY KEY)")
        con.execute("CREATE TABLE app.untracked (id serial PRIMARY KEY, name text NOT NULL)")
        con.execute("CREATE VIEW app.untracked_view AS SELECT id, name FROM app.untracked")
        con.execute("CREATE FUNCTION app.double_it(x int) RETURNS int AS 'SELECT x * 2' LANGUAGE sql")
        con.execute(
            "CREATE FUNCTION app.list_names() RETURNS TABLE(name text) AS "
            "'SELECT name FROM app.untracked' LANGUAGE sql"
        )

    scripts_dir = tmp_path / "database"
    (scripts_dir / "app" / "tables").mkdir(parents=True)
    (scripts_dir / "app" / "tables" / "tracked.sql").write_text(
        "CREATE TABLE IF NOT EXISTS app.tracked (id serial PRIMARY KEY);", encoding="utf-8"
    )
    yield scripts_dir
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


@requires_podman
def test_find_missing_objects_reports_untracked_table_view_and_functions(fetchmissing_db: Path):
    missing = find_missing_objects(fetchmissing_db, _db_dsn())
    by_name = {m.qualified_name: m.object_type for m in missing}

    assert by_name["app.untracked"] == "table"
    assert by_name["app.untracked_view"] == "view"
    assert by_name["app.double_it"] == "scalar_function"
    assert by_name["app.list_names"] == "table_function"
    assert "app.tracked" not in by_name


@requires_podman
def test_reconstruct_ddl_round_trips_for_each_object_type(fetchmissing_db: Path):
    missing = find_missing_objects(fetchmissing_db, _db_dsn())
    by_name = {m.qualified_name: m for m in missing}

    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        for name in ("app.untracked", "app.untracked_view", "app.double_it", "app.list_names"):
            ddl = reconstruct_ddl(conn, by_name[name])
            conn.execute(ddl)  # must not raise — DDL is valid and re-appliable (CREATE OR REPLACE / IF NOT EXISTS)
