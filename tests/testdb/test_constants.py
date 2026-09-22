from __future__ import annotations

import importlib

from pgdevkit.testdb import constants


def test_defaults_when_env_vars_unset(monkeypatch):
    monkeypatch.delenv("PGDEVKIT_TESTDB_HOST", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_PORT", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_USER", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_PASSWORD", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_IMAGE", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_TMPFS", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_SHM_SIZE", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_MEM_LIMIT", raising=False)
    monkeypatch.delenv("PGDEVKIT_TESTDB_CPUS", raising=False)
    try:
        importlib.reload(constants)
        assert constants.HOST == "localhost"
        assert constants.PORT == 54322
        assert constants.USER == "postgres"
        assert constants.PASSWORD == "testpwd"
        assert constants.IMAGE == "pgvector/pgvector:pg18-trixie"
        assert constants.TMPFS == ""
        assert constants.SHM_SIZE == ""
        assert constants.MEM_LIMIT == ""
        assert constants.CPUS == ""
    finally:
        importlib.reload(constants)


def test_env_vars_override_connection_defaults(monkeypatch):
    monkeypatch.setenv("PGDEVKIT_TESTDB_HOST", "otherhost")
    monkeypatch.setenv("PGDEVKIT_TESTDB_PORT", "9999")
    monkeypatch.setenv("PGDEVKIT_TESTDB_USER", "otheruser")
    monkeypatch.setenv("PGDEVKIT_TESTDB_PASSWORD", "otherpass")
    try:
        importlib.reload(constants)
        assert constants.HOST == "otherhost"
        assert constants.PORT == 9999
        assert constants.USER == "otheruser"
        assert constants.PASSWORD == "otherpass"
    finally:
        # monkeypatch only unsets the env vars after this function returns,
        # so undo them now — otherwise this reload just reloads the
        # override values right back in, leaking them into every later test.
        monkeypatch.undo()
        importlib.reload(constants)


def test_env_vars_override_docker_resource_defaults(monkeypatch):
    monkeypatch.setenv("PGDEVKIT_TESTDB_IMAGE", "otherimage:latest")
    monkeypatch.setenv("PGDEVKIT_TESTDB_TMPFS", "/var/lib/postgresql/data:size=512m")
    monkeypatch.setenv("PGDEVKIT_TESTDB_SHM_SIZE", "256m")
    monkeypatch.setenv("PGDEVKIT_TESTDB_MEM_LIMIT", "1g")
    monkeypatch.setenv("PGDEVKIT_TESTDB_CPUS", "2")
    try:
        importlib.reload(constants)
        assert constants.IMAGE == "otherimage:latest"
        assert constants.TMPFS == "/var/lib/postgresql/data:size=512m"
        assert constants.SHM_SIZE == "256m"
        assert constants.MEM_LIMIT == "1g"
        assert constants.CPUS == "2"
    finally:
        monkeypatch.undo()
        importlib.reload(constants)
