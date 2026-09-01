from __future__ import annotations

from pgdevkit.migrate import (
    _created_table_names,
    _idempotent_target,
    _split_sql,
    _strip_line_comments,
    default_tracking_table,
)


def test_created_table_names_ignores_create_table_mentioned_in_a_comment():
    sql = """
    -- with no CREATE TABLE checked in under database/ or _migration_scripts/. Adding
    -- it here (and as schema-as-code) so a fresh/reset test DB actually has it.
    CREATE TABLE IF NOT EXISTS app.widgets (
        id text NOT NULL,
        CONSTRAINT widgets_pkey PRIMARY KEY (id)
    );
    """
    assert _created_table_names(_split_sql(sql)) == ["app.widgets"]


def test_created_table_names_finds_multiple_tables():
    sql = """
    CREATE TABLE a.b (id int);
    CREATE TABLE IF NOT EXISTS c.d (id int);
    """
    assert _created_table_names(_split_sql(sql)) == ["a.b", "c.d"]


def test_created_table_names_ignores_non_create_table_statements():
    sql = """
    GRANT SELECT ON ALL TABLES IN SCHEMA app TO some_role;
    ALTER TABLE app.widgets ADD COLUMN name text;
    """
    assert _created_table_names(_split_sql(sql)) == []


def test_created_table_names_falls_back_to_regex_for_unparseable_statements():
    # sqlglot's postgres dialect can't parse DO $$ ... $$ blocks — the fallback
    # regex must still run against comment-stripped text, not raw text.
    sql = """
    -- DO NOT CREATE TABLE this manually, use the migration
    DO $$
    BEGIN
        CREATE TABLE app.dynamic_table (id int);
    END $$;
    """
    assert _created_table_names(_split_sql(sql)) == ["app.dynamic_table"]


def test_strip_line_comments_preserves_string_literals_containing_dashes():
    sql = "SELECT '--not-a-comment' AS x -- a real comment\nFROM t;"
    stripped = _strip_line_comments(sql)
    assert "--not-a-comment" in stripped
    assert "a real comment" not in stripped


def test_default_tracking_table_falls_back_when_no_pyproject_section(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert default_tracking_table(tmp_path) == "public.schema_migrations"


def test_default_tracking_table_reads_pyproject_override(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pgdevkit]\nmigrations_table = 'myschema.migrations'\n"
    )
    assert default_tracking_table(tmp_path) == "myschema.migrations"


def test_default_tracking_table_searches_parent_directories(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pgdevkit]\nmigrations_table = 'myschema.migrations'\n"
    )
    nested = tmp_path / "database" / "_migration_scripts"
    nested.mkdir(parents=True)
    assert default_tracking_table(nested) == "myschema.migrations"


def test_default_tracking_table_falls_back_with_no_pyproject_at_all(tmp_path):
    assert default_tracking_table(tmp_path / "nonexistent") == "public.schema_migrations"


def test_idempotent_target_recognizes_create_table_if_not_exists():
    assert _idempotent_target("CREATE TABLE IF NOT EXISTS app.widgets (id int)") == ("relation", "app.widgets")


def test_idempotent_target_recognizes_create_index():
    assert _idempotent_target(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS widgets_name_idx ON app.widgets (name)"
    ) == ("relation", "widgets_name_idx")


def test_idempotent_target_recognizes_create_sequence():
    assert _idempotent_target("CREATE SEQUENCE IF NOT EXISTS app.widgets_seq") == ("relation", "app.widgets_seq")


def test_idempotent_target_recognizes_create_view():
    assert _idempotent_target("CREATE VIEW IF NOT EXISTS app.widgets_v AS SELECT * FROM app.widgets") == (
        "relation",
        "app.widgets_v",
    )


def test_idempotent_target_recognizes_create_schema():
    assert _idempotent_target("CREATE SCHEMA IF NOT EXISTS app") == ("schema", "app")


def test_idempotent_target_recognizes_add_column():
    assert _idempotent_target("ALTER TABLE app.widgets ADD COLUMN IF NOT EXISTS name text") == (
        "column",
        "app.widgets",
        "name",
    )


def test_idempotent_target_recognizes_add_column_without_if_not_exists_guard():
    assert _idempotent_target("ALTER TABLE app.widgets ADD COLUMN name text") == (
        "column",
        "app.widgets",
        "name",
    )


def test_idempotent_target_ignores_create_or_replace():
    # A view/function could be replaced with different content, so existence alone never
    # means "already applied" for these.
    assert _idempotent_target("CREATE OR REPLACE VIEW app.widgets_v AS SELECT 1") is None


def test_idempotent_target_ignores_unrecognized_statements():
    assert _idempotent_target("ALTER TABLE app.widgets DROP COLUMN name") is None
    assert _idempotent_target("INSERT INTO app.widgets (id) VALUES (1)") is None
