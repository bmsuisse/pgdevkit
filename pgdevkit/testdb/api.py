from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
from psycopg.sql import SQL, Identifier

from . import constants, query
from .config import ProjectConfig, load_config
from .container import ensure_container
from .naming import current_branch, slugify, workspace_db_name
from .schema import apply_schema


def _admin_dsn() -> str:
    return constants.conninfo("postgres", connect_timeout=10)


def _db_dsn(db_name: str) -> str:
    return constants.conninfo(db_name, connect_timeout=10)


def _resolve(project_root: Path | None) -> tuple[ProjectConfig, str]:
    config = load_config(project_root)
    branch = current_branch(config.root)
    db_name = workspace_db_name(config.name, branch)
    return config, db_name


def _env_for(config: ProjectConfig, db_name: str) -> dict[str, str]:
    prefix = config.env_prefix
    return {
        f"{prefix}POSTGRES_HOST": constants.HOST,
        f"{prefix}POSTGRES_PORT": str(constants.PORT),
        f"{prefix}POSTGRES_DB": db_name,
        f"{prefix}POSTGRES_USER": constants.USER,
        f"{prefix}POSTGRES_PASSWORD": constants.PASSWORD,
    }


async def _ensure_database(db_name: str) -> None:
    async with await psycopg.AsyncConnection.connect(_admin_dsn(), autocommit=True) as con:
        result = await con.execute("SELECT 1 FROM pg_database WHERE datname = %(db)s", {"db": db_name})
        if await result.fetchone():
            return
        try:
            await con.execute(SQL("CREATE DATABASE {}").format(Identifier(db_name)))
        except psycopg.errors.DuplicateDatabase:
            pass


async def _drop_database(db_name: str) -> None:
    async with await psycopg.AsyncConnection.connect(_admin_dsn(), autocommit=True) as con:
        await con.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %(db)s",
            {"db": db_name},
        )
        await con.execute(SQL("DROP DATABASE IF EXISTS {}").format(Identifier(db_name)))


async def _apply(config: ProjectConfig, db_name: str, force_reset: bool) -> None:
    async with await psycopg.AsyncConnection.connect(_db_dsn(db_name), autocommit=True) as con:
        await apply_schema(
            con,
            config.root / config.database_dir,
            extensions=config.extensions,
            force_reset=force_reset,
        )


def ensure_testdb(project_root: Path | None = None, force_reset: bool = False) -> dict[str, str]:
    """Ensure the shared container is running, this workspace's database
    exists, and its schema is applied. Returns the {PREFIX}POSTGRES_* env
    vars for this workspace."""
    ensure_container()
    config, db_name = _resolve(project_root)

    async def _run() -> None:
        if force_reset:
            await _drop_database(db_name)
        await _ensure_database(db_name)
        await _apply(config, db_name, force_reset)

    asyncio.run(_run())
    return _env_for(config, db_name)


def reset_testdb(project_root: Path | None = None) -> dict[str, str]:
    """Drop and recreate only this workspace's database, then reapply
    schema and seed data."""
    return ensure_testdb(project_root, force_reset=True)


def clean_testdb(project_root: Path | None = None, all: bool = False) -> None:
    """Drop this workspace's database. With all=True, drop every database
    belonging to this project (matched by its name-slug prefix), across
    every worktree/branch."""
    config, db_name = _resolve(project_root)

    async def _run() -> None:
        if not all:
            await _drop_database(db_name)
            return
        prefix = f"{slugify(config.name)}_"
        escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with await psycopg.AsyncConnection.connect(_admin_dsn(), autocommit=True) as con:
            result = await con.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %(pattern)s ESCAPE '\\'",
                {"pattern": f"{escaped_prefix}%"},
            )
            names = [row[0] for row in await result.fetchall()]
        for name in names:
            await _drop_database(name)

    asyncio.run(_run())


def status(project_root: Path | None = None) -> dict[str, str]:
    config, db_name = _resolve(project_root)
    return {
        "container": constants.CONTAINER_NAME,
        "host": constants.HOST,
        "port": str(constants.PORT),
        "database": db_name,
        "dsn": _db_dsn(db_name),
    }


def run_sql(sql: str, project_root: Path | None = None) -> list[dict] | None:
    _, db_name = _resolve(project_root)
    return asyncio.run(query.execute(_db_dsn(db_name), sql))


def dsn_for(project_root: Path | None = None) -> str:
    _, db_name = _resolve(project_root)
    return _db_dsn(db_name)
