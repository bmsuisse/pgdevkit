from __future__ import annotations

from pathlib import Path

from pgdevkit.migrate import list_migration_files


def _write(dir: Path, name: str, content: str) -> Path:
    p = dir / name
    p.write_text(content, encoding="utf-8")
    return p


class TestListMigrationFilesAreaFiltering:
    def test_no_filters_lists_everything(self, tmp_path: Path):
        a = _write(tmp_path, "001_a.sql", "-- area: billing\nselect 1;\n")
        b = _write(tmp_path, "002_b.sql", "select 1;\n")
        assert list_migration_files(tmp_path) == sorted([a, b])

    def test_areas_filter_keeps_matching_and_untagged(self, tmp_path: Path):
        billing = _write(tmp_path, "001_billing.sql", "-- area: billing\nselect 1;\n")
        reporting = _write(tmp_path, "002_reporting.sql", "-- area: reporting\nselect 1;\n")
        common = _write(tmp_path, "003_common.sql", "select 1;\n")

        result = list_migration_files(tmp_path, areas=frozenset({"billing"}))
        assert set(result) == {billing, common}
        assert reporting not in result

    def test_exclude_areas_drops_matching_but_keeps_untagged(self, tmp_path: Path):
        billing = _write(tmp_path, "001_billing.sql", "-- area: billing\nselect 1;\n")
        common = _write(tmp_path, "002_common.sql", "select 1;\n")

        result = list_migration_files(tmp_path, exclude_areas=frozenset({"billing"}))
        assert result == [common]
        assert billing not in result

    def test_multiple_areas_on_one_file(self, tmp_path: Path):
        multi = _write(tmp_path, "001_multi.sql", "-- area: billing, reporting\nselect 1;\n")

        assert list_migration_files(tmp_path, areas=frozenset({"reporting"})) == [multi]
        assert list_migration_files(tmp_path, exclude_areas=frozenset({"billing"})) == []
