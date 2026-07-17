from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence, Type, TypeVar

from psycopg.connection_async import AsyncConnection
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composable, Identifier, Placeholder
from pydantic import BaseModel

from .complex_types import ComplexHelper
from .model import PostgresTableModel

T = TypeVar("T", bound=PostgresTableModel)


async def _select_list(
    con: AsyncConnection, table_name: tuple[str, str], complex_helper: ComplexHelper | None
) -> Composable:
    """Column list for a SELECT, wrapping composite/enum/JSONB columns in
    `to_jsonb(...)` so psycopg gets back plain Python values. Falls back to
    `SELECT *` (no extra query) when no ComplexHelper is given."""
    if complex_helper is None:
        return SQL("*")
    complex_types = await complex_helper.load_all_complex_types(table_name, include_generated=True)
    if not complex_types:
        return SQL("*")
    parts = [
        SQL("to_jsonb({col}) as {col}").format(col=Identifier(col)) if info is not None else Identifier(col)
        for col, info in complex_types.items()
    ]
    return SQL(", ").join(parts)


async def _convert_complex_values(
    con: AsyncConnection,
    table_name: tuple[str, str],
    data: dict,
    complex_helper: ComplexHelper | None,
) -> dict:
    """Convert dict/list values destined for composite/enum columns into the
    psycopg-registered types those columns need. A no-op (no extra query)
    unless `data` actually contains dict/list values."""
    if complex_helper is None:
        return data
    candidate_keys = [k for k, v in data.items() if isinstance(v, (dict, list))]
    if not candidate_keys:
        return data
    converted = dict(data)
    for k in candidate_keys:
        info = await complex_helper.load_complex_type(table_name, k)
        if info is not None:
            converted[k] = await complex_helper.recursive_convert(data[k], info, con)
    return converted


async def _convert_complex_values_many(
    con: AsyncConnection,
    table_name: tuple[str, str],
    rows: Sequence[dict],
    complex_helper: ComplexHelper | None,
) -> Sequence[dict]:
    if complex_helper is None or not rows:
        return rows
    candidate_keys = {k for row in rows for k, v in row.items() if isinstance(v, (dict, list))}
    if not candidate_keys:
        return rows
    infos = {k: await complex_helper.load_complex_type(table_name, k) for k in candidate_keys}
    complex_keys = {k for k, info in infos.items() if info is not None}
    if not complex_keys:
        return rows
    converted_rows = []
    for row in rows:
        new_row = dict(row)
        for k in complex_keys:
            if isinstance(new_row.get(k), (dict, list)):
                new_row[k] = await complex_helper.recursive_convert(new_row[k], infos[k], con)
        converted_rows.append(new_row)
    return converted_rows


async def pg_retrieve(
    con: AsyncConnection,
    data_type: Type[T],
    pks: dict,
    *,
    complex_helper: ComplexHelper | None = None,
) -> T | None:
    """Fetch a single row by primary key(s).

    Pass `complex_helper` (a `ComplexHelper`, optionally configured with
    `normalizers`) when the table has composite/enum columns; omitted, this
    behaves exactly like a plain `SELECT *`."""
    async with con.cursor(row_factory=dict_row) as cur:
        table_name = data_type.get_table_name()
        select_cols = await _select_list(con, table_name, complex_helper)
        query = SQL("SELECT {cols} FROM {tbl} WHERE {where}").format(
            cols=select_cols,
            tbl=Identifier(*table_name),
            where=SQL(" AND ").join(SQL("{col} = {val}").format(col=Identifier(pk), val=Placeholder(pk)) for pk in pks),
        )
        await cur.execute(query, pks)
        row = await cur.fetchone()
    return data_type(**row) if row else None


async def pg_retrieve_many(
    con: AsyncConnection,
    data_type: Type[T],
    filters: dict,
    *,
    from_dict: Optional[Callable[[Mapping], T]] = None,
    complex_helper: ComplexHelper | None = None,
) -> Sequence[T]:
    """Fetch multiple rows matching all filter key=value pairs."""
    async with con.cursor(row_factory=dict_row) as cur:
        table_name = data_type.get_table_name()
        select_cols = await _select_list(con, table_name, complex_helper)
        if filters:
            query = SQL("SELECT {cols} FROM {tbl} WHERE {where}").format(
                cols=select_cols,
                tbl=Identifier(*table_name),
                where=SQL(" AND ").join(
                    SQL("{col} = {val}").format(col=Identifier(k), val=Placeholder(k)) for k in filters
                ),
            )
        else:
            query = SQL("SELECT {cols} FROM {tbl}").format(cols=select_cols, tbl=Identifier(*table_name))
        await cur.execute(query, filters)
        rows = await cur.fetchall()
    fn = from_dict or (lambda d: data_type(**d))
    return [fn(r) for r in rows]


async def pg_insert(
    con: AsyncConnection,
    table_name: tuple[str, str],
    data: dict,
    *,
    complex_helper: ComplexHelper | None = None,
) -> dict[str, Any]:
    """Insert one row and return the full row (RETURNING *)."""
    data = await _convert_complex_values(con, table_name, data, complex_helper)
    query = SQL("INSERT INTO {tbl} ({cols}) VALUES ({vals}) RETURNING *").format(
        tbl=Identifier(*table_name),
        cols=SQL(", ").join(Identifier(k) for k in data),
        vals=SQL(", ").join(Placeholder(k) for k in data),
    )
    async with con.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, data)
        row = await cur.fetchone()
    assert row is not None
    return row


async def pg_update_dict(
    con: AsyncConnection,
    table_name: tuple[str, str],
    data: dict,
    primary_keys: Sequence[str],
) -> Any | None:
    """Update a row identified by primary_keys. Returns the raw row tuple."""
    set_parts = [
        SQL("{col} = {val}").format(col=Identifier(k), val=Placeholder(k)) for k in data if k not in primary_keys
    ]
    where_parts = [SQL("{col} = {val}").format(col=Identifier(pk), val=Placeholder(pk)) for pk in primary_keys]
    query = SQL("UPDATE {tbl} SET {sets} WHERE {where} RETURNING *").format(
        tbl=Identifier(*table_name),
        sets=SQL(", ").join(set_parts),
        where=SQL(" AND ").join(where_parts),
    )
    async with con.cursor() as cur:
        await cur.execute(query, data)
        return await cur.fetchone()


async def pg_update(con: AsyncConnection, data: T, data_type: type[T]) -> Any | None:
    """Update a typed model instance."""
    return await pg_update_dict(con, data_type.get_table_name(), data.model_dump(), data_type.get_primary_key())


async def pg_upsert_dict(
    con: AsyncConnection,
    table_name: tuple[str, str],
    data: dict,
    primary_keys: Sequence[str],
    *,
    complex_helper: ComplexHelper | None = None,
) -> dict:
    """INSERT ... ON CONFLICT ... DO UPDATE, returns the row as a dict."""
    data = await _convert_complex_values(con, table_name, data, complex_helper)
    fields = list(data)
    updates = [SQL("{col} = EXCLUDED.{col}").format(col=Identifier(k)) for k in fields]
    query = SQL(
        "INSERT INTO {tbl} ({cols}) VALUES ({vals}) ON CONFLICT ({pks}) DO UPDATE SET {updates} RETURNING *"
    ).format(
        tbl=Identifier(*table_name),
        cols=SQL(", ").join(Identifier(k) for k in fields),
        vals=SQL(", ").join(Placeholder(k) for k in fields),
        pks=SQL(", ").join(Identifier(pk) for pk in primary_keys),
        updates=SQL(", ").join(updates),
    )
    async with con.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, data)
        row = await cur.fetchone()
    assert row is not None
    return row


async def pg_upsert(
    con: AsyncConnection, data: T, data_type: type[T], *, complex_helper: ComplexHelper | None = None
) -> dict:
    """Upsert a typed model instance."""
    return await pg_upsert_dict(
        con, data_type.get_table_name(), data.model_dump(), data_type.get_primary_key(), complex_helper=complex_helper
    )


async def pg_upsert_many_dict(
    con: AsyncConnection,
    table_name: tuple[str, str],
    data: Sequence[dict],
    primary_keys: Sequence[str],
    *,
    must_exist: bool = False,
    complex_helper: ComplexHelper | None = None,
) -> None:
    """Batch upsert — one round-trip via executemany.

    `must_exist=True` switches to a plain UPDATE (no INSERT) matched on
    `primary_keys` — for callers that only ever update pre-existing rows and
    want a missing row to be a silent no-op rather than create one."""
    if not data:
        return
    data = await _convert_complex_values_many(con, table_name, data, complex_helper)
    fields = list(data[0])
    if must_exist:
        update_assignments = [
            SQL("{col} = {val}").format(col=Identifier(k), val=Placeholder(k)) for k in fields if k not in primary_keys
        ]
        target_eq = SQL(" AND ").join(
            SQL("t.{col} = {val}").format(col=Identifier(pk), val=Placeholder(pk)) for pk in primary_keys
        )
        query = SQL("UPDATE {tbl} t SET {updates} WHERE {target_eq}").format(
            tbl=Identifier(*table_name),
            updates=SQL(", ").join(update_assignments),
            target_eq=target_eq,
        )
    else:
        updates = [SQL("{col} = EXCLUDED.{col}").format(col=Identifier(k)) for k in fields if k not in primary_keys]
        query = SQL("INSERT INTO {tbl} ({cols}) VALUES ({vals}) ON CONFLICT ({pks}) DO UPDATE SET {updates}").format(
            tbl=Identifier(*table_name),
            cols=SQL(", ").join(Identifier(k) for k in fields),
            vals=SQL(", ").join(Placeholder(k) for k in fields),
            pks=SQL(", ").join(Identifier(pk) for pk in primary_keys),
            updates=SQL(", ").join(updates),
        )
    async with con.cursor() as cur:
        await cur.executemany(query, data)


async def pg_upsert_many(
    con: AsyncConnection, data: Sequence[T], data_type: type[T], *, complex_helper: ComplexHelper | None = None
) -> None:
    await pg_upsert_many_dict(
        con,
        data_type.get_table_name(),
        [d.model_dump() for d in data],
        data_type.get_primary_key(),
        complex_helper=complex_helper,
    )


async def pg_insert_many(
    con: AsyncConnection,
    table_name: tuple[str, str],
    data: Sequence[dict | BaseModel],
    *,
    complex_helper: ComplexHelper | None = None,
) -> None:
    """Batch insert — no RETURNING, one round-trip via executemany."""
    if not data:
        return
    dict_data = [d if isinstance(d, dict) else d.model_dump() for d in data]
    dict_data = await _convert_complex_values_many(con, table_name, dict_data, complex_helper)
    fields = list(dict_data[0])
    query = SQL("INSERT INTO {tbl} ({cols}) VALUES ({vals})").format(
        tbl=Identifier(*table_name),
        cols=SQL(", ").join(Identifier(k) for k in fields),
        vals=SQL(", ").join(Placeholder(k) for k in fields),
    )
    async with con.cursor() as cur:
        await cur.executemany(query, dict_data)


async def pg_delete_dict(con: AsyncConnection, table_name: tuple[str, str], data: dict) -> dict | None:
    """Delete by arbitrary key dict, returns the deleted row."""
    where_parts = [SQL("{col} = {val}").format(col=Identifier(k), val=Placeholder(k)) for k in data]
    query = SQL("DELETE FROM {tbl} WHERE {where} RETURNING *").format(
        tbl=Identifier(*table_name),
        where=SQL(" AND ").join(where_parts),
    )
    async with con.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, data)
        return await cur.fetchone()


async def pg_delete(con: AsyncConnection, data: T, data_type: type[T]) -> T | None:
    """Delete a typed model instance by its primary key(s)."""
    pk_dict = {pk: getattr(data, pk) for pk in data_type.get_primary_key()}
    row = await pg_delete_dict(con, data_type.get_table_name(), pk_dict)
    return data_type.model_validate(row) if row else None
