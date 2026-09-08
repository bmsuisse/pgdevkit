"""Optional `<name>.<env>.sql` filename convention: a file whose dot-segment
immediately before `.sql` names a deployment environment (e.g.
`grants.prod.sql`, `seed.staging.sql`) is only in scope when the caller is
targeting that same environment. A plain `<name>.sql` file (no such segment)
is untagged/common and is always in scope, regardless of which environment is
requested — mirroring the untagged-file rule for `-- area:` tags in
areas.py.

This generalizes the older, hardcoded `.prod.sql` convention (still the usual
name for a production-only file — grants, real user accounts — that
`pgdb testdb` should never touch); any string can now be used as an
environment name.

`.init.sql` (one-time setup, see docs/database-layout.md) is reserved and is
never interpreted as an environment tag.
"""

from __future__ import annotations

from pathlib import Path

_RESERVED_SQL_SUFFIXES = {"init"}


def file_env(path: Path) -> str | None:
    """The environment tag from `path`'s name, or None if it's untagged (or
    the suffix is a reserved, non-env one like `.init.sql`). Only `.sql`
    files can carry a tag."""
    if path.suffix != ".sql":
        return None
    stem = path.stem
    base, dot, suffix = stem.rpartition(".")
    if not dot or suffix in _RESERVED_SQL_SUFFIXES:
        return None
    return suffix


def env_allowed(path: Path, env: str | None) -> bool:
    """Whether `path` is in scope for `env`. `env=None` means no environment
    filtering was requested, so every file (tagged or not) is in scope."""
    if env is None:
        return True
    tag = file_env(path)
    return tag is None or tag == env


def strip_env_suffix(path: Path) -> str:
    """`path.stem` with a trailing `.<tag>` removed, so a tagged file
    resolves to the same logical name as its untagged counterpart would
    (e.g. `grants.prod.sql` -> "grants", same as `grants.sql`)."""
    tag = file_env(path)
    if tag is None:
        return path.stem
    return path.stem[: -(len(tag) + 1)]
