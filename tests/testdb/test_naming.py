from __future__ import annotations

import subprocess
from pathlib import Path

from pgdevkit.testdb.naming import current_branch, slugify, workspace_db_name


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
