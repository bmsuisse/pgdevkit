from __future__ import annotations

from typing import Callable

import pymysql
import pytest
from pathlib import Path

from pgdevkit.testdb.api import clean_testdb, ensure_testdb, find_orphaned_dbs, run_sql, status
from pgdevkit.testdb.mariadb import constants
from tests.testdb.conftest import requires_mariadb

# Selects these (live, container-requiring) tests in the dedicated GitHub
# Actions job -- see .github/workflows/python-test.yml's `mariadb-test` job,
# which runs `pytest -m mariadb`; the main `build` job runs `-m "not mssql and
# not mariadb"` so a live MariaDB never blocks the fast Postgres-only suite.
pytestmark = pytest.mark.mariadb


def _query(db_name: str, sql: str) -> list[tuple]:
    conn = pymysql.connect(**constants.connect_kwargs(db_name))
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()


@requires_mariadb
def test_ensure_testdb_applies_schema_seeds_data_and_is_idempotent(project_factory: Callable[..., Path]):
    project = project_factory("mdblive1", "main", engine="mariadb")
    try:
        env = ensure_testdb(project)
        ensure_testdb(project)  # must not raise, must not duplicate seed rows
        assert any(k.endswith("_MARIADB_DB") for k in env)

        db = status(project)["database"]
        # tags is a JSON column: the list in widget.test_data.json went in as JSON text
        assert _query(db, "SELECT id, name, JSON_EXTRACT(tags, '$[1]') FROM widget") == [(1, "sprocket", '"b"')]
    finally:
        clean_testdb(project)


@requires_mariadb
def test_view_applied_before_its_dependency_is_retried(project_factory: Callable[..., Path]):
    # a_wrapper_view sorts (and so applies) before b_base_view, which it selects from
    project = project_factory("mdblive2", "main", engine="mariadb")
    try:
        ensure_testdb(project)
        assert _query(status(project)["database"], "SELECT id, name FROM a_wrapper_view") == [(1, "sprocket")]
    finally:
        clean_testdb(project)


@requires_mariadb
def test_procedure_with_delimiter_and_run_sql(project_factory: Callable[..., Path]):
    project = project_factory("mdblive3", "main", engine="mariadb")
    try:
        ensure_testdb(project)
        assert run_sql("CALL widget_count()", project) == [{"n": 1}]
        assert run_sql("DELETE FROM widget; SELECT count(*) AS c FROM widget", project) == [{"c": 0}]
    finally:
        clean_testdb(project)


@requires_mariadb
def test_force_reset_restores_seed_and_kicks_open_sessions(project_factory: Callable[..., Path]):
    project = project_factory("mdblive4", "main", engine="mariadb")
    try:
        ensure_testdb(project)
        db = status(project)["database"]
        conn = pymysql.connect(**constants.connect_kwargs(db))  # stays open with a lock
        conn.begin()
        conn.cursor().execute("DELETE FROM widget")

        ensure_testdb(project, force_reset=True)
        conn.close()
        assert _query(db, "SELECT count(*) FROM widget") == [(1,)]
    finally:
        clean_testdb(project)


@requires_mariadb
def test_clean_drops_database_and_orphans_are_found(worktree_project_factory: Callable[..., tuple[Path, Callable[[str], Path]]]):
    repo, add_worktree = worktree_project_factory("mdblive5", "main", engine="mariadb")
    other = add_worktree("feature")
    try:
        ensure_testdb(repo)
        ensure_testdb(other)
        assert find_orphaned_dbs(repo) == []

        feature_db = status(other)["database"]
        import shutil, subprocess

        shutil.rmtree(other)
        subprocess.run(["git", "worktree", "prune"], cwd=repo, check=True)
        assert find_orphaned_dbs(repo) == [feature_db]

        clean_testdb(repo, orphaned=True)
        assert find_orphaned_dbs(repo) == []
    finally:
        clean_testdb(repo, all=True)

    names = [r[0] for r in _query("information_schema", "SELECT schema_name FROM schemata")]
    assert status(repo)["database"] not in names
