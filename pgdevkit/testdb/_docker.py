from __future__ import annotations

import os
import shutil
import subprocess

import docker
import docker.errors

# Candidate Docker-API-compatible socket URLs tried after plain
# docker.from_env() (which only looks at DOCKER_HOST / the default Docker
# socket) fails to connect -- covers rootful and rootless Podman, which
# speaks the same API but doesn't always advertise itself via DOCKER_HOST.
_FALLBACK_SOCKET_URLS = [
    f"unix://{os.environ['XDG_RUNTIME_DIR']}/podman/podman.sock" if os.environ.get("XDG_RUNTIME_DIR") else None,
    "unix:///run/podman/podman.sock",
]


# Container engines whose CLI can stand in for the Docker SDK, in the order
# tried by auto-detection. Podman first: it is the one that routinely has no
# API socket (rootless without a systemd user session), while a `docker` CLI
# needs a reachable daemon just like the SDK does.
_CLI_ENGINES = ("podman", "docker")


class CliContainer:
    """The slice of docker-py's `Container` that the container modules use
    (`status`, `start()`), backed by the engine's CLI."""

    def __init__(self, engine: str, name: str, status: str) -> None:
        self._engine = engine
        self.name = name
        self.status = status

    def start(self) -> None:
        _run_cli(self._engine, ["start", self.name])


class CliContainers:
    """The slice of docker-py's `ContainerCollection` that the container
    modules use (`get`, `run`), implemented by shelling out to
    `podman`/`docker`, for environments with no reachable API socket."""

    def __init__(self, engine: str) -> None:
        self._engine = engine

    def get(self, name: str) -> CliContainer:
        result = _run_cli(self._engine, ["inspect", "--format", "{{.State.Status}}", name], check=False)
        if result.returncode != 0:
            if "no such" in result.stderr.lower():
                raise docker.errors.NotFound(result.stderr.strip())
            raise RuntimeError(f"`{self._engine} inspect {name}` failed: {result.stderr.strip()}")
        return CliContainer(self._engine, name, result.stdout.strip())

    def run(
        self,
        image: str,
        *,
        name: str,
        ports: dict[str, int],
        environment: dict[str, str],
        detach: bool = True,  # accepted for docker-py compatibility; the CLI always runs detached
        command: list[str] | None = None,
        user: str | None = None,
        tmpfs: dict[str, str] | None = None,
        shm_size: str | None = None,
        mem_limit: str | None = None,
        memswap_limit: str | None = None,
        nano_cpus: int | None = None,
    ) -> CliContainer:
        # Values are passed as bare `-e KEY` with the real value in the child's
        # environment, so e.g. the SA password never shows up in `ps` output.
        args = ["run", "--detach", "--name", name]
        for container_port, host_port in ports.items():
            args += ["--publish", f"{host_port}:{container_port}"]
        for key in environment:
            args += ["--env", key]
        if user:
            args += ["--user", user]
        for path, options in (tmpfs or {}).items():
            args += ["--tmpfs", f"{path}:{options}" if options else path]
        if shm_size:
            args += ["--shm-size", shm_size]
        if mem_limit:
            args += ["--memory", mem_limit]
        if memswap_limit:
            args += ["--memory-swap", memswap_limit]
        if nano_cpus:
            args += ["--cpus", str(nano_cpus / 1_000_000_000)]
        args.append(image)
        args += command or []
        result = _run_cli(self._engine, args, env=environment, check=False)
        if result.returncode != 0:
            # Same exception type docker-py raises, so the callers' "name already
            # in use -> start the existing container" handling applies unchanged.
            raise docker.errors.APIError(result.stderr.strip() or f"`{self._engine} run` failed")
        return CliContainer(self._engine, name, "running")


class CliClient:
    """Stands in for `docker.DockerClient` when only a container CLI is available."""

    def __init__(self, engine: str) -> None:
        self.engine = engine
        self.containers = CliContainers(engine)


def _run_cli(
    engine: str, args: list[str], *, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [engine, *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"`{engine} {args[0]}` failed: {result.stderr.strip()}")
    return result


def _sdk_client() -> docker.DockerClient | None:
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
    return None


def client() -> docker.DockerClient | CliClient:
    """A container client, working against a real Docker daemon or a Podman
    one (Podman exposes the same API over its own socket) -- callers never
    need to know or care which one is actually running. Shared by both the
    Postgres and MSSQL container modules -- a container is a container,
    regardless of which image runs inside it.

    PGDEVKIT_CONTAINER_ENGINE=docker|podman skips the SDK and drives that
    engine's CLI directly. Unset (or "auto"), the SDK is tried first and the
    first of `podman`/`docker` on PATH is the fallback when no API socket is
    reachable -- e.g. rootless Podman without a systemd user session."""
    engine = os.environ.get("PGDEVKIT_CONTAINER_ENGINE", "").strip().lower()
    if engine in ("docker", "podman"):
        if shutil.which(engine) is None:
            raise RuntimeError(f"PGDEVKIT_CONTAINER_ENGINE={engine}, but `{engine}` is not on PATH.")
        return CliClient(engine)
    if engine not in ("", "auto"):
        raise ValueError(f"PGDEVKIT_CONTAINER_ENGINE must be docker, podman or auto, not {engine!r}")
    sdk = _sdk_client()
    if sdk is not None:
        return sdk
    for cli_engine in _CLI_ENGINES:
        if shutil.which(cli_engine):
            return CliClient(cli_engine)
    raise RuntimeError(
        "Could not reach a Docker-compatible API and found no `podman`/`docker` CLI. Set "
        "DOCKER_HOST, make sure Docker or Podman's API socket is running, or install a container CLI."
    )


def _parse_tmpfs(spec: str) -> dict[str, str]:
    """Parse a docker-CLI-style --tmpfs spec into the dict shape docker-py's
    `containers.run(tmpfs=...)` expects. Multiple mounts are semicolon-separated
    (commas are already used by docker's own comma-separated mount options),
    e.g. "/path:rw,size=64m;/other/path:ro,size=32m", matching `docker run
    --tmpfs /path:rw,size=64m --tmpfs /other/path:ro,size=32m`."""
    mounts: dict[str, str] = {}
    for entry in spec.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        path, _, options = entry.partition(":")
        mounts[path] = options
    return mounts


def resource_kwargs(*, tmpfs: str, shm_size: str, mem_limit: str, cpus: str, memswap_limit: str = "") -> dict:
    """Build the `containers.run()` kwargs for the tmpfs/shm_size/mem_limit/cpus
    overrides, omitting any that aren't set so Docker's own defaults apply.
    A mem_limit without an explicit memswap_limit also caps memory+swap at the
    same value (the `--memory-swap=<same>` idiom), so the container can't
    spill past its limit into swap; pass memswap_limit="-1" to allow unlimited swap.
    A memswap_limit without a mem_limit is ignored (Docker/Podman need both).
    Shared by the Postgres and MSSQL container modules -- these are Docker-level
    resource knobs, not anything specific to either engine."""
    kwargs: dict = {}
    if tmpfs:
        kwargs["tmpfs"] = _parse_tmpfs(tmpfs)
    if shm_size:
        kwargs["shm_size"] = shm_size
    if mem_limit:
        kwargs["mem_limit"] = mem_limit
        kwargs["memswap_limit"] = memswap_limit or mem_limit
    if cpus:
        kwargs["nano_cpus"] = int(float(cpus) * 1_000_000_000)
    return kwargs
