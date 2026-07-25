from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from pydantic import BaseModel


class TableModel(BaseModel, ABC):
    """Base class for models that map 1:1 to a database table/row (any
    engine -- schema/table naming is equally meaningful for Postgres and
    MSSQL, this base class was never actually Postgres-specific).

    Models representing partial results (joins, aggregations, projections)
    should extend `pydantic.BaseModel` directly instead."""

    @staticmethod
    @abstractmethod
    def get_table_name() -> tuple[str, str]:
        """Return (schema, table), e.g. ('public', 'users')."""

    @staticmethod
    @abstractmethod
    def get_primary_key() -> Sequence[str]:
        """Return the primary key column name(s)."""


# Backward-compat alias -- this class was named PostgresTableModel before
# MSSQL support existed; kept so existing imports keep working unchanged.
PostgresTableModel = TableModel
