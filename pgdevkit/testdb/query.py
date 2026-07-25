from __future__ import annotations

import re
from typing import LiteralString, cast

import psycopg
from psycopg.rows import dict_row

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")
_GO_LINE = re.compile(r"^\s*GO\s*(\d+)?\s*$", re.IGNORECASE)


def split_tsql_batches(sql: str) -> list[str]:
    """Split T-SQL script text on standalone `GO` batch-separator lines (the
    sqlcmd/SSMS convention T-SQL scripts conventionally use) -- `GO` is not
    valid inside a single driver `execute()` call, unlike Postgres's `;`
    statement separator which the driver handles natively. Tracks `/* ... */`
    block comments as opaque so a `GO`-looking line inside a comment doesn't
    split; does not attempt full tokenization of string literals spanning a
    `GO` line, which is exceedingly rare in practice for schema/DDL scripts.
    A script with no `GO` lines at all (any Postgres script, or a T-SQL one
    that just doesn't use them) returns as a single batch, unchanged."""
    batches: list[str] = []
    buf: list[str] = []
    in_block_comment = False
    for line in sql.splitlines():
        stripped = line.strip()
        if in_block_comment:
            buf.append(line)
            if "*/" in line:
                in_block_comment = False
            continue
        if stripped.startswith("/*") and "*/" not in stripped:
            in_block_comment = True
            buf.append(line)
            continue
        if _GO_LINE.match(line):
            batch = "\n".join(buf).strip()
            if batch:
                batches.append(batch)
            buf = []
            continue
        buf.append(line)
    tail = "\n".join(buf).strip()
    if tail:
        batches.append(tail)
    return batches


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
