from __future__ import annotations

import pytest

from pgdevkit.connection import build_conninfo, detect_provider, is_azure_postgres_host


@pytest.mark.parametrize(
    "host,expected",
    [
        ("myserver.postgres.database.azure.com", "azure_postgres"),
        ("myserver.postgres.cosmos.azure.com", "azure_postgres"),
        ("instance-abc123.database.azuredatabricks.net", "databricks_lakebase"),
        ("instance-abc123.database.cloud.databricks.com", "databricks_lakebase"),
        ("localhost", "azure_postgres"),
        ("db.internal.example.com", "azure_postgres"),
    ],
)
def test_detect_provider(host, expected):
    assert detect_provider(host) == expected


@pytest.mark.parametrize(
    "host,expected",
    [
        ("myserver.postgres.database.azure.com", True),
        ("myserver.postgres.cosmos.azure.com", True),
        ("localhost", False),
        ("db.internal.example.com", False),
        ("instance-abc123.database.azuredatabricks.net", False),
    ],
)
def test_is_azure_postgres_host(host, expected):
    assert is_azure_postgres_host(host) is expected


def test_build_conninfo_without_entra_user_returns_url_unchanged():
    url = "postgresql://user:pass@host:5432/db"
    assert build_conninfo(url) == url


def test_build_conninfo_azure_postgres_uses_token_as_password(monkeypatch):
    monkeypatch.setattr("pgdevkit.connection.get_azure_postgres_password", lambda **kwargs: "TOKEN123")
    conninfo = build_conninfo(
        "postgresql://myserver.postgres.database.azure.com:5432/db", "alice@example.com"
    )
    assert conninfo == "postgresql://alice%40example.com:TOKEN123@myserver.postgres.database.azure.com:5432/db"


def test_build_conninfo_lakebase_uses_credential_exchange(monkeypatch):
    monkeypatch.setattr(
        "pgdevkit.lakebase.get_lakebase_password",
        lambda workspace_host, instance_name: "LAKEBASE_TOKEN",
    )
    conninfo = build_conninfo(
        "postgresql://instance-abc.database.azuredatabricks.net:5432/databricks_postgres",
        "alice@example.com",
        databricks_workspace_host="https://adb-123.azuredatabricks.net",
        databricks_instance="myinstance",
    )
    assert conninfo == (
        "postgresql://alice%40example.com:LAKEBASE_TOKEN"
        "@instance-abc.database.azuredatabricks.net:5432/databricks_postgres"
    )


def test_build_conninfo_lakebase_missing_flags_raises():
    with pytest.raises(ValueError, match="databricks-workspace-host"):
        build_conninfo(
            "postgresql://instance-abc.database.azuredatabricks.net:5432/db",
            "alice@example.com",
        )
