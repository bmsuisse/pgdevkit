from __future__ import annotations

import asyncio
import gc
import time

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import QueryCanceled
from pydantic import BaseModel
from sqlglot import exp, select

from pgdevkit.db import PgPool, execute, fetch_all, fetch_one, fetch_scalar, set_default_pool
from pgdevkit.db.fetch import _render
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
        await fetch_all(t"SELECT {value}", {"x": 1})  # type: ignore[call-overload]


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
        cur = await con.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE query LIKE '%pg_sleep(30)%' AND state = 'active' AND pid <> pg_backend_pid()"
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0]


async def _wait_for_active_sleeps(pool: PgPool, expected: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while await _active_sleeps(pool) != expected:
        assert time.monotonic() < deadline, f"expected {expected} active pg_sleep(30) queries"
        await asyncio.sleep(0.05)


@requires_podman
async def test_cancel_event_aborts_running_query_and_keeps_borrowed_connection_usable(pool: PgPool):
    cancel = asyncio.Event()
    async with pool.connection() as con:
        task = asyncio.ensure_future(fetch_all("SELECT pg_sleep(30)", con=con, cancel=cancel))
        await _wait_for_active_sleeps(pool, 1)  # really mid-query, not the "already set" early exit
        started = time.monotonic()
        cancel.set()
        with pytest.raises(QueryCanceled):
            await task
        assert time.monotonic() - started < 5
        await con.rollback()
        assert await fetch_all("SELECT 1 AS one", con=con) == [{"one": 1}]
    await _wait_for_active_sleeps(pool, 0)


@requires_podman
async def test_cancelling_the_task_cancels_the_query_on_the_server(pool: PgPool):
    task = asyncio.ensure_future(fetch_all("SELECT pg_sleep(30)", pool=pool))
    await _wait_for_active_sleeps(pool, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_active_sleeps(pool, 0)
    assert await fetch_all("SELECT 1 AS one", pool=pool) == [{"one": 1}]


async def test_unsupported_query_type_and_unsafe_sqlglot_nodes_are_rejected():
    set_default_pool(None)
    with pytest.raises(TypeError, match="unsupported query type"):
        await fetch_all(123)  # type: ignore[call-overload]
    with pytest.raises(ValueError, match="Command"):
        await fetch_all(exp.Command(this="SELECT", expression=" version()"), pool=_NoPool())  # type: ignore[call-overload]
    with pytest.raises(ValueError, match="placeholder name"):
        await fetch_all(select("a").where(exp.Placeholder(this="x); DROP TABLE t;--")), {"x": 1}, pool=_NoPool())


class _NoPool:
    def connection(self):
        raise AssertionError("must be rejected before a connection is needed")


@requires_podman
async def test_second_cancel_during_drain_still_cleans_up(pool: PgPool):
    loop = asyncio.get_running_loop()
    problems: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: problems.append(ctx))
    async with pool.connection() as con:
        task = asyncio.ensure_future(fetch_all("SELECT pg_sleep(30)", con=con))
        await _wait_for_active_sleeps(pool, 1)
        task.cancel()
        await asyncio.sleep(0.002)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await con.rollback()
        assert await fetch_all("SELECT 1 AS one", con=con) == [{"one": 1}]
    await _wait_for_active_sleeps(pool, 0)
    await asyncio.sleep(0.05)
    gc.collect()
    assert problems == []  # no "Task exception was never retrieved"


@requires_podman
async def test_failing_cancel_request_closes_the_connection_and_returns_promptly(
    pool: PgPool, monkeypatch: pytest.MonkeyPatch
):
    # Own connections (not the pool's): the patch below must only affect the one under test.
    dsn = constants.conninfo(TEST_DB)
    observer = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    con = await psycopg.AsyncConnection.connect(dsn)

    async def sleeping() -> int:
        cur = await observer.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(30)%' AND state = 'active' AND pid <> pg_backend_pid()"
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0]

    async def broken_cancel(self, **_kwargs) -> None:
        raise psycopg.OperationalError("cancel connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "cancel_safe", broken_cancel)
    try:
        cancel = asyncio.Event()
        task = asyncio.ensure_future(fetch_all("SELECT pg_sleep(30)", con=con, cancel=cancel))
        deadline = time.monotonic() + 10
        while await sleeping() != 1:
            assert time.monotonic() < deadline, "query never became active"
            await asyncio.sleep(0.05)
        started = time.monotonic()
        cancel.set()
        with pytest.raises(QueryCanceled):
            await task
        assert time.monotonic() - started < 5  # not blocked until the 30 s query finishes
        assert con.closed
    finally:
        monkeypatch.undo()
        # Postgres only notices the closed client when it next talks to it, so end the sleeper
        # explicitly -- it would otherwise block dropping the test database.
        await observer.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE query LIKE '%pg_sleep(30)%' AND pid <> pg_backend_pid()"
        )
        await observer.close()
        await con.close()


# --- fetch_one / fetch_scalar / execute ---------------------------------------------------------


async def test_siblings_validate_their_arguments_before_touching_a_connection():
    with pytest.raises(TypeError, match="model"):
        await fetch_one("SELECT 1", model=Widget, row_mapper=dict)  # type: ignore[call-overload]
    value = 1
    for fn in (fetch_one, fetch_scalar, execute):
        with pytest.raises(TypeError, match="t-string"):
            await fn(t"SELECT {value}", {"x": 1})  # type: ignore[call-overload]
        with pytest.raises(ValueError, match="statement_timeout"):
            await fn("SELECT 1", statement_timeout=0)
        with pytest.raises(RuntimeError, match="set_default_pool"):
            await fn("SELECT 1")
        with pytest.raises(TypeError, match=fn.__name__):
            await fn(123, pool=_NoPool())  # type: ignore[call-overload]
        cancel = asyncio.Event()
        cancel.set()
        with pytest.raises(QueryCanceled, match=fn.__name__):
            await fn("SELECT 1", cancel=cancel, pool=_NoPool())


@requires_podman
async def test_fetch_one_returns_first_row_or_none(pool: PgPool):
    assert await fetch_one("SELECT id, name FROM widget ORDER BY id", pool=pool) == {"id": 1, "name": "sprocket"}
    assert await fetch_one("SELECT id FROM widget WHERE id = %(id)s", {"id": 99}, pool=pool) is None
    assert await fetch_one("SELECT id, name FROM widget WHERE id = 2", model=Widget, pool=pool) == Widget(id=2, name="cog")
    assert await fetch_one("SELECT name FROM widget WHERE id = 2", row_mapper=lambda r: r["name"].upper(), pool=pool) == "COG"
    assert await fetch_one("SELECT name FROM widget WHERE id = 99", model=Widget, pool=pool) is None
    widget_id = 1
    assert await fetch_one(t"SELECT name FROM widget WHERE id = {widget_id}", pool=pool) == {"name": "sprocket"}
    query = select("name").from_("widget").where(exp.column("id").eq(exp.Placeholder(this="id")))
    assert await fetch_one(query, {"id": 2}, pool=pool) == {"name": "cog"}


@requires_podman
async def test_fetch_scalar_returns_first_column_of_first_row_or_none(pool: PgPool):
    set_default_pool(pool)
    assert await fetch_scalar("SELECT count(*) FROM widget") == 2
    assert await fetch_scalar("SELECT name, id FROM widget ORDER BY id DESC") == "cog"
    assert await fetch_scalar("SELECT name FROM widget WHERE id = %(id)s", {"id": 99}) is None
    assert await fetch_scalar("SELECT NULL::int") is None
    assert await fetch_scalar("SELECT 1 AS a, 2 AS a") == 1  # first column, even if names repeat
    assert await fetch_scalar(t"SELECT {5}::int + 1") == 6


@requires_podman
async def test_execute_returns_rowcount_and_commits_on_a_pooled_connection(pool: PgPool):
    assert await execute("UPDATE widget SET name = upper(name)", pool=pool) == 2
    assert await execute("DELETE FROM widget WHERE id = %(id)s", {"id": 1}, pool=pool) == 1
    assert await execute("DELETE FROM widget WHERE id = %(id)s", {"id": 1}, pool=pool) == 0
    new_id = 7
    assert await execute(t"INSERT INTO widget VALUES ({new_id}, 'gear')", pool=pool) == 1
    assert await fetch_all("SELECT id, name FROM widget ORDER BY id", pool=pool) == [
        {"id": 2, "name": "COG"},
        {"id": 7, "name": "gear"},
    ]
    assert await execute("CREATE TABLE extra (id int)", pool=pool) == -1


@requires_podman
async def test_execute_on_a_given_connection_leaves_the_transaction_to_the_caller(pool: PgPool):
    async with pool.connection() as con:
        assert await execute("INSERT INTO widget VALUES (3, 'gear')", con=con) == 1
        assert con.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        await con.rollback()
    assert await fetch_scalar("SELECT count(*) FROM widget", pool=pool) == 2


@requires_podman
async def test_siblings_support_cancel_and_statement_timeout(pool: PgPool):
    cancel = asyncio.Event()
    task = asyncio.ensure_future(fetch_scalar("SELECT pg_sleep(30)", pool=pool, cancel=cancel))
    await _wait_for_active_sleeps(pool, 1)
    cancel.set()
    with pytest.raises(QueryCanceled):
        await task
    await _wait_for_active_sleeps(pool, 0)
    with pytest.raises(QueryCanceled, match="statement timeout"):
        await fetch_one("SELECT pg_sleep(30)", pool=pool, statement_timeout=0.3)
    with pytest.raises(QueryCanceled, match="statement timeout"):
        await execute("SELECT pg_sleep(30)", pool=pool, statement_timeout=0.3)
    assert await fetch_scalar("SELECT 1", pool=pool, statement_timeout=5) == 1


@requires_podman
async def test_default_pool_may_be_a_plain_callable(pool: PgPool):
    set_default_pool(lambda: pool.connection())
    assert await fetch_scalar("SELECT count(*) FROM widget") == 2
    assert await fetch_one("SELECT name FROM widget WHERE id = 1") == {"name": "sprocket"}


# --- literal `%` in sqlglot expressions ----------------------------------------------------------


def test_render_leaves_percent_alone_without_params():
    query = select("a").from_("t").where(exp.column("a").like("50%"))
    assert _render(query) == "SELECT a FROM t WHERE a LIKE '50%'"
    assert _render(query, None) == "SELECT a FROM t WHERE a LIKE '50%'"


@pytest.mark.parametrize("params", [{}, {"id": 1}])
def test_render_doubles_literal_percent_but_not_placeholders_when_params_are_given(params):
    query = (
        select(exp.column("50%"), exp.column("a") % 2)
        .from_("t")
        .where(exp.column("a").like("50%"))
        .where(exp.column("b").eq("%(id)s"))  # a literal that merely looks like a placeholder
        .where(exp.column("c").eq(exp.Placeholder(this="id")))
    )
    assert _render(query, params) == (
        "SELECT \"50%%\", a %% 2 FROM t WHERE (a LIKE '50%%' AND b = '%%(id)s') AND c = %(id)s"
    )
    # the query object itself is left untouched
    assert "pgdevkit_ph_" not in query.sql(dialect="postgres")
    assert query.sql(dialect="postgres").count("%(id)s") == 2  # the placeholder and the literal


def test_render_keeps_ten_or_more_placeholders_apart():
    # (placeholder token _1 must not be mistaken for the start of _10)
    names = [f"p{i}" for i in range(25)]
    query = select(*[exp.Placeholder(this=n) for n in names]).where(exp.column("a").like("x%"))
    assert _render(query, {}) == "SELECT " + ", ".join(f"%({n})s" for n in names) + " WHERE a LIKE 'x%%'"


def test_render_percent_escaping_leaves_other_query_types_alone():
    assert _render("SELECT '50%%'", {}) == "SELECT '50%%'"
    composed = sql.SQL("SELECT {}").format(sql.Literal("50%"))
    assert _render(composed, {}) is composed


@requires_podman
async def test_percent_in_sqlglot_string_literals_works_with_and_without_params(pool: PgPool):
    like = select("name").from_("widget").where(exp.column("name").like("sp%"))
    assert await fetch_all(like, pool=pool) == [{"name": "sprocket"}]
    assert await fetch_all(like, {}, pool=pool) == [{"name": "sprocket"}]
    with_placeholder = like.where(exp.column("id").eq(exp.Placeholder(this="id")))
    assert await fetch_all(with_placeholder, {"id": 1}, pool=pool) == [{"name": "sprocket"}]
    assert await fetch_all(with_placeholder, {"id": 2}, pool=pool) == []
    assert await fetch_scalar(select(exp.Literal.string("50%").as_("pct")), {}, pool=pool) == "50%"
    assert await fetch_scalar(select(exp.Literal.string("50%").as_("pct")), pool=pool) == "50%"
    # the modulo operator, and a literal that looks like a placeholder
    modulo = select((exp.column("id") % 2).as_("odd")).from_("widget").where(exp.column("id").eq(exp.Placeholder(this="id")))
    assert await fetch_scalar(modulo, {"id": 1}, pool=pool) == 1
    assert await fetch_scalar(select(exp.Literal.string("%(id)s")), {}, pool=pool) == "%(id)s"
