from __future__ import annotations

from pathlib import Path

import pgdevkit.parser as parser_mod
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


class TestParseDirectoryLaziness:
    """Area/schema detection is real work (a regex scan; a full sqlglot parse,
    for schema) done per file on top of the parse parse_directory needs
    anyway -- so each check must run only when the axis it belongs to is
    actually being filtered on, not unconditionally alongside the real parse."""

    def test_no_filters_never_detects_areas_or_schemas(self, tmp_path: Path, monkeypatch):
        _write(tmp_path, "a.sql", "CREATE TABLE billing.a (id int);\n")
        calls: dict[str, int] = {"areas": 0, "schemas": 0}
        monkeypatch.setattr(
            parser_mod, "parse_areas", lambda content: calls.__setitem__("areas", calls["areas"] + 1) or frozenset()
        )
        monkeypatch.setattr(
            parser_mod,
            "sql_schemas",
            lambda content, dialect: calls.__setitem__("schemas", calls["schemas"] + 1) or frozenset(),
        )

        parse_directory(tmp_path)

        assert calls == {"areas": 0, "schemas": 0}

    def test_area_filter_alone_never_detects_schemas(self, tmp_path: Path, monkeypatch):
        _write(tmp_path, "a.sql", "-- area: billing\nCREATE TABLE billing.a (id int);\n")
        calls = 0

        def counting_sql_schemas(content, dialect):
            nonlocal calls
            calls += 1
            return frozenset()

        monkeypatch.setattr(parser_mod, "sql_schemas", counting_sql_schemas)

        parse_directory(tmp_path, areas=frozenset({"billing"}))

        assert calls == 0

    def test_schema_filter_alone_never_detects_areas(self, tmp_path: Path, monkeypatch):
        _write(tmp_path, "a.sql", "CREATE TABLE billing.a (id int);\n")
        calls = 0

        def counting_parse_areas(content):
            nonlocal calls
            calls += 1
            return frozenset()

        monkeypatch.setattr(parser_mod, "parse_areas", counting_parse_areas)

        parse_directory(tmp_path, schemas=frozenset({"billing"}))

        assert calls == 0
