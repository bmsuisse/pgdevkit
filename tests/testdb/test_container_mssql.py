from __future__ import annotations

from pgdevkit.testdb.mssql import constants
from pgdevkit.testdb.mssql.container import _create_container


class _FakeContainers:
    def __init__(self):
        self.run_kwargs: dict | None = None

    def run(self, image, **kwargs):
        self.run_kwargs = kwargs


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()


def test_create_container_omits_resource_kwargs_by_default():
    client = _FakeClient()

    _create_container(client)

    for key in ("tmpfs", "shm_size", "mem_limit", "nano_cpus"):
        assert key not in client.containers.run_kwargs


def test_create_container_passes_through_resource_overrides(monkeypatch):
    monkeypatch.setattr(constants, "TMPFS", "/var/opt/mssql:size=512m")
    monkeypatch.setattr(constants, "SHM_SIZE", "256m")
    monkeypatch.setattr(constants, "MEM_LIMIT", "4g")
    monkeypatch.setattr(constants, "CPUS", "2")
    client = _FakeClient()

    _create_container(client)

    assert client.containers.run_kwargs["tmpfs"] == {"/var/opt/mssql": "size=512m"}
    assert client.containers.run_kwargs["shm_size"] == "256m"
    assert client.containers.run_kwargs["mem_limit"] == "4g"
    assert client.containers.run_kwargs["nano_cpus"] == 2_000_000_000
