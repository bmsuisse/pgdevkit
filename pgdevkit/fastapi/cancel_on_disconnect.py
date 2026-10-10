"""Cancel a FastAPI handler when its client goes away, so the database work it started stops too.

Requires the ``fastapi`` extra: ``pip install pgdevkit[fastapi]``.

ASGI servers such as Granian do not cancel a request handler when the client disconnects: the handler runs to the
end and its result is thrown away. ``PostgresJsonResponse`` watches for the disconnect itself, but ``fetch_all`` and
friends (or any other awaitable work, raw psycopg cursors included) only stop when the awaiting task is cancelled.
``CancelOnDisconnectRoute`` closes that gap: it runs the route handler as a task and cancels it on
``http.disconnect``; psycopg and pgdevkit then cancel the running statement on the server.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive

from .json_response import CLIENT_CLOSED_REQUEST

logger = logging.getLogger(__name__)

# Body chunks read ahead of the handler: enough to never stall it, too few to buffer an upload in memory.
_MAX_PENDING_MESSAGES = 8


class CancelOnDisconnectRoute(APIRoute):
    """An ``APIRoute`` that cancels its handler as soon as the client disconnects.

    Opt in per router, never for the whole app, and only for routes that are safe to abort halfway: reads, searches,
    exports. A cancelled write is rolled back at best and half-applied at worst::

        cancellable = APIRouter(route_class=CancelOnDisconnectRoute)

        @cancellable.post("/customers/overview")  # a POST that only reads
        async def overview(payload: Filter) -> Page:
            return await fetch_all(...)  # aborted in Postgres when the client leaves

        app.include_router(cancellable)

    A single route can opt in with ``router.add_api_route(..., route_class_override=CancelOnDisconnectRoute)``
    (``APIRouter`` only; the ``@router.get`` decorators don't take it).

    Things to know:

    * **Everything inside the route is cancelled**, dependencies included (authentication, audit logging, counters)
      and so is ``BackgroundTasks`` registration, since the client gets no response. Don't rely on side effects that
      must always happen; shield them or do them in a middleware. A ``Depends`` with ``yield`` sees the 499
      ``HTTPException`` after the cancellation, so its cleanup rolls back instead of committing.
    * The request body is not buffered: the handler reads it as usual, from a ``Request`` whose ``receive()`` is fed
      by a reader task that also spots the disconnect. While the handler isn't reading a large body, the reader
      stops after a few chunks, so a disconnect is only noticed once the handler has consumed the body.
    * Work shared with other requests must survive the cancellation of one of them: await shared tasks and futures
      through ``asyncio.shield`` (cache single-flight loaders, background refreshes), otherwise one aborted request
      cancels the load every other request is waiting on.
    * A returned ``StreamingResponse`` is not covered (the handler has finished by the time it is sent): Starlette
      watches for the disconnect there only on servers with ASGI spec < 2.4, ``PostgresJsonResponse`` always does.
    * Threads (``asyncio.to_thread``) and external services (Databricks statements, HTTP calls) keep running after
      the cancellation; only awaiting them stops.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def cancel_on_disconnect(request: Request) -> Response:
            # The handler runs in the request's own task, as without this route (context variables, dependencies with
            # `yield`, an outer `asyncio.timeout` all behave as usual); an asyncio timeout is the cancel scope, as in
            # PostgresJsonResponse. The disconnect pulls its deadline to "now".
            loop = asyncio.get_running_loop()
            receiver = _Receiver(request.receive)
            request._receive = receiver.receive  # the same Request object, so a cached body is seen by everyone
            reader = asyncio.create_task(receiver.read())

            handled = False  # as in PostgresJsonResponse: once set, the cancel scope may be gone

            async def cancel_when_gone(cancel_scope: asyncio.Timeout) -> None:
                await receiver.disconnected.wait()
                if not handled:  # (the Event can't swallow the cancellation; this keeps reschedule() safe anyway)
                    cancel_scope.reschedule(loop.time())

            try:
                async with asyncio.timeout(None) as cancel_scope:
                    watcher = asyncio.create_task(cancel_when_gone(cancel_scope))
                    try:
                        return await handler(request)
                    finally:
                        handled = True
                        watcher.cancel()
            except TimeoutError:
                if not cancel_scope.expired():  # the handler's own timeout
                    raise
            except ClientDisconnect:  # it was reading the body when the client left
                pass
            finally:
                reader.cancel()
            logger.debug("client disconnected, cancelled %s %s", request.method, request.url.path)
            # Raised, not returned: the dependencies' exit code (e.g. a transaction) must see a failure, not a success.
            raise HTTPException(status_code=CLIENT_CLOSED_REQUEST, detail="Client closed the request")

        return cancel_on_disconnect


class _Receiver:
    """Gives the handler's ``Request`` its own ``receive()`` while one reader task watches for the disconnect."""

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._messages: asyncio.Queue[Message | Exception] = asyncio.Queue(maxsize=_MAX_PENDING_MESSAGES)
        self._gone: Message | None = None
        self.disconnected = asyncio.Event()

    async def read(self) -> None:
        """Pump the server's messages into the queue until the client is gone (the queue's limit is the back-pressure)."""
        try:
            while True:
                message = await self._receive()
                if message["type"] == "http.disconnect":
                    self.disconnected.set()
                await self._messages.put(message)
                if message["type"] == "http.disconnect":
                    return
        except Exception as exc:  # noqa: BLE001 - a broken server: the handler sees it, the client is not treated as gone
            await self._messages.put(exc)

    async def receive(self) -> Message:
        if self._gone is not None:  # the disconnect is final, whoever asks again
            return self._gone
        message = await self._messages.get()
        if isinstance(message, Exception):
            raise message
        if message["type"] == "http.disconnect":
            self._gone = message
        return message
