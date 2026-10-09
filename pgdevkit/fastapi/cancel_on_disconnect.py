"""Cancel a FastAPI handler when its client goes away, so the database work it started stops too.

Requires the ``fastapi`` extra: ``pip install pgdevkit[fastapi]``.

ASGI servers such as Granian do not cancel a request handler when the client disconnects: the handler runs to the
end and its result is thrown away. ``PostgresJsonResponse`` watches for the disconnect itself, but ``fetch_all`` and
friends (or any other awaitable work, raw psycopg cursors included) only stop when the awaiting task is cancelled.
``CancelOnDisconnectRoute`` closes that gap: it runs the route handler as a task and cancels it on
``http.disconnect``; psycopg and pgdevkit then cancel the running statement on the server.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.requests import ClientDisconnect

logger = logging.getLogger(__name__)

# What a client that already left gets (nginx's code for it); nobody reads it, but the middleware stack needs a response.
CLIENT_CLOSED_REQUEST = 499


class CancelOnDisconnectRoute(APIRoute):
    """An ``APIRoute`` that cancels its handler as soon as the client disconnects.

    Opt in per router, never for the whole app, and only for routes that are safe to abort halfway: reads, searches,
    exports. A cancelled write is rolled back at best and half-applied at worst::

        cancellable = APIRouter(route_class=CancelOnDisconnectRoute)

        @cancellable.post("/customers/overview")  # a POST that only reads
        async def overview(payload: Filter) -> Page:
            return await fetch_all(...)  # aborted in Postgres when the client leaves

        app.include_router(cancellable)

    A single route can opt in with ``router.add_api_route(..., route_class_override=CancelOnDisconnectRoute)``.

    Things to know:

    * The request body is read completely before the handler starts, so the disconnect watcher is the only reader of
      ``receive()`` afterwards. Don't use it for streamed uploads.
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
            # Buffer the body first (Starlette caches it for the handler): afterwards receive() only yields the
            # disconnect, which is all the watcher below needs.
            try:
                await request.body()
            except ClientDisconnect:  # gone before the handler even started: nothing to run
                return Response(status_code=CLIENT_CLOSED_REQUEST)
            task = asyncio.ensure_future(handler(request))
            watcher = asyncio.ensure_future(_wait_for_disconnect(request))
            try:
                await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:  # the server or a middleware is tearing the request down
                await _cancel(task)
                raise
            finally:
                watcher.cancel()
            if task.done():  # finished (or failed) before, or together with, the disconnect: its outcome wins
                return task.result()
            logger.debug("client disconnected, cancelling %s %s", request.method, request.url.path)
            await _cancel(task)
            return Response(status_code=CLIENT_CLOSED_REQUEST)

        return cancel_on_disconnect


async def _wait_for_disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _cancel(task: "asyncio.Future[Response]") -> None:
    """Cancel ``task`` and wait until it has cleaned up (e.g. the server-side query cancel has been sent)."""
    task.cancel()
    # Whatever the handler turns the cancellation into (QueryCanceled, its own error handling), the client is gone.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
