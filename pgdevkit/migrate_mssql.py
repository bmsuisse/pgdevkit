"""MSSQL flavour of the `pgdevkit.migrate` primitives: the same forward-only,
tracking-table-backed migration flow, built on `mssql-python` and T-SQL.

Differences from the Postgres path that live here:

- A migration is split into batches on standalone `GO` lines (T-SQL has no usable
  statement separator for e.g. `CREATE VIEW`, which must be first in its batch) instead
  of on semicolons, and all batches run in one transaction.
- Existence checks use `OBJECT_ID` / `SCHEMA_ID` / `COL_LENGTH`.
- The tracking table is `[schema].[table]`; its columns are
  `filename nvarchar(450) primary key, applied_at datetimeoffset default
  sysdatetimeoffset(), applied_by nvarchar(128) default suser_sname()`.

Imported lazily by `pgdevkit.migrate` so the `mssql` extra is only needed when
`--dialect mssql` is actually used.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import mssql_python
import sqlglot

from .sql_text import strip_line_comments
from .testdb.query import split_tsql_batches

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_NAME = r"(?:\[[^\]]+\]|[\w\"]+)(?:\.(?:\[[^\]]+\]|[\w\"]+))*"
_CREATE_TABLE_RE = re.compile(rf"CREATE\s+TABLE\s+({_NAME})", re.IGNORECASE)
_CREATE_VIEW_RE = re.compile(rf"CREATE\s+VIEW\s+({_NAME})", re.IGNORECASE)
_CREATE_SCHEMA_RE = re.compile(r"CREATE\s+SCHEMA\s+(\[[^\]]+\]|\w+)", re.IGNORECASE)
_ADD_COLUMN_RE = re.compile(
    rf"ALTER\s+TABLE\s+({_NAME})\s+ADD\s+(?!(?:CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK|DEFAULT)\b)"
    rf"(\[[^\]]+\]|{_IDENTIFIER})\s",
    re.IGNORECASE,
)


def tracking_table_parts(tracking_table: str) -> tuple[str, str]:
    """Parse 'schema.table' into its two (unquoted) parts."""
    if not re.fullmatch(rf"{_IDENTIFIER}\.{_IDENTIFIER}", tracking_table):
        raise ValueError(f"tracking_table must look like schema.table, got {tracking_table!r}")
    schema, _, table = tracking_table.partition(".")
    return schema, table


def _tracking_ident(tracking_table: str) -> str:
    from .db.mssql_sql import qualified

    return qualified(*tracking_table_parts(tracking_table))


def _unbracket(name: str) -> str:
    return name[1:-1].replace("]]", "]") if name.startswith("[") else name.strip('"')


def split_batches(sql: str) -> list[str]:
    return split_tsql_batches(sql)


def connect(conninfo: str) -> Any:
    return mssql_python.connect(conninfo)


def _scalar(con: Any, sql: str, params: tuple = ()) -> Any:
    cur = con.cursor()
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def tracking_table_exists(con: Any, tracking_table: str) -> bool:
    schema, table = tracking_table_parts(tracking_table)
    return _scalar(con, "select object_id(?, N'U')", (f"[{schema}].[{table}]",)) is not None


def applied_migrations(conninfo: str, tracking_table: str) -> dict[str, tuple[datetime, str]]:
    from .migrate import TrackingTableMissing

    con = connect(conninfo)
    try:
        if not tracking_table_exists(con, tracking_table):
            raise TrackingTableMissing(tracking_table)
        cur = con.cursor()
        try:
            cur.execute(
                f"select filename, applied_at, applied_by from {_tracking_ident(tracking_table)} "
                "order by applied_at"
            )
            return {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        finally:
            cur.close()
    finally:
        con.close()


def record_applied(conninfo: str, tracking_table: str, filename: str) -> bool:
    """Best-effort insert into the tracking table (idempotent per filename). Returns False
    without raising if the tracking table doesn't exist yet."""
    con = connect(conninfo)
    try:
        if not tracking_table_exists(con, tracking_table):
            return False
        ident = _tracking_ident(tracking_table)
        cur = con.cursor()
        try:
            cur.execute(
                f"if not exists (select 1 from {ident} where filename = ?) "
                f"insert into {ident} (filename) values (?)",
                (filename, filename),
            )
        finally:
            cur.close()
        con.commit()
        return True
    finally:
        con.close()


def execute_batches(conninfo: str, batches: list[str]) -> None:
    """Run every batch in one transaction; roll back if any batch fails."""
    con = connect(conninfo)
    try:
        cur = con.cursor()
        try:
            for batch in batches:
                cur.execute(batch)
        finally:
            cur.close()
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def created_table_names(batches: list[str]) -> list[str]:
    """Table names any CREATE TABLE in these batches targets. Temp tables (#x) are skipped
    since they don't outlive the batch."""
    names: list[str] = []
    for batch in batches:
        stripped = strip_line_comments(batch)
        names.extend(
            m.group(1) for m in _CREATE_TABLE_RE.finditer(stripped) if not m.group(1).startswith("#")
        )
    return names


def _single_statement(batch: str) -> bool:
    try:
        return len([s for s in sqlglot.parse(batch, dialect="tsql") if s]) == 1
    except Exception:  # noqa: BLE001
        return False


def _has_top_level_comma(stmt: str) -> bool:
    depth = 0
    for c in stmt:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            return True
    return False


def idempotent_target(batch: str) -> tuple[str, ...] | None:
    """MSSQL counterpart of `migrate._idempotent_target`: ("relation", name) for a table or
    view, ("schema", name), or ("column", table, column) for an ADD column. Anything else
    (CREATE OR ALTER, indexes, procedures, data changes, a batch holding more than one
    statement, a multi-column ADD, ...) is None -- never guessed."""
    stripped = strip_line_comments(batch).strip()
    if re.search(r"\bOR\s+ALTER\b", stripped, re.IGNORECASE) or not _single_statement(stripped):
        return None
    for regex in (_CREATE_TABLE_RE, _CREATE_VIEW_RE):
        m = regex.match(stripped)
        if m:
            return ("relation", m.group(1))
    m = _CREATE_SCHEMA_RE.match(stripped)
    if m:
        return ("schema", _unbracket(m.group(1)))
    m = _ADD_COLUMN_RE.match(stripped)
    if m and not _has_top_level_comma(stripped):
        return ("column", m.group(1), _unbracket(m.group(2)))
    return None


def _target_exists(con: Any, target: tuple[str, ...]) -> bool:
    kind = target[0]
    if kind == "relation":
        return _scalar(con, "select object_id(?)", (target[1],)) is not None
    if kind == "schema":
        return _scalar(con, "select schema_id(?)", (target[1],)) is not None
    return _scalar(con, "select col_length(?, ?)", (target[1], target[2])) is not None


def targets_exist(conninfo: str, targets: list[tuple[str, ...]]) -> bool:
    con = connect(conninfo)
    try:
        return all(_target_exists(con, t) for t in targets)
    finally:
        con.close()


def missing_tables(conninfo: str, tables: list[str]) -> list[str]:
    """Table names (from `created_table_names`) that do NOT exist in the database."""
    if not tables:
        return []
    con = connect(conninfo)
    try:
        return [t for t in tables if _scalar(con, "select object_id(?, N'U')", (t,)) is None]
    finally:
        con.close()
