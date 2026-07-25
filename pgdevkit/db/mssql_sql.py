from __future__ import annotations


def ident(name: str) -> str:
    """Bracket-quote a single identifier, doubling any embedded `]`
    (T-SQL's escaping rule) -- the mssql-python driver has no
    `psycopg.sql.Identifier` equivalent, so this is the composable-SQL
    builder Postgres gets for free, hand-rolled for the one thing it's
    actually needed for here."""
    return f"[{name.replace(']', ']]')}]"


def qualified(schema: str, table: str) -> str:
    return f"{ident(schema)}.{ident(table)}"
