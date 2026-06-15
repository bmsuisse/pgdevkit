from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

TEST_PORT = int(os.environ.get("PGDB_TEST_POSTGRES_PORT", "54326"))
TEST_DB = os.environ.get("PGDB_TEST_POSTGRES_DB", "pgdb_test")
TEST_USER = os.environ.get("PGDB_TEST_POSTGRES_USER", "postgres")
TEST_PASSWORD = os.environ.get("PGDB_TEST_POSTGRES_PASSWORD", "testpwd")
TEST_HOST = os.environ.get("PGDB_TEST_POSTGRES_HOST", "localhost")
CONTAINER_NAME = "pgdb_postgres4test"

DSN = f"postgresql://{TEST_USER}:{TEST_PASSWORD}@{TEST_HOST}:{TEST_PORT}/{TEST_DB}"


def _start_docker() -> None:
    import docker  # type: ignore
    import docker.errors  # type: ignore

    client = docker.from_env()
    try:
        existing = client.containers.get(CONTAINER_NAME)
        if existing.status != "running":
            existing.start()
        return
    except docker.errors.NotFound:
        pass

    client.containers.run(
        "postgres:17",
        name=CONTAINER_NAME,
        detach=True,
        ports={"5432/tcp": TEST_PORT},
        environment={
            "POSTGRES_PASSWORD": TEST_PASSWORD,
            "POSTGRES_DB": TEST_DB,
            "POSTGRES_USER": TEST_USER,
        },
    )


def _wait_for_postgres(timeout: int = 30) -> None:
    admin_dsn = f"postgresql://{TEST_USER}:{TEST_PASSWORD}@{TEST_HOST}:{TEST_PORT}/postgres"
    for _ in range(timeout):
        try:
            with psycopg.connect(admin_dsn, connect_timeout=2):
                return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Postgres did not become ready in time")


def _ensure_test_db() -> None:
    from psycopg import sql

    admin_dsn = f"postgresql://{TEST_USER}:{TEST_PASSWORD}@{TEST_HOST}:{TEST_PORT}/postgres"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            sql.SQL("SELECT 1 FROM pg_database WHERE datname = %s"), (TEST_DB,)
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    if os.environ.get("SKIP_START_POSTGRES") == "1":
        return DSN
    _start_docker()
    _wait_for_postgres()
    _ensure_test_db()
    return DSN


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
