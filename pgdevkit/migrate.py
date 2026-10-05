"""Apply numbered, forward-only SQL migration files to a live Postgres database,
tracked in a `schema.table` (default `public.schema_migrations`) so re-runs only
apply what's pending. Ported from a hand-rolled per-project script. Every function that
touches a database takes `dialect="postgres"` (default) or `"mssql"`; the MSSQL
implementation lives in `.migrate_mssql` and is only imported when asked for, so the
`mssql` extra stays optional.
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

from .areas import filter_by_area
from .dialect import Dialect, resolve_dialect
from .envtag import env_allowed
from .schemas import filter_by_schema
from .sql_text import strip_line_comments

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"


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


def _is_mssql(dialect: str | Dialect) -> bool:
    return resolve_dialect(dialect).name == "mssql"


def _find_pyproject(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def default_tracking_table(start: Path | None = None, dialect: str | Dialect = "postgres") -> str:
    """The project's configured tracking table: `[tool.pgdevkit].migrations_table` in the
    nearest pyproject.toml at or above `start` (default: cwd), or "public.schema_migrations"
    if neither is set. For MSSQL the key is `mssql_migrations_table` (default
    "dbo.schema_migrations"), so a Postgres-style `migrations_table` never leaks into it. Lets a project fix its tracking table once instead of passing
    --tracking-table on every `pgdb migrate` invocation."""
    default = f"{resolve_dialect(dialect).default_schema}.schema_migrations"
    pyproject = _find_pyproject((start or Path.cwd()).resolve())
    if pyproject is None:
        return default
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    section = data.get("tool", {}).get("pgdevkit", {})
    key = "mssql_migrations_table" if resolve_dialect(dialect).name == "mssql" else "migrations_table"
    return section.get(key, default)


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


def _created_table_names(stmts: list[str]) -> list[str]:
    """Names of tables any CREATE TABLE statement targets, parsed via sqlglot (falls back to
    regex on comment-stripped text for statements sqlglot's postgres dialect can't parse)."""
    names: list[str] = []
    for stmt in stmts:
        stripped = strip_line_comments(stmt)
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


_ADD_COLUMN_RE = re.compile(
    rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)\s+ADD\s+COLUMN\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?\"?({_IDENTIFIER})\"?",
    re.IGNORECASE,
)
_CREATE_RELATION_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)"
    r"|CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)"
    r"|CREATE\s+(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)",
    re.IGNORECASE,
)
_CREATE_SCHEMA_RE = re.compile(r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w\"]+)", re.IGNORECASE)


def _idempotent_target(stmt: str) -> tuple[str, ...] | None:
    """Classify a single statement as one of the create-if-missing DDL shapes this module
    can check for "already applied" with no ambiguity: ("relation", name) for a table,
    index, sequence or view; ("schema", name); or ("column", table, column) for an ADD
    COLUMN. None if the statement isn't one of these shapes — including any CREATE OR
    REPLACE, which is never safe to treat as a no-op just because the object exists, since
    the migration could be replacing it with different content."""
    stripped = strip_line_comments(stmt).strip()
    if re.search(r"\bOR\s+REPLACE\b", stripped, re.IGNORECASE):
        return None

    if re.match(r"CREATE\s+TABLE\b", stripped, re.IGNORECASE):
        names = _created_table_names([stmt])
        return ("relation", names[0]) if names else None

    m = _CREATE_RELATION_RE.match(stripped)
    if m:
        name = next(g for g in m.groups() if g is not None)
        return ("relation", name)

    m = _CREATE_SCHEMA_RE.match(stripped)
    if m:
        return ("schema", m.group(1))

    m = _ADD_COLUMN_RE.match(stripped)
    if m:
        return ("column", m.group(1), m.group(2))

    return None


def _target_exists(con: psycopg.Connection, target: tuple[str, ...]) -> bool:
    kind = target[0]
    if kind == "relation":
        row = con.execute("select to_regclass(%s)", (target[1],)).fetchone()
    elif kind == "schema":
        row = con.execute("select to_regnamespace(%s)", (target[1],)).fetchone()
    else:  # column
        row = con.execute(
            "select 1 from pg_attribute where attrelid = to_regclass(%s) "
            "and attname = %s and not attisdropped",
            (target[1], target[2]),
        ).fetchone()
    return bool(row and row[0])


def already_fully_applied(conninfo: str, path: Path, dialect: str | Dialect = "postgres") -> bool:
    """Whether every statement in this migration is a recognized create-if-missing shape
    (table/index/sequence/view/schema, or add-column) AND its target already exists in the
    database — i.e. re-running the migration would do nothing. Used by `--ask` to
    auto-answer "already done" without prompting, so migrations that are trivially no-ops
    don't interrupt review. A single unrecognized or not-yet-applied statement means
    False — this never guesses. For MSSQL the unit is a `GO`-separated batch and the
    recognized shapes are table, view, schema and ADD column."""
    if _is_mssql(dialect):
        from . import migrate_mssql

        batches = migrate_mssql.split_batches(path.read_text(encoding="utf-8"))
        mssql_targets = [migrate_mssql.idempotent_target(b) for b in batches]
        if not batches or any(t is None for t in mssql_targets):
            return False
        return migrate_mssql.targets_exist(conninfo, cast(list[tuple[str, ...]], mssql_targets))
    stmts = _split_sql(path.read_text(encoding="utf-8"))
    if not stmts:
        return False
    targets = [_idempotent_target(s) for s in stmts]
    if any(t is None for t in targets):
        return False
    with psycopg.connect(conninfo) as con:
        return all(_target_exists(con, cast(tuple[str, ...], t)) for t in targets)


def list_migration_files(
    migrations_dir: Path,
    *,
    areas: frozenset[str] | None = None,
    exclude_areas: frozenset[str] | None = None,
    schemas: frozenset[str] | None = None,
    exclude_schemas: frozenset[str] | None = None,
    env: str | None = None,
) -> list[Path]:
    """Migration files under migrations_dir, restricted by area (see
    `.areas`), by schema (see `.schemas`), and by environment tag (see
    `.envtag`) — e.g. a `2026-07-10_backfill.prod.sql` is only included when
    `env="prod"`. `env=None` (the default) applies no environment filtering
    at all, so every file is a candidate regardless of its tag."""
    files = sorted(migrations_dir.glob("*.sql"))
    files = filter_by_area(files, only=areas, exclude=exclude_areas)
    files = filter_by_schema(files, only=schemas, exclude=exclude_schemas)
    return [f for f in files if env_allowed(f, env)]


def applied_migrations(
    conninfo: str, tracking_table: str, dialect: str | Dialect = "postgres"
) -> dict[str, tuple[datetime, str]]:
    """Filename -> (applied_at, applied_by) for every migration recorded in the tracking table."""
    if _is_mssql(dialect):
        from . import migrate_mssql

        return migrate_mssql.applied_migrations(conninfo, tracking_table)
    table = _tracking_table_identifier(tracking_table)
    with psycopg.connect(conninfo) as con:
        try:
            rows = con.execute(
                pg_sql.SQL("select filename, applied_at, applied_by from {} order by applied_at").format(table)
            ).fetchall()
        except pg_errors.UndefinedTable as e:
            raise TrackingTableMissing(tracking_table) from e
        return {r[0]: (r[1], r[2]) for r in rows}


def pending_migrations(
    migrations_dir: Path,
    conninfo: str,
    tracking_table: str,
    *,
    areas: frozenset[str] | None = None,
    exclude_areas: frozenset[str] | None = None,
    schemas: frozenset[str] | None = None,
    exclude_schemas: frozenset[str] | None = None,
    env: str | None = None,
    dialect: str | Dialect = "postgres",
) -> list[Path]:
    applied = applied_migrations(conninfo, tracking_table, dialect)
    files = list_migration_files(
        migrations_dir,
        areas=areas,
        exclude_areas=exclude_areas,
        schemas=schemas,
        exclude_schemas=exclude_schemas,
        env=env,
    )
    return [p for p in files if p.name not in applied]


def record_applied(
    conninfo: str, tracking_table: str, filename: str, dialect: str | Dialect = "postgres"
) -> bool:
    """Best-effort insert into the tracking table. Returns False without raising if the
    tracking table doesn't exist yet — e.g. this migration is the one that creates it."""
    if _is_mssql(dialect):
        from . import migrate_mssql

        return migrate_mssql.record_applied(conninfo, tracking_table, filename)
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


def created_table_names(sql: str, dialect: str | Dialect = "postgres") -> list[str]:
    """Table names any CREATE TABLE statement in this raw SQL script targets. Public
    wrapper around the same detection `apply_migration` uses internally, for callers that
    run a script directly (e.g. via `execute_sql_script`) instead of through a tracked
    migration file, and still want to know what tables -- if any -- it created."""
    if _is_mssql(dialect):
        from . import migrate_mssql

        return migrate_mssql.created_table_names(migrate_mssql.split_batches(sql))
    return _created_table_names(_split_sql(sql))


def verify_created_tables(conninfo: str, stmts: list[str], dialect: str | Dialect = "postgres") -> list[str]:
    """Table names from this migration's CREATE TABLE statements that do NOT exist in the
    database. Empty means everything landed. For MSSQL, `stmts` are `GO`-separated batches."""
    if _is_mssql(dialect):
        from . import migrate_mssql

        return migrate_mssql.missing_tables(conninfo, migrate_mssql.created_table_names(stmts))
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


def missing_privileges(
    conninfo: str, role: str, tables: list[str], privilege: str = "select", dialect: str | Dialect = "postgres"
) -> list[str]:
    """Table names from `tables` that `role` cannot currently exercise `privilege` on
    (checked via Postgres's own `has_table_privilege`). Empty means the role can access
    all of them. For guarding against the classic "migration creates a table, nobody
    grants it to the app's runtime role" gap: check the tables a migration just created
    against the role that will actually query them at runtime."""
    if _is_mssql(dialect):
        raise NotImplementedError("missing_privileges is only available for the postgres dialect")
    if not tables:
        return []
    missing = []
    with psycopg.connect(conninfo) as con:
        for tbl in tables:
            row = con.execute("select has_table_privilege(%s, %s, %s)", (role, tbl, privilege)).fetchone()
            if not (row and row[0]):
                missing.append(tbl)
    return missing


def _execute_stmts(conninfo: str, stmts: list[str]) -> None:
    with psycopg.connect(conninfo) as con:
        for stmt in stmts:
            con.execute(cast(LiteralString, stmt))
        con.commit()


def execute_sql_script(conninfo: str, sql: str, dialect: str | Dialect = "postgres") -> None:
    """Run a raw SQL script as one committed transaction, split into statements the same
    statement-boundary-safe way apply_migration is (dollar-quoted blocks, string literals,
    and line comments never get split mid-statement). Unlike apply_migration, this does
    no tracking-table bookkeeping and isn't forward-only -- for scripts meant to re-run
    every time, like an idempotent `GRANT ... ON ALL TABLES IN SCHEMA` privilege sync.
    For MSSQL the script is split on `GO` lines instead and runs as one transaction."""
    if _is_mssql(dialect):
        from . import migrate_mssql

        migrate_mssql.execute_batches(conninfo, migrate_mssql.split_batches(sql))
        return
    _execute_stmts(conninfo, _split_sql(sql))


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
    dialect: str | Dialect = "postgres",
) -> ApplyResult:
    sql = path.read_text(encoding="utf-8")
    filename = path.name
    mssql = _is_mssql(dialect)
    if mssql:
        from . import migrate_mssql

        stmts = migrate_mssql.split_batches(sql)
    else:
        stmts = _split_sql(sql)

    if not already_done:
        if mssql:
            migrate_mssql.execute_batches(conninfo, stmts)
        else:
            _execute_stmts(conninfo, stmts)

    # Tracking insert is a separate connection/transaction so a missing tracking table
    # never rolls back the DDL that was just applied.
    record_applied(conninfo, tracking_table, filename, dialect)

    if already_done:
        return ApplyResult(filename, executed=False, verified_tables=[])

    missing = verify_created_tables(conninfo, stmts, dialect)
    if missing:
        raise MigrationVerificationError(
            f"{filename}: table(s) not found after apply — migration may have rolled back: "
            + ", ".join(missing)
        )
    created = migrate_mssql.created_table_names(stmts) if mssql else _created_table_names(stmts)
    return ApplyResult(filename, executed=True, verified_tables=created)
