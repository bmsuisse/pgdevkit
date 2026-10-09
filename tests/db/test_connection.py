from __future__ import annotations

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict

from pgdevkit.db import fetch_scalar
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


@pytest.mark.parametrize("password", ["p ss", "back\\slash", "it's", "a=b", "two  spaces\\ and 'quote'", "", "ünï"])
async def test_dsn_quotes_the_password(monkeypatch, password):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "localhost")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", password)

    info = conninfo_to_dict(await PgPool(env_prefix=ENV_PREFIX)._dsn())
    assert info == {"host": "localhost", "port": "5432", "dbname": "mydb", "user": "myuser", "password": password}


async def test_dsn_quotes_extra_params_and_lets_them_override(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "localhost")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", "pw")

    pool = PgPool(env_prefix=ENV_PREFIX, dsn_params={"application_name": "my app\\'s", "sslmode": "require"})
    info = conninfo_to_dict(await pool._dsn())
    assert info["application_name"] == "my app\\'s"
    assert info["sslmode"] == "require"


async def test_azure_token_is_quoted_too(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "myserver.postgres.database.azure.com")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setattr("pgdevkit.db.connection.get_azure_postgres_password", lambda **kwargs: "tok en\\")
    info = conninfo_to_dict(await PgPool(env_prefix=ENV_PREFIX, entra_user="alice@example.com")._dsn())
    assert info["password"] == "tok en\\"


def _set_local_env(monkeypatch, dsn: str) -> None:
    info = conninfo_to_dict(dsn)
    for key, field in (("HOST", "host"), ("PORT", "port"), ("DB", "dbname"), ("USER", "user"), ("PASSWORD", "password")):
        monkeypatch.setenv(f"PGDEVKIT_CONNLIVE_{key}", info.get(field, ""))


async def test_pool_options_are_passed_through(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "localhost")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", "pw")

    async def configure(con: AsyncConnection) -> None: ...

    async def check(con: AsyncConnection) -> None: ...

    pool = PgPool(env_prefix=ENV_PREFIX, max_size=7, min_size=2, timeout=3.5, configure=configure, check=check)
    await pool.open()
    try:
        raw = pool.raw_pool
        assert (raw.min_size, raw.max_size, raw.timeout) == (2, 7, 3.5)
        assert raw._configure is configure
        assert raw._check is check
    finally:
        await pool.close()
    # untouched defaults: the library's own check, no hook
    plain = PgPool(env_prefix=ENV_PREFIX)
    await plain.open()
    try:
        assert plain.raw_pool._configure is None
        assert plain.raw_pool._check is not None
    finally:
        await plain.close()


async def test_configure_hook_and_timeout_work_on_a_live_pool(monkeypatch, postgres_dsn):
    _set_local_env(monkeypatch, postgres_dsn)

    async def configure(con: AsyncConnection) -> None:
        await con.execute("SET application_name = 'pgdevkit_configured'")
        await con.commit()

    pool = PgPool(env_prefix="PGDEVKIT_CONNLIVE_", max_size=1, timeout=0.3, configure=configure)
    await pool.open()
    try:
        assert await fetch_scalar("SHOW application_name", pool=pool) == "pgdevkit_configured"
        from psycopg_pool import PoolTimeout

        async with pool.connection():  # the only connection is taken: the next borrower times out
            with pytest.raises(PoolTimeout):
                await fetch_scalar("SELECT 1", pool=pool)
    finally:
        await pool.close()


async def test_pool_options_are_passed_to_a_null_pool_too(monkeypatch):
    monkeypatch.setenv(f"{ENV_PREFIX}HOST", "myserver.postgres.database.azure.com")
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", "5432")
    monkeypatch.setenv(f"{ENV_PREFIX}DB", "mydb")
    monkeypatch.setenv(f"{ENV_PREFIX}USER", "myuser")
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", "pw")

    async def configure(con: AsyncConnection) -> None: ...

    pool = PgPool(env_prefix=ENV_PREFIX, timeout=2.5, configure=configure)
    await pool.open()
    try:
        assert (pool.raw_pool.timeout, pool.raw_pool._configure) == (2.5, configure)
    finally:
        await pool.close()
