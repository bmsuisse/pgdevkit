"""PostgresJsonResponse cancels the Postgres query when the client goes away (verified in pg_stat_activity)."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from starlette.requests import ClientDisconnect

from pgdevkit.fastapi import PostgresJsonResponse
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
    task = asyncio.create_task(client.call(PostgresJsonResponse(pool.connection, SLEEP_30S)))
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
    task = asyncio.create_task(client.call(PostgresJsonResponse(pool.connection, SLOW_STREAM, batch_size=5)))
    assert await wait_until(lambda: len(client.sent) >= 2)  # response started, first rows sent

    client.disconnect.set()
    await asyncio.wait_for(task, timeout=5)

    assert client.sent[-1]["more_body"] is True, "no terminating chunk: the response was cut short"
    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)


async def test_cancelling_the_request_task_cancels_the_query(
    pool: AsyncConnectionPool, postgres_dsn: str, app_name: str
) -> None:
    client = FakeClient()
    task = asyncio.create_task(client.call(PostgresJsonResponse(pool.connection, SLEEP_30S)))
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
        await client.call(PostgresJsonResponse(pool.connection, SLOW_STREAM, batch_size=5))

    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)


async def test_caller_owned_connection_is_cancelled_and_closed(postgres_dsn: str, app_name: str) -> None:
    """After a cancel the connection still has the cancelled query's results pending: nobody can reuse it."""
    async with await AsyncConnection.connect(postgres_dsn, application_name=app_name, autocommit=True) as conn:
        client = FakeClient()
        task = asyncio.create_task(client.call(PostgresJsonResponse(conn, SLEEP_30S)))
        assert await wait_until(lambda: query_running(postgres_dsn, app_name))

        client.disconnect.set()
        await asyncio.wait_for(task, timeout=5)

        assert conn.closed
        assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)
    # leaving the `async with` of an already closed connection must not raise


async def test_a_finished_query_is_left_alone(pool: AsyncConnectionPool) -> None:
    """A cancel sent after the query finished could hit the next statement of the pooled connection."""
    async with pool.connection() as conn:
        await (await conn.execute("select 1")).fetchone()
        await _cancel_if_running(conn)
        assert not conn.closed
        assert await (await conn.execute("select 2")).fetchone() == (2,)
