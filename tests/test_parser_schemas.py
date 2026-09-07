from __future__ import annotations

from pathlib import Path

from pgdevkit.parser import parse_directory


def _write(dir: Path, name: str, content: str) -> Path:
    p = dir / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseDirectorySchemaFiltering:
    def test_no_filters_parses_everything(self, tmp_path: Path):
        _write(tmp_path, "billing.sql", "CREATE TABLE billing.a (id int);\n")
        _write(tmp_path, "reporting.sql", "CREATE TABLE reporting.b (id int);\n")
        schema = parse_directory(tmp_path)
        assert set(schema.tables) == {"billing.a", "reporting.b"}

    def test_schemas_filter_keeps_matching_and_undetectable(self, tmp_path: Path):
        _write(tmp_path, "billing.sql", "CREATE TABLE billing.a (id int);\n")
        _write(tmp_path, "reporting.sql", "CREATE TABLE reporting.b (id int);\n")

        schema = parse_directory(tmp_path, schemas=frozenset({"billing"}))
        assert set(schema.tables) == {"billing.a"}

    def test_exclude_schemas_drops_matching(self, tmp_path: Path):
        _write(tmp_path, "billing.sql", "CREATE TABLE billing.a (id int);\n")
        _write(tmp_path, "reporting.sql", "CREATE TABLE reporting.b (id int);\n")

        schema = parse_directory(tmp_path, exclude_schemas=frozenset({"billing"}))
        assert set(schema.tables) == {"reporting.b"}

    def test_area_and_schema_filters_combine(self, tmp_path: Path):
        # Passes both filters.
        _write(tmp_path, "keep.sql", "-- area: billing\nCREATE TABLE reporting.a (id int);\n")
        # Wrong area.
        _write(tmp_path, "wrong_area.sql", "-- area: ops\nCREATE TABLE reporting.b (id int);\n")
        # Wrong schema.
        _write(tmp_path, "wrong_schema.sql", "-- area: billing\nCREATE TABLE billing.c (id int);\n")

        schema = parse_directory(tmp_path, areas=frozenset({"billing"}), schemas=frozenset({"reporting"}))
        assert set(schema.tables) == {"reporting.a"}
