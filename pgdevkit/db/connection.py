from __future__ import annotations

import asyncio
import os
from typing import Literal

from psycopg_pool import AsyncConnectionPool, AsyncNullConnectionPool

from ..connection import detect_provider, get_azure_postgres_password, is_azure_postgres_host
from ..lakebase import get_lakebase_password


class PgPool:
    """A connection pool keyed off `{env_prefix}HOST/PORT/DB/USER/PASSWORD`
    environment variables (e.g. env_prefix="MDM_POSTGRES_").

    Pass `entra_user` to authenticate via Entra ID instead of a static
    password. The Postgres host is inspected to pick the Azure Postgres AAD
    token flow or Databricks Lakebase credential exchange; for the latter
    also set `{env_prefix}DATABRICKS_WORKSPACE_HOST` and
    `{env_prefix}DATABRICKS_INSTANCE`.

    `use_null_pool="auto"` (the default) switches to a null pool — no local
    pooling — when the host is Azure Postgres, since Azure's own PgBouncer
    (transaction-mode) does the pooling; `connection_kwargs` then
    auto-includes `prepare_threshold=None` too, since PgBouncer transaction
    mode doesn't support server-side prepared statements. Pass an explicit
    `use_null_pool`/`connection_kwargs` to override either.
    """

    def __init__(
        self,
        env_prefix: str = "POSTGRES_",
        max_size: int = 40,
        *,
        entra_user: str | None = None,
        credential_kind: Literal["default_azure", "managed_identity"] = "default_azure",
        exclude_interactive_browser_credential: bool = True,
        max_lifetime: float | None = None,
        use_null_pool: bool | Literal["auto"] = "auto",
        connection_kwargs: dict | None = None,
        dsn_params: dict[str, str] | None = None,
    ) -> None:
        self._env_prefix = env_prefix
        self._max_size = max_size
        self._entra_user = entra_user
        self._credential_kind = credential_kind
        self._exclude_interactive_browser_credential = exclude_interactive_browser_credential
        self._max_lifetime = max_lifetime
        self._use_null_pool = use_null_pool
        self._connection_kwargs = connection_kwargs
        self._dsn_params = dsn_params or {}
        self._pool: AsyncConnectionPool | AsyncNullConnectionPool | None = None

    def _is_azure_postgres(self, host: str) -> bool:
        return is_azure_postgres_host(host)

    async def _dsn(self) -> str:
        p = self._env_prefix
        host = os.environ[p + "HOST"]
        port = os.environ[p + "PORT"]
        dbname = os.environ[p + "DB"]

        if self._entra_user is None:
            user = os.environ[p + "USER"]
            password = os.environ[p + "PASSWORD"]
        else:
            user = self._entra_user
            if detect_provider(host) == "databricks_lakebase":
                workspace_host = os.environ[p + "DATABRICKS_WORKSPACE_HOST"]
                instance_name = os.environ[p + "DATABRICKS_INSTANCE"]
                password = await asyncio.to_thread(get_lakebase_password, workspace_host, instance_name)
            else:
                password = await asyncio.to_thread(
                    get_azure_postgres_password,
                    managed_identity=self._credential_kind == "managed_identity",
                    exclude_interactive_browser_credential=self._exclude_interactive_browser_credential,
                )

        dsn = f"host={host} port={port} dbname={dbname} user={user} password={password}"
        for key, value in self._dsn_params.items():
            dsn += f" {key}={value}"
        return dsn

    async def open(self) -> None:
        if self._pool is None:
            host = os.environ[self._env_prefix + "HOST"]
            is_azure_postgres = self._is_azure_postgres(host)
            use_null_pool = is_azure_postgres if self._use_null_pool == "auto" else self._use_null_pool
            connection_kwargs = self._connection_kwargs
            if connection_kwargs is None:
                # PgBouncer (transaction mode) doesn't support prepared statements
                connection_kwargs = {"prepare_threshold": None} if is_azure_postgres else {}
            max_lifetime = 3600.0 if self._max_lifetime is None else self._max_lifetime
            if use_null_pool:
                self._pool = AsyncNullConnectionPool(
                    conninfo=self._dsn,
                    open=False,
                    max_size=self._max_size,
                    max_lifetime=max_lifetime,
                    check=AsyncNullConnectionPool.check_connection,
                    kwargs=connection_kwargs,
                )
            else:
                self._pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    open=False,
                    max_size=self._max_size,
                    max_lifetime=max_lifetime,
                    check=AsyncConnectionPool.check_connection,
                    kwargs=connection_kwargs,
                )
        if not self._pool._opened:
            await self._pool.open()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    def connection(self):
        """Return an async connection context manager from the pool."""
        if self._pool is None:
            raise RuntimeError("Call open() first (e.g. in app startup).")
        return self._pool.connection()

    @property
    def raw_pool(self) -> AsyncConnectionPool | AsyncNullConnectionPool:
        """The underlying psycopg_pool object — for integrating with other
        pool-consuming libraries (e.g. Procrastinate's `open_async(pool=...)`)
        that need it directly rather than going through `connection()`."""
        if self._pool is None:
            raise RuntimeError("Call open() first (e.g. in app startup).")
        return self._pool
