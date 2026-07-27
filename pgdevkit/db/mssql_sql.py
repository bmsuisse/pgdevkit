from __future__ import annotations

import json
from typing import Any


def ident(name: str) -> str:
    """Bracket-quote a single identifier, doubling any embedded `]`
    (T-SQL's escaping rule) -- the mssql-python driver has no
    `psycopg.sql.Identifier` equivalent, so this is the composable-SQL
    builder Postgres gets for free, hand-rolled for the one thing it's
    actually needed for here."""
    return f"[{name.replace(']', ']]')}]"


def qualified(schema: str, table: str) -> str:
    return f"{ident(schema)}.{ident(table)}"


def json_encode_value(value: Any) -> Any:
    """Serialize a value destined for a `json`-typed (or legacy
    `nvarchar(max)`-storing-JSON) column to text.

    mssql-python has no auto-serialization for dict/list parameter values --
    binding one directly raises `TypeError: Unsupported parameter type`
    (confirmed against mssql-python 1.12.0: its `_map_sql_type` has explicit
    branches for every scalar Python type but none for dict/list, and SQL
    Server's native `json` type -- a genuine first-class type in current
    Azure SQL/SQL Server, unlike the old NVARCHAR(MAX)-plus-OPENJSON()
    convention -- has no dedicated ODBC type code in this driver either, so
    it's fetched back as plain `str`, indistinguishable from any other text
    column). Unlike Postgres, there's no type-registration ambiguity to
    resolve here (composite type vs jsonb vs plain array all need different
    handling there): MSSQL has no composite types, so a Python dict/list
    passed to any MSSQL CRUD call can only sensibly mean "serialize me as
    JSON text" -- no per-column-type lookup needed on the write side.

    There is deliberately no read-side counterpart: the driver can't tell
    us which columns are `json`-typed (it reports the same opaque `str` for
    those as for a plain `nvarchar`), so auto-parsing fetched values back
    into dict/list would need its own catalog lookup -- a ComplexHelper-like
    mechanism this backend intentionally doesn't have. Callers that know a
    column is JSON deserialize it themselves with `json.loads()`."""
    return json.dumps(value) if isinstance(value, (dict, list)) else value


def json_encode_values(data: dict) -> dict:
    return {k: json_encode_value(v) for k, v in data.items()}
