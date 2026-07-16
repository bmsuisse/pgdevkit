from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

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
