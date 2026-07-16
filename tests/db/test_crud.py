from __future__ import annotations

from typing import Sequence

import psycopg
import pytest
from pydantic import ConfigDict

from pgdevkit.db import (
    PgPool,
    PostgresTableModel,
    pg_delete,
    pg_insert,
    pg_retrieve,
    pg_retrieve_many,
    pg_update,
    pg_upsert,
    pg_upsert_many,
)
from pgdevkit.testdb import constants
from pgdevkit.testdb.container import ensure_container
from tests.testdb.conftest import RUN_SUFFIX, requires_podman

TEST_DB = f"pgdevkit_db_selftest_{RUN_SUFFIX}"
ENV_PREFIX = "PGDEVKIT_DB_SELFTEST_"


def _admin_dsn() -> str:
    return f"postgresql://{constants.USER}:{constants.PASSWORD}@{constants.HOST}:{constants.PORT}/postgres"


class Widget(PostgresTableModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str

    @staticmethod
    def get_table_name() -> tuple[str, str]:
        return ("public", "widget")

    @staticmethod
    def get_primary_key() -> Sequence[str]:
        return ["id"]


@pytest.fixture
async def pool(monkeypatch: pytest.MonkeyPatch):
    ensure_container()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        con.execute(f'CREATE DATABASE "{TEST_DB}"')
    with psycopg.connect(f"{_admin_dsn().rsplit('/', 1)[0]}/{TEST_DB}", autocommit=True) as con:
        con.execute("CREATE TABLE widget (id serial PRIMARY KEY, name text NOT NULL)")

    monkeypatch.setenv(f"{ENV_PREFIX}HOST", constants.HOST)
    monkeypatch.setenv(f"{ENV_PREFIX}PORT", str(constants.PORT))
    monkeypatch.setenv(f"{ENV_PREFIX}DB", TEST_DB)
    monkeypatch.setenv(f"{ENV_PREFIX}USER", constants.USER)
    monkeypatch.setenv(f"{ENV_PREFIX}PASSWORD", constants.PASSWORD)

    p = PgPool(env_prefix=ENV_PREFIX)
    await p.open()
    yield p
    await p.close()
    with psycopg.connect(_admin_dsn(), autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


@requires_podman
async def test_insert_retrieve_update_upsert_delete_roundtrip(pool: PgPool):
    async with pool.connection() as con:
        inserted = await pg_insert(con, ("public", "widget"), {"name": "sprocket"})
        assert inserted["name"] == "sprocket"
        widget_id = inserted["id"]

        fetched = await pg_retrieve(con, Widget, {"id": widget_id})
        assert fetched is not None
        assert fetched.name == "sprocket"

        fetched.name = "gadget"
        await pg_update(con, fetched, Widget)
        refetched = await pg_retrieve(con, Widget, {"id": widget_id})
        assert refetched is not None
        assert refetched.name == "gadget"

        many = await pg_retrieve_many(con, Widget, {})
        assert len(many) == 1

        deleted = await pg_delete(con, refetched, Widget)
        assert deleted is not None
        assert await pg_retrieve(con, Widget, {"id": widget_id}) is None


@requires_podman
async def test_upsert_and_upsert_many(pool: PgPool):
    async with pool.connection() as con:
        inserted = await pg_insert(con, ("public", "widget"), {"name": "sprocket"})
        widget = Widget(id=inserted["id"], name="sprocket")

        widget.name = "renamed"
        upserted = await pg_upsert(con, widget, Widget)
        assert upserted["name"] == "renamed"

        widget2 = Widget(id=widget.id + 1000, name="new-widget")
        await pg_upsert_many(con, [widget2], Widget)
        fetched = await pg_retrieve(con, Widget, {"id": widget2.id})
        assert fetched is not None
        assert fetched.name == "new-widget"
