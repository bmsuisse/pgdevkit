from __future__ import annotations

import psycopg
import pytest

from pgdevkit.migrate import execute_sql_script, missing_privileges

ROLE = "pgdevkit_test_grants_role"


@pytest.fixture
def app_role(clean_db: str):
    """A throwaway, no-login role to check privileges against -- dropped afterwards since
    roles are cluster-wide, not scoped to the (per-test) database like clean_db's schemas."""
    with psycopg.connect(clean_db, autocommit=True) as con:
        con.execute(f"DROP ROLE IF EXISTS {ROLE}")
        con.execute(f"CREATE ROLE {ROLE} NOLOGIN")
    yield ROLE
    with psycopg.connect(clean_db, autocommit=True) as con:
        # DROP OWNED BY revokes any grants left on tables the role can still see --
        # otherwise DROP ROLE fails with "cannot be dropped because some objects depend
        # on it" for as long as the granted-on table (e.g. widgets) still exists.
        con.execute(f"DROP OWNED BY {ROLE}")
        con.execute(f"DROP ROLE IF EXISTS {ROLE}")


def test_execute_sql_script_runs_multiple_statements_in_one_transaction(clean_db: str):
    execute_sql_script(
        clean_db,
        """
        -- a leading comment shouldn't confuse statement splitting
        CREATE TABLE public.widgets (id int primary key);
        INSERT INTO public.widgets VALUES (1);
        """,
    )
    with psycopg.connect(clean_db) as con:
        row = con.execute("select count(*) from public.widgets").fetchone()
        assert row == (1,)


def test_execute_sql_script_rolls_back_all_statements_on_any_failure(clean_db: str):
    with pytest.raises(psycopg.Error):
        execute_sql_script(
            clean_db,
            "CREATE TABLE public.widgets (id int primary key); NOT VALID SQL HERE;",
        )
    with psycopg.connect(clean_db) as con:
        row = con.execute("select to_regclass('public.widgets')").fetchone()
        assert row is not None and row[0] is None


def test_missing_privileges_reports_table_without_grant(clean_db: str, app_role: str):
    with psycopg.connect(clean_db, autocommit=True) as con:
        con.execute("CREATE TABLE public.widgets (id int primary key)")
    assert missing_privileges(clean_db, app_role, ["public.widgets"]) == ["public.widgets"]


def test_missing_privileges_empty_once_granted(clean_db: str, app_role: str):
    with psycopg.connect(clean_db, autocommit=True) as con:
        con.execute("CREATE TABLE public.widgets (id int primary key)")
        con.execute(f"GRANT SELECT ON public.widgets TO {app_role}")
    assert missing_privileges(clean_db, app_role, ["public.widgets"]) == []


def test_missing_privileges_empty_for_no_tables(clean_db: str, app_role: str):
    assert missing_privileges(clean_db, app_role, []) == []


def test_execute_sql_script_grant_on_all_tables_covers_tables_created_after_the_fact(
    clean_db: str, app_role: str
):
    """The actual use case: a re-run of `GRANT ... ON ALL TABLES IN SCHEMA` after a new
    table appears picks it up automatically -- unlike ALTER DEFAULT PRIVILEGES, which only
    covers tables created later by the exact role that ran the ALTER DEFAULT PRIVILEGES
    statement itself."""
    with psycopg.connect(clean_db, autocommit=True) as con:
        con.execute("CREATE TABLE public.widgets (id int primary key)")
    assert missing_privileges(clean_db, app_role, ["public.widgets"]) == ["public.widgets"]

    execute_sql_script(clean_db, f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {app_role}")

    assert missing_privileges(clean_db, app_role, ["public.widgets"]) == []
