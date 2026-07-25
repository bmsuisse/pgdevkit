from __future__ import annotations

from pathlib import Path

import pytest

from pgdevkit.testdb.config import load_config


def test_engine_defaults_to_postgres_when_absent(tmp_path: Path):
    config = load_config(tmp_path)
    assert config.engine == "postgres"


def test_engine_read_from_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pgdevkit]\nname = "x"\nengine = "mssql"\n', encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config.engine == "mssql"


def test_engine_rejects_unknown_value(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pgdevkit]\nname = "x"\nengine = "oracle"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="engine"):
        load_config(tmp_path)


def test_env_var_overrides_pyproject_engine(tmp_path: Path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pgdevkit]\nname = "x"\nengine = "postgres"\n', encoding="utf-8"
    )
    monkeypatch.setenv("PGDEVKIT_TESTDB_ENGINE", "mssql")
    config = load_config(tmp_path)
    assert config.engine == "mssql"


def test_env_var_used_when_no_pyproject_value(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PGDEVKIT_TESTDB_ENGINE", "mssql")
    config = load_config(tmp_path)
    assert config.engine == "mssql"
