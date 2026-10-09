"""Stream a Postgres query to the client as a JSON array, cancelling the query if the client goes away.

Requires the ``fastapi`` extra: ``pip install pgdevkit[fastapi]``.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any, LiteralString, cast

import anyio
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection, pq
from psycopg.pq import TransactionStatus
from psycopg.sql import SQL, Composable
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

ConnectionSource = AsyncConnection | Callable[[], AbstractAsyncContextManager[AsyncConnection]]
"""Either a connection the caller opened (it must stay open until the response is sent, e.g. a ``Depends`` with
``yield``; ``async with get_pg_connection() as conn: return Response(conn, ...)`` would close it too early), or a
zero-argument callable returning an async context manager of a connection, e.g. ``pool.connection`` or an app's
``get_pg_connection``. In the latter case the connection is acquired when streaming starts and released when it ends."""

_CANCEL_TIMEOUT_SECONDS = 5.0
# libpq < 17 has no chunked rows mode: stream() then has to run in single-row mode.
_CHUNKED_ROWS = pq.version() >= 170000


class PostgresJsonResponse(StreamingResponse):
    """Run ``query`` and stream its rows as ``[{...},\\n{...}]`` (``row_to_json`` per row, computed by Postgres).

    The query is only executed once the response is sent, so a connection is held exactly as long as the client
    receives data. If the client disconnects (or the request is cancelled) while the query is still running, the
    query is cancelled in Postgres and a connection we acquired is closed instead of being returned to the pool.

    ``query`` must be a literal or a ``psycopg.sql`` composable with values passed via ``parameters`` (``%(name)s``).
    It is wrapped as a subquery, so it cannot be a data-modifying CTE; for that (or for custom JSON) pass
    ``query_produces_json=True`` and make the query return one *text* column holding the JSON of each row.

    Errors before the first byte become a JSON ``{"error": ...}`` response (status 500, or the status of a raised
    ``HTTPException``). Errors after that are re-raised so the server aborts the response instead of ending it as
    if it were complete. Subclass and set ``expose_errors = True`` (e.g. in dev/test) to include ``str(error)``.

    A connection on which a query had to be cancelled is closed, also when you passed it in yourself.
    Postgres sends rows in 8 kB buffers, so a slow query with small rows delivers its first bytes late.
    """

    expose_errors: bool = False

    def __init__(
        self,
        source: ConnectionSource,
        query: LiteralString | Composable,
        *,
        parameters: Mapping[str, Any] | None = None,
        query_produces_json: bool = False,
        batch_size: int = 1000,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.source = source
        self.query = query if query_produces_json else _as_json_rows(query)
        self.parameters = parameters
        self.batch_size = batch_size
        # The body is produced by stream_response() itself, so the base class' iterator is never used.
        super().__init__((), status_code=status_code, headers=headers, media_type="application/json")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Starlette only watches for http.disconnect on ASGI spec < 2.4, so do it ourselves.
        try:
            async with anyio.create_task_group() as tg:

                async def watch_for_disconnect() -> None:
                    while (await receive())["type"] != "http.disconnect":
                        pass
                    tg.cancel_scope.cancel()

                tg.start_soon(watch_for_disconnect)
                await self.stream_response(send)
                tg.cancel_scope.cancel()
        except BaseExceptionGroup as group:
            # anyio always wraps; unwrap so callers see the original error
            if len(group.exceptions) != 1:
                raise
            error = group.exceptions[0]
            if isinstance(error, OSError):  # send() failing means the client is gone
                raise ClientDisconnect from None
            raise error from None
        if self.background is not None:
            await self.background()

    async def stream_response(self, send: Send) -> None:
        started = False
        try:
            async with AsyncExitStack() as stack:
                if isinstance(self.source, AsyncConnection):
                    conn = self.source
                else:
                    conn = await stack.enter_async_context(self.source())
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
            rows = cur.stream(self.query, self.parameters, size=self.batch_size if _CHUNKED_ROWS else 1)
            async for (row_json,) in rows:
                batch.append(row_json)
                if len(batch) >= self.batch_size:
                    yield (prefix + ",\n".join(batch)).encode()
                    batch, prefix = [], ","
        if batch or prefix == "[":  # remaining rows, or an empty result
            yield (prefix + ",\n".join(batch) + "]").encode()
        else:  # the last batch was full and has been sent already
            yield b"]"


def _as_json_rows(query: LiteralString | Composable) -> Composable:
    if isinstance(query, Composable):
        inner = query
    else:
        # bdt-lint: ignore sql-unverified-call -- LiteralString by signature; wrapped, values stay bound
        inner = SQL(cast(LiteralString, query.strip().rstrip(";")))
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
