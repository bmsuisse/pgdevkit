from __future__ import annotations

from pathlib import Path

from pgdevkit.testdb.schema import _iter_sql_files


def _write(base: Path, rel: str, content: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _names(base: Path, **kwargs) -> set[Path]:
    return {f.relative_to(base) for f, _ in _iter_sql_files(base, **kwargs)}


class TestIterSqlFilesAreaFiltering:
    def test_no_filters_yields_everything(self, tmp_path: Path):
        _write(tmp_path, "billing/tables/x.sql", "-- area: billing\nCREATE TABLE billing.x (id int);\n")
        _write(tmp_path, "reporting/tables/y.sql", "-- area: reporting\nCREATE TABLE reporting.y (id int);\n")
        assert _names(tmp_path) == {Path("billing/tables/x.sql"), Path("reporting/tables/y.sql")}

    def test_areas_filter_keeps_matching_and_untagged(self, tmp_path: Path):
        _write(tmp_path, "billing/tables/x.sql", "-- area: billing\nCREATE TABLE billing.x (id int);\n")
        _write(tmp_path, "reporting/tables/y.sql", "-- area: reporting\nCREATE TABLE reporting.y (id int);\n")
        _write(tmp_path, "common/tables/z.sql", "CREATE TABLE common.z (id int);\n")

        result = _names(tmp_path, areas=frozenset({"billing"}))
        assert result == {Path("billing/tables/x.sql"), Path("common/tables/z.sql")}

    def test_exclude_areas_drops_matching_but_keeps_untagged(self, tmp_path: Path):
        _write(tmp_path, "billing/tables/x.sql", "-- area: billing\nCREATE TABLE billing.x (id int);\n")
        _write(tmp_path, "common/tables/z.sql", "CREATE TABLE common.z (id int);\n")

        result = _names(tmp_path, exclude_areas=frozenset({"billing"}))
        assert result == {Path("common/tables/z.sql")}


class TestIterSqlFilesSchemaFiltering:
    def test_schemas_filter_keeps_matching(self, tmp_path: Path):
        _write(tmp_path, "billing/tables/x.sql", "CREATE TABLE billing.x (id int);\n")
        _write(tmp_path, "reporting/tables/y.sql", "CREATE TABLE reporting.y (id int);\n")

        result = _names(tmp_path, schemas=frozenset({"billing"}))
        assert result == {Path("billing/tables/x.sql")}

    def test_exclude_schemas_drops_matching(self, tmp_path: Path):
        _write(tmp_path, "billing/tables/x.sql", "CREATE TABLE billing.x (id int);\n")
        _write(tmp_path, "reporting/tables/y.sql", "CREATE TABLE reporting.y (id int);\n")

        result = _names(tmp_path, exclude_schemas=frozenset({"billing"}))
        assert result == {Path("reporting/tables/y.sql")}

    def test_area_and_schema_filters_combine(self, tmp_path: Path):
        _write(tmp_path, "keep/tables/a.sql", "-- area: billing\nCREATE TABLE reporting.a (id int);\n")
        _write(tmp_path, "wrong_area/tables/b.sql", "-- area: ops\nCREATE TABLE reporting.b (id int);\n")
        _write(tmp_path, "wrong_schema/tables/c.sql", "-- area: billing\nCREATE TABLE billing.c (id int);\n")

        result = _names(tmp_path, areas=frozenset({"billing"}), schemas=frozenset({"reporting"}))
        assert result == {Path("keep/tables/a.sql")}
