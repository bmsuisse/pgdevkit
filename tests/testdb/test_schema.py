from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from pgdevkit.testdb.schema import apply_schema
from tests.testdb.conftest import requires_podman

FIXTURES = Path(__file__).parent / "fixtures" / "database"
TEST_DB = "pgdevkit_schema_selftest"


def _admin_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/postgres"


def _db_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/{TEST_DB}"


@pytest.fixture
def schema_test_db():
    ensure_container()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    yield
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


@requires_podman
async def test_apply_schema_creates_tables_and_seeds_data(schema_test_db):
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)
        async with con.cursor() as cur:
            await cur.execute("SELECT id, name FROM app.widget ORDER BY id")
            rows = await cur.fetchall()
    assert rows == [(1, "sprocket")]


@requires_podman
async def test_apply_schema_is_idempotent(schema_test_db):
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)
        await apply_schema(con, FIXTURES)  # must not raise
        async with con.cursor() as cur:
            await cur.execute("SELECT count(*) FROM app.widget")
            (count,) = await cur.fetchone()
    assert count == 1


@requires_podman
async def test_apply_schema_on_missing_directory_is_a_noop(schema_test_db):
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES.parent / "does_not_exist")  # must not raise
