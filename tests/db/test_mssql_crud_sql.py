from __future__ import annotations

from pgdevkit.db.mssql_crud import (
    _build_delete,
    _build_insert,
    _build_insert_many,
    _build_retrieve,
    _build_retrieve_many,
    _build_update,
    _build_update_many,
    _build_upsert_merge,
)
from pgdevkit.db.mssql_sql import ident, qualified


def test_ident_brackets_and_doubles_embedded_bracket():
    assert ident("widget") == "[widget]"
    assert ident("weird]name") == "[weird]]name]"


def test_qualified_brackets_both_parts():
    assert qualified("dbo", "widget") == "[dbo].[widget]"


def test_build_retrieve_uses_qmark_placeholders():
    sql, params = _build_retrieve(("dbo", "widget"), {"id": 1})
    assert sql == "SELECT * FROM [dbo].[widget] WHERE [id] = ?"
    assert params == [1]


def test_build_retrieve_many_without_filters_selects_all():
    sql, params = _build_retrieve_many(("dbo", "widget"), {})
    assert sql == "SELECT * FROM [dbo].[widget]"
    assert params == []


def test_build_retrieve_many_with_filters():
    sql, params = _build_retrieve_many(("dbo", "widget"), {"status": "active"})
    assert sql == "SELECT * FROM [dbo].[widget] WHERE [status] = ?"
    assert params == ["active"]


def test_build_insert_uses_output_inserted_before_values():
    sql, params = _build_insert(("dbo", "widget"), {"name": "sprocket", "price": 9.99})
    assert sql == "INSERT INTO [dbo].[widget] ([name], [price]) OUTPUT INSERTED.* VALUES (?, ?)"
    assert params == ["sprocket", 9.99]


def test_build_insert_many_has_no_output_clause():
    sql = _build_insert_many(("dbo", "widget"), ["name", "price"])
    assert sql == "INSERT INTO [dbo].[widget] ([name], [price]) VALUES (?, ?)"


def test_build_update_excludes_pk_from_set_clause_and_orders_params():
    sql, params = _build_update(("dbo", "widget"), {"id": 1, "name": "sprocket"}, ["id"])
    assert sql == "UPDATE [dbo].[widget] SET [name] = ? OUTPUT INSERTED.* WHERE [id] = ?"
    assert params == ["sprocket", 1]


def test_build_update_many_has_no_output_and_qualifies_where_with_alias():
    sql = _build_update_many(("dbo", "widget"), ["id", "name"], ["id"])
    assert sql == "UPDATE [dbo].[widget] SET [name] = ? WHERE t.[id] = ?"


def test_build_upsert_merge_shape():
    sql, params = _build_upsert_merge(("dbo", "widget"), {"id": 1, "name": "sprocket"}, ["id"])
    assert sql.startswith("MERGE INTO [dbo].[widget] AS t USING (SELECT ? AS [id], ? AS [name]) AS s ")
    assert "ON t.[id] = s.[id]" in sql
    assert "WHEN MATCHED THEN UPDATE SET t.[name] = s.[name]" in sql
    assert "WHEN NOT MATCHED THEN INSERT ([id], [name]) VALUES (s.[id], s.[name])" in sql
    assert sql.rstrip().endswith("OUTPUT INSERTED.*;")
    assert params == [1, "sprocket"]


def test_build_upsert_merge_with_only_pk_columns_has_no_matched_update_clause():
    sql, params = _build_upsert_merge(("dbo", "widget"), {"id": 1}, ["id"])
    assert "WHEN MATCHED THEN UPDATE" not in sql
    assert "WHEN NOT MATCHED THEN INSERT ([id]) VALUES (s.[id])" in sql
    assert params == [1]


def test_build_delete_uses_output_deleted_before_where():
    sql, params = _build_delete(("dbo", "widget"), {"id": 1})
    assert sql == "DELETE FROM [dbo].[widget] OUTPUT DELETED.* WHERE [id] = ?"
    assert params == [1]
