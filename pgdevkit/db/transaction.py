from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.connection_async import AsyncConnection


@asynccontextmanager
async def readonly_transaction(con: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """Run a block in a read-only transaction on `con`: Postgres refuses writes with `ReadOnlySqlTransaction`.

    ```python
    async with pool.connection() as con, readonly_transaction(con):
        rows = await fetch_all(generated_sql, con=con)
    ```

    The transaction is committed when the block ends normally and rolled back if it raises. Afterwards `con`'s
    `read_only` setting is *always* put back to what it was before (normally `None`, the server default), because
    the connection usually goes back to a shared pool, where a leaked `read_only=True` would turn an unrelated
    write into `ReadOnlySqlTransaction`.

    This is a safety net, **not a sandbox for untrusted SQL**. Text without bound parameters goes to Postgres
    through the simple query protocol, where several statements are allowed: `COMMIT; INSERT ...` ends the
    read-only transaction and writes. TEMP tables stay writable, and `SET`/`SET search_path` persist on the
    connection. To run untrusted SQL, also connect as a role that only has SELECT privileges.

    `con` must not be inside a transaction already (psycopg raises `ProgrammingError` when `read_only` is changed
    then): a connection fresh from `pool.connection()` is fine, one that ran a statement without
    `commit()`/`rollback()` (non-autocommit) is not.
    """
    previous = con.read_only
    await con.set_read_only(True)
    try:
        async with con.transaction():
            yield con
    finally:
        if not (con.closed or con.broken):  # a dead connection can't leak into a pool, and can't be asked
            await con.set_read_only(previous)
