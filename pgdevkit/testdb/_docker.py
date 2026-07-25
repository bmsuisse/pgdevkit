from __future__ import annotations

import os

import docker

# Candidate Docker-API-compatible socket URLs tried after plain
# docker.from_env() (which only looks at DOCKER_HOST / the default Docker
# socket) fails to connect -- covers rootful and rootless Podman, which
# speaks the same API but doesn't always advertise itself via DOCKER_HOST.
_FALLBACK_SOCKET_URLS = [
    f"unix://{os.environ['XDG_RUNTIME_DIR']}/podman/podman.sock" if os.environ.get("XDG_RUNTIME_DIR") else None,
    "unix:///run/podman/podman.sock",
]


def client() -> docker.DockerClient:
    """A Docker-API client, working against a real Docker daemon or a
    Podman one (Podman exposes the same API over its own socket) -- callers
    never need to know or care which one is actually running. Shared by
    both the Postgres and MSSQL container modules -- a container is a
    container, regardless of which image runs inside it."""
    try:
        c = docker.from_env()
        c.ping()
        return c
    except Exception:  # noqa: BLE001
        pass
    for base_url in _FALLBACK_SOCKET_URLS:
        if base_url is None:
            continue
        try:
            c = docker.DockerClient(base_url=base_url)
            c.ping()
            return c
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError(
        "Could not reach a Docker-compatible API. Set DOCKER_HOST, or make sure "
        "Docker or Podman's API socket is running."
    )
