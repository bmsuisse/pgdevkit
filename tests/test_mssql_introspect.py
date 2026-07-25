from __future__ import annotations

from pgdevkit.mssql_introspect import _extract_view_query


def test_extract_view_query_strips_create_or_alter_view_prefix():
    # Regression test for a real bug caught in CI: sys.sql_modules.definition
    # is the verbatim CREATE VIEW statement text, unlike Postgres's
    # pg_get_viewdef() which returns only the query body -- storing it
    # as-is produced a spurious "definition differs" diff against every
    # script-defined view, since parser.py's side is query-only.
    definition = "CREATE OR ALTER VIEW app.b_base_view AS\nSELECT id, name FROM app.widget;"
    assert _extract_view_query(definition) == "SELECT id, name FROM app.widget"


def test_extract_view_query_falls_back_to_raw_text_on_unparseable_input():
    garbage = "not a valid create view statement !!!"
    assert _extract_view_query(garbage) == garbage
