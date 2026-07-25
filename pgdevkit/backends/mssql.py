from __future__ import annotations

from typing import Any, Callable

from ..dialect import MSSQL, Dialect
from ..models import DatabaseSchema


class MssqlBackend:
    dialect: Dialect = MSSQL

    def introspect(self, conninfo: str) -> DatabaseSchema:
        # Imported lazily so `import pgdevkit.backends` (and thus
        # `pgdevkit.cli`) doesn't require mssql-python/the mssql extra to
        # be installed unless a caller actually asks for the mssql backend.
        from ..mssql_introspect import introspect_mssql_db

        return introspect_mssql_db(conninfo)

    def complex_helper_factory(self) -> Callable[..., Any] | None:
        # MSSQL has no composite type, native enum, or first-class JSONB
        # column type -- there is nothing for a ComplexHelper to adapt.
        # Every `complex_helper` parameter in db/crud.py (and its
        # db/mssql_crud.py counterpart) is already Optional, so callers on
        # this backend simply pass/receive None and every complex-type
        # branch takes its existing no-op path.
        return None
