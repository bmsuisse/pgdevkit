from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


async def execute(dsn: str, sql: str) -> list[dict] | None:
    """Run one or more ';'-separated statements against dsn. Returns the
    rows of the final statement if it produced any, else None."""
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    last_rows: list[dict] | None = None
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as con:
        for stmt in statements:
            async with con.cursor(row_factory=dict_row) as cur:
                await cur.execute(stmt)
                last_rows = await cur.fetchall() if cur.description else None
    return last_rows
