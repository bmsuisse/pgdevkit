from __future__ import annotations

from typing import Any

import psycopg
import pytest

from pgdevkit.testdb import ensure_testdb


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    env = ensure_testdb()
    return (
        f"postgresql://{env['PGDEVKIT_POSTGRES_USER']}:{env['PGDEVKIT_POSTGRES_PASSWORD']}"
        f"@{env['PGDEVKIT_POSTGRES_HOST']}:{env['PGDEVKIT_POSTGRES_PORT']}/{env['PGDEVKIT_POSTGRES_DB']}"
    )


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
