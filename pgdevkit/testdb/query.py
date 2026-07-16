from __future__ import annotations

import re
from typing import LiteralString, cast

import psycopg
from psycopg.rows import dict_row

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def _split_statements(sql: str) -> list[str]:
    """Split on ';', but treat single/double-quoted strings and dollar-quoted
    bodies (e.g. a plpgsql function's $$ ... $$) as opaque so an embedded ';'
    inside them doesn't split the statement in two."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "$" and (m := _DOLLAR_TAG.match(sql, i)):
            tag = m.group()
            end = sql.find(tag, m.end())
            end = n if end == -1 else end + len(tag)
            buf.append(sql[i:end])
            i = end
            continue
        if ch in ("'", '"'):
            end = i + 1
            while end < n:
                if sql[end] == ch:
                    end += 1
                    if sql[end : end + 1] == ch:  # doubled quote = escaped literal quote
                        end += 1
                        continue
                    break
                end += 1
            buf.append(sql[i:end])
            i = end
            continue
        if ch == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    statements.append("".join(buf))
    return [s.strip() for s in statements if s.strip()]


async def execute(dsn: str, sql: str) -> list[dict] | None:
    """Run one or more statements against dsn. Returns the rows of the final
    statement if it produced any, else None."""
    statements = _split_statements(sql)
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
