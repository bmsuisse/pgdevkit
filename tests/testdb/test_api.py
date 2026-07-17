from __future__ import annotations

from pathlib import Path
from typing import Callable

import psycopg
import pytest

from pgdevkit.testdb import constants
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, reset_testdb, status
from pgdevkit.testdb.config import load_config
from pgdevkit.testdb.naming import slugify
from tests.testdb.conftest import requires_podman


def _admin_dsn() -> str:
    return constants.conninfo("postgres")


@requires_podman
def test_ensure_testdb_isolates_by_branch(project_factory: Callable[[str, str], Path]):
    project_a = project_factory("apitest", "main")
    project_b = project_factory("apitest", "feature")
    try:
        env_a = ensure_testdb(project_a)
        env_b = ensure_testdb(project_b)

        assert env_a["APITEST_POSTGRES_DB"] != env_b["APITEST_POSTGRES_DB"]
        assert env_a["APITEST_POSTGRES_DB"] == status(project_a)["database"]
    finally:
        clean_testdb(project_a)
        clean_testdb(project_b)


@requires_podman
def test_clean_all_removes_every_branch_database(project_factory: Callable[[str, str], Path]):
    project_a = project_factory("apitest2", "main")
    project_b = project_factory("apitest2", "feature")
    ensure_testdb(project_a)
    ensure_testdb(project_b)

    clean_testdb(project_a, all=True)

    prefix = slugify(load_config(project_a).name)
    with psycopg.connect(_admin_dsn()) as con:
        with con.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_database WHERE datname LIKE %s", (f"{prefix}_%",))
            (count,) = cur.fetchone()
    assert count == 0


@requires_podman
def test_clean_all_does_not_match_prefix_colliding_project_name(
    project_factory: Callable[[str, str], Path],
):
    # "apitestx" and "apitestxs" collide under an unescaped LIKE pattern: the
    # pattern "apitestx_%" (built from the "apitestx_" prefix) would also
    # match "apitestxs_main" because "_" is a single-character SQL wildcard
    # that consumes the "s". clean_testdb(..., all=True) for "apitestx" must
    # never touch "apitestxs"'s database.
    project_short = project_factory("apitestx", "main")
    project_long = project_factory("apitestxs", "main")
    try:
        ensure_testdb(project_short)
        ensure_testdb(project_long)

        clean_testdb(project_short, all=True)

        with psycopg.connect(_admin_dsn()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_database WHERE datname = %s",
                    (status(project_long)["database"],),
                )
                (count,) = cur.fetchone()
        assert count == 1
    finally:
        clean_testdb(project_short)
        clean_testdb(project_long)


@requires_podman
def test_reset_testdb_only_touches_own_database(project_factory: Callable[[str, str], Path]):
    project_a = project_factory("apitest3", "main")
    project_b = project_factory("apitest3", "other")
    try:
        ensure_testdb(project_a)
        ensure_testdb(project_b)

        reset_testdb(project_a)  # must not raise or affect project_b

        with psycopg.connect(_admin_dsn()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_database WHERE datname = %s",
                    (status(project_b)["database"],),
                )
                (count,) = cur.fetchone()
        assert count == 1
    finally:
        clean_testdb(project_a)
        clean_testdb(project_b)


@requires_podman
def test_dsn_for_matches_status(project_factory: Callable[[str, str], Path]):
    from pgdevkit.testdb.api import dsn_for

    project = project_factory("apitest4", "main")
    try:
        ensure_testdb(project)
        assert dsn_for(project) == status(project)["dsn"]
    finally:
        clean_testdb(project)
