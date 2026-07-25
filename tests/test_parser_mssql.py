from __future__ import annotations

from pathlib import Path

from pgdevkit.parser import _parse_function_details_tsql, parse_directory


def _write(tmp_path: Path, name: str, sql: str) -> None:
    (tmp_path / name).write_text(sql, encoding="utf-8")


def test_parses_tsql_table_with_identity_and_types(tmp_path: Path):
    _write(
        tmp_path,
        "widget.sql",
        """
        CREATE TABLE dbo.widget (
            id INT IDENTITY(1,1) PRIMARY KEY,
            name NVARCHAR(100) NOT NULL,
            price DECIMAL(10,2) NULL
        );
        """,
    )
    schema = parse_directory(tmp_path, dialect="mssql")
    table = schema.tables["dbo.widget"]
    cols = {c.name: c for c in table.columns}
    assert cols["id"].is_generated
    assert not cols["id"].is_nullable
    assert not cols["name"].is_nullable
    assert cols["price"].is_nullable


def test_unqualified_table_falls_back_to_dbo_schema(tmp_path: Path):
    _write(tmp_path, "widget.sql", "CREATE TABLE widget (id INT PRIMARY KEY);")
    schema = parse_directory(tmp_path, dialect="mssql")
    assert "dbo.widget" in schema.tables


def test_unqualified_table_falls_back_to_public_schema_for_postgres(tmp_path: Path):
    _write(tmp_path, "widget.sql", "CREATE TABLE widget (id int PRIMARY KEY);")
    schema = parse_directory(tmp_path)  # default dialect
    assert "public.widget" in schema.tables


def test_parses_tsql_view(tmp_path: Path):
    _write(
        tmp_path,
        "widget_view.sql",
        "CREATE VIEW dbo.widget_view AS SELECT id, name FROM dbo.widget;",
    )
    schema = parse_directory(tmp_path, dialect="mssql")
    assert "dbo.widget_view" in schema.views


def test_parses_tsql_function_with_returns_and_body(tmp_path: Path):
    _write(
        tmp_path,
        "get_price.sql",
        """
        CREATE FUNCTION dbo.get_price (@id INT)
        RETURNS DECIMAL(10,2)
        AS
        BEGIN
            RETURN 9.99;
        END;
        """,
    )
    schema = parse_directory(tmp_path, dialect="mssql")
    func = schema.functions["dbo.get_price"]
    assert func.language == "sql"
    assert "@id" in func.args
    assert func.return_type == "decimal(10,2)"
    assert "return 9.99" in func.body


def test_parse_function_details_tsql_extracts_args_and_body_directly():
    sql = "CREATE FUNCTION dbo.f (@a INT, @b INT) RETURNS INT AS BEGIN RETURN @a + @b; END;"
    args, return_type, language, body = _parse_function_details_tsql(sql)
    assert args == "@a int, @b int"
    assert return_type == "int"
    assert language == "sql"
    assert "return @a + @b" in body


def test_mssql_enum_style_type_is_ignored_not_erroring(tmp_path: Path):
    # A Postgres-style `CREATE TYPE ... AS ENUM` has no T-SQL equivalent --
    # parsing it under dialect="mssql" must not raise, and (since sqlglot's
    # tsql dialect won't produce the ENUM/Schema AST shapes _handle_type
    # matches on) it also shouldn't populate db.enums.
    _write(tmp_path, "mood.sql", "CREATE TYPE mood AS ENUM ('happy', 'sad');")
    schema = parse_directory(tmp_path, dialect="mssql")
    assert schema.enums == {}
