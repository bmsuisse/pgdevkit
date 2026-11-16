from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..dialect import POSTGRES, Dialect
from ..introspect import introspect_db
from ..models import DatabaseSchema


class PostgresBackend:
    dialect: Dialect = POSTGRES

    def introspect(self, conninfo: str) -> DatabaseSchema:
        return introspect_db(conninfo)

    def complex_helper_factory(self) -> Callable[..., Any] | None:
        from ..db.complex_types import ComplexHelper

        return ComplexHelper
