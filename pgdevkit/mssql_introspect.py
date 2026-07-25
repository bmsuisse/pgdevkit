from __future__ import annotations

from typing import Any

import mssql_python
import sqlglot
import sqlglot.expressions as exp

from .models import (
    ColumnDef, ConstraintDef, DatabaseSchema, FunctionDef, IndexDef, TableDef, ViewDef,
)
from .parser import _parse_function_details_tsql

# Schemas that are SQL Server system/fixed-role schemas, not user schemas --
# the equivalent of introspect.py's "pg_catalog"/"information_schema"/"pg_%"
# exclusion.
_SYSTEM_SCHEMAS = {"sys", "INFORMATION_SCHEMA", "guest"}


def _is_system_schema(name: str) -> bool:
    return name in _SYSTEM_SCHEMAS or name.startswith("db_")


def _q(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def introspect_mssql_db(conninfo: str) -> DatabaseSchema:
    conn = mssql_python.connect(conninfo)
    try:
        db = DatabaseSchema()
        _load_schemas(conn, db)
        _load_tables(conn, db)
        _load_views(conn, db)
        _load_functions(conn, db)
        # MSSQL has no native enum or composite type -- a scripts.sql file
        # that declares `CREATE TYPE ... AS ENUM`/a composite is a
        # Postgres-only construct on this backend. Leaving these empty
        # (rather than raising) means compute_diff reports every such
        # object as MISSING_IN_DB, which is the honest answer: it genuinely
        # doesn't exist as a first-class DB object here.
        db.enums = {}
        db.composites = {}
        _load_indexes(conn, db)
        return db
    finally:
        conn.close()


def _load_schemas(conn: Any, db: DatabaseSchema) -> None:
    rows = _q(conn, "SELECT schema_name FROM information_schema.schemata")
    db.schemas = {r["schema_name"] for r in rows if not _is_system_schema(r["schema_name"])}


_WCHAR_TYPES = {"nvarchar", "nchar"}
_CHAR_TYPES = {"varchar", "char", "varbinary", "binary"}
_DECIMAL_TYPES = {"decimal", "numeric"}


def _format_type(type_name: str, max_length: int, precision: int, scale: int) -> str:
    """Render a sys.columns/sys.types row as a type string comparable to
    what parser.py produces from a script's column definition (e.g.
    "nvarchar(50)", "decimal(18,2)") -- the MSSQL analog of Postgres's
    format_type(). A first cut: covers the character/decimal/float cases
    that actually carry a meaningful length/precision; anything else is
    rendered bare (int, bigint, bit, date, datetime2, uniqueidentifier, ...)."""
    tn = type_name.lower()
    if tn in _WCHAR_TYPES:
        return f"{tn}(max)" if max_length == -1 else f"{tn}({max_length // 2})"
    if tn in _CHAR_TYPES:
        return f"{tn}(max)" if max_length == -1 else f"{tn}({max_length})"
    if tn in _DECIMAL_TYPES:
        return f"{tn}({precision},{scale})"
    if tn == "float" and precision and precision != 53:
        return f"{tn}({precision})"
    return tn


def _load_tables(conn: Any, db: DatabaseSchema) -> None:
    tables = _q(conn, """
        SELECT s.name AS [schema], t.name AS name, t.object_id AS object_id
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
    """)

    for row in tables:
        tschema, tname, object_id = row["schema"], row["name"], row["object_id"]
        if _is_system_schema(tschema):
            continue
        # SQL Server has no equivalent to Postgres's declarative
        # partitioning (a table that IS a partition of another table) --
        # its own table partitioning is an internal storage detail of one
        # table, not a distinct child-table relationship, so there is
        # nothing to set here besides False.
        table = TableDef(schema=tschema, name=tname, is_partition=False)

        for c in _q(conn, """
            SELECT c.name AS name,
                   ty.name AS base_type,
                   c.max_length AS max_length,
                   c.precision AS precision,
                   c.scale AS scale,
                   c.is_nullable AS is_nullable,
                   c.is_identity AS is_identity,
                   cc.definition AS is_computed_def,
                   dc.definition AS col_default
            FROM sys.columns c
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            LEFT JOIN sys.default_constraints dc
                ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
            LEFT JOIN sys.computed_columns cc
                ON cc.object_id = c.object_id AND cc.column_id = c.column_id
            WHERE c.object_id = ?
            ORDER BY c.column_id
        """, (object_id,)):
            table.columns.append(ColumnDef(
                name=c["name"],
                data_type=_format_type(c["base_type"], c["max_length"], c["precision"], c["scale"]),
                is_nullable=bool(c["is_nullable"]),
                default=c["col_default"],
                is_generated=bool(c["is_identity"]) or c["is_computed_def"] is not None,
            ))

        table.constraints.extend(_load_key_constraints(conn, object_id))
        table.constraints.extend(_load_foreign_keys(conn, object_id))
        table.constraints.extend(_load_check_constraints(conn, object_id))

        db.tables[table.qualified_name] = table


def _load_key_constraints(conn: Any, object_id: int) -> list[ConstraintDef]:
    constraints = []
    for r in _q(conn, """
        SELECT kc.name AS name, kc.type AS type, i.index_id AS index_id
        FROM sys.key_constraints kc
        JOIN sys.indexes i ON i.object_id = kc.parent_object_id AND i.index_id = kc.unique_index_id
        WHERE kc.parent_object_id = ?
    """, (object_id,)):
        cols = _q(conn, """
            SELECT c.name AS name
            FROM sys.index_columns ic
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE ic.object_id = ? AND ic.index_id = ?
            ORDER BY ic.key_ordinal
        """, (object_id, r["index_id"]))
        col_list = ", ".join(c["name"] for c in cols)
        kind = "PRIMARY KEY" if r["type"] == "PK" else "UNIQUE"
        constraints.append(ConstraintDef(name=r["name"], kind=kind, definition=f"{kind.lower()} ({col_list})"))
    return constraints


def _load_foreign_keys(conn: Any, object_id: int) -> list[ConstraintDef]:
    constraints = []
    for r in _q(conn, "SELECT name, object_id FROM sys.foreign_keys WHERE parent_object_id = ?", (object_id,)):
        cols = _q(conn, """
            SELECT pc.name AS col, rc.name AS ref_col, rt.name AS ref_table, rs.name AS ref_schema
            FROM sys.foreign_key_columns fkc
            JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
            JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
            JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
            WHERE fkc.constraint_object_id = ?
            ORDER BY fkc.constraint_column_id
        """, (r["object_id"],))
        if not cols:
            continue
        col_list = ", ".join(c["col"] for c in cols)
        ref_list = ", ".join(c["ref_col"] for c in cols)
        ref_table = f"{cols[0]['ref_schema']}.{cols[0]['ref_table']}"
        definition = f"foreign key ({col_list}) references {ref_table} ({ref_list})"
        constraints.append(ConstraintDef(name=r["name"], kind="FOREIGN KEY", definition=definition))
    return constraints


def _load_check_constraints(conn: Any, object_id: int) -> list[ConstraintDef]:
    return [
        ConstraintDef(name=r["name"], kind="CHECK", definition=(r["definition"] or "").lower())
        for r in _q(conn, "SELECT name, definition FROM sys.check_constraints WHERE parent_object_id = ?", (object_id,))
    ]


def _load_views(conn: Any, db: DatabaseSchema) -> None:
    for r in _q(conn, """
        SELECT s.name AS [schema], v.name AS name, m.definition AS definition
        FROM sys.views v
        JOIN sys.schemas s ON s.schema_id = v.schema_id
        JOIN sys.sql_modules m ON m.object_id = v.object_id
    """):
        if _is_system_schema(r["schema"]):
            continue
        definition = _extract_view_query(r["definition"] or "").lower()
        view = ViewDef(schema=r["schema"], name=r["name"], definition=definition)
        db.views[view.qualified_name] = view


def _extract_view_query(definition: str) -> str:
    """`sys.sql_modules.definition` is the verbatim `CREATE [OR ALTER] VIEW
    ... AS <query>` statement text -- unlike Postgres's `pg_get_viewdef()`,
    which returns only the query body. Parse it back out so `ViewDef.definition`
    means the same thing on both backends and compares equal to parser.py's
    script-side definition (also query-only)."""
    try:
        parsed = sqlglot.parse_one(definition, dialect="tsql")
    except Exception:  # noqa: BLE001
        return definition
    if isinstance(parsed, exp.Create) and parsed.expression is not None:
        return parsed.expression.sql(dialect="tsql")
    return definition


_FUNCTION_KINDS = {"FN": "function", "IF": "function", "TF": "function", "P": "procedure"}


def _load_functions(conn: Any, db: DatabaseSchema) -> None:
    for r in _q(conn, """
        SELECT s.name AS [schema], o.name AS name,
               o.type AS type_code, m.definition AS definition
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.sql_modules m ON m.object_id = o.object_id
        WHERE o.type IN ('FN', 'IF', 'TF', 'P')
    """):
        if _is_system_schema(r["schema"]):
            continue
        # Same verbatim-statement-text situation as views (see
        # _extract_view_query) -- reuse parser.py's own T-SQL signature/body
        # extraction on the catalog's stored definition text, so the
        # introspected side is parsed exactly the same way the script side
        # is, rather than maintaining two separate extraction paths that can
        # drift out of sync.
        args, return_type, language, body = _parse_function_details_tsql(r["definition"] or "")

        func = FunctionDef(
            schema=r["schema"], name=r["name"],
            args=args, return_type=return_type,
            language=language, body=body,
            kind=_FUNCTION_KINDS[r["type_code"]],
        )
        db.functions[func.qualified_name] = func


def _load_indexes(conn: Any, db: DatabaseSchema) -> None:
    for r in _q(conn, """
        SELECT s.name AS [schema], t.name AS table_name, i.name AS index_name,
               i.index_id AS index_id, i.is_unique AS is_unique, t.object_id AS object_id,
               i.filter_definition AS filter_definition
        FROM sys.indexes i
        JOIN sys.tables t ON t.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE i.is_primary_key = 0 AND i.name IS NOT NULL
    """):
        if _is_system_schema(r["schema"]):
            continue
        cols = _q(conn, """
            SELECT c.name AS name
            FROM sys.index_columns ic
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE ic.object_id = ? AND ic.index_id = ? AND ic.is_included_column = 0
            ORDER BY ic.key_ordinal
        """, (r["object_id"], r["index_id"]))
        col_list = ", ".join(c["name"] for c in cols)
        unique = "UNIQUE " if r["is_unique"] else ""
        where_clause = f" WHERE {r['filter_definition']}" if r["filter_definition"] else ""
        definition = (
            f"create {unique}index {r['index_name']} "
            f"on {r['schema']}.{r['table_name']} ({col_list}){where_clause}"
        ).lower()
        idx = IndexDef(schema=r["schema"], table=r["table_name"], name=r["index_name"], definition=definition)
        db.indexes[idx.qualified_name] = idx
