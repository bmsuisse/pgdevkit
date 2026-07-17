from __future__ import annotations

import pytest

from pgdevkit.db.connection import PgPool


ENV_PREFIX = "PGDEVKIT_CONNTEST_"


async def test_dsn_static_password_when_entra_user_unset(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "localhost")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", "mypassword")

    pool = PgPool(env_prefix=ENV_PREFIX)
    dsn = await pool._dsn()
    assert dsn == "host=localhost port=5432 dbname=mydb user=myuser password=mypassword"


async def test_dsn_azure_postgres_entra(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "myserver.postgres.database.azure.com")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setattr(
        "pgdevkit.db.connection.get_azure_postgres_password",
        lambda **kwargs: "AADTOKEN",
    )

    pool = PgPool(env_prefix=ENV_PREFIX, entra_user="alice@example.com")
    dsn = await pool._dsn()
    assert dsn == (
        "host=myserver.postgres.database.azure.com port=5432 dbname=mydb "
        "user=alice@example.com password=AADTOKEN"
    )


async def test_dsn_azure_postgres_entra_managed_identity(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "myserver.postgres.database.azure.com")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")

    calls = []
    monkeypatch.setattr(
        "pgdevkit.db.connection.get_azure_postgres_password",
        lambda **kwargs: calls.append(kwargs) or "MITOKEN",
    )

    pool = PgPool(env_prefix=ENV_PREFIX, entra_user="alice@example.com", credential_kind="managed_identity")
    dsn = await pool._dsn()
    assert "password=MITOKEN" in dsn
    assert calls == [{"managed_identity": True, "exclude_interactive_browser_credential": True}]


async def test_dsn_extra_params_appended(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "localhost")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", "mypassword")

    pool = PgPool(
        env_prefix=ENV_PREFIX,
        dsn_params={"sslmode": "require", "application_name": "myapp"},
    )
    dsn = await pool._dsn()
    assert dsn == (
        "host=localhost port=5432 dbname=mydb user=myuser password=mypassword "
        "sslmode=require application_name=myapp"
    )


async def test_open_uses_null_pool_and_prepare_threshold_for_azure_host(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "myserver.postgres.database.azure.com")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", "mypassword")

    pool = PgPool(env_prefix=ENV_PREFIX)
    await pool.open()
    try:
        from psycopg_pool import AsyncNullConnectionPool

        assert isinstance(pool._pool, AsyncNullConnectionPool)
        assert pool._pool.kwargs == {"prepare_threshold": None}
    finally:
        await pool.close()


async def test_open_uses_regular_pool_for_non_azure_host(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "localhost")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", "mypassword")

    pool = PgPool(env_prefix=ENV_PREFIX)
    await pool.open()
    try:
        from psycopg_pool import AsyncConnectionPool

        assert type(pool._pool) is AsyncConnectionPool
        assert pool._pool.kwargs == {}
        assert pool.raw_pool is pool._pool
    finally:
        await pool.close()


def test_raw_pool_before_open_raises():
    pool = PgPool(env_prefix=ENV_PREFIX)
    with pytest.raises(RuntimeError, match="Call open"):
        pool.raw_pool


async def test_dsn_databricks_lakebase_entra(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "instance-abc.database.azuredatabricks.net")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "databricks_postgres")
    monkeypatch.setenv(f"{ENV_PREFIX}DATABRICKS_WORKSPACE_HOST", "https://adb-123.azuredatabricks.net")
    monkeypatch.setenv(f"{ENV_PREFIX}DATABRICKS_INSTANCE", "myinstance")

    calls = []

    def fake_get_lakebase_password(workspace_host, instance_name):
        calls.append((workspace_host, instance_name))
        return "LAKEBASE_TOKEN"

    monkeypatch.setattr("pgdevkit.db.connection.get_lakebase_password", fake_get_lakebase_password)

    pool = PgPool(env_prefix=ENV_PREFIX, entra_user="alice@example.com")
    dsn = await pool._dsn()
    assert dsn == (
        "host=instance-abc.database.azuredatabricks.net port=5432 dbname=databricks_postgres "
        "user=alice@example.com password=LAKEBASE_TOKEN"
    )
    assert calls == [("https://adb-123.azuredatabricks.net", "myinstance")]
