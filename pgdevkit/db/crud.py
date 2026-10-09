from __future__ import annotations

from string.templatelib import Interpolation, Template
from typing import Any, Callable, Literal, Mapping, Optional, Sequence, Type, TypeVar

from psycopg.connection_async import AsyncConnection
from psycopg.rows import dict_row
from psycopg.sql import Literal as SqlLiteral
from psycopg.sql import SQL, Composable, Composed, Identifier, Placeholder
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


type OrderBy = str | Sequence[str | tuple[str, Literal["asc", "desc"]]] | Composable


def _order_by_sql(order_by: OrderBy | None) -> Composable | None:
    """` ORDER BY ...` for `pg_retrieve_many(order_by=...)`: column names (quoted as identifiers, optionally
    as `(name, "desc")`), or a `psycopg.sql` composable for anything else (an expression, `NULLS LAST`, ...)."""
    if order_by is None:
        return None
    if isinstance(order_by, Composable):
        return SQL(" ORDER BY {}").format(order_by)
    if isinstance(order_by, tuple) and len(order_by) == 2 and str(order_by[1]).lower() in ("asc", "desc"):
        raise ValueError(f"order_by={order_by!r} would order by two columns; use a list: [{order_by!r}]")
    items = [order_by] if isinstance(order_by, str) else list(order_by)
    parts: list[Composable] = []
    for item in items:
        column, direction = (item, "asc") if isinstance(item, str) else item
        if direction.lower() not in ("asc", "desc"):
            raise ValueError(f"order_by direction must be 'asc' or 'desc', got {direction!r}")
        parts.append(SQL("{} DESC" if direction.lower() == "desc" else "{} ASC").format(Identifier(column)))
    return SQL(" ORDER BY {}").format(SQL(", ").join(parts)) if parts else None


async def pg_retrieve_many(
    con: AsyncConnection,
    data_type: Type[T],
    filters: dict,
    *,
    from_dict: Optional[Callable[[Mapping], T]] = None,
    complex_helper: ComplexHelper | None = None,
    where: Composable | Template | None = None,
    params: Mapping[str, Any] | None = None,
    order_by: OrderBy | None = None,
    limit: int | None = None,
) -> Sequence[T]:
    """Fetch multiple rows matching all filter key=value pairs.

    Beyond those equality `filters` (`{}` for none), the query can be narrowed and shaped with:

    - `where=`: more conditions, ANDed with the filters -- a t-string (`t"price > {minimum} AND name LIKE {pattern}"`,
      values are bound for you; prefer it), or a `psycopg.sql` composable, whose own `sql.Placeholder("name")`s are
      bound from `params=`. In a composable a literal `%` must be written `%%` whenever `filters` or `params` are
      non-empty (psycopg's rule); bind patterns as values instead (`name LIKE {pattern}`).
    - `order_by=`: a column name, a *list* of them (each a name or `(name, "asc" | "desc")`, quoted as
      identifiers; a bare `("name", "desc")` is rejected as ambiguous), or a composable for anything fancier.
    - `limit=`: at most that many rows.

    Without those it behaves exactly as before. For joins or other shapes use `fetch_all` instead."""
    # validate before the select list may cost a catalog round trip
    tail: list[Composable] = []
    if (order := _order_by_sql(order_by)) is not None:
        tail.append(order)
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"limit must be a non-negative integer, got {limit!r}")
        tail.append(SQL(" LIMIT {}").format(SqlLiteral(limit)))
    if isinstance(where, Template) and params:
        raise TypeError("A t-string `where` carries its own values; don't pass `params` with it.")
    table_name = data_type.get_table_name()
    select_cols = await _select_list(con, table_name, complex_helper)
    query: Template | Composed
    query_params: Mapping[str, Any] | None
    if isinstance(where, Template):
        parts: list[str | Interpolation] = [
            "SELECT ",
            Interpolation(select_cols, "cols", None, "q"),
            " FROM ",
            Interpolation(Identifier(*table_name), "tbl", None, "i"),
            " WHERE ",
        ]
        for k, v in filters.items():
            parts += [Interpolation(Identifier(k), k, None, "i"), " = ", Interpolation(v, k, None, ""), " AND "]
        parts += ["(", Interpolation(where, "where", None, "q"), ")"]
        parts += [Interpolation(t, "tail", None, "q") for t in tail]
        query, query_params = Template(*parts), None
    else:
        conditions: list[Composable] = [
            SQL("{col} = {val}").format(col=Identifier(k), val=Placeholder(k)) for k in filters
        ]
        if where is not None:
            conditions.append(SQL("({})").format(where))
        query_params = {**filters}
        for name, value in (params or {}).items():
            if name in query_params:
                raise ValueError(f"`params` key {name!r} collides with a filter of the same name")
            query_params[name] = value
        query = SQL("SELECT {cols} FROM {tbl}").format(cols=select_cols, tbl=Identifier(*table_name))
        if conditions:
            query = SQL("{} WHERE {}").format(query, SQL(" AND ").join(conditions))
        query = Composed([query, *tail])
        query_params = query_params or None  # an empty dict would make psycopg parse `%` for nothing
    async with con.cursor(row_factory=dict_row) as cur:
        if isinstance(query, Template):
            await cur.execute(query)
        else:
            await cur.execute(query, query_params)
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
    *,
    complex_helper: ComplexHelper | None = None,
) -> Any | None:
    """Update a row identified by primary_keys. Returns the raw row tuple.

    Pass `complex_helper` when the table has composite/enum columns among the
    values being set — omitted, this behaves exactly as before (plain values
    passed straight through to psycopg)."""
    data = await _convert_complex_values(con, table_name, data, complex_helper)
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


async def pg_update(
    con: AsyncConnection, data: T, data_type: type[T], *, complex_helper: ComplexHelper | None = None
) -> Any | None:
    """Update a typed model instance."""
    return await pg_update_dict(
        con, data_type.get_table_name(), data.model_dump(), data_type.get_primary_key(), complex_helper=complex_helper
    )


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
