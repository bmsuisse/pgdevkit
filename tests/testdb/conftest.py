from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

requires_podman = pytest.mark.skipif(
    shutil.which("podman") is None, reason="podman is not installed"
)

FIXTURES = Path(__file__).parent / "fixtures" / "database"


def _make_project(base: Path, name: str, branch: str) -> Path:
    project = base / f"{name}-{branch}"
    project.mkdir()
    (project / "database").symlink_to(FIXTURES)
    (project / "pyproject.toml").write_text(
        f'[tool.pgdevkit]\nname = "{name}"\n', encoding="utf-8"
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
