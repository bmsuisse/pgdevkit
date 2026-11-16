from __future__ import annotations

from typing import Any, Never, cast

import docker
import psycopg
import pytest

from pgdevkit.testdb import constants, container
from pgdevkit.testdb.container import _available, _create_container, ensure_container
from tests.testdb.conftest import requires_podman


class _FakeConnection:
    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_available_returns_true_when_connection_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container.psycopg, "connect", lambda *a, **k: _FakeConnection())
    assert _available(timeout=0.1) is True


def test_available_returns_false_when_connection_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> Never:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(container.psycopg, "connect", _raise)
    assert _available(timeout=0.1) is False


def test_ensure_container_skips_podman_when_already_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "_available", lambda timeout=1.0: True)

    def _fail(*a: object, **k: object) -> Never:
        raise AssertionError("must not touch the Docker API when already available")

    monkeypatch.setattr(container, "_client", _fail)
    ensure_container()  # must return without calling the Docker API


def test_ensure_container_skips_everything_when_skip_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGDEVKIT_SKIP_CONTAINER", "1")

    def _fail(*a: object, **k: object) -> Never:
        raise AssertionError("must not run when PGDEVKIT_SKIP_CONTAINER is set")

    monkeypatch.setattr(container, "_available", _fail)
    monkeypatch.setattr(container, "_client", _fail)
    ensure_container()  # must return without checking availability or calling the Docker API


class _FakeContainers:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] = {}

    def run(self, image: str, **kwargs: Any) -> None:
        self.run_kwargs = kwargs


class _FakeClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


def test_create_container_forces_utc_timezone() -> None:
    """Test Postgres must run with timezone=UTC regardless of the container
    image's/host's own locale default -- production connects with session
    timezone=UTC, so any ::date cast of a timestamptz column would otherwise
    silently disagree between test and prod for rows near local midnight."""
    client = _FakeClient()

    _create_container(cast("docker.DockerClient", client))

    assert client.containers.run_kwargs is not None
    assert "-c" in client.containers.run_kwargs["command"]
    timezone_flag_index = client.containers.run_kwargs["command"].index("timezone=UTC")
    assert client.containers.run_kwargs["command"][timezone_flag_index - 1] == "-c"
    assert client.containers.run_kwargs["environment"]["TZ"] == "UTC"
    assert client.containers.run_kwargs["environment"]["PGTZ"] == "UTC"


def test_create_container_omits_resource_kwargs_by_default() -> None:
    client = _FakeClient()

    _create_container(cast("docker.DockerClient", client))

    for key in ("tmpfs", "shm_size", "mem_limit", "nano_cpus"):
        assert key not in client.containers.run_kwargs


def test_create_container_passes_through_resource_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "TMPFS", "/var/lib/postgresql/data:size=512m")
    monkeypatch.setattr(constants, "SHM_SIZE", "256m")
    monkeypatch.setattr(constants, "MEM_LIMIT", "1g")
    monkeypatch.setattr(constants, "CPUS", "2")
    client = _FakeClient()

    _create_container(cast("docker.DockerClient", client))

    assert client.containers.run_kwargs["tmpfs"] == {"/var/lib/postgresql/data": "size=512m"}
    assert client.containers.run_kwargs["shm_size"] == "256m"
    assert client.containers.run_kwargs["mem_limit"] == "1g"
    assert client.containers.run_kwargs["memswap_limit"] == "1g"
    assert client.containers.run_kwargs["nano_cpus"] == 2_000_000_000


def _admin_dsn() -> str:
    return constants.conninfo("postgres", connect_timeout=5)


@requires_podman
def test_ensure_container_starts_and_accepts_connections() -> None:
    ensure_container()

    with psycopg.connect(_admin_dsn()) as con, con.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


@requires_podman
def test_ensure_container_is_idempotent() -> None:
    ensure_container()
    ensure_container()  # must not raise, must not error on "name already in use"

    with psycopg.connect(_admin_dsn()) as con, con.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


@requires_podman
def test_create_container_falls_back_to_start_when_name_in_use() -> None:
    ensure_container()  # container already exists under constants.CONTAINER_NAME

    _create_container(container._client())  # run will conflict; must fall back to start()

    with psycopg.connect(_admin_dsn()) as con, con.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


@requires_podman
def test_ensure_container_runs_with_utc_timezone() -> None:
    ensure_container()

    with psycopg.connect(_admin_dsn()) as con, con.cursor() as cur:
        cur.execute("SHOW timezone")
        assert cur.fetchone() == ("UTC",)
