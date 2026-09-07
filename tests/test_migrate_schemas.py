from __future__ import annotations

from pathlib import Path

from pgdevkit.migrate import list_migration_files


def _write(dir: Path, name: str, content: str) -> Path:
    p = dir / name
    p.write_text(content, encoding="utf-8")
    return p


class TestListMigrationFilesSchemaFiltering:
    def test_no_filters_lists_everything(self, tmp_path: Path):
        a = _write(tmp_path, "001_a.sql", "CREATE TABLE billing.a (id int);\n")
        b = _write(tmp_path, "002_b.sql", "CREATE TABLE reporting.b (id int);\n")
        assert list_migration_files(tmp_path) == sorted([a, b])

    def test_schemas_filter_keeps_matching(self, tmp_path: Path):
        billing = _write(tmp_path, "001_billing.sql", "CREATE TABLE billing.a (id int);\n")
        reporting = _write(tmp_path, "002_reporting.sql", "CREATE TABLE reporting.b (id int);\n")

        result = list_migration_files(tmp_path, schemas=frozenset({"billing"}))
        assert result == [billing]
        assert reporting not in result

    def test_exclude_schemas_drops_matching(self, tmp_path: Path):
        billing = _write(tmp_path, "001_billing.sql", "CREATE TABLE billing.a (id int);\n")
        reporting = _write(tmp_path, "002_reporting.sql", "CREATE TABLE reporting.b (id int);\n")

        result = list_migration_files(tmp_path, exclude_schemas=frozenset({"billing"}))
        assert result == [reporting]
        assert billing not in result

    def test_undetectable_schema_always_kept(self, tmp_path: Path):
        undetectable = _write(tmp_path, "001_common.sql", "select 1;\n")

        assert list_migration_files(tmp_path, schemas=frozenset({"billing"})) == [undetectable]
        assert list_migration_files(tmp_path, exclude_schemas=frozenset({"billing"})) == [undetectable]

    def test_area_and_schema_filters_combine(self, tmp_path: Path):
        keep = _write(tmp_path, "001_keep.sql", "-- area: billing\nCREATE TABLE reporting.a (id int);\n")
        wrong_area = _write(tmp_path, "002_wrong_area.sql", "-- area: ops\nCREATE TABLE reporting.b (id int);\n")
        wrong_schema = _write(
            tmp_path, "003_wrong_schema.sql", "-- area: billing\nCREATE TABLE billing.c (id int);\n"
        )

        result = list_migration_files(tmp_path, areas=frozenset({"billing"}), schemas=frozenset({"reporting"}))
        assert result == [keep]
        assert wrong_area not in result
        assert wrong_schema not in result
