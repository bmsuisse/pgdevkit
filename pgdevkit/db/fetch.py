from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from string.templatelib import Template
from typing import TYPE_CHECKING, Any, Literal, LiteralString, Protocol, cast, overload

from psycopg.connection_async import AsyncConnection
from psycopg.errors import QueryCanceled
from psycopg.rows import dict_row, tuple_row
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


# What `set_default_pool()` takes: a `ConnectionSource` (has `.connection()`, like `PgPool`), or a plain
# zero-argument callable returning an async connection context manager (e.g. a module-level pool's bound method).
type PoolLike = ConnectionSource | Callable[[], AbstractAsyncContextManager[AsyncConnection]]

_default_source: PoolLike | None = None


def set_default_pool(pool: PoolLike | None) -> None:
    """Register where the `fetch_*()`/`execute()` helpers get their connection when called without `con`/`pool`
    (typically the app's `PgPool`, once at startup). Pass `None` to clear it. Besides a `ConnectionSource` (anything
    with a `.connection()` method), a plain callable returning an async connection context manager is accepted, for
    a pool that doesn't have that shape: `set_default_pool(lambda: my_pool.acquire_cm())`. This is process-wide
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
    source: PoolLike | None = pool if pool is not None else _default_source
    if source is None:
        raise RuntimeError("No connection: pass `con=`/`pool=`, or register a pool with `set_default_pool()`.")
    if hasattr(source, "connection"):
        cm = cast(ConnectionSource, source).connection()
    else:
        cm = cast("Callable[[], AbstractAsyncContextManager[AsyncConnection]]", source)()
    async with cm as borrowed:
        yield borrowed


_PLACEHOLDER_NAME = re.compile(r"\w+")


def _render(query: SqlQuery, *, caller: str = "fetch_all") -> Any:
    if isinstance(query, (str, Composable, Template)):
        return query
    if not hasattr(query, "sql"):
        raise TypeError(f"{caller}: unsupported query type {type(query).__name__}")
    # Anything else is a sqlglot expression (no sqlglot import needed to know that; it is loaded
    # already). Its postgres dialect renders `exp.Placeholder("id")` as `%(id)s`.
    from sqlglot import exp

    for node in query.walk():
        # sqlglot renders these verbatim, so they could smuggle arbitrary SQL into the statement.
        if isinstance(node, exp.Command):
            raise ValueError(f"{caller}: raw sqlglot Command nodes are not allowed")
        if isinstance(node, exp.Placeholder) and node.name and not _PLACEHOLDER_NAME.fullmatch(node.name):
            raise ValueError(f"{caller}: invalid placeholder name {node.name!r}")
    return query.sql(dialect="postgres")


def _check_statement_timeout(statement_timeout: float | None) -> None:
    if statement_timeout is not None and statement_timeout <= 0:
        raise ValueError(f"statement_timeout must be a positive number of seconds, got {statement_timeout}")


class _AtMost(float):
    """A `statement_timeout` that may only tighten the connection's current one (see `at_most`)."""


def at_most(seconds: float) -> float:
    """Use as `statement_timeout=at_most(120)`: like a plain number of seconds, except that it never *loosens* the
    timeout the connection already has. A plain `statement_timeout` replaces the current one in both directions
    (a per-call 900 s overrides a database-level safety net of 600 s); with `at_most` the effective timeout is
    `min(current, seconds)`, where a current setting of 0 (none) counts as unlimited, i.e. `seconds` applies."""
    return _AtMost(seconds)


_SET_TIMEOUT = "SELECT current_setting('statement_timeout'), set_config('statement_timeout', %(ms)s, true)"
_SET_TIMEOUT_AT_MOST = """
SELECT current_setting('statement_timeout'), set_config('statement_timeout', (
    SELECT CASE WHEN setting::bigint > 0 THEN least(setting::bigint, %(ms)s::bigint) ELSE %(ms)s::bigint END
    FROM pg_settings WHERE name = 'statement_timeout'
)::text, true)"""


@asynccontextmanager
async def _statement_timeout(c: AsyncConnection, seconds: float | None) -> AsyncIterator[None]:
    """Run the block under Postgres' `statement_timeout`, set with `set_config(..., is_local => true)`: it is
    scoped to the current transaction (so it can't leak into a pool, and it is safe behind PgBouncer's
    transaction pooling) and put back afterwards for a borrowed connection that stays in its transaction.
    `None` leaves the connection's own setting alone; an `at_most()` value never raises it."""
    if seconds is None:
        yield
        return
    # `pg_settings.setting` of statement_timeout is in milliseconds, 0 = disabled
    query = _SET_TIMEOUT_AT_MOST if isinstance(seconds, _AtMost) else _SET_TIMEOUT
    async with AsyncExitStack() as stack:
        if c.autocommit:
            await stack.enter_async_context(c.transaction())  # a local setting needs a transaction to live in
        cur = await c.execute(query, {"ms": str(max(1, math.ceil(seconds * 1000)))})
        row = await cur.fetchone()
        assert row is not None
        yield
        await c.execute("SELECT set_config('statement_timeout', %(previous)s, true)", {"previous": row[0]})


type _Mode = Literal["all", "one", "scalar", "count"]


async def _run_query(
    c: AsyncConnection, query: Any, params: QueryParams, statement_timeout: float | None, mode: _Mode
) -> Any:
    row_factory = dict_row if mode in ("all", "one") else tuple_row
    async with _statement_timeout(c, statement_timeout), c.cursor(row_factory=row_factory) as cur:
        await cur.execute(query, params)
        if mode == "all":
            return await cur.fetchall()
        if mode == "count":
            return cur.rowcount
        row = await cur.fetchone()
        if mode == "scalar":
            first = cast("tuple[Any, ...] | None", row)
            return first[0] if first is not None else None
        return row


async def _abort(c: AsyncConnection, work: asyncio.Future[Any]) -> None:
    """Cancel `work`'s query on the server and wait until it has unwound, leaving `c` idle (an
    aborted transaction, to be rolled back by its owner). If the cancel request itself can't be
    delivered, the connection's state is unknown, so it is closed (the server abandons the query as
    soon as it next talks to the client) and a pool discards it instead of reusing it."""
    if not work.done():
        try:
            await c.cancel_safe()
        except Exception:
            logger.warning("cancelling the query failed; closing the connection", exc_info=True)
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


async def _run(
    query: Any,  # already rendered (see `_render`)
    params: QueryParams,
    con: AsyncConnection | None,
    pool: ConnectionSource | None,
    cancel: asyncio.Event | None,
    statement_timeout: float | None,
    mode: _Mode,
    caller: str,
) -> Any:
    """The one place that acquires a connection, runs the statement and wires up `cancel`/the task's own
    cancellation; `mode` only says what is read from the cursor (see `_run_query`)."""
    if cancel is not None and cancel.is_set():
        raise QueryCanceled(f"{caller}: cancelled before the query started")
    async with _acquire(con, pool) as c:
        work = asyncio.ensure_future(_run_query(c, query, params, statement_timeout, mode))
        stop = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
        try:
            await asyncio.wait([work, *([stop] if stop else [])], return_when=asyncio.FIRST_COMPLETED)
            if not work.done():  # `cancel` was set while the query runs: abort it on the server
                await _abort_uninterruptibly(c, work)
            if work.cancelled():
                raise QueryCanceled(f"{caller}: cancelled via `cancel`")
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


def _prepare(
    caller: str,
    query: SqlQuery,
    params: QueryParams,
    model: type[BaseModel] | None,
    row_mapper: Callable[[dict[str, Any]], Any] | None,
    statement_timeout: float | None,
) -> Any:
    """Validate the arguments shared by every helper and render the query."""
    if model is not None and row_mapper is not None:
        raise TypeError("Pass either `model` or `row_mapper`, not both.")
    if isinstance(query, Template) and params is not None:
        raise TypeError("A t-string query carries its own values; don't pass `params` with it.")
    _check_statement_timeout(statement_timeout)
    return _render(query, caller=caller)


def _convert(
    row: dict[str, Any], model: type[BaseModel] | None, row_mapper: Callable[[dict[str, Any]], Any] | None
) -> Any:
    if model is not None:
        return model.model_validate(row)
    if row_mapper is not None:
        return row_mapper(row)
    return row


@overload
async def fetch_all[M: BaseModel](
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    model: type[M],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
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
    statement_timeout: float | None = None,
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
    statement_timeout: float | None = None,
) -> list[dict[str, Any]]: ...


@overload
async def fetch_all[M: BaseModel](
    query: Template,
    *,
    model: type[M],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> list[M]: ...


@overload
async def fetch_all[T](
    query: Template,
    *,
    row_mapper: Callable[[dict[str, Any]], T],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
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
    statement_timeout: float | None = None,
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
    statement_timeout: float | None = None,
) -> list[Any]:
    """Run one query and return every row.

    Rows come back as plain dicts, or, with `model=` (a Pydantic model class), validated into
    that model; `row_mapper=` converts each dict row into any other shape. The two are exclusive.

    Pass `statement_timeout` (seconds) to let Postgres abort the query after that long: `fetch_all` then
    raises `psycopg.errors.QueryCanceled` ("canceling statement due to statement timeout"). It is applied
    with `SET LOCAL` semantics, i.e. for this query's transaction only; a borrowed `con` in autocommit mode
    gets a short transaction around the query, and any other borrowed `con` gets its previous setting back.
    Without it, the connection's own `statement_timeout` (default: none) applies.

    Pass `con` to run on a connection you already hold (e.g. inside a transaction): it is
    borrowed, never committed, closed or returned to a pool. Without it, a connection is taken
    from `pool` or the pool registered via `set_default_pool()` and released afterwards.

    Set `cancel` (an `asyncio.Event`, e.g. when the HTTP client disconnects; to stream a large result
    from FastAPI see `pgdevkit.fastapi.PostgresJsonResponse`, which does that wiring itself) to abort a running
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
    rendered = _prepare("fetch_all", query, params, model, row_mapper, statement_timeout)
    rows = await _run(rendered, params, con, pool, cancel, statement_timeout, "all", "fetch_all")
    return [_convert(row, model, row_mapper) for row in rows]


@overload
async def fetch_one[M: BaseModel](
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    model: type[M],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> M | None: ...


@overload
async def fetch_one[T](
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    row_mapper: Callable[[dict[str, Any]], T],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> T | None: ...


@overload
async def fetch_one(
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    model: None = None,
    row_mapper: None = None,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> dict[str, Any] | None: ...


@overload
async def fetch_one[M: BaseModel](
    query: Template,
    *,
    model: type[M],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> M | None: ...


@overload
async def fetch_one[T](
    query: Template,
    *,
    row_mapper: Callable[[dict[str, Any]], T],
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> T | None: ...


@overload
async def fetch_one(
    query: Template,
    *,
    model: None = None,
    row_mapper: None = None,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> dict[str, Any] | None: ...


async def fetch_one(
    query: SqlQuery,
    params: QueryParams = None,
    *,
    model: type[BaseModel] | None = None,
    row_mapper: Callable[[dict[str, Any]], Any] | None = None,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> Any | None:
    """Run one query and return its first row, or `None` if it returned none (further rows are ignored: add
    `LIMIT 1`/`ORDER BY` yourself). Same arguments and semantics as `fetch_all`: the row is a dict, validated into
    `model`, or converted by `row_mapper`."""
    rendered = _prepare("fetch_one", query, params, model, row_mapper, statement_timeout)
    row = await _run(rendered, params, con, pool, cancel, statement_timeout, "one", "fetch_one")
    return None if row is None else _convert(row, model, row_mapper)


@overload
async def fetch_scalar(
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> Any | None: ...


@overload
async def fetch_scalar(
    query: Template,
    *,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> Any | None: ...


@overload
async def execute(
    query: SqlQueryNoTemplate,
    params: QueryParams = None,
    *,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> int: ...


@overload
async def execute(
    query: Template,
    *,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> int: ...


async def fetch_scalar(
    query: SqlQuery,
    params: QueryParams = None,
    *,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> Any | None:
    """Run one query and return the first column of its first row, or `None` if it returned no row (a NULL
    value is `None` too: use `fetch_one` to tell them apart). Same arguments and semantics as `fetch_all`,
    minus `model`/`row_mapper`. The statement must return a result set (e.g. `SELECT count(*) ...`,
    or an `INSERT ... RETURNING id`)."""
    rendered = _prepare("fetch_scalar", query, params, None, None, statement_timeout)
    return await _run(rendered, params, con, pool, cancel, statement_timeout, "scalar", "fetch_scalar")


async def execute(
    query: SqlQuery,
    params: QueryParams = None,
    *,
    con: AsyncConnection | None = None,
    pool: ConnectionSource | None = None,
    cancel: asyncio.Event | None = None,
    statement_timeout: float | None = None,
) -> int:
    """Run one statement whose result you don't read (`INSERT`/`UPDATE`/`DELETE`/DDL without `RETURNING`) and
    return its row count (`-1` for statements that have none, like DDL). Same arguments and semantics as
    `fetch_all`. Without `con`, the statement runs on a pooled connection that commits when the call returns; on
    a `con` you pass, nothing is committed: that stays your transaction's business."""
    rendered = _prepare("execute", query, params, None, None, statement_timeout)
    return await _run(rendered, params, con, pool, cancel, statement_timeout, "count", "execute")
