from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from string.templatelib import Template
from typing import TYPE_CHECKING, Any, LiteralString, Protocol, overload

from psycopg.connection_async import AsyncConnection
from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row
from psycopg.sql import Composable
from pydantic import BaseModel

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlglot import exp  # imported lazily: only needed to type sqlglot queries (rendering is duck-typed)

# Same params shape psycopg's Cursor.execute() accepts: a %(name)s-style mapping,
# or a positional %s-style sequence.
QueryParams = Mapping[str, Any] | Sequence[Any] | None

# The only things a query may be: a literal string (e.g. from `SqlLoader.load_sql()`), a sqlglot
# expression, or psycopg's own safe-composition types (`psycopg.sql` / a t-string). A plain `str`
# is rejected by the type checker on purpose, so user-controlled text can't be concatenated in.
type SqlQueryNoTemplate = LiteralString | exp.Expression | Composable
type SqlQuery = SqlQueryNoTemplate | Template


class ConnectionSource(Protocol):
    """Anything that hands out an async connection context manager -- `PgPool` fits."""

    def connection(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


_default_source: ConnectionSource | None = None


def set_default_pool(pool: ConnectionSource | None) -> None:
    """Register where `fetch_all()` gets its connection when called without `con`/`pool`
    (typically the app's `PgPool`, once at startup). Pass `None` to clear it. This is process-wide
    state meant for single-database apps: with several databases/tenants pass `pool=` explicitly."""
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
    source = pool if pool is not None else _default_source
    if source is None:
        raise RuntimeError("No connection: pass `con=`/`pool=`, or register a pool with `set_default_pool()`.")
    async with source.connection() as borrowed:
        yield borrowed


_PLACEHOLDER_NAME = re.compile(r"\w+")


def _render(query: SqlQuery) -> Any:
    if isinstance(query, (str, Composable, Template)):
        return query
    if not hasattr(query, "sql"):
        raise TypeError(f"fetch_all: unsupported query type {type(query).__name__}")
    # Anything else is a sqlglot expression (no sqlglot import needed to know that; it is loaded
    # already). Its postgres dialect renders `exp.Placeholder("id")` as `%(id)s`.
    from sqlglot import exp

    for node in query.walk():
        # sqlglot renders these verbatim, so they could smuggle arbitrary SQL into the statement.
        if isinstance(node, exp.Command):
            raise ValueError("fetch_all: raw sqlglot Command nodes are not allowed")
        if isinstance(node, exp.Placeholder) and node.name and not _PLACEHOLDER_NAME.fullmatch(node.name):
            raise ValueError(f"fetch_all: invalid placeholder name {node.name!r}")
    return query.sql(dialect="postgres")


async def _run_query(c: AsyncConnection, query: Any, params: QueryParams) -> list[dict[str, Any]]:
    async with c.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def _abort(c: AsyncConnection, work: asyncio.Future[Any]) -> None:
    """Cancel `work`'s query on the server and wait until it has unwound, leaving `c` idle (an
    aborted transaction, to be rolled back by its owner). If the cancel request itself can't be
    delivered, the connection's state is unknown, so it is closed (the server abandons the query as
    soon as it next talks to the client) and a pool discards it instead of reusing it."""
    if not work.done():
        try:
            await c.cancel_safe()
        except Exception:
            logger.warning("fetch_all: cancelling the query failed; closing the connection", exc_info=True)
            with suppress(Exception):
                await c.close()  # first: a cancelled-but-still-busy connection would make `work` wait out the query
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return
    await asyncio.gather(work, return_exceptions=True)  # always consume `work`'s outcome


async def _abort_uninterruptibly(c: AsyncConnection, work: asyncio.Future[Any]) -> None:
    """`_abort`, but a further cancellation of the awaiting task can't cut it short (which would
    leave `work` running); it is re-raised once the abort is complete."""
    task = asyncio.ensure_future(_abort(c, work))
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError


async def _fetch_dicts(
    query: Any,  # already rendered (see `_render`)
    params: QueryParams,
    con: AsyncConnection | None,
    pool: ConnectionSource | None,
    cancel: asyncio.Event | None,
) -> list[dict[str, Any]]:
    if cancel is not None and cancel.is_set():
        raise QueryCanceled("fetch_all: cancelled before the query started")
    async with _acquire(con, pool) as c:
        work = asyncio.ensure_future(_run_query(c, query, params))
        stop = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
        try:
            await asyncio.wait([work, *([stop] if stop else [])], return_when=asyncio.FIRST_COMPLETED)
            if not work.done():  # `cancel` was set while the query runs: abort it on the server
                await _abort_uninterruptibly(c, work)
            if work.cancelled():
                raise QueryCanceled("fetch_all: cancelled via `cancel`")
            return work.result()  # raises psycopg.errors.QueryCanceled if it was cancelled
        except asyncio.CancelledError:
            # The awaiting task itself was cancelled: abandoning the query client-side would leave it
            # running on the server (and the connection mid-query), so cancel it there and let it
            # unwind first -- the connection is then idle for its owner/the pool.
            await _abort_uninterruptibly(c, work)
            raise
        finally:
            if stop is not None:
                stop.cancel()


@overload
async def fetch_all[M: BaseModel](
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    model: type[M],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
) -> list[M]: ...


@overload
async def fetch_all[T](
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    row_mapper: Callable[[dict[str, Any]], T],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
) -> list[T]: ...


@overload
async def fetch_all(
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    model: None = None,
    row_mapper: None = None,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
) -> list[dict[str, Any]]: ...


@overload
async def fetch_all[M: BaseModel](
    query: Template,
    *,
    model: type[M],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
) -> list[M]: ...


@overload
async def fetch_all[T](
    query: Template,
    *,
    row_mapper: Callable[[dict[str, Any]], T],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
) -> list[T]: ...


@overload
async def fetch_all(
    query: Template,
    *,
    model: None = None,
    row_mapper: None = None,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
) -> list[dict[str, Any]]: ...


async def fetch_all(
    query: SqlQuery,
    params: QueryParams = None,
    *,
    model: type[BaseModel] | None = None,
    row_mapper: Callable[[dict[str, Any]], Any] | None = None,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
) -> list[Any]:
    """Run one query and return every row.

    Rows come back as plain dicts, or, with `model=` (a Pydantic model class), validated into
    that model; `row_mapper=` converts each dict row into any other shape. The two are exclusive.

    Pass `con` to run on a connection you already hold (e.g. inside a transaction): it is
    borrowed, never committed, closed or returned to a pool. Without it, a connection is taken
    from `pool` or the pool registered via `set_default_pool()` and released afterwards.

    Set `cancel` (an `asyncio.Event`, e.g. when the HTTP client disconnects) to abort a running
    query on the server: `fetch_all` then raises `psycopg.errors.QueryCanceled`. If the awaiting
    task is cancelled instead, the query is cancelled server-side too and `CancelledError`
    propagates, so the query never keeps running unattended. `cancel` aborts whatever statement is
    running on the connection, so don't use it on a `con` that concurrent tasks share. If the
    cancel request itself can't be delivered, the connection is closed.

    `query` must be a literal string, a sqlglot expression, a `psycopg.sql` composable or a
    t-string -- never text built with f-strings/concatenation (a plain `str` fails type checking).
    A sqlglot expression is only as safe as the strings it was built from (the builders parse plain
    strings as SQL): pass user values as `exp.Placeholder` + `params`, never as literals/raw text.
    In one, write a literal `%` as `%%` when you also pass `params`.
    A t-string already carries its values, so it must not be combined with `params`.
    """
    if model is not None and row_mapper is not None:
        raise TypeError("Pass either `model` or `row_mapper`, not both.")
    if isinstance(query, Template) and params is not None:
        raise TypeError("A t-string query carries its own values; don't pass `params` with it.")
    rows = await _fetch_dicts(_render(query), params, con, pool, cancel)
    if model is not None:
        return [model.model_validate(row) for row in rows]
    if row_mapper is not None:
        return [row_mapper(row) for row in rows]
    return rows
