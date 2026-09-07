from __future__ import annotations

from pathlib import Path

from pgdevkit.areas import area_allowed, file_areas, filter_by_area, parse_areas


class TestParseAreas:
    def test_no_directive_is_untagged(self):
        assert parse_areas("CREATE TABLE t (id int);\n") == frozenset()

    def test_single_area(self):
        sql = "-- area: billing\nCREATE TABLE t (id int);\n"
        assert parse_areas(sql) == frozenset({"billing"})

    def test_comma_separated_areas(self):
        sql = "-- area: billing, reporting\nCREATE TABLE t (id int);\n"
        assert parse_areas(sql) == frozenset({"billing", "reporting"})

    def test_repeated_lines_union(self):
        sql = "-- area: billing\n-- area: reporting\nCREATE TABLE t (id int);\n"
        assert parse_areas(sql) == frozenset({"billing", "reporting"})

    def test_case_insensitive_directive(self):
        sql = "-- Area: billing\nCREATE TABLE t (id int);\n"
        assert parse_areas(sql) == frozenset({"billing"})

    def test_blank_lines_before_directive_are_skipped(self):
        sql = "\n\n-- area: billing\nCREATE TABLE t (id int);\n"
        assert parse_areas(sql) == frozenset({"billing"})

    def test_directive_after_other_leading_comments_still_counts(self):
        sql = "-- Copyright 2026\n-- area: billing\nCREATE TABLE t (id int);\n"
        assert parse_areas(sql) == frozenset({"billing"})

    def test_directive_past_leading_comment_block_is_ignored(self):
        sql = "CREATE TABLE t (id int);\n-- area: billing\n"
        assert parse_areas(sql) == frozenset()

    def test_directive_stops_at_first_blank_then_statement(self):
        # A directive after a blank line inside the header still counts (blank
        # lines don't end the header), but nothing after the first real
        # statement does, even inside a later comment.
        sql = "-- area: billing\n\nCREATE TABLE t (id int);\n-- area: reporting\n"
        assert parse_areas(sql) == frozenset({"billing"})


class TestFileAreas:
    def test_reads_from_disk(self, tmp_path: Path):
        f = tmp_path / "001_thing.sql"
        f.write_text("-- area: billing\nCREATE TABLE t (id int);\n", encoding="utf-8")
        assert file_areas(f) == frozenset({"billing"})


class TestAreaAllowed:
    def test_no_filters_always_allowed(self):
        assert area_allowed(frozenset({"billing"})) is True
        assert area_allowed(frozenset()) is True

    def test_only_filter_untagged_always_passes(self):
        assert area_allowed(frozenset(), only=frozenset({"billing"})) is True

    def test_only_filter_matching_area_passes(self):
        assert area_allowed(frozenset({"billing"}), only=frozenset({"billing"})) is True

    def test_only_filter_non_matching_area_fails(self):
        assert area_allowed(frozenset({"reporting"}), only=frozenset({"billing"})) is False

    def test_exclude_filter_untagged_never_dropped(self):
        assert area_allowed(frozenset(), exclude=frozenset({"billing"})) is True

    def test_exclude_filter_matching_area_dropped(self):
        assert area_allowed(frozenset({"billing"}), exclude=frozenset({"billing"})) is False

    def test_exclude_filter_non_matching_area_passes(self):
        assert area_allowed(frozenset({"reporting"}), exclude=frozenset({"billing"})) is True

    def test_only_and_exclude_combined_exclude_wins(self):
        areas = frozenset({"billing"})
        assert area_allowed(areas, only=frozenset({"billing"}), exclude=frozenset({"billing"})) is False


class TestFilterByArea:
    def test_no_filters_returns_paths_unchanged(self, tmp_path: Path):
        paths = [tmp_path / "a.sql", tmp_path / "b.sql"]
        assert filter_by_area(paths) == paths

    def test_only_keeps_matching_and_untagged(self, tmp_path: Path):
        tagged = tmp_path / "billing.sql"
        tagged.write_text("-- area: billing\nselect 1;\n", encoding="utf-8")
        other = tmp_path / "reporting.sql"
        other.write_text("-- area: reporting\nselect 1;\n", encoding="utf-8")
        untagged = tmp_path / "common.sql"
        untagged.write_text("select 1;\n", encoding="utf-8")

        result = filter_by_area([tagged, other, untagged], only=frozenset({"billing"}))
        assert set(result) == {tagged, untagged}

    def test_exclude_drops_matching_but_keeps_untagged(self, tmp_path: Path):
        tagged = tmp_path / "billing.sql"
        tagged.write_text("-- area: billing\nselect 1;\n", encoding="utf-8")
        untagged = tmp_path / "common.sql"
        untagged.write_text("select 1;\n", encoding="utf-8")

        result = filter_by_area([tagged, untagged], exclude=frozenset({"billing"}))
        assert result == [untagged]
