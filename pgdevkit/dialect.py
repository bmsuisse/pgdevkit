from __future__ import annotations

from dataclasses import dataclass, field


# Postgres type-name synonyms so scripts-vs-db type comparisons in diff.py
# are spelling-insensitive (e.g. a script written as "int4" matching a
# catalog-reported "integer"). Moved here (unchanged) from diff.py so both
# dialects' tables live next to the Dialect they belong to.
_POSTGRES_TYPE_SYNONYMS = {
    "int": "integer", "int4": "integer",
    "int2": "smallint",
    "int8": "bigint",
    "float4": "real",
    "float8": "double precision",
    "bool": "boolean",
    "decimal": "numeric",
    "varchar": "character varying",
    "char": "character", "bpchar": "character",
    "timestamptz": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "timetz": "time with time zone",
    "time": "time without time zone",
    "varbit": "bit varying",
    "serial": "integer", "serial4": "integer",
    "smallserial": "smallint", "serial2": "smallint",
    "bigserial": "bigint", "serial8": "bigint",
}

# T-SQL's ISO/ODBC synonyms (per Microsoft's documented list) plus the one
# genuinely deprecated pair (timestamp/rowversion) that scripts still use.
_MSSQL_TYPE_SYNONYMS = {
    "integer": "int",
    "double precision": "float",
    "national character": "nchar",
    "national char": "nchar",
    "national character varying": "nvarchar",
    "national char varying": "nvarchar",
    "char varying": "varchar",
    "binary varying": "varbinary",
    "numeric": "decimal",
    "timestamp": "rowversion",
}


@dataclass(frozen=True)
class Dialect:
    """A thin wrapper around a sqlglot dialect name plus the handful of
    other facts that vary between engines and were previously hardcoded
    throughout parser.py/diff.py/schema.py (default schema, type-name
    synonyms, enum/composite-type support). Intentionally NOT a
    reimplementation of anything sqlglot already does — `sqlglot_name` is
    passed straight through to `sqlglot.parse()`/`.sql(dialect=...)`."""

    name: str
    sqlglot_name: str
    default_schema: str
    type_synonyms: dict[str, str] = field(default_factory=dict)
    supports_enums: bool = True
    supports_composites: bool = True


POSTGRES = Dialect(
    name="postgres",
    sqlglot_name="postgres",
    default_schema="public",
    type_synonyms=_POSTGRES_TYPE_SYNONYMS,
    supports_enums=True,
    supports_composites=True,
)

MSSQL = Dialect(
    name="mssql",
    sqlglot_name="tsql",
    default_schema="dbo",
    type_synonyms=_MSSQL_TYPE_SYNONYMS,
    supports_enums=False,
    supports_composites=False,
)

_REGISTRY = {"postgres": POSTGRES, "mssql": MSSQL}


def resolve_dialect(dialect: str | Dialect = "postgres") -> Dialect:
    """Resolve a dialect name (or an already-resolved `Dialect`) to a
    `Dialect` instance. Defaults to postgres, matching every caller's
    default before this module existed."""
    if isinstance(dialect, Dialect):
        return dialect
    try:
        return _REGISTRY[dialect]
    except KeyError:
        raise ValueError(f"Unknown dialect {dialect!r}; expected one of {sorted(_REGISTRY)}") from None
