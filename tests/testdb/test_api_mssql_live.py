from __future__ import annotations

from typing import Callable

import mssql_python
import pytest
from pathlib import Path

from pgdevkit.testdb.api import clean_testdb, ensure_testdb, status
from tests.testdb.conftest import requires_mssql

# Selects these (live, container-requiring) tests in the dedicated GitHub
# Actions job -- see .github/workflows/python-test.yml's `mssql-test` job,
# which runs `pytest -m mssql`; the main `build` job runs `-m "not mssql"`
# so a live SQL Server never blocks the fast Postgres-only suite.
pytestmark = pytest.mark.mssql


def _query(dsn: str, sql: str) -> list[tuple]:
    conn = mssql_python.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()


@requires_mssql
def test_ensure_testdb_applies_schema_and_seeds_data(project_factory: Callable[..., Path]):
    project = project_factory("mssqllive", "main", engine="mssql")
    try:
        env = ensure_testdb(project)
        assert any(k.endswith("_MSSQL_DB") for k in env)

        rows = _query(status(project)["dsn"], "SELECT id, name FROM app.widget ORDER BY id")
        assert rows == [(1, "sprocket")]
    finally:
        clean_testdb(project)


@requires_mssql
def test_ensure_testdb_is_idempotent(project_factory: Callable[..., Path]):
    project = project_factory("mssqllive2", "main", engine="mssql")
    try:
        ensure_testdb(project)
        ensure_testdb(project)  # must not raise

        rows = _query(status(project)["dsn"], "SELECT count(*) FROM app.widget")
        assert rows == [(1,)]
    finally:
        clean_testdb(project)


@requires_mssql
def test_ensure_testdb_resolves_view_to_view_dependency(project_factory: Callable[..., Path]):
    # a_wrapper_view selects from b_base_view -- only passes if the
    # dependency-ordering logic in schema.py (shared with the Postgres path,
    # dialect-parametrized in this PR) also works for T-SQL scripts.
    project = project_factory("mssqllive3", "main", engine="mssql")
    try:
        ensure_testdb(project)
        rows = _query(status(project)["dsn"], "SELECT id, name FROM app.a_wrapper_view ORDER BY id")
        assert rows == [(1, "sprocket")]
    finally:
        clean_testdb(project)
