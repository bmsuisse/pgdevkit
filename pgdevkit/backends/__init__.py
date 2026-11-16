from __future__ import annotations

from pgdevkit.backends.base import Backend
from pgdevkit.backends.mssql import MssqlBackend
from pgdevkit.backends.postgres import PostgresBackend
from pgdevkit.dialect import Dialect, resolve_dialect

_REGISTRY: dict[str, Backend] = {
    "postgres": PostgresBackend(),
    "mssql": MssqlBackend(),
}


def get_backend(dialect: str | Dialect = "postgres") -> Backend:
    """Look up the `Backend` for a dialect name (or an already-resolved
    `Dialect`). Defaults to postgres."""
    resolved = resolve_dialect(dialect)
    return _REGISTRY[resolved.name]


__all__ = ["Backend", "MssqlBackend", "PostgresBackend", "get_backend"]
