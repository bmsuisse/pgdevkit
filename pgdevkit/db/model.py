from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from pydantic import BaseModel


class PostgresTableModel(BaseModel, ABC):
    """Base class for models that map 1:1 to a database table/row.

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
