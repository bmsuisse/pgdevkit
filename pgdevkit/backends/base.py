from __future__ import annotations

from typing import Any, Callable, Protocol

from ..dialect import Dialect
from ..models import DatabaseSchema


class Backend(Protocol):
    """Introspection + a couple of engine facts, behind one interface.

    CRUD is deliberately NOT part of this protocol -- psycopg's
    `AsyncConnection` and an MSSQL driver's connection type are unrelated,
    so a unified `backend.retrieve()`/`backend.insert()` surface would force
    existing Postgres callers to go through a new indirection just to keep
    working. Callers that want CRUD import `pgdevkit.db.crud`'s `pg_*`
    functions or `pgdevkit.db.mssql_crud`'s `mssql_*` functions directly,
    exactly as `db/crud.py`'s functions are imported today."""

    dialect: Dialect

    def introspect(self, conninfo: str) -> DatabaseSchema: ...

    def complex_helper_factory(self) -> Callable[..., Any] | None:
        """A `ComplexHelper`-like factory for composite/enum/JSONB columns,
        or None when the engine has no equivalent (MSSQL)."""
        ...
