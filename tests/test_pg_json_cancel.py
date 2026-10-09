"""PostgresJsonResponse cancels the Postgres query when the client goes away (verified in pg_stat_activity)."""

import asyncio
import contextlib
import gc
import inspect
import time
import uuid
from collections.abc import AsyncIterator

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool
from starlette.requests import ClientDisconnect

from pgdevkit.fastapi import PostgresJsonResponse
from pgdevkit.fastapi import json_response
from pgdevkit.fastapi.json_response import _cancel_if_running
from tests._asgi import FakeClient, active_queries, wait_until

SLEEP_30S = "select pg_sleep(30) as slept"
# padded rows: Postgres only flushes its 8 kB output buffer, so tiny rows would all arrive at the very end
SLOW_STREAM = "select i, repeat('x', 2000) as pad, pg_sleep(0.02) as slept from generate_series(1, 2000) i"


@pytest.fixture
def app_name() -> str:
    return f"pgjson-{uuid.uuid4().hex[:12]}"  # isolates this test's backends in pg_stat_activity


@pytest.fixture
async def pool(postgres_dsn: str, app_name: str) -> AsyncIterator[AsyncConnectionPool]:
    kwargs = {"application_name": app_name}
    async with AsyncConnectionPool(postgres_dsn, min_size=1, max_size=1, kwargs=kwargs, open=False) as pool:
        yield pool


async def query_running(dsn: str, app_name: str) -> bool:
    return await active_queries(dsn, app_name) == 1


async def query_stopped(dsn: str, app_name: str) -> bool:
    return await active_queries(dsn, app_name) == 0


async def test_disconnect_before_the_first_byte_cancels_the_query(
    pool: AsyncConnectionPool, postgres_dsn: str, app_name: str
) -> None:
    client = FakeClient()
    task = asyncio.create_task(client.call(PostgresJsonResponse(SLEEP_30S, pool=pool)))
    assert await wait_until(lambda: query_running(postgres_dsn, app_name))

    started = time.monotonic()
    client.disconnect.set()
    await asyncio.wait_for(task, timeout=5)  # finishes quietly: nobody is left to answer

    assert client.sent == []
    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)
    assert time.monotonic() - started < 5, "the 30s query must not run to completion"
    async with pool.connection() as conn:  # the pool replaced the connection we closed
        assert await (await conn.execute("select 1")).fetchone() == (1,)


async def test_disconnect_while_streaming_cancels_the_query(
    pool: AsyncConnectionPool, postgres_dsn: str, app_name: str
) -> None:
    client = FakeClient()
    task = asyncio.create_task(client.call(PostgresJsonResponse(SLOW_STREAM, pool=pool, batch_size=5)))
    assert await wait_until(lambda: len(client.sent) >= 2)  # response started, first rows sent

    client.disconnect.set()
    await asyncio.wait_for(task, timeout=5)

    assert client.sent[-1]["more_body"] is True, "no terminating chunk: the response was cut short"
    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)


async def test_cancelling_the_request_task_cancels_the_query(
    pool: AsyncConnectionPool, postgres_dsn: str, app_name: str
) -> None:
    client = FakeClient()
    task = asyncio.create_task(client.call(PostgresJsonResponse(SLEEP_30S, pool=pool)))
    assert await wait_until(lambda: query_running(postgres_dsn, app_name))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # an outer cancellation is never swallowed
        await task

    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)


async def test_send_failing_mid_stream_cancels_the_query(
    pool: AsyncConnectionPool, postgres_dsn: str, app_name: str
) -> None:
    client = FakeClient()
    client.fail_sends_after = 2  # what servers speaking ASGI spec >= 2.4 do instead of sending http.disconnect
    with pytest.raises(ClientDisconnect):
        await client.call(PostgresJsonResponse(SLOW_STREAM, pool=pool, batch_size=5))

    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)


async def test_disconnect_leaves_a_caller_owned_connection_usable_or_closed(postgres_dsn: str, app_name: str) -> None:
    """Never hand the owner a connection stuck mid-query (ACTIVE): its own cleanup, e.g. commit(), would fail."""
    async with await AsyncConnection.connect(postgres_dsn, application_name=app_name, autocommit=True) as conn:
        client = FakeClient()
        task = asyncio.create_task(client.call(PostgresJsonResponse(SLEEP_30S, con=conn)))
        assert await wait_until(lambda: query_running(postgres_dsn, app_name))

        client.disconnect.set()
        await asyncio.wait_for(task, timeout=5)

        assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)
        assert conn.closed or conn.info.transaction_status == TransactionStatus.IDLE


@pytest.mark.parametrize("source_kind", ["pool", "caller-owned"])
async def test_scope_cancellation_cancels_the_query_and_closes_the_connection(
    pool: AsyncConnectionPool, postgres_dsn: str, app_name: str, source_kind: str
) -> None:
    """Cancellation by an anyio scope (e.g. a middleware) is re-delivered at every await, which defeats psycopg's
    own cleanup: the connection stays ACTIVE and Postgres keeps running the query unless we cancel it, shielded."""
    async with await AsyncConnection.connect(postgres_dsn, application_name=app_name, autocommit=True) as own_conn:
        response = (
            PostgresJsonResponse(SLEEP_30S, pool=pool)
            if source_kind == "pool"
            else PostgresJsonResponse(SLEEP_30S, con=own_conn)
        )
        client = FakeClient()
        async with anyio.create_task_group() as tg:
            tg.start_soon(client.call, response)
            assert await wait_until(lambda: query_running(postgres_dsn, app_name))
            tg.cancel_scope.cancel()

        assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)
        if source_kind == "caller-owned":
            assert own_conn.closed


async def test_a_second_cancellation_during_cleanup_does_not_skip_it(postgres_dsn: str, app_name: str) -> None:
    """A server whose send() fails also reports http.disconnect: the watcher cancels the stream while the cancel
    request is still being sent. That must not leave the connection mid-query (ACTIVE) for its owner."""
    async with await AsyncConnection.connect(postgres_dsn, application_name=app_name, autocommit=True) as conn:
        client = FakeClient()
        client.fail_sends_after = 2
        client.disconnect_when_send_fails = True
        with contextlib.suppress(ClientDisconnect):  # raised, or swallowed as 'the client is gone': both fine
            await client.call(PostgresJsonResponse(SLOW_STREAM, con=conn, batch_size=5))

        assert conn.closed or conn.info.transaction_status == TransactionStatus.IDLE
        assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)


async def test_a_finished_query_is_left_alone(pool: AsyncConnectionPool) -> None:
    """A cancel sent after the query finished could hit the next statement of the pooled connection."""
    async with pool.connection() as conn:
        await (await conn.execute("select 1")).fetchone()
        await _cancel_if_running(conn)
        assert not conn.closed
        assert await (await conn.execute("select 2")).fetchone() == (2,)


def _suspended_row_generators() -> list[str]:
    return [
        o.ag_code.co_name
        for o in gc.get_objects()
        if inspect.isasyncgen(o) and o.ag_frame is not None and o.ag_code.co_name in ("_chunks", "stream")
    ]


@pytest.mark.parametrize("failure", ["send fails", "client disconnects"])
async def test_row_generators_are_finished_before_the_connection_is_closed(
    pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A generator left suspended is finalized later by the event loop, when its connection is long closed: its
    cleanup then registers the dead socket's fd number, which may belong to a new connection by then, and asyncio's
    selector fails with FileNotFoundError (seen once in CI). So they must be closed before the connection is."""
    suspended_at_cleanup: list[str] = []
    original = json_response._cancel_if_running

    async def spy(conn: AsyncConnection) -> None:
        suspended_at_cleanup.extend(_suspended_row_generators())
        await original(conn)

    monkeypatch.setattr(json_response, "_cancel_if_running", spy)
    client = FakeClient()
    if failure == "send fails":
        client.fail_sends_after = 2
    task = asyncio.create_task(client.call(PostgresJsonResponse(SLOW_STREAM, pool=pool, batch_size=5)))
    if failure == "client disconnects":
        assert await wait_until(lambda: len(client.sent) >= 2)
        client.disconnect.set()
    with contextlib.suppress(ClientDisconnect):
        await asyncio.wait_for(task, timeout=5)

    assert suspended_at_cleanup == []
    assert _suspended_row_generators() == []
