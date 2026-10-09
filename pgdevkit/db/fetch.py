from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from string.templatelib import Template
from typing import TYPE_CHECKING, Any, AsyncIterator, LiteralString, Protocol, cast, overload

from psycopg.connection_async import AsyncConnection
from psycopg.rows import dict_row
from psycopg.sql import Composable

if TYPE_CHECKING:
    from pydantic import BaseModel
    from sqlglot import exp  # imported lazily: only needed to type (and render) sqlglot queries

# Same params shape psycopg's Cursor.execute() accepts: a %(name)s-style mapping,
# or a positional %s-style sequence.
QueryParams = Mapping[str, Any] | Sequence[Any] | None

# The only things a query may be: a literal string (e.g. from `SqlLoader.load_sql()`), a sqlglot
# expression, or psycopg's own safe-composition types (`psycopg.sql` / a t-string). A plain `str`
# is rejected by the type checker on purpose, so user-controlled text can't be concatenated in.
type SqlQuery = LiteralString | exp.Expression | Composable | Template


class ConnectionSource(Protocol):
    """Anything that hands out an async connection context manager -- `PgPool` fits."""

    def connection(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


_default_source: ConnectionSource | None = None


def set_default_pool(pool: ConnectionSource | None) -> None:
    """Register where `fetch_all()` gets its connection when called without `con`/`pool`
    (typically the app's `PgPool`, once at startup). Pass `None` to clear it."""
    global _default_source
    _default_source = pool


@asynccontextmanager
async def _acquire(con: AsyncConnection | None, pool: ConnectionSource | None) -> AsyncIterator[AsyncConnection]:
    """Yield `con` untouched (the caller owns it: no commit/close/return-to-pool here), or a
    connection borrowed from `pool` / the default pool for the duration of the block."""
    if con is not None:
        if pool is not None:
            raise TypeError("Pass either `con` or `pool`, not both.")
        yield con
        return
    source = pool or _default_source
    if source is None:
        raise RuntimeError("No connection: pass `con=`/`pool=`, or register a pool with `set_default_pool()`.")
    async with source.connection() as borrowed:
        yield borrowed


def _render(query: SqlQuery) -> Any:
    if isinstance(query, (str, bytes, Composable, Template)):
        return query
    # Anything else is a sqlglot expression (no sqlglot import needed to know that); its postgres
    # dialect renders `exp.Placeholder("id")` as `%(id)s`.
    return query.sql(dialect="postgres")


async def _fetch_dicts(
    query: SqlQuery, params: QueryParams, con: AsyncConnection | None, pool: ConnectionSource | None
) -> list[dict[str, Any]]:
    async with _acquire(con, pool) as c, c.cursor(row_factory=dict_row) as cur:
        await cur.execute(_render(query), params)
        return await cur.fetchall()


@overload
async def fetch_all[M: BaseModel](
    query: SqlQuery,
    params: QueryParams = None,
    *,
    model: type[M],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
) -> list[M]: ...


@overload
async def fetch_all[T](
    query: SqlQuery,
    params: QueryParams = None,
    *,
    row_mapper: Callable[[dict[str, Any]], T],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
) -> list[T]: ...


@overload
async def fetch_all(
    query: SqlQuery,
    params: QueryParams = None,
    *,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
) -> list[dict[str, Any]]: ...


async def fetch_all(
    query: SqlQuery,
    params: QueryParams = None,
    *,
    model: type[BaseModel] | None = None,
    row_mapper: Callable[[dict[str, Any]], Any] | None = None,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
) -> list[Any]:
    """Run one query and return every row.

    Rows come back as plain dicts, or, with `model=` (a Pydantic model class), validated into
    that model; `row_mapper=` converts each dict row into any other shape. The two are exclusive.

    Pass `con` to run on a connection you already hold (e.g. inside a transaction): it is
    borrowed, never committed, closed or returned to a pool. Without it, a connection is taken
    from `pool` or the pool registered via `set_default_pool()` and released afterwards.

    `query` must be a literal string, a sqlglot expression, a `psycopg.sql` composable or a
    t-string -- never text built with f-strings/concatenation (`bdt lint` enforces this too).
    A t-string already carries its values, so it must not be combined with `params`.
    """
    if model is not None and row_mapper is not None:
        raise TypeError("Pass either `model` or `row_mapper`, not both.")
    if isinstance(query, Template) and params is not None:
        raise TypeError("A t-string query carries its own values; don't pass `params` with it.")
    rows = await _fetch_dicts(query, params, con, pool)
    if model is not None:
        return [model.model_validate(row) for row in rows]
    if row_mapper is not None:
        return [row_mapper(row) for row in rows]
    return cast(list[Any], rows)
