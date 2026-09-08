"""Small text-level SQL helpers shared by modules that can't import each
other directly (`migrate.py` imports `schemas.py`, so `schemas.py` can't
import back from `migrate.py`)."""

from __future__ import annotations

import re


def strip_line_comments(sql: str) -> str:
    """Drop '--' line comments, respecting string literals and $$...$$ blocks."""
    buf: list[str] = []
    i = 0
    in_string = False
    in_line_comment = False
    dollar_tag: str | None = None

    while i < len(sql):
        c = sql[i]
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                buf.append(c)
        elif dollar_tag is not None:
            buf.append(c)
            if c == "$" and sql[i:i + len(dollar_tag)] == dollar_tag:
                buf.extend(list(dollar_tag[1:]))
                i += len(dollar_tag)
                dollar_tag = None
                continue
        elif in_string:
            if c == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append(c)
                buf.append(sql[i + 1])
                i += 2
                continue
            elif c == "'":
                in_string = False
            buf.append(c)
        elif c == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            in_line_comment = True
        elif c == "$":
            m = re.match(r"\$([A-Za-z0-9_]*)\$", sql[i:])
            if m:
                dollar_tag = m.group(0)
                buf.extend(list(dollar_tag))
                i += len(dollar_tag)
                continue
            buf.append(c)
        elif c == "'":
            in_string = True
            buf.append(c)
        else:
            buf.append(c)
        i += 1
    return "".join(buf)
