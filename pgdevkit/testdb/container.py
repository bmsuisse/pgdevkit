from __future__ import annotations

import os
import time

import docker
import docker.errors
import psycopg

from . import constants

# Candidate Docker-API-compatible socket URLs tried after plain
# docker.from_env() (which only looks at DOCKER_HOST / the default Docker
# socket) fails to connect -- covers rootful and rootless Podman, which
# speaks the same API but doesn't always advertise itself via DOCKER_HOST.
_FALLBACK_SOCKET_URLS = [
    f"unix://{os.environ['XDG_RUNTIME_DIR']}/podman/podman.sock" if os.environ.get("XDG_RUNTIME_DIR") else None,
    "unix:///run/podman/podman.sock",
]


def _client() -> docker.DockerClient:
    """A Docker-API client, working against a real Docker daemon or a
    Podman one (Podman exposes the same API over its own socket) -- callers
    never need to know or care which one is actually running."""
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception:  # noqa: BLE001
        pass
    for base_url in _FALLBACK_SOCKET_URLS:
        if base_url is None:
            continue
        try:
            client = docker.DockerClient(base_url=base_url)
            client.ping()
            return client
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError(
        "Could not reach a Docker-compatible API. Set DOCKER_HOST, or make sure "
        "Docker or Podman's API socket is running."
    )


def _available(timeout: float = 3.0) -> bool:
    """Quick check (short timeout) for whether Postgres is already reachable
    at HOST:PORT, so a database started outside pgdevkit's control (or the
    container from a previous run) doesn't trigger another Docker API call."""
    # libpq's connect_timeout is whole seconds; anything below 1 means "wait
    # indefinitely" instead of a short timeout, so it's clamped up to 1.
    connect_timeout = max(1, round(timeout))
    dsn = constants.conninfo("postgres", connect_timeout=connect_timeout)
    try:
        with psycopg.connect(dsn):
            return True
    except Exception:  # noqa: BLE001
        return False


def _create_container(client: docker.DockerClient) -> None:
    try:
        client.containers.run(
            constants.IMAGE,
            name=constants.CONTAINER_NAME,
            detach=True,
            ports={"5432/tcp": constants.PORT},
            environment={
                "POSTGRES_USER": constants.USER,
                "POSTGRES_PASSWORD": constants.PASSWORD,
            },
            command=["postgres", *constants.PG_SPEED_FLAGS],
        )
    except docker.errors.APIError as e:
        if getattr(e, "status_code", None) == 409 or "already in use" in str(e):
            client.containers.get(constants.CONTAINER_NAME).start()
            return
        raise RuntimeError(f"Starting the {constants.CONTAINER_NAME} container failed: {e}") from e


def _wait_ready(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    dsn = constants.conninfo("postgres", connect_timeout=2)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn):
                return
        except Exception as e:  # noqa: BLE001
            last_error = e
            time.sleep(0.5)
    raise RuntimeError(f"Postgres did not become ready within {timeout}s: {last_error}")


def ensure_container() -> None:
    """Idempotently ensure the shared pgdevkit-postgres container is running
    and accepting connections. Never touches the Docker API if Postgres is
    already reachable, or if PGDEVKIT_SKIP_CONTAINER says to assume it is."""
    if os.environ.get("PGDEVKIT_SKIP_CONTAINER"):
        return
    if _available():
        return
    client = _client()
    try:
        container = client.containers.get(constants.CONTAINER_NAME)
    except docker.errors.NotFound:
        container = None
    if container is not None:
        if container.status != "running":
            container.start()
    else:
        _create_container(client)
    _wait_ready()
