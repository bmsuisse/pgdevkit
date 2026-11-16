from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pgdevkit.dialect import POSTGRES, Dialect
from pgdevkit.introspect import introspect_db
from pgdevkit.models import DatabaseSchema


class PostgresBackend:
    dialect: Dialect = POSTGRES

    def introspect(self, conninfo: str) -> DatabaseSchema:
        return introspect_db(conninfo)

    def complex_helper_factory(self) -> Callable[..., Any] | None:
        from pgdevkit.db.complex_types import ComplexHelper

        return ComplexHelper
