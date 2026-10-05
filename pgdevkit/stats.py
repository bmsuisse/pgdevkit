from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import psycopg.sql
from psycopg.rows import dict_row

from .dialect import resolve_dialect

STATS_DIRNAME = "_stats"
TABLES_FILE = "_tables.json"

_TABLES_SQL = """
SELECT n.nspname AS schema, c.relname AS name,
       c.reltuples::bigint AS estimated_rows,
       pg_table_size(c.oid) AS table_bytes,
       pg_indexes_size(c.oid) AS index_bytes,
       pg_total_relation_size(c.oid) AS total_bytes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT LIKE 'pg\\_%' AND n.nspname != 'information_schema'
ORDER BY 1, 2
"""

_COLUMNS_SQL = """
SELECT a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       s.null_frac, s.n_distinct, s.avg_width
FROM pg_attribute a
LEFT JOIN pg_stats s
  ON s.schemaname = %(schema)s AND s.tablename = %(table)s AND s.attname = a.attname
WHERE a.attrelid = to_regclass(%(qualified)s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum, s.inherited NULLS FIRST
"""


def stats_dir(scripts_dir: Path) -> Path:
    return scripts_dir / STATS_DIRNAME


def column_stats_path(scripts_dir: Path, qualified_name: str) -> Path:
    return stats_dir(scripts_dir) / f"{qualified_name}.json"


def _q(conn: Any, sql: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


_MSSQL_TABLES_SQL = """
SELECT s.name AS [schema], t.name AS name,
       SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count ELSE 0 END) AS estimated_rows,
       SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.used_page_count ELSE 0 END) * 8192 AS table_bytes,
       SUM(CASE WHEN ps.index_id > 1 THEN ps.used_page_count ELSE 0 END) * 8192 AS index_bytes,
       SUM(ps.used_page_count) * 8192 AS total_bytes
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
GROUP BY s.name, t.name
ORDER BY 1, 2
"""

_MSSQL_COLUMNS_SQL = """
SELECT c.name AS name, ty.name AS base_type, c.max_length AS max_length,
       c.precision AS precision, c.scale AS scale
FROM sys.columns c
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE c.object_id = OBJECT_ID(?)
ORDER BY c.column_id
"""

# Types that can't be COUNT(DISTINCT)ed / measured with DATALENGTH.
_MSSQL_UNMEASURABLE = {"text", "ntext", "image", "xml", "geography", "geometry", "hierarchyid", "sql_variant"}


def _mssql_q(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def _mssql_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _collect_mssql_stats(
    conninfo: str, only: set[str] | None, exact: bool, analyze: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """MSSQL flavour of collect_stats. Row counts/sizes come from
    sys.dm_db_partition_stats. SQL Server has no pg_stats equivalent, so column
    null_fraction/n_distinct/avg_width are only filled in with exact=True
    (computed by scanning each table); otherwise they are null.
    analyze=True runs UPDATE STATISTICS."""
    # Lazy: the mssql extra isn't required for postgres-only use.
    import mssql_python

    from .mssql_introspect import _format_type, _is_system_schema

    tables: dict[str, dict[str, Any]] = {}
    columns: dict[str, dict[str, Any]] = {}
    conn = mssql_python.connect(conninfo, autocommit=True)
    try:
        rows = [r for r in _mssql_q(conn, _MSSQL_TABLES_SQL) if not _is_system_schema(r["schema"])]
        if only is not None:
            unknown = only - {f"{r['schema']}.{r['name']}" for r in rows}
            if unknown:
                raise KeyError(", ".join(sorted(unknown)))
        for r in rows:
            qn = f"{r['schema']}.{r['name']}"
            if only is not None and qn not in only:
                continue
            ident = f"{_mssql_ident(r['schema'])}.{_mssql_ident(r['name'])}"
            if analyze:
                conn.execute(f"UPDATE STATISTICS {ident}")
            if exact:
                row_count = _mssql_q(conn, f"SELECT COUNT_BIG(*) AS n FROM {ident}")[0]["n"]
            else:
                row_count = r["estimated_rows"]
            tables[qn] = {
                "row_count": row_count,
                "row_count_exact": exact,
                "table_bytes": r["table_bytes"],
                "index_bytes": r["index_bytes"],
                "total_bytes": r["total_bytes"],
            }
            col_stats: dict[str, Any] = {}
            for c in _mssql_q(conn, _MSSQL_COLUMNS_SQL, (ident,)):
                entry: dict[str, Any] = {
                    "data_type": _format_type(c["base_type"], c["max_length"], c["precision"], c["scale"]),
                    "null_fraction": None,
                    "n_distinct": None,
                    "avg_width": None,
                }
                if exact and row_count and c["base_type"].lower() not in _MSSQL_UNMEASURABLE:
                    col = _mssql_ident(c["name"])
                    m = _mssql_q(
                        conn,
                        f"SELECT COUNT_BIG({col}) AS nn, COUNT_BIG(DISTINCT {col}) AS nd, "
                        f"AVG(CAST(DATALENGTH({col}) AS float)) AS w FROM {ident}",
                    )[0]
                    entry["null_fraction"] = 1 - m["nn"] / row_count
                    entry["n_distinct"] = m["nd"]
                    entry["avg_width"] = None if m["w"] is None else round(m["w"])
                col_stats[c["name"]] = entry
            columns[qn] = col_stats
    finally:
        conn.close()
    return tables, columns


def collect_stats(
    conninfo: str,
    only: set[str] | None = None,
    exact: bool = False,
    analyze: bool = False,
    dialect: str = "postgres",
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Returns (table_stats, column_stats), both keyed by "schema.table".

    Row counts come from pg_class.reltuples (null if the table was never
    analyzed) unless exact=True, which runs count(*). analyze=True runs ANALYZE
    first so column stats (null_frac/n_distinct/avg_width) are populated."""
    if resolve_dialect(dialect).name == "mssql":
        return _collect_mssql_stats(conninfo, only, exact, analyze)
    tables: dict[str, dict[str, Any]] = {}
    columns: dict[str, dict[str, Any]] = {}
    with psycopg.connect(conninfo, autocommit=True) as conn:
        rows = _q(conn, _TABLES_SQL)
        if only is not None:
            unknown = only - {f"{r['schema']}.{r['name']}" for r in rows}
            if unknown:
                raise KeyError(", ".join(sorted(unknown)))
        for r in rows:
            qn = f"{r['schema']}.{r['name']}"
            if only is not None and qn not in only:
                continue
            ident = psycopg.sql.SQL("{}.{}").format(
                psycopg.sql.Identifier(r["schema"]), psycopg.sql.Identifier(r["name"])
            )
            if analyze:
                conn.execute(psycopg.sql.SQL("ANALYZE {}").format(ident))
            if exact:
                row_count = _q(conn, psycopg.sql.SQL("SELECT count(*) AS n FROM {}").format(ident))[0]["n"]
            else:
                row_count = r["estimated_rows"] if r["estimated_rows"] >= 0 else None
            tables[qn] = {
                "row_count": row_count,
                "row_count_exact": exact,
                "table_bytes": r["table_bytes"],
                "index_bytes": r["index_bytes"],
                "total_bytes": r["total_bytes"],
            }
            cols = _q(
                conn,
                _COLUMNS_SQL,
                {"schema": r["schema"], "table": r["name"], "qualified": ident.as_string(conn)},
            )
            columns[qn] = {
                c["name"]: {
                    "data_type": c["data_type"],
                    "null_fraction": c["null_frac"],
                    "n_distinct": c["n_distinct"],
                    "avg_width": c["avg_width"],
                }
                for c in cols
            }
    return tables, columns


def write_stats(
    scripts_dir: Path, tables: dict[str, dict[str, Any]], columns: dict[str, dict[str, Any]], prune: bool = False
) -> Path:
    """Merge into _stats/_tables.json (keys sorted; tables not in `tables` are kept,
    unless prune=True, which drops them and their column files — use for a full run)
    and write one _stats/<schema.table>.json per table for column stats."""
    path = stats_dir(scripts_dir) / TABLES_FILE
    existing: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if prune:
        for gone in set(existing) - set(tables):
            column_stats_path(scripts_dir, gone).unlink(missing_ok=True)
        existing = {}
    existing.update(tables)
    _write_json(path, existing)
    for qn, cols in columns.items():
        _write_json(column_stats_path(scripts_dir, qn), cols)
    return path


def read_stats(scripts_dir: Path, names: list[str], columns: bool = True) -> dict[str, Any]:
    """Stats for the given "schema.table" names (all tables if empty), read
    from the JSON files. Unknown tables raise KeyError."""
    path = stats_dir(scripts_dir) / TABLES_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `pgdb update-stats` first")
    all_tables: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    wanted = names or sorted(all_tables)
    missing = [n for n in wanted if n not in all_tables]
    if missing:
        raise KeyError(", ".join(missing))
    result: dict[str, Any] = {}
    for qn in wanted:
        entry = dict(all_tables[qn])
        cpath = column_stats_path(scripts_dir, qn)
        if columns and cpath.exists():
            entry["columns"] = json.loads(cpath.read_text(encoding="utf-8"))
        result[qn] = entry
    return result
