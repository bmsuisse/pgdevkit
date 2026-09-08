from __future__ import annotations

from pathlib import Path

from pgdevkit.migrate import list_migration_files


def _write(dir: Path, name: str, content: str = "select 1;\n") -> Path:
    p = dir / name
    p.write_text(content, encoding="utf-8")
    return p


class TestListMigrationFilesEnvFiltering:
    def test_no_env_applies_every_file_regardless_of_tag(self, tmp_path: Path):
        prod = _write(tmp_path, "001_prod.prod.sql")
        common = _write(tmp_path, "002_common.sql")

        assert list_migration_files(tmp_path) == sorted([prod, common])

    def test_env_keeps_matching_tag_and_untagged(self, tmp_path: Path):
        prod = _write(tmp_path, "001_backfill.prod.sql")
        staging = _write(tmp_path, "002_backfill.staging.sql")
        common = _write(tmp_path, "003_common.sql")

        result = list_migration_files(tmp_path, env="prod")
        assert set(result) == {prod, common}
        assert staging not in result

    def test_init_file_is_never_filtered_by_env(self, tmp_path: Path):
        init = _write(tmp_path, "001_setup.init.sql")

        assert list_migration_files(tmp_path, env="prod") == [init]
        assert list_migration_files(tmp_path, env="staging") == [init]

    def test_env_and_area_filters_compose(self, tmp_path: Path):
        billing_prod = _write(tmp_path, "001_billing.prod.sql", "-- area: billing\nselect 1;\n")
        billing_staging = _write(tmp_path, "002_billing.staging.sql", "-- area: billing\nselect 1;\n")
        reporting_prod = _write(tmp_path, "003_reporting.prod.sql", "-- area: reporting\nselect 1;\n")

        result = list_migration_files(tmp_path, areas=frozenset({"billing"}), env="prod")
        assert result == [billing_prod]
        assert billing_staging not in result
        assert reporting_prod not in result
