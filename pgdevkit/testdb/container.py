from __future__ import annotations

import subprocess
import time

import psycopg

from . import constants


def _podman(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=check)


def _container_status() -> str | None:
    """Return 'running', 'exited', etc., or None if the container doesn't exist."""
    result = _podman(
        "inspect", constants.CONTAINER_NAME, "--format", "{{.State.Status}}", check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _create_container() -> None:
    result = _podman(
        "run", "-d",
        "--name", constants.CONTAINER_NAME,
        "-p", f"{constants.PORT}:5432",
        "-e", f"POSTGRES_USER={constants.USER}",
        "-e", f"POSTGRES_PASSWORD={constants.PASSWORD}",
        constants.IMAGE,
        "postgres", *constants.PG_SPEED_FLAGS,
        check=False,
    )
    if result.returncode != 0 and "already in use" in result.stderr:
        _podman("start", constants.CONTAINER_NAME)
        return
    if result.returncode != 0:
        raise RuntimeError(f"podman run failed: {result.stderr}")


def _wait_ready(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    dsn = (
        f"postgresql://{constants.USER}:{constants.PASSWORD}"
        f"@{constants.HOST}:{constants.PORT}/postgres?connect_timeout=2"
    )
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
    and accepting connections."""
    status = _container_status()
    if status == "running":
        return
    if status is not None:
        _podman("start", constants.CONTAINER_NAME)
    else:
        _create_container()
    _wait_ready()
