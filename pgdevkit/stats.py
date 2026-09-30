from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import psycopg.sql
from psycopg.rows import dict_row

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


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_stats(
    conninfo: str, only: set[str] | None = None, exact: bool = False, analyze: bool = False
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Returns (table_stats, column_stats), both keyed by "schema.table".

    Row counts come from pg_class.reltuples (null if the table was never
    analyzed) unless exact=True, which runs count(*). analyze=True runs ANALYZE
    first so column stats (null_frac/n_distinct/avg_width) are populated."""
    tables: dict[str, dict[str, Any]] = {}
    columns: dict[str, dict[str, Any]] = {}
    with psycopg.connect(conninfo, autocommit=True, row_factory=dict_row) as conn:
        rows = conn.execute(_TABLES_SQL).fetchall()
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
                row_count = conn.execute(psycopg.sql.SQL("SELECT count(*) AS n FROM {}").format(ident)).fetchone()["n"]  # type: ignore[index]
            else:
                row_count = r["estimated_rows"] if r["estimated_rows"] >= 0 else None
            tables[qn] = {
                "row_count": row_count,
                "row_count_exact": exact,
                "table_bytes": r["table_bytes"],
                "index_bytes": r["index_bytes"],
                "total_bytes": r["total_bytes"],
            }
            cols = conn.execute(
                _COLUMNS_SQL,
                {"schema": r["schema"], "table": r["name"], "qualified": str(ident.as_string(conn))},
            ).fetchall()
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
