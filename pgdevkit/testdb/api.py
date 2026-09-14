from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
from psycopg.sql import SQL, Identifier

from . import constants, query
from .config import ProjectConfig, load_config
from .container import ensure_container
from .naming import (
    current_branch,
    escape_like_prefix,
    expected_db_names,
    live_worktree_branches,
    slugify,
    workspace_db_name,
)
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


def _mssql_api():
    # Imported lazily so importing pgdevkit.testdb (and thus pgdevkit.cli)
    # doesn't require the mssql extra unless a project actually opts into
    # `engine = "mssql"`.
    from .mssql import api as mssql_api

    return mssql_api


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


async def _dbs_with_prefix(prefix: str) -> list[str]:
    escaped_prefix = escape_like_prefix(prefix)
    async with await psycopg.AsyncConnection.connect(_admin_dsn(), autocommit=True) as con:
        result = await con.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %(pattern)s ESCAPE '\\'",
            {"pattern": f"{escaped_prefix}%"},
        )
        return [row[0] for row in await result.fetchall()]


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
    async with await psycopg.AsyncConnection.connect(_db_dsn(db_name), autocommit=True) as con:
        await apply_schema(
            con,
            config.root / config.database_dir,
            extensions=config.extensions,
            force_reset=force_reset,
            env=env,
            areas=areas,
            exclude_areas=exclude_areas,
            schemas=schemas,
            exclude_schemas=exclude_schemas,
        )


def ensure_testdb(
    project_root: Path | None = None,
    force_reset: bool = False,
    *,
    env: str = "local_test",
    areas: frozenset[str] | None = None,
    exclude_areas: frozenset[str] | None = None,
    schemas: frozenset[str] | None = None,
    exclude_schemas: frozenset[str] | None = None,
) -> dict[str, str]:
    """Ensure the shared container is running, this workspace's database
    exists, and its schema is applied. Returns the {PREFIX}POSTGRES_* env
    vars for this workspace (or the mssql equivalent's env vars, per
    `config.engine`).

    `env` selects which environment-tagged files apply (see pgdevkit.envtag,
    e.g. a `grants.prod.sql` is skipped unless env="prod").

    `areas`/`exclude_areas` and `schemas`/`exclude_schemas` restrict which
    database/ files get applied -- e.g. for a test DB scoped to one area or
    schema. Neither filters what gets *dropped* by force_reset/clean, only
    what gets (re)applied."""
    config, db_name = _resolve(project_root)
    if config.engine == "mssql":
        return _mssql_api().ensure_testdb(
            config, db_name, force_reset,
            env=env, areas=areas, exclude_areas=exclude_areas, schemas=schemas, exclude_schemas=exclude_schemas,
        )

    ensure_container()

    async def _run() -> None:
        if force_reset:
            await _drop_database(db_name)
        await _ensure_database(db_name)
        await _apply(
            config, db_name, force_reset,
            env=env, areas=areas, exclude_areas=exclude_areas, schemas=schemas, exclude_schemas=exclude_schemas,
        )

    asyncio.run(_run())
    return _env_for(config, db_name)


def reset_testdb(
    project_root: Path | None = None,
    *,
    env: str = "local_test",
    areas: frozenset[str] | None = None,
    exclude_areas: frozenset[str] | None = None,
    schemas: frozenset[str] | None = None,
    exclude_schemas: frozenset[str] | None = None,
) -> dict[str, str]:
    """Drop and recreate only this workspace's database, then reapply
    schema and seed data."""
    return ensure_testdb(
        project_root, force_reset=True,
        env=env, areas=areas, exclude_areas=exclude_areas, schemas=schemas, exclude_schemas=exclude_schemas,
    )


async def _find_orphaned_dbs(config: ProjectConfig) -> list[str]:
    prefix = f"{slugify(config.name)}_"
    actual = await _dbs_with_prefix(prefix)
    expected = expected_db_names(config, live_worktree_branches(config.root))
    return sorted(set(actual) - expected)


def find_orphaned_dbs(project_root: Path | None = None) -> list[str]:
    """Databases belonging to this project (matched by its name-slug prefix)
    that don't belong to any currently live git worktree of this repo --
    i.e. their branch's worktree was removed (or never existed) without
    also dropping its database."""
    config, _ = _resolve(project_root)
    if config.engine == "mssql":
        return _mssql_api().find_orphaned_dbs(config)
    return asyncio.run(_find_orphaned_dbs(config))


def workspace_db_names(project_root: Path | None = None) -> frozenset[str]:
    """Every DB name this exact workspace (the branch currently checked out
    at `project_root`) owns: its main workspace DB plus one
    `<main>{suffix}` sibling per configured `extra_db_suffixes` entry. The
    single-workspace analog of what `find_orphaned_dbs` computes across
    every *live* worktree -- for a caller that wants "which DBs belong to
    this one worktree right now" (e.g. to drop them before removing the
    worktree itself), as opposed to a whole-project orphan sweep. Engine
    (postgres/mssql) doesn't affect naming, so this doesn't dispatch on it."""
    config = load_config(project_root)
    branch = current_branch(config.root)
    return frozenset(expected_db_names(config, [branch]))


def clean_testdb(project_root: Path | None = None, all: bool = False, orphaned: bool = False) -> None:
    """Drop this workspace's database. With all=True, drop every database
    belonging to this project (matched by its name-slug prefix), across
    every worktree/branch. With orphaned=True, drop only those without a
    currently live git worktree (see `find_orphaned_dbs`). At most one of
    all/orphaned may be set."""
    if all and orphaned:
        raise ValueError("clean_testdb: pass at most one of all=True, orphaned=True")

    config, db_name = _resolve(project_root)
    if config.engine == "mssql":
        _mssql_api().clean_testdb(config, db_name, all, orphaned)
        return

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


def status(project_root: Path | None = None) -> dict[str, str]:
    config, db_name = _resolve(project_root)
    if config.engine == "mssql":
        return _mssql_api().status(config, db_name)
    return {
        "engine": config.engine,
        "container": constants.CONTAINER_NAME,
        "host": constants.HOST,
        "port": str(constants.PORT),
        "database": db_name,
        "dsn": _db_dsn(db_name),
    }


def run_sql(sql: str, project_root: Path | None = None) -> list[dict] | None:
    config, db_name = _resolve(project_root)
    if config.engine == "mssql":
        return _mssql_api().run_sql(config, db_name, sql)
    return asyncio.run(query.execute(_db_dsn(db_name), sql))


def dsn_for(project_root: Path | None = None) -> str:
    config, db_name = _resolve(project_root)
    if config.engine == "mssql":
        return _mssql_api().dsn_for(config, db_name)
    return _db_dsn(db_name)


def shell_argv(project_root: Path | None = None) -> tuple[str, list[str]]:
    """The (binary, argv) to `os.execvp` for an interactive shell against
    this workspace's database -- `psql` for Postgres, `sqlcmd` for MSSQL."""
    config, db_name = _resolve(project_root)
    if config.engine == "mssql":
        return _mssql_api().shell_argv(config, db_name)
    return "psql", ["psql", _db_dsn(db_name)]
