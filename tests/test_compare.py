from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from pgdb.diff import DiffKind, compute_diff
from pgdb.introspect import introspect_db
from pgdb.parser import parse_directory

FIXTURES = Path(__file__).parent / "fixtures"


def _apply_sql(dsn: str, sql: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)  # type: ignore[arg-type]


def _apply_fixtures(dsn: str) -> None:
    for f in sorted(FIXTURES.glob("*.sql")):
        _apply_sql(dsn, f.read_text(encoding="utf-8"))


class TestCompare:
    def test_no_diff_when_matching(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db)
        assert diffs == [], [d for d in diffs]

    def test_missing_table_in_db(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "DROP TABLE myapp.posts CASCADE")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db)
        missing = [d for d in diffs if d.kind == DiffKind.MISSING_IN_DB and d.object_type == "table"]
        assert any(d.object_name == "myapp.posts" for d in missing)

    def test_extra_table_in_db_not_reported_by_default(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "CREATE TABLE myapp.extra (id BIGINT)")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db, report_extra_db=False)
        assert not any(d.object_name == "myapp.extra" for d in diffs)

    def test_extra_table_in_db_reported_with_flag(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "CREATE TABLE myapp.extra (id BIGINT)")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db, report_extra_db=True)
        assert any(
            d.kind == DiffKind.MISSING_IN_SCRIPTS and d.object_name == "myapp.extra"
            for d in diffs
        )

    def test_column_type_mismatch(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "ALTER TABLE myapp.users ALTER COLUMN email TYPE VARCHAR(255)")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db)
        assert any(
            d.kind == DiffKind.MISMATCH and d.object_type == "column"
            and d.object_name == "myapp.users.email"
            for d in diffs
        )

    def test_missing_column_in_db(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "ALTER TABLE myapp.users DROP COLUMN tags")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db)
        assert any(
            d.kind == DiffKind.MISSING_IN_DB and d.object_type == "column"
            and d.object_name == "myapp.users.tags"
            for d in diffs
        )

    def test_missing_view_in_db(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "DROP VIEW myapp.active_users")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db)
        assert any(
            d.kind == DiffKind.MISSING_IN_DB and d.object_type == "view"
            and d.object_name == "myapp.active_users"
            for d in diffs
        )

    def test_missing_function_in_db(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "DROP FUNCTION myapp.greet(TEXT)")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db)
        assert any(
            d.kind == DiffKind.MISSING_IN_DB and d.object_type == "function"
            and d.object_name == "myapp.greet"
            for d in diffs
        )

    def test_missing_enum_in_db(self, clean_db: str) -> None:
        _apply_fixtures(clean_db)
        _apply_sql(clean_db, "ALTER TABLE myapp.users ALTER COLUMN status TYPE TEXT USING status::TEXT; DROP TYPE myapp.status")
        scripts = parse_directory(FIXTURES)
        db = introspect_db(clean_db)
        diffs = compute_diff(scripts, db)
        assert any(
            d.kind == DiffKind.MISSING_IN_DB and d.object_type == "enum"
            and d.object_name == "myapp.status"
            for d in diffs
        )
