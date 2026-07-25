from __future__ import annotations

import subprocess
from pathlib import Path

from pgdevkit.testdb.api import shell_argv, status


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
