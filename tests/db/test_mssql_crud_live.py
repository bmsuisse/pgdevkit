from __future__ import annotations

from pathlib import Path

import mssql_python
import pytest

from pgdevkit.db.mssql_crud import mssql_delete_dict, mssql_insert, mssql_retrieve, mssql_update_dict, mssql_upsert_dict
from pgdevkit.db.model import TableModel
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, status
# _make_project (not the project_factory fixture) -- project_factory lives
# in tests/testdb/conftest.py, whose fixtures pytest only injects into tests
# under tests/testdb/ itself. This file sits outside that directory, so it
# calls the same helper directly with its own tmp_path instead.
from tests.testdb.conftest import _make_project, requires_mssql

pytestmark = pytest.mark.mssql


class Widget(TableModel):
    id: int
    name: str

    @staticmethod
    def get_table_name() -> tuple[str, str]:
        return ("app", "widget")

    @staticmethod
    def get_primary_key() -> list[str]:
        return ["id"]


@requires_mssql
async def test_crud_round_trip(tmp_path: Path):
    project = _make_project(tmp_path, "mssqlcrudlive", "main", engine="mssql")
    try:
        ensure_testdb(project)
        conn = mssql_python.connect(status(project)["dsn"], autocommit=True)
        try:
            inserted = await mssql_insert(conn, ("app", "widget"), {"id": 2, "name": "cog"})
            assert inserted["name"] == "cog"

            fetched = await mssql_retrieve(conn, Widget, {"id": 2})
            assert fetched is not None and fetched.name == "cog"

            updated = await mssql_update_dict(conn, ("app", "widget"), {"id": 2, "name": "cog2"}, ["id"])
            assert updated is not None and updated["name"] == "cog2"

            upserted = await mssql_upsert_dict(conn, ("app", "widget"), {"id": 3, "name": "sprocket3"}, ["id"])
            assert upserted["name"] == "sprocket3"
            upserted_again = await mssql_upsert_dict(
                conn, ("app", "widget"), {"id": 3, "name": "sprocket3-updated"}, ["id"]
            )
            assert upserted_again["name"] == "sprocket3-updated"

            deleted = await mssql_delete_dict(conn, ("app", "widget"), {"id": 2})
            assert deleted is not None and deleted["name"] == "cog2"
            assert await mssql_retrieve(conn, Widget, {"id": 2}) is None
        finally:
            conn.close()
    finally:
        clean_testdb(project)
