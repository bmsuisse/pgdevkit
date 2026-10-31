from __future__ import annotations

from typing import Callable

import mssql_python
import pytest
from pathlib import Path

from pgdevkit.testdb.mssql import constants
from pgdevkit.testdb.api import (
    clean_testdb,
    ensure_testdb,
    find_orphaned_dbs,
    restore_testdb,
    snapshot_testdb,
    status,
)
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


def _generated_ddl(conn) -> None:
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('gen') IS NULL EXEC('CREATE SCHEMA gen')")
    cur.execute("IF OBJECT_ID('gen.[Company$Cust_ Ledg_ Entry é]') IS NULL CREATE TABLE gen.[Company$Cust_ Ledg_ Entry é] (id int PRIMARY KEY)")


@requires_mssql
def test_apply_hook_runs_after_scripts(project_factory: Callable[..., Path]):
    project = project_factory("mssqlhook1", "main", engine="mssql")
    try:
        ensure_testdb(project, apply=_generated_ddl)
        ensure_testdb(project, apply=_generated_ddl)  # idempotent hook, second call must not raise

        dsn = status(project)["dsn"]
        assert _query(dsn, "SELECT count(*) FROM app.widget") == [(1,)]
        assert _query(dsn, "SELECT count(*) FROM gen.[Company$Cust_ Ledg_ Entry é]") == [(0,)]
    finally:
        clean_testdb(project)


@requires_mssql
def test_apply_hook_can_replace_scripts(project_factory: Callable[..., Path]):
    project = project_factory("mssqlhook2", "main", engine="mssql")
    try:
        ensure_testdb(project, apply=_generated_ddl, apply_scripts=False)

        dsn = status(project)["dsn"]
        assert _query(dsn, "SELECT count(*) FROM sys.tables WHERE name = 'widget'") == [(0,)]
        assert _query(dsn, "SELECT count(*) FROM gen.[Company$Cust_ Ledg_ Entry é]") == [(0,)]
    finally:
        clean_testdb(project)


@requires_mssql
def test_snapshot_restore_round_trip(project_factory: Callable[..., Path]):
    project = project_factory("mssqlsnap1", "main", engine="mssql")
    try:
        ensure_testdb(project)
        dsn = status(project)["dsn"]
        snapshot_testdb(project)

        conn = mssql_python.connect(dsn, autocommit=True)  # stays open: restore must kick it
        conn.cursor().execute("DELETE FROM app.widget")
        conn.cursor().execute("CREATE TABLE app.scratch (id int)")

        restore_testdb(project)
        conn.close()
        assert _query(dsn, "SELECT count(*) FROM app.widget") == [(1,)]
        assert _query(dsn, "SELECT count(*) FROM sys.tables WHERE name = 'scratch'") == [(0,)]

        _query(dsn, "SELECT 1")
        restore_testdb(project)  # the snapshot survives a restore
    finally:
        clean_testdb(project)


@requires_mssql
def test_restore_without_snapshot_raises(project_factory: Callable[..., Path]):
    project = project_factory("mssqlsnap2", "main", engine="mssql")
    try:
        ensure_testdb(project)
        with pytest.raises(RuntimeError, match="no snapshot"):
            restore_testdb(project)
    finally:
        clean_testdb(project)


@requires_mssql
def test_restore_refuses_when_other_snapshots_exist(project_factory: Callable[..., Path]):
    project = project_factory("mssqlsnap3", "main", engine="mssql")
    try:
        ensure_testdb(project)
        snapshot_testdb(project, "one")
        snapshot_testdb(project, "two")
        with pytest.raises(RuntimeError, match="sole snapshot"):
            restore_testdb(project, "one")
    finally:
        clean_testdb(project)


@requires_mssql
def test_snapshot_is_not_an_orphan_and_goes_with_its_database(project_factory: Callable[..., Path]):
    project = project_factory("mssqlsnap4", "main", engine="mssql")
    try:
        ensure_testdb(project)
        snapshot = snapshot_testdb(project)
        assert find_orphaned_dbs(project) == []
        assert _query(status(project)["dsn"], f"SELECT count(*) FROM sys.databases WHERE name = '{snapshot}'") == [(1,)]
    finally:
        clean_testdb(project)
    assert _query(constants.conninfo("master"), f"SELECT count(*) FROM sys.databases WHERE name = '{snapshot}'") == [(0,)]


@requires_mssql
def test_force_reset_drops_snapshots(project_factory: Callable[..., Path]):
    project = project_factory("mssqlsnap5", "main", engine="mssql")
    try:
        ensure_testdb(project)
        snapshot_testdb(project)
        ensure_testdb(project, force_reset=True)  # must not trip over the dependent snapshot
        with pytest.raises(RuntimeError, match="no snapshot"):
            restore_testdb(project)
    finally:
        clean_testdb(project)
