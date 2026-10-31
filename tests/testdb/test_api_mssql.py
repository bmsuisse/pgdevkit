from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pgdevkit.testdb.api import ensure_testdb, restore_testdb, shell_argv, snapshot_testdb, status
from pgdevkit.testdb.mssql.api import _snapshot_db_name


def _make_mssql_project(base: Path, name: str) -> Path:
    project = base / name
    project.mkdir()
    (project / "pyproject.toml").write_text(
        f'[tool.pgdevkit]\nname = "{name}"\nengine = "mssql"\n', encoding="utf-8"
    )
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        subprocess.run(cmd, cwd=project, check=True)
    (project / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project, check=True)
    return project


def test_status_dispatches_to_mssql_and_reports_engine(tmp_path: Path):
    project = _make_mssql_project(tmp_path, "mssqlproj")
    info = status(project)
    assert info["engine"] == "mssql"
    assert "container" in info and "dsn" in info


def test_shell_argv_uses_sqlcmd_for_mssql(tmp_path: Path):
    project = _make_mssql_project(tmp_path, "mssqlproj2")
    binary, argv = shell_argv(project)
    assert binary == "sqlcmd"
    assert argv[0] == "sqlcmd"
    assert "-S" in argv and "-d" in argv


def test_shell_argv_uses_psql_for_postgres(tmp_path: Path):
    project = tmp_path / "pgproj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[tool.pgdevkit]\nname = "pgproj"\n', encoding="utf-8")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
    ):
        subprocess.run(cmd, cwd=project, check=True)
    (project / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project, check=True)

    binary, argv = shell_argv(project)
    assert binary == "psql"
    assert argv[0] == "psql"


def test_snapshot_db_name_is_derived_from_the_source_db():
    assert _snapshot_db_name("proj_main", "template") == "proj_main__ss_template"


@pytest.mark.parametrize("name", ["", "a b", "x]; DROP DATABASE master;--", "é"])
def test_snapshot_name_rejects_anything_but_word_characters(name: str):
    with pytest.raises(ValueError, match="Snapshot name"):
        _snapshot_db_name("proj_main", name)


def test_snapshot_and_hook_are_mssql_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PGDEVKIT_TESTDB_BRANCH", "main")
    project = tmp_path / "pgproj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[tool.pgdevkit]\nname = "pgproj"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="mssql"):
        snapshot_testdb(project)
    with pytest.raises(ValueError, match="mssql"):
        restore_testdb(project)
    with pytest.raises(ValueError, match="mssql"):
        ensure_testdb(project, apply=lambda conn: None)
    with pytest.raises(ValueError, match="mssql"):
        ensure_testdb(project, apply_scripts=False)
