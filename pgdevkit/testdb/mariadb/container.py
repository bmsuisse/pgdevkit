from __future__ import annotations

import os
import time

import docker
import docker.errors
import pymysql

from .. import _docker
from . import constants


def _available(timeout: float = 3.0) -> bool:
    """Quick check (short timeout) for whether MariaDB is already reachable at
    HOST:PORT, so a server started outside pgdevkit's control doesn't trigger
    another Docker API call."""
    try:
        pymysql.connect(**constants.connect_kwargs(connect_timeout=max(1, round(timeout)))).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _create_container(client: docker.DockerClient | _docker.CliClient) -> None:
    try:
        client.containers.run(
            constants.IMAGE,
            name=constants.CONTAINER_NAME,
            detach=True,
            ports={"3306/tcp": constants.PORT},
            environment={"MARIADB_ROOT_PASSWORD": constants.PASSWORD, "TZ": "UTC"},
            **({"user": constants.CONTAINER_USER} if constants.CONTAINER_USER else {}),
            **_docker.resource_kwargs(
                tmpfs=constants.TMPFS,
                shm_size=constants.SHM_SIZE,
                mem_limit=constants.MEM_LIMIT,
                cpus=constants.CPUS,
                memswap_limit=constants.MEMSWAP_LIMIT,
            ),
        )
    except docker.errors.APIError as e:
        if getattr(e, "status_code", None) == 409 or "already in use" in str(e):
            client.containers.get(constants.CONTAINER_NAME).start()
            return
        raise RuntimeError(f"Starting the {constants.CONTAINER_NAME} container failed: {e}") from e


def _wait_ready(timeout: float = 60.0) -> None:
    # The image's first start initialises the data directory with a temporary,
    # socket-only server before the real one listens on TCP, so a successful
    # TCP connection means the final server is up. A cold first start is
    # slower than Postgres's, hence the longer default timeout.
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pymysql.connect(**constants.connect_kwargs(connect_timeout=2)).close()
            return
        except Exception as e:  # noqa: BLE001
            last_error = e
            time.sleep(0.5)
    raise RuntimeError(f"MariaDB did not become ready within {timeout}s: {last_error}")


def ensure_mariadb_container() -> None:
    """Idempotently ensure the shared pgdevkit-mariadb container is running and
    accepting connections. Never touches the Docker API if MariaDB is already
    reachable, or if PGDEVKIT_SKIP_CONTAINER says to assume it is (the same
    on/off switch as for the other engines -- one project uses one engine)."""
    if os.environ.get("PGDEVKIT_SKIP_CONTAINER"):
        return
    if _available():
        return
    client = _docker.client()
    try:
        container = client.containers.get(constants.CONTAINER_NAME)
    except docker.errors.NotFound:
        container = None
    if container is not None:
        # See the Postgres container's ensure_container() -- an existing (even
        # stopped) container is restarted as-is, so config constants only take
        # effect via _create_container().
        if container.status != "running":
            container.start()
    else:
        _create_container(client)
    _wait_ready()
