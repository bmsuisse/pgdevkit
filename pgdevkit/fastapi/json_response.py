"""Stream a Postgres query to the client as a JSON array, cancelling the query if the client goes away.

Requires the ``fastapi`` extra: ``pip install pgdevkit[fastapi]``.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import anyio
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection, pq
from psycopg.pq import TransactionStatus
from psycopg.sql import SQL, Composable, Composed
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive, Scope, Send

from ..db.fetch import ConnectionSource, QueryParams, SqlQueryNoTemplate, _acquire, _render

logger = logging.getLogger(__name__)

_CANCEL_TIMEOUT_SECONDS = 5.0
# libpq < 17 has no chunked rows mode: stream() then has to run in single-row mode.
_CHUNKED_ROWS = pq.version() >= 170000


class PostgresJsonResponse(StreamingResponse):
    """Run ``query`` and stream its rows as ``[{...},\\n{...}]`` (``row_to_json`` per row, computed by Postgres).

    Connections work as in ``pgdevkit.db.fetch_all``: pass ``con`` (one you opened; it must stay open until the
    response has been sent, e.g. a ``Depends`` with ``yield``), or ``pool`` (anything with ``.connection()``), or
    register the app's pool once with ``set_default_pool()``. A pooled connection is acquired when streaming starts
    and released when it ends. ``query`` and ``params`` are those of ``fetch_all`` too (a literal string, a
    ``psycopg.sql`` composable or a sqlglot expression; never a t-string) with values bound via ``params``.

    The query is wrapped as a subquery, so it cannot be a data-modifying CTE; for that (or for custom JSON) pass
    ``query_produces_json=True`` and make it return one *text* column holding the JSON of each row.

    If the client disconnects (or the request is cancelled) while the query is still running, the query is
    cancelled in Postgres. A connection that was mid-query at that point is closed instead of being reused.

    Errors before the first byte become a JSON ``{"error": ...}`` response (status 500, or the status of a raised
    ``HTTPException``). After that the status line is gone: the error is logged and re-raised, and the response
    ends without its closing ``]``, so clients can't mistake it for a complete array. Subclass and set
    ``expose_errors = True`` (e.g. in dev/test) to include ``str(error)`` in the 500 response.

    Postgres sends rows in 8 kB buffers, so a slow query with small rows delivers its first bytes late.
    """

    expose_errors: bool = False

    def __init__(
        self,
        query: SqlQueryNoTemplate,
        params: QueryParams = None,
        *,
        con: AsyncConnection | None = None,
        pool: ConnectionSource | None = None,
        query_produces_json: bool = False,
        batch_size: int = 1000,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if con is not None and pool is not None:
            raise TypeError("Pass either `con` or `pool`, not both.")
        rendered = _render(query)
        self.query = rendered if query_produces_json else _as_json_rows(rendered)
        self.params = params
        self.con = con
        self.pool = pool
        self.batch_size = batch_size
        # The body is produced by stream_response() itself, so the base class' iterator is never used.
        super().__init__((), status_code=status_code, headers=headers, media_type="application/json")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Starlette only watches for http.disconnect on ASGI spec < 2.4, so do it ourselves. An asyncio timeout is
        # the cancel scope: unlike an anyio task group, it doesn't leave Granian hanging when the body fails.
        loop = asyncio.get_running_loop()

        async def watch_for_disconnect(cancel_scope: asyncio.Timeout) -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            cancel_scope.reschedule(loop.time())  # i.e. cancel the stream now

        try:
            async with asyncio.timeout(None) as cancel_scope:
                watcher = asyncio.create_task(watch_for_disconnect(cancel_scope))
                try:
                    await self.stream_response(send)
                finally:
                    watcher.cancel()
        except TimeoutError:
            if not cancel_scope.expired():
                raise
            # the client is gone and the query has been cancelled: nobody is left to answer
        except OSError:  # send() failing means the client is gone
            raise ClientDisconnect from None
        if self.background is not None:
            await self.background()

    async def stream_response(self, send: Send) -> None:
        started = False
        try:
            async with _acquire(self.con, self.pool) as conn:
                try:
                    async for chunk in self._chunks(conn):
                        if not started:
                            await send(self._start_message(self.status_code, self.raw_headers))
                            started = True
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                finally:
                    # Runs for completion too, but only acts if the query is still in flight.
                    await _cancel_if_running(conn)
        except OSError:  # send() failed: the client is gone, nothing to report to
            raise
        except Exception as err:
            if started:
                raise
            logger.exception("Error executing Postgres query")
            await self._send_error(send, err)
            return
        if not started:
            await send(self._start_message(self.status_code, self.raw_headers))
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    def _start_message(status: int, headers: list[tuple[bytes, bytes]]) -> Message:
        return {"type": "http.response.start", "status": status, "headers": headers}

    async def _send_error(self, send: Send, err: Exception) -> None:
        if isinstance(err, HTTPException):
            status, message = err.status_code, err.detail
        else:
            status, message = 500, str(err) if self.expose_errors else "Internal Server Error"
        body = json.dumps({"error": message}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        await send(self._start_message(status, headers))
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _chunks(self, conn: AsyncConnection) -> AsyncIterator[bytes]:
        """Yield the JSON array in pieces of ``batch_size`` rows. Nothing is yielded before the first batch is
        complete, so a failing query surfaces before any byte (and the status line) is sent."""
        batch: list[str] = []
        prefix = "["
        async with conn.cursor() as cur:
            rows = cur.stream(self.query, self.params, size=self.batch_size if _CHUNKED_ROWS else 1)
            async for (row_json,) in rows:
                batch.append(row_json)
                if len(batch) >= self.batch_size:
                    yield (prefix + ",\n".join(batch)).encode()
                    batch, prefix = [], ","
        if batch or prefix == "[":  # remaining rows, or an empty result
            yield (prefix + ",\n".join(batch) + "]").encode()
        else:  # the last batch was full and has been sent already
            yield b"]"


def _as_json_rows(query: Any) -> Composed:
    if isinstance(query, Composable):
        inner = query
    else:
        # bdt-lint: ignore sql-unverified-call -- a literal or a rendered sqlglot expression; wrapped, values stay bound
        inner = SQL(query.strip().rstrip(";"))
    # newline before the closing paren: the query may end in a line comment
    return SQL("select row_to_json(s)::text from (\n{}\n) s").format(inner)


async def _cancel_if_running(conn: AsyncConnection) -> None:
    """Cancel the in-flight query on ``conn`` and close it. Shielded: this runs while the request task is cancelled.

    The connection can't be reused afterwards (the cancelled query's results are still pending), and a pool must
    not get it back mid-query, so it is closed; a pool simply opens a fresh one.

    Only a query that is still running (status ACTIVE) is cancelled: a cancel request that arrives after the
    query finished would hit whatever statement the connection runs next once it is back in the pool.
    """
    if conn.closed or conn.info.transaction_status != TransactionStatus.ACTIVE:
        return
    with anyio.CancelScope(shield=True):
        with anyio.move_on_after(_CANCEL_TIMEOUT_SECONDS):
            try:
                await conn.cancel_safe(timeout=_CANCEL_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("Could not cancel Postgres query", exc_info=True)
        await conn.close()
