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


def test_get_sql_deps_keeps_bracketed_nav_style_names_unquoted():
    # exp.table_name() would re-quote this as dbo."Company$Cust Ledger Entry é",
    # which never equals the name derived from the file name.
    sql = "CREATE VIEW dbo.[V] AS SELECT [a] FROM dbo.[Company$Cust Ledger Entry é] AS c"
    assert _get_sql_deps(sql, MSSQL) == {"dbo.V", "dbo.Company$Cust Ledger Entry é"}


def test_iter_sql_files_orders_tables_before_views_with_nav_style_names(tmp_path: Path):
    table = "Company$Cust_ Ledg_ Entry é"
    tables = tmp_path / "dbo" / "tables"
    views = tmp_path / "dbo" / "views"
    tables.mkdir(parents=True)
    views.mkdir(parents=True)
    (tables / f"{table}.sql").write_text(
        f"CREATE TABLE dbo.[{table}] ([a] int);\nCREATE INDEX [{table}$1] ON dbo.[{table}] ([a]);\n", encoding="utf-8"
    )
    (views / "A View.sql").write_text(f"CREATE VIEW dbo.[A View] AS SELECT [a] FROM dbo.[{table}];\n", encoding="utf-8")

    order = [f.relative_to(tmp_path) for f, _ in _iter_sql_files(tmp_path, MSSQL)]

    assert order == [Path("dbo/tables") / f"{table}.sql", Path("dbo/views/A View.sql")]
