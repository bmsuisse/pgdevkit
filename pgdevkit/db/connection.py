from __future__ import annotations

import os

from psycopg_pool import AsyncConnectionPool


class PgPool:
    """A connection pool keyed off `{env_prefix}HOST/PORT/DB/USER/PASSWORD`
    environment variables (e.g. env_prefix="MDM_POSTGRES_")."""

    def __init__(self, env_prefix: str = "POSTGRES_", max_size: int = 40) -> None:
        self._env_prefix = env_prefix
        self._max_size = max_size
        self._pool: AsyncConnectionPool | None = None

    def _dsn(self) -> str:
        p = self._env_prefix
        return (
            f"host={os.environ[p + 'HOST']} "
            f"port={os.environ[p + 'PORT']} "
            f"dbname={os.environ[p + 'DB']} "
            f"user={os.environ[p + 'USER']} "
            f"password={os.environ[p + 'PASSWORD']}"
        )

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
