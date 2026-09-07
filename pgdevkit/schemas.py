"""Schema-membership filtering, composable with (but independent of) the
`-- area:` tag filtering in `areas.py`.

Unlike area, which is an explicit opt-in tag, a file's schema membership is
derived by parsing its SQL: every schema-qualified (or default-schema, when
unqualified) table/view/function/index/schema reference across every
statement in the file, DDL or DML alike.

A file whose schema(s) can't be determined -- content sqlglot can't parse at
all, or with no table/schema reference in it (e.g. a DO block touching no
table) -- is treated the same as an untagged file for `-- area:`: `only`
filters always keep it, `exclude` filters never drop it. Being unable to
prove a file belongs to an excluded schema is not the same as proving it
doesn't, so this errs toward keeping the file in scope rather than silently
dropping it.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

from .dialect import Dialect, POSTGRES, SYSTEM_SCHEMAS

_CREATE_SCHEMA_RE = re.compile(
    r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:AUTHORIZATION\s+)?\"?(\w+)\"?", re.IGNORECASE
)
_QUALIFIED_REF_RE = re.compile(r"\b(\w+)\.\w+")


def _regex_fallback(sql: str) -> frozenset[str]:
    """Crude schema scan used when sqlglot can't parse `sql` at all. Errs
    toward over-matching (any `schema.name`-shaped token) rather than
    under-matching -- a false positive here only keeps a file in scope for
    one extra schema filter, where a false negative would silently drop it
    from an `only` filter."""
    schemas = set(_CREATE_SCHEMA_RE.findall(sql))
    schemas.update(m.group(1) for m in _QUALIFIED_REF_RE.finditer(sql))
    return frozenset(s for s in schemas if s.lower() not in SYSTEM_SCHEMAS)


def sql_schemas(content: str, dialect: Dialect = POSTGRES) -> frozenset[str]:
    """Schema names referenced anywhere in `content`: every table/view/
    function/index reference's schema (its `dialect.default_schema` when
    unqualified), plus any `CREATE SCHEMA name`. Falls back to a regex scan
    when sqlglot can't parse the content, or finds no reference at all."""
    try:
        exprs = sqlglot.parse(content, dialect=dialect.sqlglot_name, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:  # noqa: BLE001
        return _regex_fallback(content)

    schemas: set[str] = set()
    for e in exprs:
        if e is None:
            continue
        for t in e.find_all(exp.Table):
            # T-SQL's `EXEC('...dynamic sql...')` parses as a Table subquery
            # whose "name" is the whole literal string, not a real
            # identifier -- e.g. a quoted "CREATE SCHEMA app". Guard against
            # treating that as a schema-qualified (or default-schema) table
            # reference by requiring a *non-empty* name to actually look like
            # one -- but still accept an empty name, which is the legitimate
            # shape sqlglot gives a bare `CREATE SCHEMA x` (whose schema
            # name ends up in `db`, not `this`).
            if t.name and not re.fullmatch(r"\w+", t.name):
                continue
            db_node = t.args.get("db")
            name = db_node.name if db_node else dialect.default_schema
            if name.lower() not in SYSTEM_SCHEMAS:
                schemas.add(name)

    if not schemas:
        return _regex_fallback(content)
    return frozenset(schemas)


def file_schemas(path: Path, dialect: Dialect = POSTGRES) -> frozenset[str]:
    """Schema names referenced in the file at `path`."""
    return sql_schemas(path.read_text(encoding="utf-8"), dialect)


def schema_allowed(
    schemas: frozenset[str],
    *,
    only: frozenset[str] | None = None,
    exclude: frozenset[str] | None = None,
) -> bool:
    """Whether a file that references `schemas` passes an `only`/`exclude` filter."""
    if exclude and schemas & exclude:
        return False
    if only and schemas and not (schemas & only):
        return False
    return True


def filter_by_schema(
    paths: list[Path],
    *,
    only: frozenset[str] | None = None,
    exclude: frozenset[str] | None = None,
    dialect: Dialect = POSTGRES,
) -> list[Path]:
    """`paths` restricted by an `only`/`exclude` schema filter. Returns `paths`
    unchanged (no file reads) when neither filter is set."""
    if not only and not exclude:
        return paths
    return [p for p in paths if schema_allowed(file_schemas(p, dialect), only=only, exclude=exclude)]
