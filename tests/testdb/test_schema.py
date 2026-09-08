from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from pgdevkit.testdb.schema import apply_schema
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

FIXTURES = Path(__file__).parent / "fixtures" / "database"
TEST_DB = f"pgdevkit_schema_selftest_{RUN_SUFFIX}"


def _admin_dsn() -> str:
    return constants.conninfo("postgres")


def _db_dsn() -> str:
    return constants.conninfo(TEST_DB)


@pytest.fixture
def schema_test_db():
    ensure_container()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    yield
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


@requires_podman
async def test_apply_schema_creates_tables_and_seeds_data(schema_test_db):
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)
        async with con.cursor() as cur:
            await cur.execute("SELECT id, name FROM app.widget ORDER BY id")
            rows = await cur.fetchall()
    assert rows == [(1, "sprocket")]


@requires_podman
async def test_apply_schema_is_idempotent(schema_test_db):
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)
        await apply_schema(con, FIXTURES)  # must not raise
        async with con.cursor() as cur:
            await cur.execute("SELECT count(*) FROM app.widget")
            (count,) = await cur.fetchone()
    assert count == 1


@requires_podman
async def test_apply_schema_on_missing_directory_is_a_noop(schema_test_db):
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES.parent / "does_not_exist")  # must not raise


@requires_podman
async def test_apply_schema_resolves_multi_level_fk_dependency(schema_test_db):
    # widget_part_detail REFERENCES widget_part REFERENCES widget, and their
    # filenames already sort alphabetically in that same dependency order.
    # This is the layout that exposed a real bug: _iter_sql_files derived the
    # schema/table name from the wrong path component (file.parent.parent.name
    # instead of file.parent.name for a database/{schema}/tables/{name}.sql
    # layout), so table dependency tracking never activated and files were
    # applied in filename order, which for this fixture is the exact reverse
    # of the required FK order.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)  # must not raise
        async with con.cursor() as cur:
            await cur.execute("INSERT INTO app.widget_part (widget_id) VALUES (1) RETURNING id")
            (part_id,) = await cur.fetchone()
            await cur.execute(
                "INSERT INTO app.widget_part_detail (widget_part_id) VALUES (%s) RETURNING id",
                (part_id,),
            )
            (detail_id,) = await cur.fetchone()
    assert detail_id == 1


@requires_podman
async def test_apply_schema_resolves_view_to_view_dependency(schema_test_db):
    # a_wrapper_view selects from b_base_view, but the filenames sort in the
    # opposite order — this only passes if cross-view dependency tracking
    # (not just table FK tracking) delays a_wrapper_view until its dependency
    # exists.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)  # must not raise
        async with con.cursor() as cur:
            await cur.execute("SELECT id, name FROM app.a_wrapper_view ORDER BY id")
            rows = await cur.fetchall()
    assert rows == [(1, "sprocket")]


@requires_podman
async def test_apply_schema_reapplies_permissions_for_a_delayed_table(schema_test_db):
    # permissions/grants.sql (a blanket "grant select on all tables in schema app")
    # has no SQL dependencies of its own, so it always applies in the first pass, at
    # its sorted position -- before app.event, whose own filename ("event.sql")
    # sorts alphabetically before its FK target's filename ("event_kind.sql") and
    # so always falls to the delayed-retry pass, landing (chronologically) after
    # permissions/grants.sql already ran. A blanket GRANT is a one-time snapshot: on
    # its own it would never cover app.event. Confirms apply_schema() closes this by
    # re-running every permissions-type file again after the whole tree (delayed
    # retries included) has converged.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)  # must not raise

    # permissions/grants.sql creates this login role itself -- a fixed,
    # non-secret password, matching pgdevkit's own testdb container convention.
    reader_dsn = make_conninfo(
        host=constants.HOST, port=str(constants.PORT), dbname=TEST_DB,
        user="pgdevkit_test_reader", password="testpwd",
    )
    async with await psycopg.AsyncConnection.connect(reader_dsn, autocommit=True) as con:
        async with con.cursor() as cur:
            await cur.execute("SELECT count(*) FROM app.event")  # must not raise InsufficientPrivilege
            (count,) = await cur.fetchone()
    assert count == 0


@requires_podman
async def test_apply_schema_seeds_composite_enum_and_jsonb_columns(schema_test_db):
    # gadget.mood is an enum, gadget.size a composite type, gadget.tags
    # jsonb — plain json.dumps() would corrupt/error on the first two.
    # gadget.test_data.json also carries a "stale_removed_column" key that
    # doesn't match any real column (simulating a fixture left behind after
    # a column rename/removal) — must be silently dropped, not error.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)
        async with con.cursor() as cur:
            await cur.execute("SELECT mood, size, tags FROM app.gadget WHERE id = 1")
            mood, size, tags = await cur.fetchone()
    assert mood.name == "happy"
    assert size == (10, 20)
    assert tags == ["small", "shiny"]


@requires_podman
async def test_apply_schema_skips_env_tagged_file_for_a_different_env(schema_test_db):
    # prod_only.prod.sql is tagged for "prod" -- with the default env
    # ("local_test"), it must not be applied.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)
        async with con.cursor() as cur:
            await cur.execute("SELECT to_regclass('app.prod_only')")
            (regclass,) = await cur.fetchone()
    assert regclass is None


@requires_podman
async def test_apply_schema_applies_env_tagged_file_for_the_matching_env(schema_test_db):
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES, env="prod")
        async with con.cursor() as cur:
            await cur.execute("SELECT to_regclass('app.prod_only')")
            (regclass,) = await cur.fetchone()
    assert regclass is not None


@requires_podman
async def test_apply_schema_init_file_applies_regardless_of_env(schema_test_db):
    # widget.init.sql adds init_flag to app.widget -- "init" is a reserved
    # suffix, never an environment tag, so this must apply under any env.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES, env="staging")
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'widget' AND column_name = 'init_flag'"
            )
            row = await cur.fetchone()
    assert row is not None


@requires_podman
async def test_apply_schema_never_applies_migrations_dir(schema_test_db):
    # migrations/ is for one-time manual application against real databases,
    # not for building a fresh schema — the base table file is the only
    # source of truth apply_schema uses. app/migrations/001_add_gadget_note.sql
    # adds a column that gadget.sql itself doesn't have; it must NOT appear.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await apply_schema(con, FIXTURES)
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'gadget' AND column_name = 'note'"
            )
            row = await cur.fetchone()
    assert row is None
