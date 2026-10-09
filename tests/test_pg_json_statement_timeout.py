"""PostgresJsonResponse(statement_timeout=...): Postgres aborts a slow query, before or after the first byte."""

import json
import time
from collections.abc import AsyncIterator

import pytest
from psycopg import AsyncConnection
from psycopg.errors import QueryCanceled
from psycopg_pool import AsyncConnectionPool

from pgdevkit.db import at_most
from pgdevkit.fastapi import PostgresJsonResponse
from tests._asgi import FakeClient

SLOW = "select pg_sleep(30) as slept"
# the first 1000 rows (one batch) are fast, then row 1500 stalls
SLOW_AFTER_FIRST_BATCH = (
    "select i, repeat('x', 100) as pad, pg_sleep(case when i = 1500 then 30 else 0 end) as slept "
    "from generate_series(1, 2000) i"
)


class VerboseResponse(PostgresJsonResponse):
    expose_errors = True


@pytest.fixture
async def pool(postgres_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(postgres_dsn, min_size=1, max_size=1, open=False) as pool:
        yield pool


async def show_statement_timeout(pool: AsyncConnectionPool) -> str:
    async with pool.connection() as conn:
        row = await (await conn.execute("show statement_timeout")).fetchone()
        assert row is not None
        return row[0]


async def test_timeout_before_the_first_byte_is_a_504(pool: AsyncConnectionPool) -> None:
    client = FakeClient()
    started = time.monotonic()
    await client.call(PostgresJsonResponse(SLOW, pool=pool, statement_timeout=0.3))

    assert time.monotonic() - started < 5
    assert client.sent[0]["status"] == 504
    assert json.loads(client.body) == {"error": "Query timed out"}
    assert await show_statement_timeout(pool) == "0"  # the pooled connection is clean again


async def test_timeout_after_the_first_byte_cuts_the_response_short(pool: AsyncConnectionPool) -> None:
    client = FakeClient()
    with pytest.raises(QueryCanceled, match="statement timeout"):
        await client.call(PostgresJsonResponse(SLOW_AFTER_FIRST_BATCH, pool=pool, statement_timeout=0.5))

    assert client.sent[0]["status"] == 200
    assert client.body.startswith(b'[{"i":1')
    assert not client.body.rstrip().endswith(b"]")  # unterminated: can't be mistaken for a complete array
    assert await show_statement_timeout(pool) == "0"


async def test_fast_query_with_a_timeout_is_complete(pool: AsyncConnectionPool) -> None:
    client = FakeClient()
    await client.call(
        PostgresJsonResponse("select i from generate_series(1, 2500) i", pool=pool, statement_timeout=10)
    )
    assert [r["i"] for r in json.loads(client.body)] == list(range(1, 2501))


async def test_caller_owned_connection_gets_its_previous_setting_back(postgres_dsn: str) -> None:
    async with await AsyncConnection.connect(postgres_dsn) as conn:
        await conn.execute("select set_config('statement_timeout', '7s', false)")
        client = FakeClient()
        await client.call(PostgresJsonResponse("select 1 as a", con=conn, statement_timeout=2))
        assert json.loads(client.body) == [{"a": 1}]
        assert (await (await conn.execute("show statement_timeout")).fetchone()) == ("7s",)


async def test_autocommit_connection_is_refused_with_a_clear_error(postgres_dsn: str) -> None:
    async with await AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        client = FakeClient()
        await client.call(VerboseResponse("select 1 as a", con=conn, statement_timeout=2))
    assert client.sent[0]["status"] == 500
    assert "autocommit" in json.loads(client.body)["error"]


@pytest.mark.parametrize("seconds", [0, -1])
def test_statement_timeout_must_be_positive(pool: AsyncConnectionPool, seconds: float) -> None:
    with pytest.raises(ValueError, match="statement_timeout"):
        PostgresJsonResponse("select 1", pool=pool, statement_timeout=seconds)


async def test_at_most_never_loosens_a_caller_owned_connections_timeout(postgres_dsn: str) -> None:
    async with await AsyncConnection.connect(postgres_dsn) as conn:
        await conn.execute("select set_config('statement_timeout', '600s', false)")
        await conn.commit()
        query = "select setting::bigint as ms from pg_settings where name = 'statement_timeout'"
        for timeout, expected in ((at_most(900), 600_000), (at_most(120), 120_000), (900, 900_000)):
            client = FakeClient()
            await client.call(PostgresJsonResponse(query, con=conn, statement_timeout=timeout))
            assert json.loads(client.body) == [{"ms": expected}]
        await conn.execute("select set_config('statement_timeout', '0', false)")
        await conn.commit()
