from __future__ import annotations

import asyncio
import time

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import QueryCanceled
from pydantic import BaseModel
from sqlglot import exp, select

from pgdevkit.db import PgPool, fetch_all, set_default_pool
from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

TEST_DB = f"pgdevkit_fetch_selftest_{RUN_SUFFIX}"
ENV_PREFIX = "PGDEVKIT_FETCH_SELFTEST_"


class Widget(BaseModel):
    id: int
    name: str


@pytest.fixture(autouse=True)
def _no_default_pool():
    set_default_pool(None)
    yield
    set_default_pool(None)


@pytest.fixture
async def pool(monkeypatch: pytest.MonkeyPatch):
    ensure_container()
    with psycopg.connect(constants.conninfo("postgres"), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    with psycopg.connect(constants.conninfo(TEST_DB), autocommit=True) as con:
        con.execute("CREATE TABLE widget (id int PRIMARY KEY, name text NOT NULL)")
        con.execute("INSERT INTO widget VALUES (1, 'sprocket'), (2, 'cog')")

    monkeypatch.setenv(f"{ENV_PREFIX}HOST", constants.HOST)
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", str(constants.PORT))
    monkeypatch.setenv(f"{ENV_PREFIX}DB", TEST_DB)
    monkeypatch.setenv(f"{ENV_PREFIX}USER", constants.USER)
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", constants.PASSWORD)

    p = PgPool(env_prefix=ENV_PREFIX)
    await p.open()
    yield p
    await p.close()
    with psycopg.connect(constants.conninfo("postgres"), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


async def test_model_and_row_mapper_are_exclusive():
    with pytest.raises(TypeError, match="model"):
        await fetch_all("SELECT 1", model=Widget, row_mapper=dict)  # type: ignore[call-overload]


async def test_without_any_connection_source_raises():
    with pytest.raises(RuntimeError, match="set_default_pool"):
        await fetch_all("SELECT 1")


async def test_t_string_rejects_params():
    value = 1
    with pytest.raises(TypeError, match="t-string"):
        await fetch_all(t"SELECT {value}", {"x": 1})


@requires_podman
async def test_returns_dicts_via_pool_and_default_pool(pool: PgPool):
    assert await fetch_all("SELECT id, name FROM widget ORDER BY id", pool=pool) == [
        {"id": 1, "name": "sprocket"},
        {"id": 2, "name": "cog"},
    ]
    set_default_pool(pool)
    assert await fetch_all("SELECT name FROM widget WHERE id = %(id)s", {"id": 2}) == [{"name": "cog"}]


@requires_podman
async def test_model_and_row_mapper(pool: PgPool):
    widgets = await fetch_all("SELECT id, name FROM widget ORDER BY id", model=Widget, pool=pool)
    assert widgets == [Widget(id=1, name="sprocket"), Widget(id=2, name="cog")]
    names = await fetch_all("SELECT name FROM widget ORDER BY id", row_mapper=lambda r: r["name"].upper(), pool=pool)
    assert names == ["SPROCKET", "COG"]


@requires_podman
async def test_given_connection_is_borrowed_not_finalized(pool: PgPool):
    async with pool.connection() as con:
        await con.execute("INSERT INTO widget VALUES (3, 'gear')")
        # same connection => sees its own uncommitted row, and it stays open and usable afterwards
        assert len(await fetch_all("SELECT id FROM widget", con=con)) == 3
        assert not con.closed
        assert con.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        await con.rollback()
    assert len(await fetch_all("SELECT id FROM widget", pool=pool)) == 2


@requires_podman
async def test_con_and_pool_are_exclusive(pool: PgPool):
    async with pool.connection() as con:
        with pytest.raises(TypeError, match="con"):
            await fetch_all("SELECT 1", con=con, pool=pool)


@requires_podman
async def test_accepts_sqlglot_expression_psycopg_sql_and_t_string(pool: PgPool):
    query = select("name").from_("widget").where(exp.column("id").eq(exp.Placeholder(this="id")))
    assert await fetch_all(query, {"id": 1}, pool=pool) == [{"name": "sprocket"}]
    composed = sql.SQL("SELECT name FROM {t} WHERE id = {i}").format(t=sql.Identifier("widget"), i=sql.Literal(2))
    assert await fetch_all(composed, pool=pool) == [{"name": "cog"}]
    widget_id = 1
    assert await fetch_all(t"SELECT name FROM widget WHERE id = {widget_id}", pool=pool) == [{"name": "sprocket"}]


async def test_already_set_cancel_raises_before_touching_a_connection():
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(QueryCanceled):
        await fetch_all("SELECT pg_sleep(10)", cancel=cancel)


async def _active_sleeps(pool: PgPool) -> int:
    async with pool.connection() as con:
        cur = await con.execute("SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(30)%' AND state = 'active' AND pid <> pg_backend_pid()")
        row = await cur.fetchone()
        assert row is not None
        return row[0]


@requires_podman
async def test_cancel_event_aborts_running_query_and_keeps_borrowed_connection_usable(pool: PgPool):
    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, cancel.set)
    async with pool.connection() as con:
        started = time.monotonic()
        with pytest.raises(QueryCanceled):
            await fetch_all("SELECT pg_sleep(30)", con=con, cancel=cancel)
        assert time.monotonic() - started < 5
        await con.rollback()
        assert await fetch_all("SELECT 1 AS one", con=con) == [{"one": 1}]
    assert await _active_sleeps(pool) == 0


@requires_podman
async def test_cancelling_the_task_cancels_the_query_on_the_server(pool: PgPool):
    task = asyncio.ensure_future(fetch_all("SELECT pg_sleep(30)", pool=pool))
    await asyncio.sleep(0.3)
    assert await _active_sleeps(pool) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _active_sleeps(pool) == 0
    assert await fetch_all("SELECT 1 AS one", pool=pool) == [{"one": 1}]
