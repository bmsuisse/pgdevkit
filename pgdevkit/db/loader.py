from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import LiteralString, cast


class SqlLoader:
    """Loads and caches SQL text from `{root}/<topic>/<name>.sql`."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._load = lru_cache(maxsize=None)(self._read)

    def _read(self, topic: str, name: str) -> LiteralString:
        return cast(LiteralString, (self._root / topic / f"{name}.sql").read_text(encoding="utf-8"))

    def load_sql(self, topic: str, name: str) -> LiteralString:
        return self._load(topic, name)
