"""fetch_all(statement_timeout=...): Postgres aborts the query, and the setting never outlives the call."""

import time
from collections.abc import AsyncIterator

import pytest
from psycopg import AsyncConnection
from psycopg.errors import QueryCanceled
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool

from pgdevkit.db import fetch_all

SLOW = "SELECT pg_sleep(30)"


@pytest.fixture
async def pool(postgres_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(postgres_dsn, min_size=1, max_size=1, open=False) as pool:
        yield pool


async def test_slow_query_is_aborted_by_postgres(pool: AsyncConnectionPool) -> None:
    started = time.monotonic()
    with pytest.raises(QueryCanceled, match="statement timeout"):
        await fetch_all(SLOW, pool=pool, statement_timeout=0.3)
    assert time.monotonic() - started < 5


async def test_fast_query_is_unaffected(pool: AsyncConnectionPool) -> None:
    assert await fetch_all("SELECT 1 AS one", pool=pool, statement_timeout=5) == [{"one": 1}]


async def test_the_timeout_does_not_leak_into_the_pool(pool: AsyncConnectionPool) -> None:
    with pytest.raises(QueryCanceled):
        await fetch_all(SLOW, pool=pool, statement_timeout=0.3)
    assert await fetch_all("SHOW statement_timeout", pool=pool) == [{"statement_timeout": "0"}]
    assert await fetch_all("SELECT 1 AS one", pool=pool) == [{"one": 1}]  # not left in an aborted transaction


async def test_borrowed_connection_gets_its_previous_setting_back(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as con:
        await con.execute("SELECT set_config('statement_timeout', '7s', false)")
        assert await fetch_all("SELECT 1 AS one", con=con, statement_timeout=2) == [{"one": 1}]
        assert await fetch_all("SHOW statement_timeout", con=con) == [{"statement_timeout": "7s"}]
        await con.execute("SELECT set_config('statement_timeout', '0', false)")


async def test_autocommit_connection_works_and_stays_usable(postgres_dsn: str) -> None:
    async with await AsyncConnection.connect(postgres_dsn, autocommit=True) as con:
        assert await fetch_all("SELECT 1 AS one", con=con, statement_timeout=5) == [{"one": 1}]
        with pytest.raises(QueryCanceled, match="statement timeout"):
            await fetch_all(SLOW, con=con, statement_timeout=0.3)
        assert con.info.transaction_status == TransactionStatus.IDLE
        assert await fetch_all("SHOW statement_timeout", con=con) == [{"statement_timeout": "0"}]


@pytest.mark.parametrize("seconds", [0, -1])
async def test_statement_timeout_must_be_positive(seconds: float) -> None:
    with pytest.raises(ValueError, match="statement_timeout"):
        await fetch_all("SELECT 1", statement_timeout=seconds)
