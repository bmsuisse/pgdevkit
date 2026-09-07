from __future__ import annotations

from pathlib import Path

from pgdevkit.parser import parse_directory


def _write(dir: Path, name: str, content: str) -> Path:
    p = dir / name
    p.write_text(content, encoding="utf-8")
    return p


class TestParseDirectoryAreaFiltering:
    def test_no_filters_parses_everything(self, tmp_path: Path):
        _write(tmp_path, "billing.sql", "-- area: billing\nCREATE TABLE public.a (id int);\n")
        _write(tmp_path, "common.sql", "CREATE TABLE public.b (id int);\n")
        schema = parse_directory(tmp_path)
        assert set(schema.tables) == {"public.a", "public.b"}

    def test_areas_filter_keeps_matching_and_untagged(self, tmp_path: Path):
        _write(tmp_path, "billing.sql", "-- area: billing\nCREATE TABLE public.a (id int);\n")
        _write(tmp_path, "reporting.sql", "-- area: reporting\nCREATE TABLE public.b (id int);\n")
        _write(tmp_path, "common.sql", "CREATE TABLE public.c (id int);\n")

        schema = parse_directory(tmp_path, areas=frozenset({"billing"}))
        assert set(schema.tables) == {"public.a", "public.c"}

    def test_exclude_areas_drops_matching_but_keeps_untagged(self, tmp_path: Path):
        _write(tmp_path, "billing.sql", "-- area: billing\nCREATE TABLE public.a (id int);\n")
        _write(tmp_path, "common.sql", "CREATE TABLE public.c (id int);\n")

        schema = parse_directory(tmp_path, exclude_areas=frozenset({"billing"}))
        assert set(schema.tables) == {"public.c"}
