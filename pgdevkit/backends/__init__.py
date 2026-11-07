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
    try:
        return _REGISTRY[resolved.name]
    except KeyError:
        raise ValueError(
            f"Dialect {resolved.name!r} has no introspection backend; "
            f"this command supports {sorted(_REGISTRY)}"
        ) from None


__all__ = ["Backend", "MssqlBackend", "PostgresBackend", "get_backend"]
