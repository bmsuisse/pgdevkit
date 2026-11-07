from __future__ import annotations

from pathlib import Path

import pytest

from pgdevkit.testdb.api import ensure_testdb, restore_testdb, shell_argv, snapshot_testdb, status
from pgdevkit.testdb.config import load_config
from tests.testdb.test_api_mssql import _make_mssql_project


def _make_mariadb_project(base: Path, name: str) -> Path:
    project = _make_mssql_project(base, name)
    (project / "pyproject.toml").write_text(
        f'[tool.pgdevkit]\nname = "{name}"\nengine = "mariadb"\n', encoding="utf-8"
    )
    return project


def test_engine_mariadb_is_accepted_from_pyproject(tmp_path: Path):
    project = _make_mariadb_project(tmp_path, "mdbproj0")
    assert load_config(project).engine == "mariadb"


def test_status_dispatches_to_mariadb_and_reports_engine(tmp_path: Path):
    project = _make_mariadb_project(tmp_path, "mdbproj")
    info = status(project)
    assert info["engine"] == "mariadb"
    assert info["container"] == "pgdevkit-mariadb"
    assert info["dsn"].startswith("mysql://")


def test_shell_argv_uses_mariadb_client_over_tcp(tmp_path: Path):
    project = _make_mariadb_project(tmp_path, "mdbproj2")
    binary, argv = shell_argv(project)
    assert binary == "mariadb"
    assert "--protocol=tcp" in argv
    assert argv[-1] == status(project)["database"]


def test_unsupported_options_fail_before_touching_a_container(tmp_path: Path):
    project = _make_mariadb_project(tmp_path, "mdbproj3")

    with pytest.raises(ValueError, match="schemas"):
        ensure_testdb(project, schemas=frozenset({"app"}))
    with pytest.raises(ValueError, match="mssql"):
        ensure_testdb(project, apply=lambda conn: None)
    with pytest.raises(ValueError, match="mssql"):
        ensure_testdb(project, apply_scripts=False)
    with pytest.raises(ValueError, match="mssql"):
        snapshot_testdb(project)
    with pytest.raises(ValueError, match="mssql"):
        restore_testdb(project)
