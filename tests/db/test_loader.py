from __future__ import annotations

from pathlib import Path

from pgdevkit.db.loader import SqlLoader


def test_load_sql_reads_topic_name_file(tmp_path: Path):
    (tmp_path / "users").mkdir()
    (tmp_path / "users" / "get_by_id.sql").write_text("SELECT * FROM users WHERE id = %(id)s", encoding="utf-8")

    loader = SqlLoader(tmp_path)
    assert loader.load_sql("users", "get_by_id") == "SELECT * FROM users WHERE id = %(id)s"


def test_load_sql_is_cached(tmp_path: Path):
    (tmp_path / "users").mkdir()
    sql_file = tmp_path / "users" / "get_by_id.sql"
    sql_file.write_text("original", encoding="utf-8")

    loader = SqlLoader(tmp_path)
    assert loader.load_sql("users", "get_by_id") == "original"

    sql_file.write_text("changed", encoding="utf-8")
    assert loader.load_sql("users", "get_by_id") == "original"  # cached, not re-read
