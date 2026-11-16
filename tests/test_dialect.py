from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pgdevkit.dialect import MSSQL, POSTGRES, resolve_dialect


def test_resolve_dialect_defaults_to_postgres() -> None:
    assert resolve_dialect() is POSTGRES


def test_resolve_dialect_by_name() -> None:
    assert resolve_dialect("postgres") is POSTGRES
    assert resolve_dialect("mssql") is MSSQL


def test_resolve_dialect_passes_through_a_dialect_instance() -> None:
    assert resolve_dialect(MSSQL) is MSSQL


def test_resolve_dialect_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown dialect"):
        resolve_dialect("oracle")


def test_postgres_dialect_fields() -> None:
    assert POSTGRES.sqlglot_name == "postgres"
    assert POSTGRES.default_schema == "public"
    assert POSTGRES.supports_enums
    assert POSTGRES.supports_composites
    assert POSTGRES.type_synonyms["int4"] == "integer"


def test_mssql_dialect_fields() -> None:
    assert MSSQL.sqlglot_name == "tsql"
    assert MSSQL.default_schema == "dbo"
    assert not MSSQL.supports_enums
    assert not MSSQL.supports_composites
    assert MSSQL.type_synonyms["integer"] == "int"


def test_dialect_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        POSTGRES.name = "mssql"  # type: ignore[misc]
