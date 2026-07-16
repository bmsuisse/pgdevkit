from __future__ import annotations

import psycopg

from pgdevkit.testdb import constants
from pgdevkit.testdb.container import _create_container, ensure_container
from tests.testdb.conftest import requires_podman


def _admin_dsn() -> str:
    return (
        f"postgresql://{constants.USER}:{constants.PASSWORD}"
        f"@{constants.HOST}:{constants.PORT}/postgres?connect_timeout=5"
    )


@requires_podman
def test_ensure_container_starts_and_accepts_connections():
    ensure_container()

    with psycopg.connect(_admin_dsn()) as con:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


@requires_podman
def test_ensure_container_is_idempotent():
    ensure_container()
    ensure_container()  # must not raise, must not error on "name already in use"

    with psycopg.connect(_admin_dsn()) as con:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


@requires_podman
def test_create_container_falls_back_to_start_when_name_in_use():
    ensure_container()  # container already exists under constants.CONTAINER_NAME

    _create_container()  # "podman run" will fail with "already in use"; must fall back to "podman start"

    with psycopg.connect(_admin_dsn()) as con:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
