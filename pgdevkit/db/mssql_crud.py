from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Optional, Sequence, Type, TypeVar

from .mssql_sql import ident, json_encode_values, qualified
from .model import TableModel

T = TypeVar("T", bound=TableModel)

# `con` below is an mssql-python (github.com/microsoft/mssql-python)
# connection, but typed as `Any` rather than `mssql_python.Connection` so
# this module -- and, importantly, the pure `_build_*` query builders
# below, which have no driver dependency at all -- stays importable (and
# unit-testable) without the `mssql` extra installed. mssql-python bundles
# its own ODBC driver, so unlike pyodbc it needs no system driver install;
# its Connection/Cursor API otherwise mirrors pyodbc's (cursor(), execute(),
# executemany(), fetchone()/fetchall(), qmark `?` placeholders via a
# positional params list), which is what `_execute_returning`/`_execute_many`
# below rely on. `complex_helper` is likewise typed loosely: MSSQL has no
# ComplexHelper equivalent (see backends/mssql.py), so every caller on this
# backend passes/receives None here -- the parameter exists purely for
# signature symmetry with db/crud.py's `pg_*` functions.


def _build_retrieve(table_name: tuple[str, str], pks: dict) -> tuple[str, list]:
    where = " AND ".join(f"{ident(k)} = ?" for k in pks)
    sql = f"SELECT * FROM {qualified(*table_name)} WHERE {where}"
    return sql, list(pks.values())


def _build_retrieve_many(table_name: tuple[str, str], filters: dict) -> tuple[str, list]:
    if not filters:
        return f"SELECT * FROM {qualified(*table_name)}", []
    where = " AND ".join(f"{ident(k)} = ?" for k in filters)
    sql = f"SELECT * FROM {qualified(*table_name)} WHERE {where}"
    return sql, list(filters.values())


def _build_insert(table_name: tuple[str, str], data: dict) -> tuple[str, list]:
    fields = list(data)
    cols = ", ".join(ident(k) for k in fields)
    placeholders = ", ".join("?" for _ in fields)
    sql = f"INSERT INTO {qualified(*table_name)} ({cols}) OUTPUT INSERTED.* VALUES ({placeholders})"
    return sql, [data[k] for k in fields]


def _build_insert_many(table_name: tuple[str, str], fields: Sequence[str]) -> str:
    cols = ", ".join(ident(k) for k in fields)
    placeholders = ", ".join("?" for _ in fields)
    return f"INSERT INTO {qualified(*table_name)} ({cols}) VALUES ({placeholders})"


def _build_update(table_name: tuple[str, str], data: dict, primary_keys: Sequence[str]) -> tuple[str, list]:
    set_fields = [k for k in data if k not in primary_keys]
    set_clause = ", ".join(f"{ident(k)} = ?" for k in set_fields)
    where_clause = " AND ".join(f"{ident(pk)} = ?" for pk in primary_keys)
    sql = f"UPDATE {qualified(*table_name)} SET {set_clause} OUTPUT INSERTED.* WHERE {where_clause}"
    params = [data[k] for k in set_fields] + [data[pk] for pk in primary_keys]
    return sql, params


def _build_update_many(table_name: tuple[str, str], fields: Sequence[str], primary_keys: Sequence[str]) -> str:
    set_clause = ", ".join(f"{ident(k)} = ?" for k in fields if k not in primary_keys)
    where_clause = " AND ".join(f"t.{ident(pk)} = ?" for pk in primary_keys)
    return f"UPDATE {qualified(*table_name)} SET {set_clause} WHERE {where_clause}"


def _build_upsert_merge(table_name: tuple[str, str], data: dict, primary_keys: Sequence[str]) -> tuple[str, list]:
    """MERGE INTO ... USING (SELECT ? AS col, ...) AS s ON pk = pk WHEN
    MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ... OUTPUT
    INSERTED.* -- the MSSQL replacement for Postgres's `INSERT ... ON
    CONFLICT ... DO UPDATE ... EXCLUDED.col`. Structurally different from
    an upsert-by-string-swap: MERGE is its own statement shape."""
    fields = list(data)
    src_cols = ", ".join(f"? AS {ident(k)}" for k in fields)
    on_clause = " AND ".join(f"t.{ident(pk)} = s.{ident(pk)}" for pk in primary_keys)
    update_fields = [k for k in fields if k not in primary_keys]
    insert_cols = ", ".join(ident(k) for k in fields)
    insert_vals = ", ".join(f"s.{ident(k)}" for k in fields)

    sql = f"MERGE INTO {qualified(*table_name)} AS t USING (SELECT {src_cols}) AS s ON {on_clause} "
    if update_fields:
        update_clause = ", ".join(f"t.{ident(k)} = s.{ident(k)}" for k in update_fields)
        sql += f"WHEN MATCHED THEN UPDATE SET {update_clause} "
    sql += f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals}) OUTPUT INSERTED.*;"
    return sql, [data[k] for k in fields]


def _build_delete(table_name: tuple[str, str], data: dict) -> tuple[str, list]:
    where_clause = " AND ".join(f"{ident(k)} = ?" for k in data)
    sql = f"DELETE FROM {qualified(*table_name)} OUTPUT DELETED.* WHERE {where_clause}"
    return sql, list(data.values())


async def _execute_returning(con: Any, sql: str, params: list) -> dict | None:
    def _run() -> dict | None:
        cur = con.cursor()
        try:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row is not None else None
        finally:
            cur.close()

    return await asyncio.to_thread(_run)


async def _execute_many(con: Any, sql: str, param_rows: Sequence[Sequence]) -> None:
    def _run() -> None:
        cur = con.cursor()
        try:
            cur.executemany(sql, list(param_rows))
        finally:
            cur.close()

    await asyncio.to_thread(_run)


async def mssql_retrieve(
    con: Any,
    data_type: Type[T],
    pks: dict,
    *,
    complex_helper: Any | None = None,
) -> T | None:
    """Fetch a single row by primary key(s). MSSQL has no ComplexHelper
    equivalent (see backends/mssql.py) -- `complex_helper` exists only for
    signature symmetry with `db.crud.pg_retrieve` and is otherwise unused."""
    sql, params = _build_retrieve(data_type.get_table_name(), pks)
    row = await _execute_returning(con, sql, params)
    return data_type(**row) if row else None


async def mssql_retrieve_many(
    con: Any,
    data_type: Type[T],
    filters: dict,
    *,
    from_dict: Optional[Callable[[Mapping], T]] = None,
    complex_helper: Any | None = None,
) -> Sequence[T]:
    """Fetch multiple rows matching all filter key=value pairs."""
    sql, params = _build_retrieve_many(data_type.get_table_name(), filters)

    def _run() -> list[dict]:
        cur = con.cursor()
        try:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    rows = await asyncio.to_thread(_run)
    fn = from_dict or (lambda d: data_type(**d))
    return [fn(r) for r in rows]


async def mssql_insert(
    con: Any,
    table_name: tuple[str, str],
    data: dict,
    *,
    complex_helper: Any | None = None,
) -> dict[str, Any]:
    """Insert one row and return the full row (`OUTPUT INSERTED.*`)."""
    data = json_encode_values(data)
    sql, params = _build_insert(table_name, data)
    row = await _execute_returning(con, sql, params)
    assert row is not None
    return row


async def mssql_insert_many(
    con: Any,
    table_name: tuple[str, str],
    data: Sequence[dict],
    *,
    complex_helper: Any | None = None,
) -> None:
    """Batch insert -- no OUTPUT, one round-trip via executemany."""
    if not data:
        return
    data = [json_encode_values(row) for row in data]
    fields = list(data[0])
    sql = _build_insert_many(table_name, fields)
    await _execute_many(con, sql, [[row[k] for k in fields] for row in data])


async def mssql_update_dict(
    con: Any,
    table_name: tuple[str, str],
    data: dict,
    primary_keys: Sequence[str],
) -> dict | None:
    """Update a row identified by primary_keys. Returns the updated row."""
    data = json_encode_values(data)
    sql, params = _build_update(table_name, data, primary_keys)
    return await _execute_returning(con, sql, params)


async def mssql_update(con: Any, data: T, data_type: type[T]) -> dict | None:
    """Update a typed model instance."""
    return await mssql_update_dict(con, data_type.get_table_name(), data.model_dump(), data_type.get_primary_key())


async def mssql_upsert_dict(
    con: Any,
    table_name: tuple[str, str],
    data: dict,
    primary_keys: Sequence[str],
    *,
    complex_helper: Any | None = None,
) -> dict:
    """MERGE-based upsert, returns the row as a dict."""
    data = json_encode_values(data)
    sql, params = _build_upsert_merge(table_name, data, primary_keys)
    row = await _execute_returning(con, sql, params)
    assert row is not None
    return row


async def mssql_upsert(
    con: Any, data: T, data_type: type[T], *, complex_helper: Any | None = None
) -> dict:
    """Upsert a typed model instance."""
    return await mssql_upsert_dict(con, data_type.get_table_name(), data.model_dump(), data_type.get_primary_key())


async def mssql_upsert_many_dict(
    con: Any,
    table_name: tuple[str, str],
    data: Sequence[dict],
    primary_keys: Sequence[str],
    *,
    must_exist: bool = False,
    complex_helper: Any | None = None,
) -> None:
    """Batch upsert.

    `must_exist=True` switches to a plain UPDATE (no INSERT) matched on
    `primary_keys` -- for callers that only ever update pre-existing rows
    and want a missing row to be a silent no-op rather than create one."""
    if not data:
        return
    data = [json_encode_values(row) for row in data]
    fields = list(data[0])
    if must_exist:
        sql = _build_update_many(table_name, fields, primary_keys)
        non_pk = [k for k in fields if k not in primary_keys]
        rows = [[row[k] for k in non_pk] + [row[pk] for pk in primary_keys] for row in data]
        await _execute_many(con, sql, rows)
    else:
        # MERGE's USING clause is per-row here (first cut) -- a set-based
        # multi-row MERGE ... USING (VALUES (...), (...)) is more efficient
        # but adds real complexity (a dynamic column-count VALUES list);
        # row-by-row via executemany matches how the must_exist branch above
        # already works.
        for row in data:
            sql, params = _build_upsert_merge(table_name, row, primary_keys)

            def _run() -> None:
                cur = con.cursor()
                try:
                    cur.execute(sql, params)
                finally:
                    cur.close()

            await asyncio.to_thread(_run)


async def mssql_upsert_many(
    con: Any, data: Sequence[T], data_type: type[T], *, complex_helper: Any | None = None
) -> None:
    await mssql_upsert_many_dict(con, data_type.get_table_name(), [d.model_dump() for d in data], data_type.get_primary_key())


async def mssql_delete_dict(con: Any, table_name: tuple[str, str], data: dict) -> dict | None:
    """Delete by arbitrary key dict, returns the deleted row."""
    sql, params = _build_delete(table_name, data)
    return await _execute_returning(con, sql, params)


async def mssql_delete(con: Any, data: T, data_type: type[T]) -> T | None:
    """Delete a typed model instance by its primary key(s)."""
    pk_dict = {pk: getattr(data, pk) for pk in data_type.get_primary_key()}
    row = await mssql_delete_dict(con, data_type.get_table_name(), pk_dict)
    return data_type.model_validate(row) if row else None
