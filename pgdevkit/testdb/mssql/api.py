from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable

import mssql_python

from ...db.mssql_sql import ident, json_encode_value
from ...dialect import MSSQL
from ...envtag import strip_env_suffix
from .. import query
from ..config import ProjectConfig
from ..schema import _iter_sql_files, _strip_layer_prefix
from . import constants
from .container import ensure_mssql_container


def _admin_dsn() -> str:
    return constants.conninfo("master")


def _db_dsn(db_name: str) -> str:
    return constants.conninfo(db_name)


def _env_for(config: ProjectConfig, db_name: str) -> dict[str, str]:
    prefix = config.env_prefix
    return {
        f"{prefix}MSSQL_HOST": constants.HOST,
        f"{prefix}MSSQL_PORT": str(constants.PORT),
        f"{prefix}MSSQL_DB": db_name,
        f"{prefix}MSSQL_USER": constants.USER,
        f"{prefix}MSSQL_PASSWORD": constants.PASSWORD,
    }


async def _ensure_database(db_name: str) -> None:
    def _run() -> None:
        conn = mssql_python.connect(_admin_dsn(), autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM sys.databases WHERE name = ?", [db_name])
            if cur.fetchone():
                return
            cur.execute(f"CREATE DATABASE {ident(db_name)}")
        finally:
            conn.close()

    await asyncio.to_thread(_run)


async def _drop_database(db_name: str) -> None:
    def _run() -> None:
        conn = mssql_python.connect(_admin_dsn(), autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM sys.databases WHERE name = ?", [db_name])
            if not cur.fetchone():
                return
            # SQL Server refuses to drop a database that still has snapshots.
            for snapshot in _snapshots_of(cur, db_name):
                cur.execute(f"DROP DATABASE IF EXISTS {ident(snapshot)}")
            # One statement kills other sessions and drops -- SQL Server's
            # equivalent of Postgres's pg_terminate_backend()+DROP DATABASE.
            cur.execute(f"ALTER DATABASE {ident(db_name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(f"DROP DATABASE IF EXISTS {ident(db_name)}")
        finally:
            conn.close()

    await asyncio.to_thread(_run)


async def _insert_test_data(json_file: Path, table: str, force_reset: bool, conn: Any) -> None:
    if not json_file.exists():
        return
    rows: list[dict[str, Any]] = json.loads(json_file.read_text(encoding="utf-8"))
    if not rows:
        return
    schema, table_name = table.split(".")
    qualified = f"{ident(schema)}.{ident(table_name)}"

    def _run() -> None:
        cur = conn.cursor()
        if not force_reset:
            cur.execute(f"SELECT count(*) FROM {qualified}")
            (count,) = cur.fetchone()
            if count == len(rows):
                return
        # Unlike the Postgres path (ComplexHelper-driven composite/enum
        # conversion), MSSQL has no composite/enum equivalent to convert
        # into -- dict/list values are serialized as JSON text instead
        # (see db/mssql_sql.json_encode_value), matching a `json`-typed or
        # legacy NVARCHAR(MAX)-storing-JSON column, rather than silently
        # dropped or erroring.
        col_names = list(rows[0])
        cur.execute(f"DELETE FROM {qualified}")
        cols = ", ".join(ident(c) for c in col_names)
        placeholders = ", ".join("?" for _ in col_names)
        insert_sql = f"INSERT INTO {qualified} ({cols}) VALUES ({placeholders})"
        param_rows = [[json_encode_value(row[c]) for c in col_names] for row in rows]
        cur.executemany(insert_sql, param_rows)

    await asyncio.to_thread(_run)


async def _apply(
    config: ProjectConfig,
    db_name: str,
    force_reset: bool,
    *,
    env: str = "local_test",
    areas: frozenset[str] | None = None,
    exclude_areas: frozenset[str] | None = None,
    schemas: frozenset[str] | None = None,
    exclude_schemas: frozenset[str] | None = None,
) -> None:
    def _connect() -> Any:
        return mssql_python.connect(_db_dsn(db_name), autocommit=True)

    conn = await asyncio.to_thread(_connect)
    try:
        database_dir = config.root / config.database_dir
        if not database_dir.is_dir():
            return
        for file, sql in _iter_sql_files(
            database_dir, MSSQL,
            env=env, areas=areas, exclude_areas=exclude_areas, schemas=schemas, exclude_schemas=exclude_schemas,
        ):
            for batch in query.split_tsql_batches(sql):

                def _exec(batch: str = batch) -> None:
                    conn.cursor().execute(batch)

                await asyncio.to_thread(_exec)
            table_stem = strip_env_suffix(file)
            json_file = file.parent / f"{table_stem}.test_data.json"
            if json_file.exists():
                schema_name = _strip_layer_prefix(file.parent.parent.name)
                await _insert_test_data(
                    json_file, f"{schema_name}.{_strip_layer_prefix(table_stem)}", force_reset, conn
                )
    finally:
        await asyncio.to_thread(conn.close)


async def _run_apply_hook(db_name: str, apply: Callable[[Any], None]) -> None:
    def _run() -> None:
        conn = mssql_python.connect(_db_dsn(db_name), autocommit=True)
        try:
            apply(conn)
        finally:
            conn.close()

    await asyncio.to_thread(_run)


def ensure_testdb(
    config: ProjectConfig,
    db_name: str,
    force_reset: bool,
    *,
    env: str = "local_test",
    areas: frozenset[str] | None = None,
    exclude_areas: frozenset[str] | None = None,
    schemas: frozenset[str] | None = None,
    exclude_schemas: frozenset[str] | None = None,
    apply: Callable[[Any], None] | None = None,
    apply_scripts: bool = True,
) -> dict[str, str]:
    ensure_mssql_container()

    async def _run() -> None:
        if force_reset:
            await _drop_database(db_name)
        await _ensure_database(db_name)
        if apply_scripts:
            await _apply(
                config, db_name, force_reset,
                env=env, areas=areas, exclude_areas=exclude_areas, schemas=schemas, exclude_schemas=exclude_schemas,
            )
        if apply is not None:
            await _run_apply_hook(db_name, apply)

    asyncio.run(_run())
    return _env_for(config, db_name)


async def _dbs_with_prefix(prefix: str) -> list[str]:
    from ..naming import escape_like_prefix

    escaped_prefix = escape_like_prefix(prefix)

    def _list_names() -> list[str]:
        conn = mssql_python.connect(_admin_dsn(), autocommit=True)
        try:
            cur = conn.cursor()
            # Snapshots (source_database_id set) belong to their source database:
            # they are dropped together with it, never listed on their own -- else
            # a live worktree's snapshot would look like an orphan.
            cur.execute(
                "SELECT name FROM sys.databases WHERE name LIKE ? ESCAPE '\\' AND source_database_id IS NULL",
                [f"{escaped_prefix}%"],
            )
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    return await asyncio.to_thread(_list_names)


async def _find_orphaned_dbs(config: ProjectConfig) -> list[str]:
    from ..naming import expected_db_names, live_worktree_branches, slugify

    prefix = f"{slugify(config.name)}_"
    actual = await _dbs_with_prefix(prefix)
    expected = expected_db_names(config, live_worktree_branches(config.root))
    return sorted(set(actual) - expected)


def find_orphaned_dbs(config: ProjectConfig) -> list[str]:
    return asyncio.run(_find_orphaned_dbs(config))


def clean_testdb(config: ProjectConfig, db_name: str, all: bool, orphaned: bool = False) -> None:
    from ..naming import slugify

    async def _run() -> None:
        if orphaned:
            names = await _find_orphaned_dbs(config)
        elif all:
            names = await _dbs_with_prefix(f"{slugify(config.name)}_")
        else:
            names = [db_name]
        for name in names:
            await _drop_database(name)

    asyncio.run(_run())


_SNAPSHOT_NAME = re.compile(r"[A-Za-z0-9_]+")


def _snapshot_db_name(db_name: str, name: str) -> str:
    if not _SNAPSHOT_NAME.fullmatch(name):
        raise ValueError(f"Snapshot name must match [A-Za-z0-9_]+, got {name!r}")
    return f"{db_name}__ss_{name}"


def _literal(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def _snapshots_of(cur: Any, db_name: str) -> list[str]:
    cur.execute("SELECT name FROM sys.databases WHERE source_database_id = DB_ID(?) ORDER BY name", [db_name])
    return [row[0] for row in cur.fetchall()]


def snapshot_testdb(config: ProjectConfig, db_name: str, name: str) -> str:
    """Create (or replace) the database snapshot `name` of `db_name` and
    return the snapshot's database name. The snapshot's sparse files go next to
    the source database's data files (so they follow it onto a tmpfs mount) unless
    PGDEVKIT_TESTDB_MSSQL_SNAPSHOT_DIR says otherwise -- a path inside the container."""
    snapshot = _snapshot_db_name(db_name, name)
    conn = mssql_python.connect(_admin_dsn(), autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name, physical_name FROM sys.master_files WHERE database_id = DB_ID(?) AND type = 0 ORDER BY file_id",
            [db_name],
        )
        files = [(row[0], row[1]) for row in cur.fetchall()]
        if not files:
            raise RuntimeError(f"Database {db_name!r} does not exist -- call ensure_testdb() first")
        directory = constants.SNAPSHOT_DIR or files[0][1].rsplit("/", 1)[0]
        cur.execute(f"DROP DATABASE IF EXISTS {ident(snapshot)}")
        file_specs = ", ".join(
            f"(NAME = {_literal(logical)}, FILENAME = {_literal(f'{directory}/{snapshot}_{logical}.ss')})"
            for logical, _ in files
        )
        cur.execute(f"CREATE DATABASE {ident(snapshot)} ON {file_specs} AS SNAPSHOT OF {ident(db_name)}")
    finally:
        conn.close()
    return snapshot


def restore_testdb(config: ProjectConfig, db_name: str, name: str) -> None:
    """Revert `db_name` to the snapshot `name`, disconnecting everyone else
    first. The snapshot is kept, so it can be restored again. SQL Server only
    reverts from a database's sole snapshot, so any other snapshot of
    `db_name` has to be dropped first -- this raises instead of dropping it."""
    snapshot = _snapshot_db_name(db_name, name)
    conn = mssql_python.connect(_admin_dsn(), autocommit=True)
    try:
        cur = conn.cursor()
        snapshots = _snapshots_of(cur, db_name)
        if snapshot not in snapshots:
            raise RuntimeError(f"Database {db_name!r} has no snapshot {name!r} -- call snapshot_testdb() first")
        others = [s for s in snapshots if s != snapshot]
        if others:
            raise RuntimeError(
                f"SQL Server can only revert from the sole snapshot of a database; also found: {', '.join(others)}"
            )
        cur.execute(f"ALTER DATABASE {ident(db_name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        try:
            cur.execute(f"RESTORE DATABASE {ident(db_name)} FROM DATABASE_SNAPSHOT = {_literal(snapshot)}")
        finally:
            cur.execute(f"ALTER DATABASE {ident(db_name)} SET MULTI_USER")
    finally:
        conn.close()


def status(config: ProjectConfig, db_name: str) -> dict[str, str]:
    return {
        "engine": config.engine,
        "container": constants.CONTAINER_NAME,
        "host": constants.HOST,
        "port": str(constants.PORT),
        "database": db_name,
        "dsn": _db_dsn(db_name),
    }


def run_sql(config: ProjectConfig, db_name: str, sql: str) -> list[dict] | None:
    def _run() -> list[dict] | None:
        conn = mssql_python.connect(_db_dsn(db_name), autocommit=True)
        try:
            last_rows: list[dict] | None = None
            for batch in query.split_tsql_batches(sql):
                cur = conn.cursor()
                cur.execute(batch)
                if cur.description:
                    cols = [c[0] for c in cur.description]
                    last_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                else:
                    last_rows = None
            return last_rows
        finally:
            conn.close()

    return _run()


def dsn_for(config: ProjectConfig, db_name: str) -> str:
    return _db_dsn(db_name)


def shell_argv(config: ProjectConfig, db_name: str) -> tuple[str, list[str]]:
    """`sqlcmd` (the modern standalone github.com/microsoft/go-sqlcmd build,
    not the legacy mssql-tools18 package) is the documented external
    prerequisite here -- the same category as `psql` being assumed on PATH
    for the Postgres path. `-C` trusts the container's self-signed cert,
    required since sqlcmd v18+ defaults to encrypted+verified connections."""
    return "sqlcmd", [
        "sqlcmd",
        "-S", f"{constants.HOST},{constants.PORT}",
        "-U", constants.USER,
        "-P", constants.PASSWORD,
        "-d", db_name,
        "-C",
    ]
