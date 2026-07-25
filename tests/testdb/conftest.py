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


def _has_mssql_support() -> bool:
    try:
        import mssql_python  # noqa: F401
    except ImportError:
        return False
    return _has_container_runtime()


requires_mssql = pytest.mark.skipif(
    not _has_mssql_support(),
    reason="no Docker-compatible API reachable, or the mssql extra isn't installed",
)

FIXTURES = Path(__file__).parent / "fixtures" / "database"
MSSQL_FIXTURES = Path(__file__).parent / "fixtures" / "database_mssql"

# Appended to every test project's [tool.pgdevkit].name so that two pytest
# processes (e.g. from separate git worktrees) running against the shared
# pgdevkit-postgres container at the same time get distinct database names
# instead of dropping each other's throwaway databases mid-run.
RUN_SUFFIX = f"pid{os.getpid()}"


def _make_project(base: Path, name: str, branch: str, engine: str = "postgres") -> Path:
    project = base / f"{name}-{branch}"
    project.mkdir()
    (project / "database").symlink_to(MSSQL_FIXTURES if engine == "mssql" else FIXTURES)
    engine_line = f'engine = "{engine}"\n' if engine != "postgres" else ""
    (project / "pyproject.toml").write_text(
        f'[tool.pgdevkit]\nname = "{name}_{RUN_SUFFIX}"\nenv_prefix = "{name.upper()}_"\n{engine_line}',
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
def project_factory(tmp_path: Path) -> Callable[..., Path]:
    def _factory(name: str, branch: str, engine: str = "postgres") -> Path:
        return _make_project(tmp_path, name, branch, engine)

    return _factory
