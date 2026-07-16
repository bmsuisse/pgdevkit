from __future__ import annotations

from typing import LiteralString, cast

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
                # stmt is arbitrary, caller-provided SQL text (a .sql file or
                # --sql argument) — not a compile-time literal, but this
                # function's entire purpose is to run it as-is.
                await cur.execute(cast(LiteralString, stmt))
                last_rows = await cur.fetchall() if cur.description else None
    return last_rows
