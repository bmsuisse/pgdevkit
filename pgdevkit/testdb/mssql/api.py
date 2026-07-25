from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import mssql_python

from ...db.mssql_sql import ident, json_encode_value
from ...dialect import MSSQL
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


async def _apply(config: ProjectConfig, db_name: str, force_reset: bool) -> None:
    def _connect() -> Any:
        return mssql_python.connect(_db_dsn(db_name), autocommit=True)

    conn = await asyncio.to_thread(_connect)
    try:
        database_dir = config.root / config.database_dir
        if not database_dir.is_dir():
            return
        for file, sql in _iter_sql_files(database_dir, MSSQL):
            for batch in query.split_tsql_batches(sql):

                def _exec(batch: str = batch) -> None:
                    conn.cursor().execute(batch)

                await asyncio.to_thread(_exec)
            json_file = file.with_suffix(".test_data.json")
            if json_file.exists():
                schema_name = _strip_layer_prefix(file.parent.parent.name)
                table_stem = _strip_layer_prefix(file.stem)
                await _insert_test_data(json_file, f"{schema_name}.{table_stem}", force_reset, conn)
    finally:
        await asyncio.to_thread(conn.close)


def ensure_testdb(config: ProjectConfig, db_name: str, force_reset: bool) -> dict[str, str]:
    ensure_mssql_container()

    async def _run() -> None:
        if force_reset:
            await _drop_database(db_name)
        await _ensure_database(db_name)
        await _apply(config, db_name, force_reset)

    asyncio.run(_run())
    return _env_for(config, db_name)


def clean_testdb(config: ProjectConfig, db_name: str, all: bool) -> None:
    from ..naming import slugify

    async def _run() -> None:
        if not all:
            await _drop_database(db_name)
            return
        prefix = f"{slugify(config.name)}_"
        escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        def _list_names() -> list[str]:
            conn = mssql_python.connect(_admin_dsn(), autocommit=True)
            try:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sys.databases WHERE name LIKE ? ESCAPE '\\'", [f"{escaped_prefix}%"])
                return [row[0] for row in cur.fetchall()]
            finally:
                conn.close()

        names = await asyncio.to_thread(_list_names)
        for name in names:
            await _drop_database(name)

    asyncio.run(_run())


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
