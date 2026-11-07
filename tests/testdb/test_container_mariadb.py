from __future__ import annotations

from pgdevkit.testdb.mariadb import constants
from pgdevkit.testdb.mariadb.container import _create_container


class _FakeContainers:
    def __init__(self):
        self.run_kwargs: dict | None = None

    def run(self, image, **kwargs):
        self.run_kwargs = kwargs


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()


def test_create_container_bootstraps_root_password_and_port():
    client = _FakeClient()

    _create_container(client)

    assert client.containers.run_kwargs["ports"] == {"3306/tcp": constants.PORT}
    assert client.containers.run_kwargs["environment"]["MARIADB_ROOT_PASSWORD"] == constants.PASSWORD


def test_create_container_omits_resource_kwargs_and_user_by_default(monkeypatch):
    monkeypatch.setattr(constants, "CONTAINER_USER", "")
    client = _FakeClient()

    _create_container(client)

    for key in ("tmpfs", "shm_size", "mem_limit", "nano_cpus", "user"):
        assert key not in client.containers.run_kwargs


def test_create_container_passes_through_overrides(monkeypatch):
    monkeypatch.setattr(constants, "TMPFS", "/var/lib/mysql:size=512m")
    monkeypatch.setattr(constants, "MEM_LIMIT", "2g")
    monkeypatch.setattr(constants, "CONTAINER_USER", "0")
    client = _FakeClient()

    _create_container(client)

    assert client.containers.run_kwargs["tmpfs"] == {"/var/lib/mysql": "size=512m"}
    assert client.containers.run_kwargs["mem_limit"] == "2g"
    assert client.containers.run_kwargs["user"] == "0"


def test_connect_kwargs_select_a_database_only_when_given(monkeypatch):
    assert "database" not in constants.connect_kwargs()
    assert constants.connect_kwargs("proj_main")["database"] == "proj_main"


def test_dsn_percent_encodes_credentials(monkeypatch):
    monkeypatch.setattr(constants, "PASSWORD", "p@ss/w:rd")
    assert constants.dsn("db1").startswith("mysql://root:p%40ss%2Fw%3Ard@")
