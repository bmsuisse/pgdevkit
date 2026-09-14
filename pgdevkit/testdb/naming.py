from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ProjectConfig

_INVALID_CHARS = re.compile(r"[^a-z0-9_]+")
_MAX_SLUG_LEN = 30


def slugify(value: str) -> str:
    """Lowercase, replace invalid chars with '_', truncate+hash if too long."""
    slug = _INVALID_CHARS.sub("_", value.lower()).strip("_")
    if not slug:
        slug = "x"
    if len(slug) <= _MAX_SLUG_LEN:
        return slug
    digest = hashlib.sha256(slug.encode()).hexdigest()[:8]
    return f"{slug[:_MAX_SLUG_LEN]}_{digest}"


def current_branch(cwd: Path | None = None) -> str:
    """Return the branch checked out in the git worktree rooted at cwd."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def workspace_db_name(project_name: str, branch: str) -> str:
    """Compute a Postgres-safe, collision-resistant database name for this
    project+branch. A second slugify pass over the joined string guarantees
    the result stays under Postgres's 63-byte identifier limit even when
    both inputs are already at the per-component truncation limit."""
    joined = f"{slugify(project_name)}_{slugify(branch)}"
    return slugify(joined)


def live_worktree_branches(repo: Path) -> list[str]:
    """Branches checked out in every currently-live worktree of the repo
    containing `repo` -- "live" meaning its filesystem path still exists and
    it isn't a bare checkout. `git worktree list` reports every worktree of
    a repo regardless of which one it's run from, so this works whether
    `repo` is the main checkout or a linked worktree.

    A detached-HEAD worktree (no `branch ...` porcelain line) is reported as
    branch "HEAD" -- matching what `current_branch()` (and so
    `workspace_db_name()`) computes for that same worktree via `git
    rev-parse --abbrev-ref HEAD`. Without this, a detached-HEAD worktree
    (e.g. a CI checkout, which defaults to one) would never appear "live"
    here, and its database would look orphaned and get dropped out from
    under it."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    branches: list[str] = []
    path: Path | None = None
    branch: str | None = None
    bare = False

    def _flush() -> None:
        if path is not None and not bare and branch is not None and path.exists():
            branches.append(branch)

    for line in result.stdout.splitlines():
        if not line:
            _flush()
            path, branch, bare = None, None, False
        elif line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif line == "detached":
            branch = "HEAD"
        elif line == "bare":
            bare = True
    _flush()

    return branches


def escape_like_prefix(prefix: str) -> str:
    """Escape a literal string for use as a `LIKE ... ESCAPE '\\'` prefix
    pattern (with a trailing `%` the caller adds), so a project name/branch
    containing `%` or `_` can't widen the match to an unrelated database."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def expected_db_names(config: "ProjectConfig", branches: list[str]) -> set[str]:
    """Every DB name a currently live worktree of this project is entitled
    to own: the main workspace DB per branch, plus one `<main>{suffix}`
    sibling per `[tool.pgdevkit].extra_db_suffixes` entry."""
    names: set[str] = set()
    for branch in branches:
        main_db = workspace_db_name(config.name, branch)
        names.add(main_db)
        names.update(f"{main_db}{suffix}" for suffix in config.extra_db_suffixes)
    return names
