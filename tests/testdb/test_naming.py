from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pgdevkit.testdb.config import ProjectConfig
from pgdevkit.testdb.naming import current_branch, expected_db_names, live_worktree_branches, slugify, workspace_db_name


def test_slugify_lowercases_and_replaces_invalid_chars():
    assert slugify("MDMApp") == "mdmapp"
    assert slugify("feature/customer-contacts") == "feature_customer_contacts"


def test_slugify_strips_leading_trailing_underscores():
    assert slugify("--hello--") == "hello"


def test_slugify_truncates_long_input_and_appends_hash():
    long_name = "a" * 50
    result = slugify(long_name)
    assert len(result) == 30 + 1 + 8
    assert result.startswith("a" * 30 + "_")


def test_slugify_is_deterministic():
    long_name = "worktree-procrastinate-job-events-extra-long-branch-name"
    assert slugify(long_name) == slugify(long_name)


def test_workspace_db_name_differs_by_branch():
    a = workspace_db_name("mdmapp", "main")
    b = workspace_db_name("mdmapp", "multi_lng")
    assert a != b
    assert a == "mdmapp_main"


def test_workspace_db_name_stays_under_postgres_identifier_limit():
    name = workspace_db_name("a" * 50, "b" * 50)
    assert len(name) <= 63


def test_current_branch_reads_the_checked_out_branch(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "my-feature"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    assert current_branch(tmp_path) == "my-feature"


def _init_repo(repo: Path, initial_branch: str) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", initial_branch], cwd=repo, check=True)


def test_live_worktree_branches_lists_main_and_linked_worktrees(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo, "main")
    feature = tmp_path / "wt-feature"
    subprocess.run(["git", "worktree", "add", "-q", str(feature), "-b", "feature"], cwd=repo, check=True)

    assert set(live_worktree_branches(repo)) == {"main", "feature"}


def test_live_worktree_branches_excludes_a_removed_worktree(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo, "main")
    gone = tmp_path / "wt-gone"
    subprocess.run(["git", "worktree", "add", "-q", str(gone), "-b", "gone"], cwd=repo, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(gone)], cwd=repo, check=True)

    assert live_worktree_branches(repo) == ["main"]


def test_live_worktree_branches_reports_detached_head_as_head(tmp_path: Path):
    # A detached-HEAD worktree (e.g. a CI checkout, which defaults to one)
    # has no `branch ...` porcelain line -- it must still be reported as
    # "live", using the same "HEAD" value `current_branch()` (and so
    # `workspace_db_name()`) would compute for it, or its database would
    # look orphaned and get dropped out from under it.
    repo = tmp_path / "repo"
    _init_repo(repo, "main")
    detached = tmp_path / "wt-detached"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(detached)], cwd=repo, check=True)

    assert set(live_worktree_branches(repo)) == {"main", "HEAD"}
    assert current_branch(detached) == "HEAD"


def test_live_worktree_branches_excludes_a_manually_deleted_worktree_dir(tmp_path: Path):
    # `rm -rf` on a worktree dir without `git worktree remove` leaves it
    # registered (and reported by `git worktree list`) but its path gone --
    # still not "live".
    repo = tmp_path / "repo"
    _init_repo(repo, "main")
    gone = tmp_path / "wt-gone"
    subprocess.run(["git", "worktree", "add", "-q", str(gone), "-b", "gone"], cwd=repo, check=True)
    shutil.rmtree(gone)

    assert live_worktree_branches(repo) == ["main"]


def test_expected_db_names_covers_every_live_branch():
    config = ProjectConfig(name="proj")
    assert expected_db_names(config, ["main", "feature"]) == {"proj_main", "proj_feature"}


def test_expected_db_names_includes_extra_db_suffixes():
    config = ProjectConfig(name="proj", extra_db_suffixes=("_sibling",))
    assert expected_db_names(config, ["main"]) == {"proj_main", "proj_main_sibling"}
