from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, cast

import psycopg
import sqlglot
import sqlglot.expressions as exp
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier, Placeholder

from ..db.complex_types import ComplexHelper

logger = logging.getLogger(__name__)
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# The `database/` folder convention (layer dirs, object-type subfolders and
# their apply order, file-naming rules) is documented in
# docs/database-layout.md — keep that table in sync with this dict.
_TYPE_ORDER = {
    "schema": 1,
    "types": 2,
    "tables": 3,
    "scalar_functions": 4,
    "functions": 5,
    "views": 6,
    "table_functions": 7,
    "procedures": 8,
    "permissions": 100,
    "indexes": 101,
}


def _get_type_order(path: Path) -> int:
    filename = re.sub(r"^\d+(\.\d+)?", "", path.name).removeprefix("_").removesuffix(".sql")
    if filename in _TYPE_ORDER:
        return _TYPE_ORDER[filename]
    if path.parent.name in _TYPE_ORDER:
        return _TYPE_ORDER[path.parent.name]
    raise ValueError(f"Unknown SQL type for {path.name} in {path.parent.name}")


def _strip_layer_prefix(schema_name: str) -> str:
    """Strip a leading numeric layer prefix (e.g. "1_dim" -> "dim") so it
    matches the unprefixed schema name used in SQL identifiers."""
    if re.match(r"^\d+_", schema_name):
        return schema_name.split("_", 1)[1]
    return schema_name


_SCHEMA_QUALIFIED_TYPES = {
    "types",
    "tables",
    "scalar_functions",
    "functions",
    "views",
    "table_functions",
    "procedures",
}


def _get_sql_deps(sql: str) -> set[str]:
    exprs = sqlglot.parse(sql, dialect="postgres")
    deps: set[str] = set()
    for e in exprs:
        if e is None:
            continue
        for t in e.find_all(exp.Table):
            if t.args.get("this") is not None and t.args.get("db") is not None:
                deps.add(str(t))
    return deps


def _iter_sql_files(database_dir: Path):
    """Yield (Path, sql_content) pairs in dependency-safe execution order."""
    files: list[Path] = []
    for root, _, dbfiles in os.walk(database_dir):
        if "_migration_scripts" in root or "migrations" in root:
            continue
        for file in dbfiles:
            if file in ("all.sql", "100_permissions.sql"):
                continue
            if file.endswith(".sql") and ".prod" not in file:
                files.append(Path(root) / file)

    delivered: set[str] = set()
    delayed: list[tuple[str | None, Path, str]] = []
    all_declared: set[str] = set()

    for file in sorted(files, key=lambda p: (_get_type_order(p), p.name)):
        content = file.read_text(encoding="utf-8")
        deps = _get_sql_deps(content)
        if file.parent.name in _SCHEMA_QUALIFIED_TYPES:
            schema = _strip_layer_prefix(file.parent.parent.name)
            full_name = f"{schema}.{file.stem}"
            deps.discard(full_name)  # the file's own CREATE target is not a real dependency
            all_declared.add(full_name)
            if not deps or all(d in delivered for d in deps):
                delivered.add(full_name)
                yield file, content
            else:
                delayed.append((full_name, file, content))
            continue
        if not deps or all(d in delivered for d in deps):
            yield file, content
        else:
            delayed.append((None, file, content))

    while delayed:
        progressed = False
        for i in range(len(delayed) - 1, -1, -1):
            declared_name, file, content = delayed[i]
            deps = _get_sql_deps(content)
            if declared_name:
                deps.discard(declared_name)
            if all(d in delivered or d not in all_declared for d in deps):
                if declared_name:
                    delivered.add(declared_name)
                yield file, content
                delayed.pop(i)
                progressed = True
        if not progressed:
            raise ValueError(f"Circular or missing SQL dependencies: {[f[1] for f in delayed]}")


async def _insert_test_data(
    json_file: Path,
    table: str,
    force_reset: bool,
    con: psycopg.AsyncConnection,
    complex_helper: ComplexHelper,
) -> None:
    if not json_file.exists():
        return
    rows: list[dict[str, Any]] = json.loads(json_file.read_text(encoding="utf-8"))
    if not rows:
        return

    schema, table_name = table.split(".")
    async with con.cursor(row_factory=dict_row) as cur:
        if not force_reset:
            await cur.execute(SQL("SELECT count(*) AS cnt FROM {t}").format(t=Identifier(schema, table_name)))
            row = await cur.fetchone()
            if row and row["cnt"] == len(rows):
                return

        complex_types = await complex_helper.load_all_complex_types((schema, table_name))
        # Silently drop JSON keys that don't correspond to a real column —
        # e.g. a stale fixture left over from a since-renamed/removed column.
        col_names = [c for c in rows[0] if c in complex_types]
        for row in rows:
            for col in col_names:
                info = complex_types.get(col)
                if info is not None and isinstance(row[col], (dict, list)):
                    # composite/enum/JSONB: needs psycopg-registered-type
                    # conversion. Anything else (plain scalars, and native
                    # Postgres arrays like text[], which aren't "complex" —
                    # psycopg already adapts a Python list to those natively)
                    # is left untouched.
                    row[col] = await complex_helper.recursive_convert(row[col], info, con)

        await cur.execute(SQL("DELETE FROM {t}").format(t=Identifier(schema, table_name)))
        insert_sql = SQL("INSERT INTO {t} ({cols}) VALUES ({vals})").format(
            t=Identifier(schema, table_name),
            cols=SQL(", ").join(Identifier(c) for c in col_names),
            vals=SQL(", ").join(Placeholder(c) for c in col_names),
        )
        await cur.executemany(insert_sql, rows)


def _is_additive_migration(sql: str) -> bool:
    """Only pure-additive migrations (ADD COLUMN/CREATE ... IF NOT EXISTS,
    etc.) are safe to (re-)apply against a schema that already reflects
    later migrations — skip anything that alters an existing column or
    changes ownership."""
    normalized = sql.upper()
    has_alter_column = "ALTER COLUMN" in normalized and "ADD COLUMN" not in normalized
    has_owner_change = "OWNER TO" in normalized
    return not (has_alter_column or has_owner_change)


def _iter_migration_files(database_dir: Path):
    """Yield every `*.sql` file in any `migrations/` subdirectory, in
    filename order (the project's naming convention — numeric or date
    prefixes — sorts chronologically)."""
    for root, _, files in os.walk(database_dir):
        if Path(root).name != "migrations":
            continue
        for file in sorted(files):
            if file.endswith(".sql"):
                yield Path(root) / file


async def apply_schema(
    con: psycopg.AsyncConnection,
    database_dir: Path,
    extensions: tuple[str, ...] = (),
    force_reset: bool = False,
) -> None:
    """Apply every .sql file under database_dir (in dependency-safe order),
    seed any matching .test_data.json files, then apply purely-additive
    `migrations/*.sql` files (see `_is_additive_migration`) so a schema that
    ships changes via migration files rather than editing the base object
    files stays in sync. Safe to call repeatedly."""
    await con.set_autocommit(True)
    for extension in extensions:
        await con.execute(SQL("CREATE EXTENSION IF NOT EXISTS {e}").format(e=Identifier(extension)))

    complex_helper = ComplexHelper(con)

    async def _apply(file: Path, sql: str) -> None:
        await con.execute(cast(Any, sql))
        json_file = file.with_suffix(".test_data.json")
        if json_file.exists():
            schema_name = _strip_layer_prefix(file.parent.parent.name)
            await _insert_test_data(json_file, f"{schema_name}.{file.stem}", force_reset, con, complex_helper)

    failures: list[tuple[Path, str]] = []
    for file, sql in _iter_sql_files(database_dir):
        try:
            await _apply(file, sql)
        except Exception as e:  # noqa: BLE001
            logger.warning("Error executing %s (will retry): %s", file, e)
            failures.append((file, sql))

    for file, sql in failures:
        await _apply(file, sql)

    for migration_file in _iter_migration_files(database_dir):
        content = migration_file.read_text(encoding="utf-8")
        if not _is_additive_migration(content):
            continue
        try:
            await con.execute(cast(Any, content))
            logger.info("Applied migration %s", migration_file.name)
        except Exception as e:  # noqa: BLE001
            logger.debug("Migration %s skipped (likely already applied): %s", migration_file.name, e)
