from __future__ import annotations

from typing import Sequence

import psycopg
import pytest
from psycopg.types.enum import EnumInfo

from pgdevkit.db import PostgresTableModel, pg_insert, pg_retrieve, pg_retrieve_many
from pgdevkit.db.complex_types import ComplexHelper
from pgdevkit.db.crud import _select_list
from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

TEST_DB = f"pgdevkit_complextypes_selftest_{RUN_SUFFIX}"


def _admin_dsn() -> str:
    return constants.conninfo("postgres")


def _db_dsn() -> str:
    return constants.conninfo(TEST_DB)


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
        async with con.cursor() as cur:
            await helper.register_on(cur, info)  # on this cursor only, never on the connection (#54)
            await cur.execute("INSERT INTO gadget (id, moods) VALUES (1, %s)", (converted,))
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


def test_helper_attributes_stay_assignable():
    helper = ComplexHelper(con=None)  # type: ignore[arg-type]
    helper.complex_types = {}
    helper.system_complex_type_dict = {}
    assert helper.system_complex_type_dict == {}


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


class _CountingCursor(psycopg.AsyncCursor):
    async def execute(self, *args, **kwargs):
        type(self.connection).queries += 1
        return await super().execute(*args, **kwargs)


async def _connect() -> psycopg.AsyncConnection:
    """A connection that counts the statements run through its cursors (every psycopg helper uses them)."""

    class CountingConnection(psycopg.AsyncConnection):
        queries = 0

    return await CountingConnection.connect(_db_dsn(), autocommit=True, cursor_factory=_CountingCursor)


class Gadget(PostgresTableModel):
    id: int
    mood: str | None = None
    dims: dict | None = None
    note: str | None = None

    @staticmethod
    def get_table_name() -> tuple[str, str]:
        return ("public", "gadget")

    @staticmethod
    def get_primary_key() -> Sequence[str]:
        return ["id"]


async def _setup_gadget(con: psycopg.AsyncConnection) -> None:
    await con.execute("CREATE TYPE mood AS ENUM ('happy', 'sad')")
    await con.execute("CREATE TYPE dims AS (w int, h int)")
    await con.execute("CREATE TABLE gadget (id int PRIMARY KEY, mood mood, dims dims, note text)")
    await con.execute("INSERT INTO gadget VALUES (1, 'happy', ROW(3, 4), 'a'), (2, 'sad', NULL, 'b')")


async def _queries_of(con, call) -> tuple[int, object]:
    before = type(con).queries
    result = await call()
    return type(con).queries - before, result


@requires_podman
async def test_pg_retrieve_does_not_requery_the_catalog_for_every_call(complex_types_test_db):
    async with await _connect() as con:
        await _setup_gadget(con)

        async def retrieve():
            return await pg_retrieve(con, Gadget, {"id": 1}, complex_helper=ComplexHelper(con))  # a new helper per call

        first_count, first = await _queries_of(con, retrieve)
        second_count, second = await _queries_of(con, retrieve)
        third_count, third = await _queries_of(con, retrieve)
        assert first == second == third == Gadget(id=1, mood="happy", dims={"w": 3, "h": 4}, note="a")
        assert first_count > 1  # type catalog + columns + the enum/composite lookups + the SELECT itself
        assert (second_count, third_count) == (1, 1)  # just the SELECT

        # the other read path, and writes with composite values, benefit too
        count, rows = await _queries_of(
            con, lambda: pg_retrieve_many(con, Gadget, {"mood": "sad"}, complex_helper=ComplexHelper(con))
        )
        assert count == 1 and [r.id for r in rows] == [2]  # type: ignore[union-attr]
        count, _ = await _queries_of(
            con,
            lambda: pg_insert(con, ("public", "gadget"), {"id": 3, "mood": "sad", "dims": {"w": 1, "h": 2}}, complex_helper=ComplexHelper(con)),
        )
        assert count > 1  # first conversion of values on this connection: looks the two columns up
        count, row = await _queries_of(
            con,
            lambda: pg_insert(con, ("public", "gadget"), {"id": 4, "mood": "sad", "dims": {"w": 1, "h": 2}}, complex_helper=ComplexHelper(con)),
        )
        assert count == 1
        assert row["dims"].w == 1  # type: ignore[index]


@requires_podman
async def test_cache_can_be_disabled_or_cleared(complex_types_test_db):
    async with await _connect() as con:
        await _setup_gadget(con)
        first_count, _ = await _queries_of(
            con, lambda: pg_retrieve(con, Gadget, {"id": 1}, complex_helper=ComplexHelper(con))
        )
        uncached_count, _ = await _queries_of(
            con, lambda: pg_retrieve(con, Gadget, {"id": 1}, complex_helper=ComplexHelper(con, cache=False))
        )
        assert uncached_count == first_count
        helper = ComplexHelper(con)
        assert (await _queries_of(con, lambda: pg_retrieve(con, Gadget, {"id": 1}, complex_helper=helper)))[0] == 1
        helper.clear_cache()
        cleared_count, _ = await _queries_of(con, lambda: pg_retrieve(con, Gadget, {"id": 1}, complex_helper=helper))
        assert cleared_count == first_count
        # ... which is how a schema change gets picked up
        await con.execute("ALTER TABLE gadget ADD COLUMN extra mood")
        stale = await ComplexHelper(con).load_all_complex_types(("public", "gadget"), include_generated=True)
        assert "extra" not in stale
        helper.clear_cache()
        fresh = await ComplexHelper(con).load_all_complex_types(("public", "gadget"), include_generated=True)
        assert "extra" in fresh
        # cache=False never caches column lookups, not even within one helper
        private = ComplexHelper(con, cache=False)
        assert "extra" in await private.load_all_complex_types(("public", "gadget"), include_generated=True)
        await con.execute("ALTER TABLE gadget ADD COLUMN extra2 mood")
        assert "extra2" in await private.load_all_complex_types(("public", "gadget"), include_generated=True)


@requires_podman
async def test_the_cache_is_per_connection_and_skips_missing_tables(complex_types_test_db):
    async with await _connect() as con_a, await _connect() as con_b:
        await _setup_gadget(con_a)
        assert await ComplexHelper(con_a).load_all_complex_types(("public", "nope")) == {}  # not cached
        await con_a.execute("CREATE TABLE nope (id int, m mood)")
        assert set(await ComplexHelper(con_a).load_all_complex_types(("public", "nope"))) == {"id", "m"}
        helper_a, helper_b = ComplexHelper(con_a), ComplexHelper(con_b)
        assert helper_a.complex_types is not helper_b.complex_types
        await helper_a.load_all_complex_types(("public", "gadget"))
        assert helper_b.system_complex_type_dict is None  # b has learned nothing from a
        assert ComplexHelper(con_a).complex_types is helper_a.complex_types  # a's helpers share


@requires_podman
async def test_complex_helper_auto_works_for_every_crud_helper(complex_types_test_db):
    from pgdevkit.db import pg_insert_many, pg_update_dict, pg_upsert_dict

    async with await _connect() as con:
        await _setup_gadget(con)
        table = ("public", "gadget")
        row = await pg_insert(con, table, {"id": 5, "mood": "sad", "dims": {"w": 1, "h": 2}}, complex_helper="auto")
        assert row["mood"] == "sad" and row["dims"].w == 1  # type: ignore[attr-defined]
        await pg_update_dict(con, table, {"id": 5, "dims": {"w": 9, "h": 9}}, ["id"], complex_helper="auto")
        await pg_upsert_dict(con, table, {"id": 5, "mood": "happy"}, ["id"], complex_helper="auto")
        await pg_insert_many(con, table, [{"id": 6, "mood": "sad", "dims": {"w": 1, "h": 1}}, {"id": 7, "mood": "happy", "dims": {"w": 2, "h": 2}}], complex_helper="auto")
        got = await pg_retrieve(con, Gadget, {"id": 5}, complex_helper="auto")
        assert got == Gadget(id=5, mood="happy", dims={"w": 9, "h": 9}, note=None)
        assert {g.id for g in await pg_retrieve_many(con, Gadget, {"mood": "happy"}, complex_helper="auto")} == {1, 5, 7}
        # one helper per call, yet the catalog was asked once: the next call is just the statement itself
        count, _ = await _queries_of(con, lambda: pg_retrieve(con, Gadget, {"id": 1}, complex_helper="auto"))
        assert count == 1


# --- Fixes #54: a ComplexHelper must not change how *other* code on the same connection reads enums/composites ---


async def _plain_read(con: psycopg.AsyncConnection):
    """What unrelated code on `con` sees: an enum label, a composite and an enum array, read with a plain SELECT."""
    async with con.cursor() as cur:
        await cur.execute("SELECT 'sad'::mood, ROW(1, 2)::dims, ARRAY['happy']::mood[]")
        return await cur.fetchone()


@requires_podman
async def test_pg_helpers_do_not_change_how_the_connection_reads_enums_and_composites(complex_types_test_db):
    from pgdevkit.db import pg_insert_many, pg_update, pg_update_dict, pg_upsert, pg_upsert_dict, pg_upsert_many_dict

    async with await _connect() as con:
        await _setup_gadget(con)
        table = ("public", "gadget")
        before = await _plain_read(con)
        assert before[0] == "sad" and isinstance(before[0], str)  # the label, as a str

        async def check(what: str, call) -> None:
            await call()
            assert await _plain_read(con) == before, f"{what} changed how the connection reads enums/composites"

        for helper_arg in ("auto", ComplexHelper(con), ComplexHelper(con, cache=False)):
            kw = {"complex_helper": helper_arg}
            await check("pg_retrieve", lambda: pg_retrieve(con, Gadget, {"id": 1}, **kw))
            await check("pg_retrieve_many", lambda: pg_retrieve_many(con, Gadget, {}, limit=1, **kw))
            await check("pg_insert", lambda: pg_insert(con, table, {"id": 10, "mood": "sad", "dims": {"w": 1, "h": 2}}, **kw))
            await check("pg_insert (enum only)", lambda: pg_insert(con, table, {"id": 11, "mood": "happy"}, **kw))
            await check("pg_update_dict", lambda: pg_update_dict(con, table, {"id": 10, "dims": {"w": 5, "h": 5}}, ["id"], **kw))
            await check("pg_update", lambda: pg_update(con, Gadget(id=10, mood="happy", dims={"w": 6, "h": 6}), Gadget, **kw))
            await check("pg_upsert_dict", lambda: pg_upsert_dict(con, table, {"id": 10, "dims": {"w": 7, "h": 7}}, ["id"], **kw))
            await check("pg_upsert", lambda: pg_upsert(con, Gadget(id=12, mood="sad", dims={"w": 1, "h": 1}), Gadget, **kw))
            await check(
                "pg_insert_many",
                lambda: pg_insert_many(con, table, [{"id": 20, "dims": {"w": 1, "h": 1}}, {"id": 21, "dims": {"w": 2, "h": 2}}], **kw),
            )
            await check(
                "pg_upsert_many_dict",
                lambda: pg_upsert_many_dict(con, table, [{"id": 20, "dims": {"w": 3, "h": 3}}, {"id": 22, "dims": None}], ["id"], **kw),
            )
            await check(
                "pg_upsert_many_dict(must_exist)",
                lambda: pg_upsert_many_dict(con, table, [{"id": 20, "dims": {"w": 4, "h": 4}}], ["id"], must_exist=True, **kw),
            )
            await con.execute("DELETE FROM gadget WHERE id >= 10")

        # the data written through the helper is right nevertheless
        await pg_insert(con, table, {"id": 30, "mood": "sad", "dims": {"w": 8, "h": 9}}, complex_helper="auto")
        got = await pg_retrieve(con, Gadget, {"id": 30}, complex_helper="auto")
        assert got == Gadget(id=30, mood="sad", dims={"w": 8, "h": 9}, note=None)
        assert await _plain_read(con) == before


@requires_podman
async def test_issue_54_snippet_enum_label_survives_pg_retrieve_many(complex_types_test_db):
    async with await _connect() as con:
        await _setup_gadget(con)

        async def plain() -> tuple:
            return await (await con.execute("select 'happy'::mood")).fetchone()  # type: ignore[return-value]

        assert await plain() == ("happy",)
        await pg_retrieve_many(con, Gadget, {}, limit=1, complex_helper=ComplexHelper(con))
        assert await plain() == ("happy",)  # not <Mood.happy: 1>
        row = await (await con.execute("select 'happy'::mood, ROW(1, 2)::dims")).fetchone()
        assert row == ("happy", "(1,2)")  # a composite is still its text form, not a namedtuple


@requires_podman
async def test_registration_is_scoped_to_the_cursor_that_writes(complex_types_test_db):
    # the public way to use `recursive_convert` yourself: register on the cursor that runs the statement
    async with await _connect() as con:
        await con.execute("CREATE TYPE mood AS ENUM ('happy', 'sad')")
        await con.execute("CREATE TYPE dims AS (w int, h mood)")
        await con.execute("CREATE TABLE gadget (id serial PRIMARY KEY, moods mood[], d dims, d2 dims[])")
        helper = ComplexHelper(con)
        types = await helper.load_all_complex_types(("public", "gadget"))
        async with con.cursor() as cur:
            for info in types.values():
                await helper.register_on(cur, info)
            await cur.execute(
                "INSERT INTO gadget (moods, d, d2) VALUES (%s, %s, %s)",
                (
                    await helper.recursive_convert(["happy", "sad"], types["moods"], con),
                    await helper.recursive_convert({"w": 1, "h": "sad"}, types["d"], con),
                    await helper.recursive_convert([{"w": 2, "h": "happy"}], types["d2"], con),
                ),
            )
            await cur.execute("SELECT moods, d, d2 FROM gadget")
            moods, d, d2 = await cur.fetchone()  # type: ignore[misc]
        assert [m.name for m in moods] == ["happy", "sad"]
        assert (d.w, d.h.name) == (1, "sad") and (d2[0].w, d2[0].h.name) == (2, "happy")  # nested types registered too
        # a plain SELECT on the connection (or on another cursor) is untouched
        assert await (await con.execute("SELECT moods, d FROM gadget")).fetchone() == ("{happy,sad}", "(1,sad)")
