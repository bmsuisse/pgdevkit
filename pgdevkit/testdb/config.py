from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_ENGINES = ("postgres", "mssql")


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    database_dir: str = "database"
    env_prefix: str = ""
    extensions: tuple[str, ...] = ()
    extra_db_suffixes: tuple[str, ...] = ()
    engine: str = "postgres"
    root: Path = field(default_factory=Path)

    def __post_init__(self) -> None:
        if not self.env_prefix:
            object.__setattr__(self, "env_prefix", f"{self.name.upper()}_")
        if self.engine not in _ENGINES:
            raise ValueError(f"[tool.pgdevkit].engine must be one of {_ENGINES}, got {self.engine!r}")


def _find_pyproject(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def load_config(start: Path | None = None) -> ProjectConfig:
    start = (start or Path.cwd()).resolve()
    pyproject = _find_pyproject(start)
    root = pyproject.parent if pyproject else start

    section: dict = {}
    if pyproject is not None:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        section = data.get("tool", {}).get("pgdevkit", {})

    extensions = section.get("extensions", [])
    if not isinstance(extensions, list):
        raise TypeError(
            f"[tool.pgdevkit].extensions in {pyproject} must be a list, got {type(extensions).__name__}"
        )

    # Lets a repo that layers an extra, literally-suffixed sibling database
    # on top of its main workspace DB (e.g. a mock-service DB used only by
    # that repo's own test setup) teach `find_orphaned_dbs`/`clean_testdb`
    # about it, without pgdevkit needing to know why that suffix exists.
    extra_db_suffixes = section.get("extra_db_suffixes", [])
    if not isinstance(extra_db_suffixes, list):
        raise TypeError(
            f"[tool.pgdevkit].extra_db_suffixes in {pyproject} must be a list, "
            f"got {type(extra_db_suffixes).__name__}"
        )

    # PGDEVKIT_TESTDB_ENGINE lets CI/ad-hoc runs flip engines without
    # editing pyproject.toml; the toml value is the durable, per-project
    # default (a project's database/ tree is written in one dialect, so
    # this isn't meant to vary per-invocation the way a CLI flag would).
    engine = os.environ.get("PGDEVKIT_TESTDB_ENGINE") or section.get("engine", "postgres")

    return ProjectConfig(
        name=section.get("name") or root.name,
        database_dir=section.get("database_dir", "database"),
        env_prefix=section.get("env_prefix", ""),
        extensions=tuple(extensions),
        extra_db_suffixes=tuple(extra_db_suffixes),
        engine=engine,
        root=root,
    )
