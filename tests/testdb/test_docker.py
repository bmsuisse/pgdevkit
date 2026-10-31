from __future__ import annotations

import docker.errors
import pytest

from pgdevkit.testdb import _docker
from pgdevkit.testdb._docker import _parse_tmpfs, resource_kwargs


def test_parse_tmpfs_single_mount():
    assert _parse_tmpfs("/var/lib/postgresql/data:rw,size=512m") == {
        "/var/lib/postgresql/data": "rw,size=512m"
    }


def test_parse_tmpfs_multiple_mounts_semicolon_separated():
    assert _parse_tmpfs("/data:size=512m;/tmp:rw,size=64m") == {
        "/data": "size=512m",
        "/tmp": "rw,size=64m",
    }


def test_parse_tmpfs_path_without_options():
    assert _parse_tmpfs("/tmp") == {"/tmp": ""}


def test_parse_tmpfs_ignores_blank_entries():
    assert _parse_tmpfs("/data:size=512m;;/tmp:size=64m;") == {
        "/data": "size=512m",
        "/tmp": "size=64m",
    }


def test_resource_kwargs_all_unset_returns_empty_dict():
    assert resource_kwargs(tmpfs="", shm_size="", mem_limit="", cpus="") == {}


def test_resource_kwargs_only_includes_set_values():
    kwargs = resource_kwargs(tmpfs="/data:size=512m", shm_size="", mem_limit="1g", cpus="")
    assert kwargs == {"tmpfs": {"/data": "size=512m"}, "mem_limit": "1g", "memswap_limit": "1g"}


def test_resource_kwargs_memswap_limit_defaults_to_mem_limit():
    assert resource_kwargs(tmpfs="", shm_size="", mem_limit="512m", cpus="") == {
        "mem_limit": "512m",
        "memswap_limit": "512m",
    }


def test_resource_kwargs_explicit_memswap_limit_wins():
    kwargs = resource_kwargs(tmpfs="", shm_size="", mem_limit="512m", cpus="", memswap_limit="1g")
    assert kwargs == {"mem_limit": "512m", "memswap_limit": "1g"}


def test_resource_kwargs_memswap_limit_without_mem_limit_is_ignored():
    assert resource_kwargs(tmpfs="", shm_size="", mem_limit="", cpus="", memswap_limit="1g") == {}


def test_resource_kwargs_cpus_converted_to_nano_cpus():
    assert resource_kwargs(tmpfs="", shm_size="", mem_limit="", cpus="1.5") == {
        "nano_cpus": 1_500_000_000
    }


def test_resource_kwargs_shm_size_passed_through():
    assert resource_kwargs(tmpfs="", shm_size="256m", mem_limit="", cpus="") == {"shm_size": "256m"}


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _record_cli(monkeypatch, result: _Completed) -> list[tuple[list[str], dict | None]]:
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("env")))
        return result

    monkeypatch.setattr(_docker.subprocess, "run", fake_run)
    return calls


def test_cli_run_translates_kwargs_and_keeps_secrets_out_of_argv(monkeypatch):
    calls = _record_cli(monkeypatch, _Completed())

    _docker.CliContainers("podman").run(
        "img:1",
        name="c1",
        detach=True,
        ports={"1433/tcp": 14330},
        environment={"MSSQL_SA_PASSWORD": "s3cret!Pw"},
        user="0",
        **_docker.resource_kwargs(tmpfs="/data:size=64m", shm_size="1g", mem_limit="2g", cpus="1.5"),
    )

    argv, env = calls[0]
    assert argv == [
        "podman", "run", "--detach", "--name", "c1",
        "--publish", "14330:1433/tcp",
        "--env", "MSSQL_SA_PASSWORD",
        "--user", "0",
        "--tmpfs", "/data:size=64m",
        "--shm-size", "1g",
        "--memory", "2g",
        "--memory-swap", "2g",
        "--cpus", "1.5",
        "img:1",
    ]  # fmt: skip
    assert "s3cret!Pw" not in " ".join(argv)
    assert env is not None and env["MSSQL_SA_PASSWORD"] == "s3cret!Pw"


def test_cli_run_appends_command_after_image(monkeypatch):
    calls = _record_cli(monkeypatch, _Completed())

    _docker.CliContainers("docker").run(
        "pg:17", name="c", ports={"5432/tcp": 54322}, environment={}, command=["postgres", "-c", "fsync=off"]
    )

    assert calls[0][0][-4:] == ["pg:17", "postgres", "-c", "fsync=off"]


def test_cli_run_failure_raises_api_error_so_name_conflicts_are_handled(monkeypatch):
    _record_cli(monkeypatch, _Completed(125, stderr='the container name "c" is already in use'))

    with pytest.raises(docker.errors.APIError, match="already in use"):
        _docker.CliContainers("podman").run("img", name="c", ports={}, environment={})


def test_cli_get_reports_status(monkeypatch):
    calls = _record_cli(monkeypatch, _Completed(stdout="exited\n"))

    container = _docker.CliContainers("podman").get("c1")

    assert container.status == "exited"
    assert calls[0][0] == ["podman", "inspect", "--format", "{{.State.Status}}", "c1"]


@pytest.mark.parametrize("message", ["Error: no such object: c1", "Error: No such object: c1"])
def test_cli_get_missing_container_raises_not_found(monkeypatch, message):
    _record_cli(monkeypatch, _Completed(1, stderr=message))

    with pytest.raises(docker.errors.NotFound):
        _docker.CliContainers("podman").get("c1")


def test_cli_get_other_failure_is_not_mistaken_for_missing(monkeypatch):
    _record_cli(monkeypatch, _Completed(125, stderr="cannot connect to the engine"))

    with pytest.raises(RuntimeError, match="cannot connect"):
        _docker.CliContainers("podman").get("c1")


def test_cli_container_start(monkeypatch):
    calls = _record_cli(monkeypatch, _Completed())

    _docker.CliContainer("podman", "c1", "exited").start()

    assert calls[0][0] == ["podman", "start", "c1"]


def test_client_forced_engine_skips_sdk(monkeypatch):
    monkeypatch.setenv("PGDEVKIT_CONTAINER_ENGINE", "podman")
    monkeypatch.setattr(_docker.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(_docker, "_sdk_client", lambda: pytest.fail("SDK must not be tried"))

    client = _docker.client()

    assert isinstance(client, _docker.CliClient) and client.engine == "podman"


def test_client_forced_engine_missing_cli_is_an_error(monkeypatch):
    monkeypatch.setenv("PGDEVKIT_CONTAINER_ENGINE", "docker")
    monkeypatch.setattr(_docker.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="not on PATH"):
        _docker.client()


def test_client_unknown_engine_is_rejected(monkeypatch):
    monkeypatch.setenv("PGDEVKIT_CONTAINER_ENGINE", "nerdctl")

    with pytest.raises(ValueError, match="docker, podman or auto"):
        _docker.client()


def test_client_prefers_sdk_when_reachable(monkeypatch):
    monkeypatch.delenv("PGDEVKIT_CONTAINER_ENGINE", raising=False)
    sentinel = object()
    monkeypatch.setattr(_docker, "_sdk_client", lambda: sentinel)

    assert _docker.client() is sentinel


def test_client_falls_back_to_first_cli_on_path(monkeypatch):
    monkeypatch.delenv("PGDEVKIT_CONTAINER_ENGINE", raising=False)
    monkeypatch.setattr(_docker, "_sdk_client", lambda: None)
    monkeypatch.setattr(_docker.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    client = _docker.client()

    assert isinstance(client, _docker.CliClient) and client.engine == "docker"


def test_client_without_sdk_or_cli_is_an_error(monkeypatch):
    monkeypatch.delenv("PGDEVKIT_CONTAINER_ENGINE", raising=False)
    monkeypatch.setattr(_docker, "_sdk_client", lambda: None)
    monkeypatch.setattr(_docker.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="no `podman`/`docker` CLI"):
        _docker.client()
