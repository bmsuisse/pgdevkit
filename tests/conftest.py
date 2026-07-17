from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from pgdevkit.testdb import ensure_testdb


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    env = ensure_testdb()
    params = {
        "host": env["PGDEVKIT_POSTGRES_HOST"],
        "port": env["PGDEVKIT_POSTGRES_PORT"],
        "user": env["PGDEVKIT_POSTGRES_USER"],
        "dbname": env["PGDEVKIT_POSTGRES_DB"],
    }
    if env["PGDEVKIT_POSTGRES_PASSWORD"]:
        params["password"] = env["PGDEVKIT_POSTGRES_PASSWORD"]
    return make_conninfo(**params)


@pytest.fixture
def clean_db(postgres_dsn: str) -> Any:
    """Fixture that provides a clean database connection, dropping/recreating public schema."""
    from psycopg import sql

    def _reset(conn: psycopg.Connection[Any]) -> None:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sql.SQL("CREATE SCHEMA public"))
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS myapp CASCADE"))

    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        _reset(conn)
    yield postgres_dsn
    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        _reset(conn)
