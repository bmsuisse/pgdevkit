from __future__ import annotations

from pathlib import Path

import pgdevkit.testdb.schema as schema_mod
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


class TestIterSqlFilesLaziness:
    """Same guarantee as parse_directory's (see test_parser_schemas.py): area/
    schema detection must run only when that axis is actually being filtered
    on, on top of the read+dependency-scan _iter_sql_files always does."""

    def test_no_filters_never_detects_areas_or_schemas(self, tmp_path: Path, monkeypatch):
        _write(tmp_path, "tables/x.sql", "CREATE TABLE billing.x (id int);\n")
        calls: dict[str, int] = {"areas": 0, "schemas": 0}
        monkeypatch.setattr(
            schema_mod, "parse_areas", lambda content: calls.__setitem__("areas", calls["areas"] + 1) or frozenset()
        )
        monkeypatch.setattr(
            schema_mod,
            "sql_schemas",
            lambda content, dialect: calls.__setitem__("schemas", calls["schemas"] + 1) or frozenset(),
        )

        list(_iter_sql_files(tmp_path))

        assert calls == {"areas": 0, "schemas": 0}

    def test_area_filter_alone_never_detects_schemas(self, tmp_path: Path, monkeypatch):
        _write(tmp_path, "tables/x.sql", "-- area: billing\nCREATE TABLE billing.x (id int);\n")
        calls = 0

        def counting_sql_schemas(content, dialect):
            nonlocal calls
            calls += 1
            return frozenset()

        monkeypatch.setattr(schema_mod, "sql_schemas", counting_sql_schemas)

        list(_iter_sql_files(tmp_path, areas=frozenset({"billing"})))

        assert calls == 0

    def test_schema_filter_alone_never_detects_areas(self, tmp_path: Path, monkeypatch):
        _write(tmp_path, "tables/x.sql", "CREATE TABLE billing.x (id int);\n")
        calls = 0

        def counting_parse_areas(content):
            nonlocal calls
            calls += 1
            return frozenset()

        monkeypatch.setattr(schema_mod, "parse_areas", counting_parse_areas)

        list(_iter_sql_files(tmp_path, schemas=frozenset({"billing"})))

        assert calls == 0
