from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import pgdevkit.connection as connection
from pgdevkit.cli import app

runner = CliRunner()


def test_compare_lakebase_host_without_databricks_flags_fails_fast(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    result = runner.invoke(
        app,
        [
            "compare",
            "--url",
            "postgresql://user@instance-abc.database.azuredatabricks.net:5432/db",
            "--entra-user",
            "alice@example.com",
            str(tmp_path / "empty"),
        ],
    )
    assert result.exit_code == 2
    assert "--databricks-workspace-host" in result.output


def test_compare_lakebase_host_with_flags_reaches_introspection(tmp_path: Path, monkeypatch):
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(connection, "get_azure_postgres_password", lambda: "unused")

    calls = []

    def fake_get_lakebase_password(workspace_host, instance_name):
        calls.append((workspace_host, instance_name))
        return "LAKEBASE_TOKEN"

    monkeypatch.setattr("pgdevkit.lakebase.get_lakebase_password", fake_get_lakebase_password)

    def fake_introspect_db(conninfo):
        assert "LAKEBASE_TOKEN" in conninfo
        raise RuntimeError("stop after conninfo built — introspection itself isn't under test here")

    monkeypatch.setattr("pgdevkit.cli.introspect_db", fake_introspect_db)

    result = runner.invoke(
        app,
        [
            "compare",
            "--url",
            "postgresql://user@instance-abc.database.azuredatabricks.net:5432/db",
            "--entra-user",
            "alice@example.com",
            "--databricks-workspace-host",
            "https://adb-123.azuredatabricks.net",
            "--databricks-instance",
            "myinstance",
            str(tmp_path / "empty"),
        ],
    )
    assert calls == [("https://adb-123.azuredatabricks.net", "myinstance")]
    assert result.exit_code == 1  # CliRunner captures the exception as a non-zero exit
