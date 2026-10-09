"""readonly_transaction(): writes are refused inside, and the setting never leaks into the pool."""

from collections.abc import AsyncIterator

import pytest
from psycopg import AsyncConnection, ProgrammingError
from psycopg.errors import ReadOnlySqlTransaction
from psycopg.pq import TransactionStatus
from psycopg_pool import AsyncConnectionPool

from pgdevkit.db import fetch_all, fetch_scalar, readonly_transaction

WRITE = "CREATE TABLE pgdevkit_readonly_probe (id int)"
CLEANUP = "DROP TABLE IF EXISTS pgdevkit_readonly_probe"


@pytest.fixture
async def pool(postgres_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    # one connection only: every borrower gets the very same (possibly tainted) connection back
    async with AsyncConnectionPool(postgres_dsn, min_size=1, max_size=1, open=False) as pool:
        yield pool
        async with pool.connection() as con:
            await con.execute(CLEANUP)


async def test_reads_work_and_writes_are_refused(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as con:
        async with readonly_transaction(con) as ro:
            assert ro is con
            assert await fetch_scalar("SHOW transaction_read_only", con=con) == "on"
            assert await fetch_all("SELECT 1 AS one", con=con) == [{"one": 1}]
            with pytest.raises(ReadOnlySqlTransaction):
                await con.execute(WRITE)


async def test_a_pooled_connection_is_not_left_read_only(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as con:
        async with readonly_transaction(con):
            await con.execute("SELECT 1")
        assert con.read_only is None
    # the same connection again: an unrelated write must work
    async with pool.connection() as con:
        assert await fetch_scalar("SHOW transaction_read_only", con=con) == "off"
        await con.execute(WRITE)
        await con.execute(CLEANUP)


async def test_read_only_is_reset_when_the_block_raises(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as con:
        with pytest.raises(RuntimeError, match="boom"):
            async with readonly_transaction(con):
                await con.execute("SELECT 1")
                raise RuntimeError("boom")
        assert con.read_only is None
        assert con.info.transaction_status == TransactionStatus.IDLE
    async with pool.connection() as con:
        await con.execute(WRITE)
        await con.execute(CLEANUP)


async def test_read_only_is_reset_after_a_refused_write(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as con:
        with pytest.raises(ReadOnlySqlTransaction):
            async with readonly_transaction(con):
                await con.execute(WRITE)
        assert con.read_only is None
        assert con.info.transaction_status == TransactionStatus.IDLE
        await con.execute(WRITE)  # allowed again, same connection
        await con.execute(CLEANUP)


async def test_previous_read_only_setting_is_restored(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as con:
        await con.set_read_only(True)  # e.g. a connection to a replica
        async with readonly_transaction(con):
            await con.execute("SELECT 1")
        assert con.read_only is True
        await con.set_read_only(None)


async def test_refuses_a_connection_that_is_in_a_transaction(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as con:
        await con.execute("SELECT 1")  # starts a transaction (no autocommit)
        with pytest.raises(ProgrammingError, match="read_only"):
            async with readonly_transaction(con):
                pass
        assert con.read_only is None
        await con.rollback()


async def test_works_on_an_autocommit_connection(postgres_dsn: str) -> None:
    async with await AsyncConnection.connect(postgres_dsn, autocommit=True) as con:
        async with readonly_transaction(con):
            assert await fetch_scalar("SHOW transaction_read_only", con=con) == "on"
            with pytest.raises(ReadOnlySqlTransaction):
                await con.execute(WRITE)
        assert con.read_only is None
        assert await fetch_scalar("SHOW transaction_read_only", con=con) == "off"


async def test_closed_connection_does_not_mask_the_original_error(postgres_dsn: str) -> None:
    con = await AsyncConnection.connect(postgres_dsn)
    with pytest.raises(RuntimeError, match="boom"):
        async with readonly_transaction(con):
            await con.close()
            raise RuntimeError("boom")
