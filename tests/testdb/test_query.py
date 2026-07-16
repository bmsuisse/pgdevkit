from __future__ import annotations

import psycopg
import pytest

from pgdevkit.testdb import constants, query
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import requires_podman

TEST_DB = "pgdevkit_query_selftest"


def _admin_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/postgres"


def _db_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/{TEST_DB}"


@pytest.fixture
def query_test_db():
    ensure_container()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    yield
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


@requires_podman
async def test_execute_returns_rows_for_select(query_test_db):
    rows = await query.execute(_db_dsn(), "SELECT 1 AS one, 2 AS two")
    assert rows == [{"one": 1, "two": 2}]


@requires_podman
async def test_execute_returns_none_for_ddl(query_test_db):
    rows = await query.execute(_db_dsn(), "CREATE TABLE t (id int)")
    assert rows is None


@requires_podman
async def test_execute_runs_multiple_statements_and_returns_last(query_test_db):
    rows = await query.execute(
        _db_dsn(),
        "CREATE TABLE t2 (id int); INSERT INTO t2 VALUES (1); SELECT * FROM t2",
    )
    assert rows == [{"id": 1}]
