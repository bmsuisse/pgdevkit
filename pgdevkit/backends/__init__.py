from __future__ import annotations

from ..dialect import Dialect, resolve_dialect
from .base import Backend
from .mssql import MssqlBackend
from .postgres import PostgresBackend

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
