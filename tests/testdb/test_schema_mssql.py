from __future__ import annotations

from pathlib import Path

from pgdevkit.dialect import MSSQL, POSTGRES
from pgdevkit.testdb.schema import _get_sql_deps, _iter_sql_files

FIXTURES = Path(__file__).parent / "fixtures" / "database_mssql"


def test_get_sql_deps_excludes_sys_schema_references():
    # T-SQL has no native "CREATE SCHEMA IF NOT EXISTS", so scripts commonly
    # guard with "IF NOT EXISTS (SELECT ... FROM sys.schemas ...)" -- a
    # reference to sys.schemas/sys.tables must never count as a real
    # cross-file dependency, since no file ever "delivers" it. Regression
    # test for a real bug caught in CI: this previously delayed such files
    # into the retry loop, whose reverse-order resolution then applied a
    # table file before the schema.sql file it actually depended on.
    sql = "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'app') BEGIN EXEC('CREATE SCHEMA app'); END"
    assert _get_sql_deps(sql, MSSQL) == set()


def test_get_sql_deps_excludes_pg_catalog_references_for_postgres_too():
    sql = "SELECT 1 FROM pg_catalog.pg_class WHERE relname = 'widget'"
    assert _get_sql_deps(sql, POSTGRES) == set()


def test_iter_sql_files_applies_schema_before_dependent_table_and_views():
    order = [f.relative_to(FIXTURES) for f, _ in _iter_sql_files(FIXTURES, MSSQL)]
    assert order == [
        Path("schema/app.sql"),
        Path("app/tables/widget.sql"),
        Path("app/views/b_base_view.sql"),
        Path("app/views/a_wrapper_view.sql"),
    ]
