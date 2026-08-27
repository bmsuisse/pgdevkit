"""Apply numbered, forward-only SQL migration files to a live Postgres database,
tracked in a `schema.table` (default `public.schema_migrations`) so re-runs only
apply what's pending. Ported from a hand-rolled per-project script — MSSQL is not
supported yet.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import sqlglot
from psycopg import errors as pg_errors
from psycopg import sql as pg_sql

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_DEFAULT_TRACKING_TABLE = "public.schema_migrations"


class MigrationVerificationError(RuntimeError):
    """A CREATE TABLE statement's target doesn't exist after applying the migration."""


class TrackingTableMissing(RuntimeError):
    """The migration-tracking table doesn't exist yet (e.g. before it's bootstrapped)."""

    def __init__(self, tracking_table: str) -> None:
        super().__init__(tracking_table)
        self.tracking_table = tracking_table


def _tracking_table_identifier(tracking_table: str) -> pg_sql.Identifier:
    """Parse 'schema.table' into a safely-quoted, injection-proof identifier."""
    if not re.fullmatch(rf"{_IDENTIFIER}\.{_IDENTIFIER}", tracking_table):
        raise ValueError(f"tracking_table must look like schema.table, got {tracking_table!r}")
    schema, _, table = tracking_table.partition(".")
    return pg_sql.Identifier(schema, table)


def _find_pyproject(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def default_tracking_table(start: Path | None = None) -> str:
    """The project's configured tracking table: `[tool.pgdevkit].migrations_table` in the
    nearest pyproject.toml at or above `start` (default: cwd), or "public.schema_migrations"
    if neither is set. Lets a project fix its tracking table once instead of passing
    --tracking-table on every `pgdb migrate` invocation."""
    pyproject = _find_pyproject((start or Path.cwd()).resolve())
    if pyproject is None:
        return _DEFAULT_TRACKING_TABLE
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    section = data.get("tool", {}).get("pgdevkit", {})
    return section.get("migrations_table", _DEFAULT_TRACKING_TABLE)


def _split_sql(sql: str) -> list[str]:
    """Split SQL on semicolons, ignoring those inside comments, '...' strings, or $$...$$ blocks."""
    stmts: list[str] = []
    buf: list[str] = []
    i = 0
    in_string = False
    in_line_comment = False
    dollar_tag: str | None = None

    while i < len(sql):
        c = sql[i]
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            buf.append(c)
        elif dollar_tag is not None:
            buf.append(c)
            if c == "$" and sql[i:i + len(dollar_tag)] == dollar_tag:
                buf.extend(list(dollar_tag[1:]))
                i += len(dollar_tag)
                dollar_tag = None
                continue
        elif in_string:
            if c == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append(c)
                buf.append(sql[i + 1])
                i += 2
                continue
            elif c == "'":
                in_string = False
            buf.append(c)
        elif c == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            in_line_comment = True
            buf.append(c)
        elif c == "$":
            m = re.match(r"\$([A-Za-z0-9_]*)\$", sql[i:])
            if m:
                dollar_tag = m.group(0)
                buf.extend(list(dollar_tag))
                i += len(dollar_tag)
                continue
            buf.append(c)
        elif c == "'":
            in_string = True
            buf.append(c)
        elif c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
        else:
            buf.append(c)
        i += 1
    remainder = "".join(buf).strip()
    if remainder:
        stmts.append(remainder)
    return stmts


def _strip_line_comments(sql: str) -> str:
    """Drop '--' line comments, respecting string literals and $$...$$ blocks."""
    buf: list[str] = []
    i = 0
    in_string = False
    in_line_comment = False
    dollar_tag: str | None = None

    while i < len(sql):
        c = sql[i]
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                buf.append(c)
        elif dollar_tag is not None:
            buf.append(c)
            if c == "$" and sql[i:i + len(dollar_tag)] == dollar_tag:
                buf.extend(list(dollar_tag[1:]))
                i += len(dollar_tag)
                dollar_tag = None
                continue
        elif in_string:
            if c == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append(c)
                buf.append(sql[i + 1])
                i += 2
                continue
            elif c == "'":
                in_string = False
            buf.append(c)
        elif c == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            in_line_comment = True
        elif c == "$":
            m = re.match(r"\$([A-Za-z0-9_]*)\$", sql[i:])
            if m:
                dollar_tag = m.group(0)
                buf.extend(list(dollar_tag))
                i += len(dollar_tag)
                continue
            buf.append(c)
        elif c == "'":
            in_string = True
            buf.append(c)
        else:
            buf.append(c)
        i += 1
    return "".join(buf)


def _created_table_names(stmts: list[str]) -> list[str]:
    """Names of tables any CREATE TABLE statement targets, parsed via sqlglot (falls back to
    regex on comment-stripped text for statements sqlglot's postgres dialect can't parse)."""
    names: list[str] = []
    for stmt in stmts:
        stripped = _strip_line_comments(stmt)
        if not re.search(r"CREATE\s+TABLE", stripped, re.IGNORECASE):
            continue  # skip sqlglot entirely for statements that can't be a CREATE TABLE
        try:
            parsed = sqlglot.parse_one(stmt, dialect="postgres")
        except Exception:
            parsed = None
        if parsed is not None and isinstance(parsed, sqlglot.exp.Create) and parsed.kind == "TABLE":
            table = parsed.this.this if isinstance(parsed.this, sqlglot.exp.Schema) else parsed.this
            if isinstance(table, sqlglot.exp.Table):
                names.append(table.sql(dialect="postgres"))
            continue
        names.extend(
            m.group(1)
            for m in re.finditer(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w."]+)', stripped, re.IGNORECASE
            )
        )
    return names


def list_migration_files(migrations_dir: Path) -> list[Path]:
    return sorted(migrations_dir.glob("*.sql"))


def applied_migrations(conninfo: str, tracking_table: str) -> dict[str, tuple[datetime, str]]:
    """Filename -> (applied_at, applied_by) for every migration recorded in the tracking table."""
    table = _tracking_table_identifier(tracking_table)
    with psycopg.connect(conninfo) as con:
        try:
            rows = con.execute(
                pg_sql.SQL("select filename, applied_at, applied_by from {} order by applied_at").format(table)
            ).fetchall()
        except pg_errors.UndefinedTable as e:
            raise TrackingTableMissing(tracking_table) from e
        return {r[0]: (r[1], r[2]) for r in rows}


def pending_migrations(migrations_dir: Path, conninfo: str, tracking_table: str) -> list[Path]:
    applied = applied_migrations(conninfo, tracking_table)
    return [p for p in list_migration_files(migrations_dir) if p.name not in applied]


def record_applied(conninfo: str, tracking_table: str, filename: str) -> bool:
    """Best-effort insert into the tracking table. Returns False without raising if the
    tracking table doesn't exist yet — e.g. this migration is the one that creates it."""
    table = _tracking_table_identifier(tracking_table)
    with psycopg.connect(conninfo) as con:
        try:
            con.execute(
                pg_sql.SQL("insert into {} (filename) values (%s) on conflict do nothing").format(table),
                (filename,),
            )
            con.commit()
            return True
        except pg_errors.UndefinedTable:
            con.rollback()
            return False


def verify_created_tables(conninfo: str, stmts: list[str]) -> list[str]:
    """Table names from this migration's CREATE TABLE statements that do NOT exist in the
    database. Empty means everything landed."""
    tables = _created_table_names(stmts)
    if not tables:
        return []
    missing = []
    with psycopg.connect(conninfo) as con:
        for tbl in tables:
            row = con.execute("select to_regclass(%s)", (tbl,)).fetchone()
            if not (row and row[0]):
                missing.append(tbl)
    return missing


@dataclass
class ApplyResult:
    filename: str
    executed: bool  # False if the caller marked it "already done" instead of running it
    verified_tables: list[str]


def apply_migration(
    conninfo: str,
    path: Path,
    tracking_table: str,
    *,
    already_done: bool = False,
) -> ApplyResult:
    sql = path.read_text(encoding="utf-8")
    filename = path.name
    stmts = _split_sql(sql)

    if not already_done:
        # DDL in its own committed transaction.
        with psycopg.connect(conninfo) as con:
            for stmt in stmts:
                con.execute(cast(LiteralString, stmt))
            con.commit()

    # Tracking insert is a separate connection/transaction so a missing tracking table
    # never rolls back the DDL that was just applied.
    record_applied(conninfo, tracking_table, filename)

    if already_done:
        return ApplyResult(filename, executed=False, verified_tables=[])

    missing = verify_created_tables(conninfo, stmts)
    if missing:
        raise MigrationVerificationError(
            f"{filename}: table(s) not found after apply — migration may have rolled back: "
            + ", ".join(missing)
        )
    return ApplyResult(filename, executed=True, verified_tables=_created_table_names(stmts))
