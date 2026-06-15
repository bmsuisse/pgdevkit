from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .models import (
    ColumnDef, ConstraintDef, CompositeTypeDef, DatabaseSchema,
    EnumDef, FunctionDef, IndexDef, TableDef, ViewDef,
)


def _q(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()  # type: ignore[return-value]


def introspect_db(conninfo: str) -> DatabaseSchema:
    with psycopg.connect(conninfo) as conn:
        db = DatabaseSchema()
        _load_schemas(conn, db)
        _load_tables(conn, db)
        _load_views(conn, db)
        _load_functions(conn, db)
        _load_enums(conn, db)
        _load_composites(conn, db)
        _load_indexes(conn, db)
        return db


def _load_schemas(conn: Any, db: DatabaseSchema) -> None:
    rows = _q(conn, (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'"
    ))
    db.schemas = {r["schema_name"] for r in rows}


def _load_tables(conn: Any, db: DatabaseSchema) -> None:
    tables = _q(conn, """
        SELECT n.nspname AS schema, c.relname AS name
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_%'
    """)

    for row in tables:
        tschema, tname = row["schema"], row["name"]
        table = TableDef(schema=tschema, name=tname)

        for c in _q(conn, """
            SELECT a.attname AS name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                   NOT a.attnotnull AS is_nullable,
                   pg_get_expr(d.adbin, d.adrelid) AS col_default,
                   a.attgenerated != '' AS is_generated
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE n.nspname = %(s)s AND c.relname = %(t)s
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
        """, {"s": tschema, "t": tname}):
            table.columns.append(ColumnDef(
                name=c["name"],
                data_type=c["data_type"],
                is_nullable=bool(c["is_nullable"]),
                default=c["col_default"],
                is_generated=bool(c["is_generated"]),
            ))

        kind_map = {"p": "PRIMARY KEY", "u": "UNIQUE", "f": "FOREIGN KEY", "c": "CHECK"}
        for c in _q(conn, """
            SELECT conname AS name, contype AS kind,
                   pg_get_constraintdef(oid) AS definition
            FROM pg_catalog.pg_constraint
            WHERE conrelid = (
                SELECT c.oid FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %(s)s AND c.relname = %(t)s
            )
        """, {"s": tschema, "t": tname}):
            table.constraints.append(ConstraintDef(
                name=c["name"],
                kind=kind_map.get(c["kind"], c["kind"]),
                definition=(c["definition"] or "").lower(),
            ))

        db.tables[table.qualified_name] = table


def _load_views(conn: Any, db: DatabaseSchema) -> None:
    for r in _q(conn, """
        SELECT n.nspname AS schema, c.relname AS name,
               pg_get_viewdef(c.oid, true) AS definition
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'v'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_%'
    """):
        v = ViewDef(schema=r["schema"], name=r["name"], definition=(r["definition"] or "").lower())
        db.views[v.qualified_name] = v


def _load_functions(conn: Any, db: DatabaseSchema) -> None:
    for r in _q(conn, """
        SELECT n.nspname AS schema, p.proname AS name,
               pg_get_function_arguments(p.oid) AS args,
               pg_get_function_result(p.oid) AS return_type,
               l.lanname AS language, p.prosrc AS body,
               CASE WHEN p.prokind = 'p' THEN 'procedure' ELSE 'function' END AS kind
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_catalog.pg_language l ON l.oid = p.prolang
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_%'
    """):
        raw_body = r["body"] or ""
        lines = [line.strip() for line in raw_body.splitlines()]
        body = "\n".join(line.lower() for line in lines if line)
        func = FunctionDef(
            schema=r["schema"], name=r["name"],
            args=(r["args"] or "").lower(),
            return_type=(r["return_type"] or "").lower(),
            language=r["language"], body=body, kind=r["kind"],
        )
        db.functions[func.qualified_name] = func


def _load_enums(conn: Any, db: DatabaseSchema) -> None:
    for r in _q(conn, """
        SELECT n.nspname AS schema, t.typname AS name,
               array_agg(e.enumlabel ORDER BY e.enumsortorder) AS values
        FROM pg_catalog.pg_type t
        JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
        JOIN pg_catalog.pg_enum e ON e.enumtypid = t.oid
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_%'
        GROUP BY n.nspname, t.typname
    """):
        enum = EnumDef(schema=r["schema"], name=r["name"], values=list(r["values"]))
        db.enums[enum.qualified_name] = enum


def _load_composites(conn: Any, db: DatabaseSchema) -> None:
    composites: dict[str, CompositeTypeDef] = {}
    for r in _q(conn, """
        SELECT n.nspname AS schema, t.typname AS name,
               a.attname AS field_name,
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS field_type
        FROM pg_catalog.pg_type t
        JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
        JOIN pg_catalog.pg_class c ON c.oid = t.typrelid AND c.relkind = 'c'
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_%'
        ORDER BY n.nspname, t.typname, a.attnum
    """):
        key = f"{r['schema']}.{r['name']}"
        if key not in composites:
            composites[key] = CompositeTypeDef(schema=r["schema"], name=r["name"], fields=[])
        composites[key].fields.append((r["field_name"], r["field_type"]))
    db.composites = composites


def _load_indexes(conn: Any, db: DatabaseSchema) -> None:
    for r in _q(conn, """
        SELECT n.nspname AS schema, t.relname AS table_name,
               i.relname AS index_name,
               pg_get_indexdef(ix.indexrelid) AS definition
        FROM pg_catalog.pg_index ix
        JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
        JOIN pg_catalog.pg_class t ON t.oid = ix.indrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_%'
          AND NOT ix.indisprimary
    """):
        idx = IndexDef(
            schema=r["schema"], table=r["table_name"],
            name=r["index_name"],
            definition=(r["definition"] or "").lower(),
        )
        db.indexes[idx.qualified_name] = idx
