from __future__ import annotations

import pytest

from pgdevkit.dialect import MSSQL, POSTGRES, Dialect, resolve_dialect


def test_resolve_dialect_defaults_to_postgres():
    assert resolve_dialect() is POSTGRES


def test_resolve_dialect_by_name():
    assert resolve_dialect("postgres") is POSTGRES
    assert resolve_dialect("mssql") is MSSQL


def test_resolve_dialect_passes_through_a_dialect_instance():
    assert resolve_dialect(MSSQL) is MSSQL


def test_resolve_dialect_rejects_unknown_name():
    with pytest.raises(ValueError, match="Unknown dialect"):
        resolve_dialect("oracle")


def test_postgres_dialect_fields():
    assert POSTGRES.sqlglot_name == "postgres"
    assert POSTGRES.default_schema == "public"
    assert POSTGRES.supports_enums
    assert POSTGRES.supports_composites
    assert POSTGRES.type_synonyms["int4"] == "integer"


def test_mssql_dialect_fields():
    assert MSSQL.sqlglot_name == "tsql"
    assert MSSQL.default_schema == "dbo"
    assert not MSSQL.supports_enums
    assert not MSSQL.supports_composites
    assert MSSQL.type_synonyms["integer"] == "int"


def test_dialect_is_frozen():
    with pytest.raises(Exception):
        POSTGRES.name = "mssql"  # type: ignore[misc]
