from __future__ import annotations

from pathlib import Path

from pgdevkit.testdb.config import load_config


def test_defaults_when_no_pyproject(tmp_path: Path):
    project = tmp_path / "myproj"
    project.mkdir()
    config = load_config(project)
    assert config.name == "myproj"
    assert config.database_dir == "database"
    assert config.env_prefix == "MYPROJ_"
    assert config.extensions == ()
    assert config.root == project


def test_reads_tool_pgdevkit_section(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pgdevkit]
name = "mdmapp"
database_dir = "db"
env_prefix = "MDM_"
extensions = ["vector"]
""",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.name == "mdmapp"
    assert config.database_dir == "db"
    assert config.env_prefix == "MDM_"
    assert config.extensions == ("vector",)


def test_env_prefix_defaults_from_name(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pgdevkit]\nname = "ccmt"\n', encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config.env_prefix == "CCMT_"


def test_searches_upward_for_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pgdevkit]\nname = "root_project"\n', encoding="utf-8"
    )
    subdir = tmp_path / "src" / "nested"
    subdir.mkdir(parents=True)
    config = load_config(subdir)
    assert config.name == "root_project"
    assert config.root == tmp_path
