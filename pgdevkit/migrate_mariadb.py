"""MariaDB flavour of the `pgdevkit.migrate` primitives: the same forward-only,
tracking-table-backed migration flow, built on PyMySQL.

Differences from the Postgres path that live here:

- The connection target is a `mysql://user:pass@host:port/db` URL.
- MariaDB DDL commits implicitly, so a migration is *not* atomic: statements run one
  by one and a failure part-way leaves the earlier DDL in place (data changes made
  before the failure are rolled back). Make migrations idempotent where possible.
- There are no schemas inside a database, so the tracking table is `table` (in the
  URL's database) or `database.table`. Its columns are `filename varchar(255)
  primary key, applied_at timestamp default current_timestamp, applied_by
  varchar(128) default current_user()`.
- A migration is split into statements by `testdb.query.split_mariadb_statements`
  (honors `DELIMITER`). `--ask`'s "already done" detection covers single-statement
  table, view, schema and ADD column creations.

Imported lazily by `pgdevkit.migrate` so the `mariadb` extra is only needed when
`--dialect mariadb` is actually used.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from .sql_text import has_top_level_comma
from .testdb.query import split_mariadb_statements

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$]*"
_PART = rf"(?:`(?:[^`]|``)+`|{_IDENTIFIER})"
_NAME = rf"{_PART}(?:\.{_PART})?"
_CREATE_TABLE_RE = re.compile(
    rf"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.IGNORECASE
)
_CREATE_VIEW_RE = re.compile(rf"CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})", re.IGNORECASE)
_CREATE_SCHEMA_RE = re.compile(
    rf"CREATE\s+(?:SCHEMA|DATABASE)\s+(?:IF\s+NOT\s+EXISTS\s+)?({_PART})", re.IGNORECASE
)
_ADD_COLUMN_RE = re.compile(
    rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?({_NAME})\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?!(?:CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK|INDEX|KEY|FULLTEXT|SPATIAL|PERIOD)\b)({_PART})\s",
    re.IGNORECASE,
)


def _unquote_part(part: str) -> str:
    return part[1:-1].replace("``", "`") if part.startswith("`") else part


def split_name(name: str) -> tuple[str | None, str]:
    """Parse 'table' or 'database.table' (backticks allowed) into (database or None, table)."""
    match = re.fullmatch(rf"(?:({_PART})\.)?({_PART})", name)
    if not match:
        raise ValueError(f"tracking_table must look like table or database.table, got {name!r}")
    database, table = match.groups()
    return (_unquote_part(database) if database else None), _unquote_part(table)


def ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _qualified(name: str) -> str:
    database, table = split_name(name)
    return f"{ident(database)}.{ident(table)}" if database else ident(table)


def split_batches(sql: str) -> list[str]:
    return split_mariadb_statements(sql)


def require_driver() -> None:
    """Raise a clear ImportError if the `mariadb` extra isn't installed."""
    try:
        import pymysql  # noqa: F401
    except ImportError:
        raise ImportError("MariaDB support needs the mariadb extra: pip install pgdevkit[mariadb]") from None


def connect(conninfo: str, *, autocommit: bool = False) -> Any:
    """Open a connection from a `mysql://user:pass@host:port/db` URL."""
    require_driver()
    import pymysql

    parsed = urlparse(conninfo)
    if parsed.scheme not in ("mysql", "mariadb") or not parsed.hostname:
        raise ValueError("MariaDB --url must look like mysql://user:pass@host:port/database")
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=unquote(parsed.path.lstrip("/")) or None,
        autocommit=autocommit,
    )


def strip_comments(sql: str) -> str:
    """Drop `-- `, `#` and `/* ... */` comments, leaving '...', "..." and `...` literals
    untouched. Executable `/*! ... */` comments are dropped too (they never matter here)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            end = i + 1
            while end < n:
                if sql[end] == "\\" and c != "`":
                    end += 2
                    continue
                if sql[end] == c:
                    end += 1
                    if sql[end : end + 1] == c:
                        end += 1
                        continue
                    break
                end += 1
            out.append(sql[i:end])
            i = end
        elif c == "#" or (sql.startswith("--", i) and sql[i + 2 : i + 3] in ("", " ", "\t", "\n", "\r")):
            end = sql.find("\n", i)
            i = n if end == -1 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _scalar(con: Any, sql: str, params: tuple = ()) -> Any:
    with con.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def _table_exists(con: Any, name: str) -> bool:
    database, table = split_name(name)
    return (
        _scalar(
            con,
            "select 1 from information_schema.tables "
            "where table_schema = coalesce(%s, database()) and table_name = %s",
            (database, table),
        )
        is not None
    )


def applied_migrations(conninfo: str, tracking_table: str) -> dict[str, tuple[datetime, str]]:
    import pymysql

    from .migrate import TrackingTableMissing

    con = connect(conninfo, autocommit=True)
    try:
        with con.cursor() as cur:
            try:
                cur.execute(
                    f"select filename, applied_at, applied_by from {_qualified(tracking_table)} order by applied_at"
                )
            except pymysql.err.ProgrammingError as e:
                if e.args and e.args[0] == 1146:  # ER_NO_SUCH_TABLE
                    raise TrackingTableMissing(tracking_table) from e
                raise
            return {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    finally:
        con.close()


def record_applied(conninfo: str, tracking_table: str, filename: str) -> bool:
    """Best-effort insert into the tracking table (idempotent per filename). Returns False
    without raising if the tracking table doesn't exist yet."""
    import pymysql

    con = connect(conninfo, autocommit=True)
    try:
        with con.cursor() as cur:
            try:
                cur.execute(f"insert ignore into {_qualified(tracking_table)} (filename) values (%s)", (filename,))
            except pymysql.err.ProgrammingError as e:
                if e.args and e.args[0] == 1146:
                    return False
                raise
        return True
    finally:
        con.close()


def execute_batches(conninfo: str, batches: list[str]) -> None:
    """Run every statement in order. DDL commits implicitly, so only data changes are
    rolled back if one fails; earlier DDL stays applied."""
    con = connect(conninfo)
    try:
        with con.cursor() as cur:
            for statement in batches:
                cur.execute(statement)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def created_table_names(batches: list[str]) -> list[str]:
    """Table names any CREATE TABLE in these statements targets (comments ignored).
    CREATE TEMPORARY TABLE never matches, since `TEMPORARY` follows `CREATE`."""
    names: list[str] = []
    for statement in batches:
        names.extend(m.group(1) for m in _CREATE_TABLE_RE.finditer(strip_comments(statement)))
    return names


def idempotent_target(statement: str) -> tuple[str, ...] | None:
    """MariaDB counterpart of `migrate._idempotent_target`: ("relation", name) for a table or
    view, ("schema", name), or ("column", table, column) for an ADD column. Anything else
    (CREATE OR REPLACE, indexes, procedures, data changes, a multi-column ADD, ...) is
    None -- never guessed."""
    stripped = strip_comments(statement).strip()
    if re.search(r"\bOR\s+REPLACE\b", stripped, re.IGNORECASE):
        return None
    for regex in (_CREATE_TABLE_RE, _CREATE_VIEW_RE):
        m = regex.match(stripped)
        if m:
            return ("relation", m.group(1))
    m = _CREATE_SCHEMA_RE.match(stripped)
    if m:
        return ("schema", _unquote_part(m.group(1)))
    m = _ADD_COLUMN_RE.match(stripped + " ")
    if m and not has_top_level_comma(stripped):
        return ("column", m.group(1), _unquote_part(m.group(2)))
    return None


def _target_exists(con: Any, target: tuple[str, ...]) -> bool:
    kind = target[0]
    if kind == "relation":
        return _table_exists(con, target[1])
    if kind == "schema":
        return _scalar(con, "select 1 from information_schema.schemata where schema_name = %s", (target[1],)) is not None
    database, table = split_name(target[1])
    return (
        _scalar(
            con,
            "select 1 from information_schema.columns "
            "where table_schema = coalesce(%s, database()) and table_name = %s and column_name = %s",
            (database, table, target[2]),
        )
        is not None
    )


def targets_exist(conninfo: str, targets: list[tuple[str, ...]]) -> bool:
    con = connect(conninfo, autocommit=True)
    try:
        return all(_target_exists(con, t) for t in targets)
    finally:
        con.close()


def missing_tables(conninfo: str, tables: list[str]) -> list[str]:
    """Table names (from `created_table_names`) that do NOT exist in the database."""
    if not tables:
        return []
    con = connect(conninfo, autocommit=True)
    try:
        return [t for t in tables if not _table_exists(con, t)]
    finally:
        con.close()
