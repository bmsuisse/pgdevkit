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

logger = logging.getLogger(__name__)

# What a client that already left gets (nginx's code for it); nobody reads it.
CLIENT_CLOSED_REQUEST = 499
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
    * The handler runs in its own task (with a copy of the current context), so context variables a dependency sets
      are not visible to middleware afterwards.
    * Work shared with other requests must survive the cancellation of one of them: await shared tasks and futures
      through ``asyncio.shield`` (cache single-flight loaders, background refreshes), otherwise one aborted request
      cancels the load every other request is waiting on.
    * A returned ``StreamingResponse`` is not covered (the handler has finished by the time it is sent); that is what
      ``PostgresJsonResponse`` handles itself.
    * Threads (``asyncio.to_thread``) and external services (Databricks statements, HTTP calls) keep running after
      the cancellation; only awaiting them stops.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def cancel_on_disconnect(request: Request) -> Response:
            receiver = _Receiver(request.receive)
            reader = asyncio.create_task(receiver.read())
            gone = asyncio.create_task(receiver.disconnected.wait())
            task = asyncio.create_task(handler(Request(request.scope, receiver.receive)))
            try:
                await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:  # the server or a middleware is tearing the request down
                await _cancel(task)
                raise
            finally:
                reader.cancel()
                gone.cancel()
            if task.done():  # finished (or failed) before, or together with, the disconnect: its outcome wins
                try:
                    return task.result()
                except ClientDisconnect:  # it was reading the body when the client left
                    raise HTTPException(status_code=CLIENT_CLOSED_REQUEST, detail="Client closed the request") from None
            logger.debug("client disconnected, cancelling %s %s", request.method, request.url.path)
            await _cancel(task)
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


async def _cancel(task: asyncio.Task[Response]) -> None:
    """Cancel ``task`` and wait until it has cleaned up (e.g. the server-side query cancel has been sent)."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # Expected, the handler was cancelled. Unless we are being cancelled ourselves (server shutdown, a timeout).
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:  # whatever the handler turns the cancellation into, the client is gone
        logger.debug("handler failed while being cancelled", exc_info=True)
