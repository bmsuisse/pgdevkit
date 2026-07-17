from __future__ import annotations

import os
import subprocess
import time

import psycopg

from . import constants


def _podman(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=check)


def _available(timeout: float = 3.0) -> bool:
    """Quick check (short timeout) for whether Postgres is already reachable
    at HOST:PORT, so a database started outside pgdevkit's control (or the
    container from a previous run) doesn't trigger another podman/docker
    lifecycle call."""
    # libpq's connect_timeout is whole seconds; anything below 1 means "wait
    # indefinitely" instead of a short timeout, so it's clamped up to 1.
    connect_timeout = max(1, round(timeout))
    dsn = constants.conninfo("postgres", connect_timeout=connect_timeout)
    try:
        with psycopg.connect(dsn):
            return True
    except Exception:  # noqa: BLE001
        return False


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
    and accepting connections. Never touches podman/docker if Postgres is
    already reachable, or if PGDEVKIT_SKIP_CONTAINER says to assume it is."""
    if os.environ.get("PGDEVKIT_SKIP_CONTAINER"):
        return
    if _available():
        return
    status = _container_status()
    if status == "running":
        return
    if status is not None:
        _podman("start", constants.CONTAINER_NAME)
    else:
        _create_container()
    _wait_ready()
