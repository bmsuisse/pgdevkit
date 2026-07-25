from __future__ import annotations

import os
import time

import docker
import docker.errors
import mssql_python

from .. import _docker
from . import constants


def _available(timeout: float = 3.0) -> bool:
    """Quick check (short timeout) for whether SQL Server is already
    reachable at HOST:PORT, so a container started outside pgdevkit's
    control doesn't trigger another Docker API call."""
    try:
        conn = mssql_python.connect(constants.conninfo("master"), timeout=max(1, round(timeout)))
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _create_container(client: docker.DockerClient) -> None:
    constants.validate_sa_password(constants.PASSWORD)
    try:
        client.containers.run(
            constants.IMAGE,
            name=constants.CONTAINER_NAME,
            detach=True,
            ports={"1433/tcp": constants.PORT},
            environment={
                "ACCEPT_EULA": "Y",
                "MSSQL_SA_PASSWORD": constants.PASSWORD,
                # Unset defaults to the Evaluation edition, which stops
                # working after 180 days -- a real footgun for a long-lived
                # shared dev container.
                "MSSQL_PID": "Developer",
                "MSSQL_MEMORY_LIMIT_MB": str(constants.MEMORY_LIMIT_MB),
            },
        )
    except docker.errors.APIError as e:
        if getattr(e, "status_code", None) == 409 or "already in use" in str(e):
            client.containers.get(constants.CONTAINER_NAME).start()
            return
        raise RuntimeError(f"Starting the {constants.CONTAINER_NAME} container failed: {e}") from e


def _wait_ready(timeout: float = 90.0) -> None:
    # SQL Server accepts TCP connections before its internal init finishes,
    # surfacing as transient "Login failed"/"server is not currently
    # accepting connections" errors -- treated as "not ready yet," same as
    # the Postgres container's broad `except Exception`. Cold start is
    # slower than Postgres's, hence the longer default timeout.
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = mssql_python.connect(constants.conninfo("master"), timeout=2)
            conn.close()
            return
        except Exception as e:  # noqa: BLE001
            last_error = e
            time.sleep(0.5)
    raise RuntimeError(f"SQL Server did not become ready within {timeout}s: {last_error}")


def ensure_mssql_container() -> None:
    """Idempotently ensure the shared pgdevkit-mssql container is running
    and accepting connections. Never touches the Docker API if SQL Server
    is already reachable, or if PGDEVKIT_SKIP_CONTAINER says to assume it
    is (the same on/off switch used for the Postgres container -- one
    project uses one engine, so there's no need for a second env var)."""
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
        if container.status != "running":
            container.start()
    else:
        _create_container(client)
    _wait_ready()
