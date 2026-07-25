from __future__ import annotations

import psycopg
import pytest

from pgdevkit.testdb import constants, query
from pgdevkit.testdb.container import ensure_container
from pgdevkit.testdb.query import _split_statements, split_tsql_batches
from tests.testdb.conftest import RUN_SUFFIX, requires_podman


def test_split_statements_ignores_semicolon_inside_dollar_quote():
    sql = """
    CREATE FUNCTION f() RETURNS int AS $$
    BEGIN
        RETURN 1;
    END;
    $$ LANGUAGE plpgsql;
    SELECT f();
    """
    statements = _split_statements(sql)
    assert len(statements) == 2
    assert "RETURN 1;" in statements[0]
    assert statements[1] == "SELECT f();" or statements[1].startswith("SELECT f()")


def test_split_statements_ignores_semicolon_inside_string_literal():
    statements = _split_statements("INSERT INTO t (s) VALUES ('a;b'); SELECT 1")
    assert statements == ["INSERT INTO t (s) VALUES ('a;b')", "SELECT 1"]


def test_split_statements_handles_tagged_dollar_quotes():
    statements = _split_statements("SELECT $tag$a;b$tag$ AS x; SELECT 2")
    assert statements == ["SELECT $tag$a;b$tag$ AS x", "SELECT 2"]

def test_split_tsql_batches_splits_on_standalone_go_line():
    sql = "CREATE TABLE dbo.widget (id INT);\nGO\nINSERT INTO dbo.widget VALUES (1);\nGO\n"
    batches = split_tsql_batches(sql)
    assert len(batches) == 2
    assert batches[0] == "CREATE TABLE dbo.widget (id INT);"
    assert batches[1] == "INSERT INTO dbo.widget VALUES (1);"


def test_split_tsql_batches_with_no_go_returns_single_batch():
    sql = "SELECT 1;\nSELECT 2;"
    assert split_tsql_batches(sql) == [sql]


def test_split_tsql_batches_ignores_go_inside_block_comment():
    sql = "SELECT 1;\n/* remember:\nGO\ndo something */\nSELECT 2;"
    batches = split_tsql_batches(sql)
    assert len(batches) == 1
    assert "GO" in batches[0]


def test_split_tsql_batches_accepts_go_with_repeat_count():
    sql = "SELECT 1;\nGO 3\nSELECT 2;"
    batches = split_tsql_batches(sql)
    assert batches == ["SELECT 1;", "SELECT 2;"]


TEST_DB = f"pgdevkit_query_selftest_{RUN_SUFFIX}"


def _admin_dsn() -> str:
    return constants.conninfo("postgres")


def _db_dsn() -> str:
    return constants.conninfo(TEST_DB)


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
