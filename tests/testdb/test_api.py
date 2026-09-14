from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

import psycopg
import pytest

from pgdevkit.testdb import constants
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, find_orphaned_dbs, reset_testdb, status
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


def test_clean_testdb_rejects_all_and_orphaned_together(project_factory: Callable[[str, str], Path]):
    project = project_factory("apitest6", "main")
    with pytest.raises(ValueError, match="all=True, orphaned=True"):
        clean_testdb(project, all=True, orphaned=True)


@requires_podman
def test_find_orphaned_dbs_excludes_live_worktrees(
    worktree_project_factory: Callable[..., tuple[Path, Callable[[str], Path]]],
):
    repo, add_worktree = worktree_project_factory("orphtest")
    feature = add_worktree("feature")
    ghost = add_worktree("ghost")
    try:
        ensure_testdb(repo)
        ensure_testdb(feature)
        ghost_db = ensure_testdb(ghost)["ORPHTEST_POSTGRES_DB"]
        subprocess.run(["git", "worktree", "remove", "--force", str(ghost)], cwd=repo, check=True)

        assert find_orphaned_dbs(repo) == [ghost_db]
    finally:
        clean_testdb(repo, all=True)


@requires_podman
def test_find_orphaned_dbs_treats_manually_deleted_worktree_as_orphaned(
    worktree_project_factory: Callable[..., tuple[Path, Callable[[str], Path]]],
):
    # A worktree dir removed with plain `rm -rf` (no `git worktree remove`)
    # still shows up in `git worktree list` as prunable -- it must still be
    # treated as not-live.
    repo, add_worktree = worktree_project_factory("orphtest2")
    ghost = add_worktree("ghost")
    try:
        ghost_db = ensure_testdb(ghost)["ORPHTEST2_POSTGRES_DB"]
        shutil.rmtree(ghost)

        assert find_orphaned_dbs(repo) == [ghost_db]
    finally:
        clean_testdb(repo, all=True)


@requires_podman
def test_clean_orphaned_drops_only_orphaned_dbs(
    worktree_project_factory: Callable[..., tuple[Path, Callable[[str], Path]]],
):
    repo, add_worktree = worktree_project_factory("orphtest3")
    feature = add_worktree("feature")
    ghost = add_worktree("ghost")
    try:
        ensure_testdb(repo)
        feature_db = ensure_testdb(feature)["ORPHTEST3_POSTGRES_DB"]
        ensure_testdb(ghost)
        subprocess.run(["git", "worktree", "remove", "--force", str(ghost)], cwd=repo, check=True)

        clean_testdb(repo, orphaned=True)

        assert find_orphaned_dbs(repo) == []
        with psycopg.connect(constants.conninfo("postgres")) as con:
            with con.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (feature_db,))
                (count,) = cur.fetchone()
        assert count == 1
    finally:
        clean_testdb(repo, all=True)


@requires_podman
def test_find_orphaned_dbs_respects_extra_db_suffixes(
    worktree_project_factory: Callable[..., tuple[Path, Callable[[str], Path]]],
):
    repo, _ = worktree_project_factory("orphtest4")
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").rstrip("\n") + '\nextra_db_suffixes = ["_sibling"]\n',
        encoding="utf-8",
    )
    try:
        main_db = ensure_testdb(repo)["ORPHTEST4_POSTGRES_DB"]
        sibling_db = f"{main_db}_sibling"
        stray_db = f"{main_db}_stray"
        with psycopg.connect(constants.conninfo("postgres"), autocommit=True) as con:
            con.execute(f'CREATE DATABASE "{sibling_db}"')
            con.execute(f'CREATE DATABASE "{stray_db}"')

        assert find_orphaned_dbs(repo) == [stray_db]
    finally:
        clean_testdb(repo, all=True)
