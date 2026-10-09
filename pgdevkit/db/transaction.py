from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.connection_async import AsyncConnection


@asynccontextmanager
async def readonly_transaction(con: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """Run a block in a read-only transaction on `con`, e.g. to execute untrusted / LLM-generated SQL: Postgres
    itself rejects any write with `ReadOnlySqlTransaction`, so nothing needs to be parsed or filtered.

    ```python
    async with pool.connection() as con, readonly_transaction(con):
        rows = await fetch_all(generated_sql, con=con)
    ```

    The transaction is committed when the block ends normally and rolled back if it raises. Afterwards the
    connection's `read_only` setting is *always* reset to `None` (the server default), because the connection
    usually goes back to a shared pool, where a leaked `read_only=True` would turn an unrelated write into
    `ReadOnlySqlTransaction`.

    `con` must not be inside a transaction already (psycopg refuses to change `read_only` then): for a pooled
    connection that is the case right after `pool.connection()` hands it out, but not after a statement that
    was run on it without `commit()`/`rollback()`.
    """
    await con.set_read_only(True)
    try:
        async with con.transaction():
            yield con
    finally:
        if not (con.closed or con.broken):  # a dead connection can't leak into a pool, and can't be asked
            await con.set_read_only(None)
