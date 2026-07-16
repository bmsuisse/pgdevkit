from __future__ import annotations

import asyncio
import os

from psycopg_pool import AsyncConnectionPool

from ..connection import detect_provider, get_azure_postgres_password
from ..lakebase import get_lakebase_password


class PgPool:
    """A connection pool keyed off `{env_prefix}HOST/PORT/DB/USER/PASSWORD`
    environment variables (e.g. env_prefix="MDM_POSTGRES_").

    Pass `entra_user` to authenticate via Entra ID instead of a static
    password. The Postgres host is inspected to pick the Azure Postgres AAD
    token flow or Databricks Lakebase credential exchange; for the latter
    also set `{env_prefix}DATABRICKS_WORKSPACE_HOST` and
    `{env_prefix}DATABRICKS_INSTANCE`.
    """

    def __init__(
        self,
        env_prefix: str = "POSTGRES_",
        max_size: int = 40,
        *,
        entra_user: str | None = None,
    ) -> None:
        self._env_prefix = env_prefix
        self._max_size = max_size
        self._entra_user = entra_user
        self._pool: AsyncConnectionPool | None = None

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
                password = await asyncio.to_thread(get_azure_postgres_password)

        return f"host={host} port={port} dbname={dbname} user={user} password={password}"

    async def open(self) -> None:
        if self._pool is None:
            self._pool = AsyncConnectionPool(
                conninfo=self._dsn,
                open=False,
                max_size=self._max_size,
                check=AsyncConnectionPool.check_connection,
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
