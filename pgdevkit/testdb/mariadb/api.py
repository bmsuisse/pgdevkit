from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

import pymysql

from ...db.mssql_sql import json_encode_value
from ...dialect import MARIADB
from ...envtag import strip_env_suffix
from .. import query
from ..config import ProjectConfig
from ..naming import escape_like_prefix, expected_db_names, live_worktree_branches, slugify
from ..schema import _iter_sql_files, _strip_layer_prefix
from . import constants
from .container import ensure_mariadb_container

logger = logging.getLogger(__name__)


def _connect(db_name: str | None = None) -> Any:
    return pymysql.connect(**constants.connect_kwargs(db_name))


def ident(name: str) -> str:
    """Backtick-quote a single identifier, doubling any embedded backtick."""
    return "`" + name.replace("`", "``") + "`"


def _env_for(config: ProjectConfig, db_name: str) -> dict[str, str]:
    prefix = config.env_prefix
    return {
        f"{prefix}MARIADB_HOST": constants.HOST,
        f"{prefix}MARIADB_PORT": str(constants.PORT),
        f"{prefix}MARIADB_DB": db_name,
        f"{prefix}MARIADB_USER": constants.USER,
        f"{prefix}MARIADB_PASSWORD": constants.PASSWORD,
    }


def _drop_database(db_name: str) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        # Open sessions would make DROP DATABASE wait on their metadata locks --
        # MariaDB's counterpart of Postgres's pg_terminate_backend().
        cur.execute("SELECT id FROM information_schema.processlist WHERE db = %s", [db_name])
        for (connection_id,) in cur.fetchall():
            try:
                cur.execute(f"KILL {int(connection_id)}")
            except pymysql.err.OperationalError:  # already gone
                pass
        cur.execute(f"DROP DATABASE IF EXISTS {ident(db_name)}")
    finally:
        conn.close()


def _insert_test_data(json_file: Path, table: str, force_reset: bool, cur: Any) -> None:
    rows: list[dict[str, Any]] = json.loads(json_file.read_text(encoding="utf-8"))
    if not rows:
        return
    if not force_reset:
        cur.execute(f"SELECT count(*) FROM {ident(table)}")
        (count,) = cur.fetchone()
        if count == len(rows):
            return
    col_names = list(rows[0])
    cur.execute(f"DELETE FROM {ident(table)}")
    cols = ", ".join(ident(c) for c in col_names)
    placeholders = ", ".join("%s" for _ in col_names)
    # dict/list values are stored as JSON text (MariaDB's JSON type is an alias of LONGTEXT).
    cur.executemany(
        f"INSERT INTO {ident(table)} ({cols}) VALUES ({placeholders})",
        [[json_encode_value(row[c]) for c in col_names] for row in rows],
    )


def _apply(
    config: ProjectConfig,
    db_name: str,
    force_reset: bool,
    *,
    env: str,
    areas: frozenset[str] | None,
    exclude_areas: frozenset[str] | None,
) -> None:
    database_dir = config.root / config.database_dir
    if not database_dir.is_dir():
        return
    conn = _connect(db_name)
    try:
        cur = conn.cursor()

        def _apply_file(file: Path, sql: str) -> None:
            for statement in query.split_mariadb_statements(sql):
                cur.execute(statement)
            table_stem = strip_env_suffix(file)
            json_file = file.parent / f"{table_stem}.test_data.json"
            if json_file.exists():
                _insert_test_data(json_file, _strip_layer_prefix(table_stem), force_reset, cur)

        # Everything lives in the one workspace database and files use unqualified
        # names, so the static dependency scan can't order them; a failed file is
        # retried until a full pass makes no progress, like the Postgres path.
        failures: list[tuple[Path, str]] = []
        for file, sql in _iter_sql_files(
            database_dir, MARIADB, env=env, areas=areas, exclude_areas=exclude_areas
        ):
            try:
                _apply_file(file, sql)
            except pymysql.MySQLError as e:
                logger.warning("Error executing %s (will retry): %s", file, e)
                failures.append((file, sql))
        while failures:
            still_failing: list[tuple[Path, str]] = []
            last_error: Exception | None = None
            for file, sql in failures:
                try:
                    _apply_file(file, sql)
                except pymysql.MySQLError as e:
                    still_failing.append((file, sql))
                    last_error = e
            if len(still_failing) == len(failures):
                assert last_error is not None
                raise last_error
            failures = still_failing
    finally:
        conn.close()


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
    if schemas or exclude_schemas:
        raise ValueError("schemas/exclude_schemas are not supported for engine = \"mariadb\" (no schemas inside a database)")
    if apply is not None or not apply_scripts:
        raise ValueError("ensure_testdb: apply/apply_scripts are only supported for engine = \"mssql\"")
    ensure_mariadb_container()
    if force_reset:
        _drop_database(db_name)
    conn = _connect()
    try:
        conn.cursor().execute(f"CREATE DATABASE IF NOT EXISTS {ident(db_name)}")
    finally:
        conn.close()
    _apply(config, db_name, force_reset, env=env, areas=areas, exclude_areas=exclude_areas)
    return _env_for(config, db_name)


def _dbs_with_prefix(prefix: str) -> list[str]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s",
            [f"{escape_like_prefix(prefix)}%"],
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _orphaned_dbs(config: ProjectConfig) -> list[str]:
    actual = _dbs_with_prefix(f"{slugify(config.name)}_")
    expected = expected_db_names(config, live_worktree_branches(config.root))
    return sorted(set(actual) - expected)


def find_orphaned_dbs(config: ProjectConfig) -> list[str]:
    return _orphaned_dbs(config)


def clean_testdb(config: ProjectConfig, db_name: str, all: bool, orphaned: bool = False) -> None:
    if orphaned:
        names = _orphaned_dbs(config)
    elif all:
        names = _dbs_with_prefix(f"{slugify(config.name)}_")
    else:
        names = [db_name]
    for name in names:
        _drop_database(name)


def status(config: ProjectConfig, db_name: str) -> dict[str, str]:
    return {
        "engine": config.engine,
        "container": constants.CONTAINER_NAME,
        "host": constants.HOST,
        "port": str(constants.PORT),
        "database": db_name,
        "dsn": constants.dsn(db_name),
    }


def run_sql(config: ProjectConfig, db_name: str, sql: str) -> list[dict] | None:
    conn = _connect(db_name)
    try:
        last_rows: list[dict] | None = None
        for statement in query.split_mariadb_statements(sql):
            cur = conn.cursor()
            cur.execute(statement)
            if cur.description:
                cols = [c[0] for c in cur.description]
                last_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            else:
                last_rows = None
        return last_rows
    finally:
        conn.close()


def dsn_for(config: ProjectConfig, db_name: str) -> str:
    return constants.dsn(db_name)


def shell_argv(config: ProjectConfig, db_name: str) -> tuple[str, list[str]]:
    """The `mariadb` client is the documented external prerequisite, like `psql`
    for Postgres. `--protocol=tcp` because `-h localhost` would otherwise pick
    a unix socket instead of the container's published port."""
    return "mariadb", [
        "mariadb",
        "--protocol=tcp",
        "-h", constants.HOST,
        "-P", str(constants.PORT),
        "-u", constants.USER,
        *([f"-p{constants.PASSWORD}"] if constants.PASSWORD else []),
        db_name,
    ]
