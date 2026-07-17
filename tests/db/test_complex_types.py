from __future__ import annotations

import psycopg
import pytest
from psycopg.types.enum import EnumInfo

from pgdevkit.db.complex_types import ComplexHelper
from pgdevkit.db.crud import _select_list
from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

TEST_DB = f"pgdevkit_complextypes_selftest_{RUN_SUFFIX}"


def _admin_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/postgres"


def _db_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/{TEST_DB}"


@pytest.fixture
def complex_types_test_db():
    ensure_container()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    yield
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


@requires_podman
async def test_array_of_enum_column_is_detected_and_converted(complex_types_test_db):
    # Regression: information_schema.columns.udt_name for an array-of-enum
    # column is the underscore-prefixed array type name (e.g. "_mood"), which
    # never matched the enum_types CTE's bare enum type names — so is_enum
    # was always computed False for such columns, and load_all_complex_types
    # returned a CompositeInfo instead of an EnumInfo, crashing
    # recursive_convert's `assert isinstance(info, EnumInfo)`.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await con.execute("CREATE TYPE mood AS ENUM ('happy', 'sad')")
        await con.execute("CREATE TABLE gadget (id serial PRIMARY KEY, moods mood[])")

        helper = ComplexHelper(con)
        types = await helper.load_all_complex_types(("public", "gadget"))
        info = types["moods"]
        assert isinstance(info, EnumInfo)

        converted = await helper.recursive_convert(["happy", "sad"], info, con)
        await con.execute("INSERT INTO gadget (id, moods) VALUES (1, %s)", (converted,))
        async with con.cursor() as cur:
            await cur.execute("SELECT moods FROM gadget WHERE id = 1")
            (moods,) = await cur.fetchone()
    assert [m.name for m in moods] == ["happy", "sad"]


@requires_podman
async def test_select_list_includes_generated_columns(complex_types_test_db):
    # Regression: _select_list built its column list from
    # load_all_complex_types(table_name) with the default include_generated
    # =False, so any GENERATED ALWAYS column was silently dropped from the
    # SELECT whenever a complex_helper was passed — unlike the plain
    # `SELECT *` path (used when no complex_helper is given), which always
    # includes generated columns.
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await con.execute("""
            CREATE TABLE t (
                id serial PRIMARY KEY,
                price numeric,
                qty numeric,
                total numeric GENERATED ALWAYS AS (price * qty) STORED
            )
        """)
        helper = ComplexHelper(con)
        select = await _select_list(con, ("public", "t"), helper)
        rendered = select.as_string(con)
    assert '"total"' in rendered


@requires_podman
async def test_jsonb_array_column_wraps_each_element_not_the_whole_list(complex_types_test_db):
    # Regression: a jsonb[] column (a Postgres ARRAY of jsonb) and a plain
    # jsonb column both surface identically from information_schema (a bare
    # Python list, if that's the value) -- previously both were detected as
    # the same `Jsonb` sentinel, and recursive_convert wrapped the whole
    # incoming list as one Jsonb(...) for either case. That's correct for a
    # plain jsonb column storing a JSON array *value*, but wrong for a
    # jsonb[] column, where the list *is* the array and each element needs
    # its own Jsonb(...) wrapper -- psycopg would otherwise try to bind a
    # single jsonb value to an array column and Postgres would raise
    # DatatypeMismatch ("column ... is of type jsonb[] but expression is of
    # type jsonb").
    async with await psycopg.AsyncConnection.connect(_db_dsn(), autocommit=True) as con:
        await con.execute("""
            CREATE TABLE gadget (
                id serial PRIMARY KEY,
                tags jsonb[],
                metadata jsonb
            )
        """)
        helper = ComplexHelper(con)
        types = await helper.load_all_complex_types(("public", "gadget"))

        tags_value = [{"name": "a"}, {"name": "b"}]
        converted_tags = await helper.recursive_convert(tags_value, types["tags"], con)

        metadata_value = [1, 2, 3]  # JSON array *content* for one scalar jsonb value
        converted_metadata = await helper.recursive_convert(metadata_value, types["metadata"], con)

        await con.execute(
            "INSERT INTO gadget (id, tags, metadata) VALUES (1, %s, %s)",
            (converted_tags, converted_metadata),
        )
        async with con.cursor() as cur:
            await cur.execute("SELECT tags, metadata FROM gadget WHERE id = 1")
            tags, metadata = await cur.fetchone()
    assert tags == tags_value
    assert metadata == metadata_value


def test_complex_types_cache_is_per_instance_not_shared():
    # Regression: complex_types used to be a mutable class attribute, so a
    # CompositeInfo/EnumInfo (which carries OIDs from one specific
    # connection/database) fetched by one ComplexHelper would leak into any
    # other ComplexHelper that happens to look up the same type NAME —
    # exactly what happens across pgdevkit's per-worktree isolated test
    # databases, which legitimately reuse type names like "locale_labels"
    # with different OIDs per database.
    helper_a = ComplexHelper(con=None)  # con is unused by this path
    helper_b = ComplexHelper(con=None)

    helper_a.complex_types[("app", "dimensions")] = object()  # type: ignore[assignment]

    assert helper_b.complex_types == {}
    assert helper_a.complex_types is not helper_b.complex_types
