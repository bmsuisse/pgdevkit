from __future__ import annotations

from pgdevkit.diff import DiffKind, _norm_type, _parse_index_def, compute_diff
from pgdevkit.dialect import MSSQL
from pgdevkit.models import ColumnDef, DatabaseSchema, IndexDef, TableDef


def _table(schema: str, name: str, **cols: tuple[str, bool]) -> TableDef:
    t = TableDef(schema=schema, name=name)
    for cname, (dtype, nullable) in cols.items():
        t.columns.append(ColumnDef(name=cname, data_type=dtype, is_nullable=nullable, default=None))
    return t


def test_norm_type_applies_mssql_synonyms():
    assert _norm_type("integer", MSSQL) == _norm_type("int", MSSQL)
    assert _norm_type("numeric(10,2)", MSSQL) == _norm_type("decimal(10,2)", MSSQL)


def test_norm_type_mssql_synonyms_dont_leak_into_postgres_normalization():
    # "integer" is already canonical for Postgres and must not be rewritten
    # to "int" there -- the two dialects' synonym tables are independent.
    assert _norm_type("integer") == "integer"


def test_compute_diff_reports_missing_table_for_mssql():
    scripts = DatabaseSchema(tables={"dbo.widget": _table("dbo", "widget", id=("int", False))})
    db = DatabaseSchema()
    diffs = compute_diff(scripts, db, dialect="mssql")
    assert any(d.kind == DiffKind.MISSING_IN_DB and d.object_type == "table" and d.object_name == "dbo.widget" for d in diffs)


def test_compute_diff_column_type_synonym_insensitive_for_mssql():
    scripts = DatabaseSchema(tables={"dbo.widget": _table("dbo", "widget", n=("numeric(10,2)", False))})
    db = DatabaseSchema(tables={"dbo.widget": _table("dbo", "widget", n=("decimal(10,2)", False))})
    diffs = compute_diff(scripts, db, dialect="mssql")
    assert diffs == []


def test_compute_diff_column_type_mismatch_for_mssql():
    scripts = DatabaseSchema(tables={"dbo.widget": _table("dbo", "widget", n=("int", False))})
    db = DatabaseSchema(tables={"dbo.widget": _table("dbo", "widget", n=("nvarchar(50)", False))})
    diffs = compute_diff(scripts, db, dialect="mssql")
    assert any(d.kind == DiffKind.MISMATCH and d.object_type == "column" for d in diffs)


def test_parse_index_def_parses_tsql_filtered_index():
    definition = "create unique index ix_widget_name on dbo.widget (name) where name is not null"
    info = _parse_index_def(definition, MSSQL)
    assert info is not None
    assert info["unique"] is True
    assert info["columns"] == ["name"]


def test_diff_index_matches_equivalent_tsql_definitions():
    idx = IndexDef(schema="dbo", table="widget", name="ix_widget_name", definition="create index ix_widget_name on dbo.widget (name)")
    scripts = DatabaseSchema(indexes={idx.qualified_name: idx})
    db = DatabaseSchema(indexes={idx.qualified_name: idx})
    diffs = compute_diff(scripts, db, dialect="mssql")
    assert diffs == []
