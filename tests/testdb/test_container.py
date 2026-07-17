from __future__ import annotations

import psycopg

from pgdevkit.testdb import constants, container
from pgdevkit.testdb.container import _available, _create_container, ensure_container
from tests.testdb.conftest import requires_podman


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_available_returns_true_when_connection_succeeds(monkeypatch):
    monkeypatch.setattr(container.psycopg, "connect", lambda *a, **k: _FakeConnection())
    assert _available(timeout=0.1) is True


def test_available_returns_false_when_connection_fails(monkeypatch):
    def _raise(*a, **k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(container.psycopg, "connect", _raise)
    assert _available(timeout=0.1) is False


def test_ensure_container_skips_podman_when_already_available(monkeypatch):
    monkeypatch.setattr(container, "_available", lambda timeout=1.0: True)

    def _fail(*a, **k):
        raise AssertionError("must not touch the Docker API when already available")

    monkeypatch.setattr(container, "_client", _fail)
    ensure_container()  # must return without calling the Docker API


def test_ensure_container_skips_everything_when_skip_env_set(monkeypatch):
    monkeypatch.setenv("PGDEVKIT_SKIP_CONTAINER", "1")

    def _fail(*a, **k):
        raise AssertionError("must not run when PGDEVKIT_SKIP_CONTAINER is set")

    monkeypatch.setattr(container, "_available", _fail)
    monkeypatch.setattr(container, "_client", _fail)
    ensure_container()  # must return without checking availability or calling the Docker API


def _admin_dsn() -> str:
    return constants.conninfo("postgres", connect_timeout=5)


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

    _create_container(container._client())  # run will conflict; must fall back to start()

    with psycopg.connect(_admin_dsn()) as con:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
