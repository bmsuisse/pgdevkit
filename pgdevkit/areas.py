"""Optional `-- area: NAME[, NAME...]` tag recognized in the leading comment
block of a migration file or a `database/` code file (blank lines and `--`
comments at the very top, stopping at the first real statement — like a file
header). A file may declare more than one area, either as a comma-separated
list on one line or across several `-- area:` lines (the areas union).

A file with no such directive is "untagged" and is treated as shared/common:
`only` filters always keep untagged files, and `exclude` filters never drop
them — only a file that explicitly declares an excluded area is dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

_AREA_LINE = re.compile(r"^\s*--\s*area\s*:\s*(.+?)\s*$", re.IGNORECASE)


def parse_areas(content: str) -> frozenset[str]:
    """Area names declared in `content`'s leading comment block."""
    areas: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            break
        m = _AREA_LINE.match(stripped)
        if m:
            areas.update(a.strip() for a in m.group(1).split(",") if a.strip())
    return frozenset(areas)


def file_areas(path: Path) -> frozenset[str]:
    """Area names declared in the file at `path`."""
    return parse_areas(path.read_text(encoding="utf-8"))


def area_allowed(
    areas: frozenset[str],
    *,
    only: frozenset[str] | None = None,
    exclude: frozenset[str] | None = None,
) -> bool:
    """Whether a file that declares `areas` passes an `only`/`exclude` filter."""
    if exclude and areas & exclude:
        return False
    if only and areas and not (areas & only):
        return False
    return True


def filter_by_area(
    paths: list[Path],
    *,
    only: frozenset[str] | None = None,
    exclude: frozenset[str] | None = None,
) -> list[Path]:
    """`paths` restricted by an `only`/`exclude` area filter. Returns `paths`
    unchanged (no file reads) when neither filter is set."""
    if not only and not exclude:
        return paths
    return [p for p in paths if area_allowed(file_areas(p), only=only, exclude=exclude)]
