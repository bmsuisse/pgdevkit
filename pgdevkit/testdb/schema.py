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

logger = logging.getLogger(__name__)
logging.getLogger("sqlglot").setLevel(logging.ERROR)

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


def _get_sql_deps(sql: str) -> tuple[set[str], set[str]]:
    exprs = sqlglot.parse(sql, dialect="postgres")
    deps: set[str] = set()
    declares: set[str] = set()
    for e in exprs:
        if e is None:
            continue
        for t in e.find_all(exp.Create):
            if t.args.get("this") is not None and t.args.get("db") is not None:
                declares.add(str(t))
        for t in e.find_all(exp.Table):
            if t.args.get("this") is not None and t.args.get("db") is not None:
                deps.add(str(t))
    return declares, deps


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

    delivered_tables: set[str] = set()
    delayed: list[tuple[str | None, Path, str]] = []
    all_declared: set[str] = set()

    for file in sorted(files, key=lambda p: (_get_type_order(p), p.name)):
        content = file.read_text(encoding="utf-8")
        declares, deps = _get_sql_deps(content)
        if file.parent.parent.name == "tables":
            schema = file.parent.name
            full_name = f"{schema}.{file.stem}"
            declares.add(full_name)
            all_declared.update(declares)
            if not deps or all(d in delivered_tables for d in deps):
                delivered_tables.add(full_name)
                yield file, content
            else:
                delayed.append((full_name, file, content))
            continue
        if not deps or all(d in delivered_tables for d in deps):
            yield file, content
        else:
            delayed.append((None, file, content))

    while delayed:
        progressed = False
        for i in range(len(delayed) - 1, -1, -1):
            tbl_name, file, content = delayed[i]
            _, deps = _get_sql_deps(content)
            if all(d in delivered_tables or d not in all_declared for d in deps):
                if tbl_name:
                    delivered_tables.add(tbl_name)
                yield file, content
                delayed.pop(i)
                progressed = True
        if not progressed:
            raise ValueError(f"Circular or missing SQL dependencies: {[f[1] for f in delayed]}")


async def _insert_test_data(
    json_file: Path, table: str, force_reset: bool, con: psycopg.AsyncConnection
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

        col_names = list(rows[0].keys())
        for row in rows:
            for col in col_names:
                if isinstance(row[col], (dict, list)):
                    row[col] = json.dumps(row[col])

        await cur.execute(SQL("DELETE FROM {t}").format(t=Identifier(schema, table_name)))
        insert_sql = SQL("INSERT INTO {t} ({cols}) VALUES ({vals})").format(
            t=Identifier(schema, table_name),
            cols=SQL(", ").join(Identifier(c) for c in col_names),
            vals=SQL(", ").join(Placeholder(c) for c in col_names),
        )
        await cur.executemany(insert_sql, rows)


async def apply_schema(
    con: psycopg.AsyncConnection,
    database_dir: Path,
    extensions: tuple[str, ...] = (),
    force_reset: bool = False,
) -> None:
    """Apply every .sql file under database_dir (in dependency-safe order)
    and seed any matching .test_data.json files. Safe to call repeatedly."""
    await con.set_autocommit(True)
    for extension in extensions:
        await con.execute(SQL("CREATE EXTENSION IF NOT EXISTS {e}").format(e=Identifier(extension)))

    failures: list[tuple[Path, str]] = []
    for file, sql in _iter_sql_files(database_dir):
        try:
            await con.execute(cast(Any, sql))
            json_file = file.with_suffix(".test_data.json")
            if json_file.exists():
                schema_name = file.parent.parent.name
                if re.match(r"^\d+_", schema_name):
                    schema_name = schema_name.split("_", 1)[1]
                await _insert_test_data(json_file, f"{schema_name}.{file.stem}", force_reset, con)
        except Exception as e:  # noqa: BLE001
            logger.warning("Error executing %s (will retry): %s", file, e)
            failures.append((file, sql))

    for file, sql in failures:
        await con.execute(cast(Any, sql))
