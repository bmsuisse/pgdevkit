from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

import pytest


def _has_container_runtime() -> bool:
    from pgdevkit.testdb.container import _client

    try:
        _client()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_podman = pytest.mark.skipif(
    not _has_container_runtime(), reason="no Docker-compatible API reachable"
)

FIXTURES = Path(__file__).parent / "fixtures" / "database"

# Appended to every test project's [tool.pgdevkit].name so that two pytest
# processes (e.g. from separate git worktrees) running against the shared
# pgdevkit-postgres container at the same time get distinct database names
# instead of dropping each other's throwaway databases mid-run.
RUN_SUFFIX = f"pid{os.getpid()}"


def _make_project(base: Path, name: str, branch: str) -> Path:
    project = base / f"{name}-{branch}"
    project.mkdir()
    (project / "database").symlink_to(FIXTURES)
    (project / "pyproject.toml").write_text(
        f'[tool.pgdevkit]\nname = "{name}_{RUN_SUFFIX}"\nenv_prefix = "{name.upper()}_"\n',
        encoding="utf-8",
    )
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "test"],
        ["git", "checkout", "-q", "-b", branch],
    ):
        subprocess.run(cmd, cwd=project, check=True)
    (project / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project, check=True)
    return project


@pytest.fixture
def project_factory(tmp_path: Path) -> Callable[[str, str], Path]:
    def _factory(name: str, branch: str) -> Path:
        return _make_project(tmp_path, name, branch)

    return _factory
