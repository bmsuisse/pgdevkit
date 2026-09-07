from __future__ import annotations

from pathlib import Path

from pgdevkit.dialect import MSSQL, POSTGRES
from pgdevkit.schemas import file_schemas, filter_by_schema, schema_allowed, sql_schemas


class TestSqlSchemas:
    def test_unqualified_create_table_uses_default_schema(self):
        assert sql_schemas("CREATE TABLE t (id int);\n") == frozenset({"public"})

    def test_qualified_create_table(self):
        assert sql_schemas("CREATE TABLE billing.invoice (id int);\n") == frozenset({"billing"})

    def test_multiple_schemas_referenced(self):
        sql = "CREATE TABLE billing.invoice (id int, customer_id int references reporting.customer(id));\n"
        assert sql_schemas(sql) == frozenset({"billing", "reporting"})

    def test_create_schema_statement(self):
        assert sql_schemas("CREATE SCHEMA billing;\n") == frozenset({"billing"})

    def test_alter_table(self):
        assert sql_schemas("ALTER TABLE billing.invoice ADD COLUMN paid boolean;\n") == frozenset({"billing"})

    def test_drop_table(self):
        assert sql_schemas("DROP TABLE billing.invoice;\n") == frozenset({"billing"})

    def test_data_migration_statements(self):
        assert sql_schemas("INSERT INTO billing.invoice (id) VALUES (1);\n") == frozenset({"billing"})
        assert sql_schemas("UPDATE billing.invoice SET paid = true;\n") == frozenset({"billing"})
        assert sql_schemas("DELETE FROM billing.invoice WHERE id = 1;\n") == frozenset({"billing"})

    def test_system_schema_references_excluded(self):
        sql = "SELECT 1 FROM pg_catalog.pg_class WHERE relname = 'invoice'"
        assert sql_schemas(sql) == frozenset()

    def test_information_schema_reference_excluded(self):
        sql = "SELECT 1 FROM information_schema.columns WHERE table_name = 'invoice'"
        assert sql_schemas(sql) == frozenset()

    def test_no_table_reference_is_undetectable(self):
        assert sql_schemas("SELECT 1;\n") == frozenset()

    def test_mssql_default_schema(self):
        assert sql_schemas("CREATE TABLE t (id int);\n", MSSQL) == frozenset({"dbo"})

    def test_mssql_sys_schema_excluded(self):
        # The idempotency-guard SELECT against sys.schemas must not itself
        # count as a schema reference; the dynamic `EXEC('CREATE SCHEMA
        # app')` this guards is still a real (if indirect) declaration of
        # "app" -- picked up by the regex fallback once sqlglot's own
        # mis-parse of the EXEC(...) literal as a table name is discarded.
        sql = "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'app') BEGIN EXEC('CREATE SCHEMA app'); END"
        assert sql_schemas(sql, MSSQL) == frozenset({"app"})

    def test_create_schema_alongside_another_real_table_reference(self):
        # Regression test: a bare `CREATE SCHEMA x` parses to a Table node
        # with an empty name (its schema name lives in `db`, not `this`) --
        # the guard against sqlglot's EXEC(...) literal misparse (which has
        # a *non-empty*, sentence-shaped name) must not also reject this.
        sql = "CREATE SCHEMA analytics;\nSELECT 1 FROM public.tenants;\n"
        assert sql_schemas(sql) == frozenset({"analytics", "public"})

    def test_unparseable_content_falls_back_to_regex(self):
        # Deliberately malformed SQL that still contains a schema-qualified
        # reference sqlglot can't make sense of as a whole statement.
        sql = "!!! not sql billing.invoice !!!"
        assert sql_schemas(sql) == frozenset({"billing"})


class TestFileSchemas:
    def test_reads_from_disk(self, tmp_path: Path):
        f = tmp_path / "001_thing.sql"
        f.write_text("CREATE TABLE billing.invoice (id int);\n", encoding="utf-8")
        assert file_schemas(f) == frozenset({"billing"})


class TestSchemaAllowed:
    def test_no_filters_always_allowed(self):
        assert schema_allowed(frozenset({"billing"})) is True
        assert schema_allowed(frozenset()) is True

    def test_only_filter_undetectable_always_passes(self):
        assert schema_allowed(frozenset(), only=frozenset({"billing"})) is True

    def test_only_filter_matching_schema_passes(self):
        assert schema_allowed(frozenset({"billing"}), only=frozenset({"billing"})) is True

    def test_only_filter_non_matching_schema_fails(self):
        assert schema_allowed(frozenset({"reporting"}), only=frozenset({"billing"})) is False

    def test_exclude_filter_undetectable_never_dropped(self):
        assert schema_allowed(frozenset(), exclude=frozenset({"billing"})) is True

    def test_exclude_filter_matching_schema_dropped(self):
        assert schema_allowed(frozenset({"billing"}), exclude=frozenset({"billing"})) is False

    def test_exclude_filter_non_matching_schema_passes(self):
        assert schema_allowed(frozenset({"reporting"}), exclude=frozenset({"billing"})) is True

    def test_only_and_exclude_combined_exclude_wins(self):
        schemas = frozenset({"billing"})
        assert schema_allowed(schemas, only=frozenset({"billing"}), exclude=frozenset({"billing"})) is False


class TestFilterBySchema:
    def test_no_filters_returns_paths_unchanged(self, tmp_path: Path):
        paths = [tmp_path / "a.sql", tmp_path / "b.sql"]
        assert filter_by_schema(paths) == paths

    def test_only_keeps_matching_and_undetectable(self, tmp_path: Path):
        billing = tmp_path / "billing.sql"
        billing.write_text("CREATE TABLE billing.invoice (id int);\n", encoding="utf-8")
        reporting = tmp_path / "reporting.sql"
        reporting.write_text("CREATE TABLE reporting.customer (id int);\n", encoding="utf-8")
        undetectable = tmp_path / "common.sql"
        undetectable.write_text("SELECT 1;\n", encoding="utf-8")

        result = filter_by_schema([billing, reporting, undetectable], only=frozenset({"billing"}))
        assert set(result) == {billing, undetectable}

    def test_exclude_drops_matching_but_keeps_undetectable(self, tmp_path: Path):
        billing = tmp_path / "billing.sql"
        billing.write_text("CREATE TABLE billing.invoice (id int);\n", encoding="utf-8")
        undetectable = tmp_path / "common.sql"
        undetectable.write_text("SELECT 1;\n", encoding="utf-8")

        result = filter_by_schema([billing, undetectable], exclude=frozenset({"billing"}))
        assert result == [undetectable]

    def test_dialect_affects_default_schema(self, tmp_path: Path):
        f = tmp_path / "thing.sql"
        f.write_text("CREATE TABLE t (id int);\n", encoding="utf-8")

        assert filter_by_schema([f], only=frozenset({"public"}), dialect=POSTGRES) == [f]
        assert filter_by_schema([f], only=frozenset({"public"}), dialect=MSSQL) == []
        assert filter_by_schema([f], only=frozenset({"dbo"}), dialect=MSSQL) == [f]
