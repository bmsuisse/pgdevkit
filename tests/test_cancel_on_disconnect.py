"""CancelOnDisconnectRoute cancels the handler (and with it the Postgres query) when the client goes away."""

import asyncio
import contextvars
import json
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from pgdevkit.db import fetch_all
from pgdevkit.fastapi import CancelOnDisconnectRoute
from tests._asgi import active_queries, wait_until


class Filter(BaseModel):
    n: int


current_user: contextvars.ContextVar[str] = contextvars.ContextVar("current_user")


class HttpClient:
    """Drives an ASGI app by hand: sends one request, then stays connected until `disconnect` is set."""

    def __init__(self, method: str = "GET", path: str = "/", body: bytes | list[bytes] = b"") -> None:
        self.method, self.path = method, path
        self.chunks = [body] if isinstance(body, bytes) else list(body)
        self.chunks_read = 0
        self.disconnect = asyncio.Event()
        self.sent: list[dict] = []
        self.gone_at_start = False  # the client disconnected before the body arrived
        self.gone_after_body = False  # ... right after it

    async def receive(self) -> dict:
        if self.gone_at_start:
            return {"type": "http.disconnect"}
        if self.chunks_read < max(len(self.chunks), 1):
            chunk = self.chunks[self.chunks_read] if self.chunks else b""
            self.chunks_read += 1
            return {"type": "http.request", "body": chunk, "more_body": self.chunks_read < len(self.chunks)}
        if not self.gone_after_body:
            await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def call(self, app: FastAPI) -> None:
        scope = {
            "type": "http",
            "asgi": {"spec_version": "2.3"},
            "http_version": "1.1",
            "method": self.method,
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")] if self.chunks and self.method == "POST" else [],
            "client": ("testclient", 1),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        }
        await app(scope, self.receive, self.send)

    @property
    def status(self) -> int | None:
        return next((m["status"] for m in self.sent if m["type"] == "http.response.start"), None)

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


@pytest.fixture
def app_name() -> str:
    return f"cod-{uuid.uuid4().hex[:12]}"  # isolates this test's backends in pg_stat_activity


@pytest.fixture
async def pool(postgres_dsn: str, app_name: str) -> AsyncIterator[AsyncConnectionPool]:
    kwargs = {"application_name": app_name}
    async with AsyncConnectionPool(postgres_dsn, min_size=1, max_size=2, kwargs=kwargs, open=False) as pool:
        yield pool


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def app(pool: AsyncConnectionPool, events: list[str]) -> FastAPI:
    cancellable = APIRouter(route_class=CancelOnDisconnectRoute)
    plain = APIRouter()

    @cancellable.get("/sleep")
    async def sleep() -> list[dict]:
        try:
            return await fetch_all("select pg_sleep(30) as slept", pool=pool)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    @plain.get("/plain-sleep")
    async def plain_sleep() -> list[dict]:
        return await fetch_all("select pg_sleep(1.5) as slept", pool=pool)

    @cancellable.get("/ok")
    async def ok() -> list[dict]:
        return await fetch_all("select 1 as one", pool=pool)

    @cancellable.post("/echo")
    async def echo(payload: dict) -> dict:  # the body was buffered for the watcher: the handler must still get it
        return payload

    @cancellable.post("/size")  # reads a (large) body in the handler
    async def size(request: Request) -> dict:
        return {"size": len(await request.body())}

    async def deny() -> None:
        raise HTTPException(status_code=401, detail="no")

    @cancellable.post("/denied", dependencies=[Depends(deny)])  # no body parameters, rejected before any body read
    async def denied() -> None: ...

    async def transaction() -> AsyncIterator[None]:
        try:
            yield
        except BaseException as exc:
            events.append(f"dependency saw {type(exc).__name__}")
            raise
        events.append("dependency saw success")

    @cancellable.get("/in-transaction", dependencies=[Depends(transaction)])
    async def in_transaction() -> list[dict]:
        return await fetch_all("select pg_sleep(30) as slept", pool=pool)

    @cancellable.get("/slow-cleanup")
    async def slow_cleanup() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            events.append("cleaning up")
            await asyncio.sleep(30)

    @cancellable.get("/instant")
    async def instant() -> dict:
        return {"done": True}

    async def with_context() -> AsyncIterator[None]:  # set before, reset after the handler: needs one context
        token = current_user.set("me")
        try:
            yield
        finally:
            current_user.reset(token)

    @cancellable.get("/context", dependencies=[Depends(with_context)])
    async def context() -> dict:
        return {"user": current_user.get()}

    @cancellable.post("/filter")
    async def filter_(payload: Filter) -> Filter:
        return payload

    @cancellable.get("/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403, detail="no")

    @cancellable.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def log_the_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid", "body": (await request.body()).decode()}, status_code=422)

    app.include_router(cancellable)
    app.include_router(plain)
    return app


async def query_running(dsn: str, app_name: str) -> bool:
    return await active_queries(dsn, app_name) == 1


async def query_stopped(dsn: str, app_name: str) -> bool:
    return await active_queries(dsn, app_name) == 0


async def test_disconnect_cancels_the_handler_and_the_query(
    app: FastAPI, events: list[str], postgres_dsn: str, app_name: str
) -> None:
    client = HttpClient(path="/sleep")
    task = asyncio.create_task(client.call(app))
    assert await wait_until(lambda: query_running(postgres_dsn, app_name))

    client.disconnect.set()
    await asyncio.wait_for(task, timeout=5)

    assert events == ["cancelled"]
    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)
    assert client.status == 499


async def test_a_plain_route_is_not_cancelled(app: FastAPI, postgres_dsn: str, app_name: str) -> None:
    client = HttpClient(path="/plain-sleep")
    task = asyncio.create_task(client.call(app))
    assert await wait_until(lambda: query_running(postgres_dsn, app_name))

    client.disconnect.set()
    await asyncio.wait_for(task, timeout=5)

    assert client.status == 200  # opt-in: it ran to completion


async def test_normal_request_and_body_still_reach_the_handler(app: FastAPI) -> None:
    ok = HttpClient(path="/ok")
    await ok.call(app)
    assert (ok.status, ok.body) == (200, b'[{"one":1}]')

    echo = HttpClient("POST", "/echo", b'{"a": 1}')
    await echo.call(app)
    assert (echo.status, echo.body) == (200, b'{"a":1}')


async def test_errors_of_the_handler_are_handled_as_usual(app: FastAPI) -> None:
    forbidden = HttpClient(path="/forbidden")
    await forbidden.call(app)
    assert forbidden.status == 403

    boom = HttpClient(path="/boom")
    with pytest.raises(RuntimeError, match="boom"):
        await boom.call(app)


async def test_cancelling_the_request_cancels_the_handler_and_the_query(
    app: FastAPI, events: list[str], postgres_dsn: str, app_name: str
) -> None:
    client = HttpClient(path="/sleep")
    task = asyncio.create_task(client.call(app))
    assert await wait_until(lambda: query_running(postgres_dsn, app_name))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # an outer cancellation is never swallowed
        await task

    assert events == ["cancelled"]
    assert await wait_until(lambda: query_stopped(postgres_dsn, app_name), timeout=3)


async def test_no_task_is_left_behind(app: FastAPI) -> None:
    before = len(asyncio.all_tasks())
    client = HttpClient(path="/ok")
    await client.call(app)
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= before


async def test_client_gone_before_the_handler_starts_is_not_an_error(app: FastAPI, events: list[str]) -> None:
    client = HttpClient("POST", "/size", b"x")
    client.gone_at_start = True
    await client.call(app)  # no ClientDisconnect / 500 / traceback
    assert client.status == 499
    assert events == []


async def test_a_large_body_is_streamed_to_the_handler_not_cut_off(app: FastAPI) -> None:
    client = HttpClient("POST", "/size", [b"x" * 65536] * 50)
    await client.call(app)
    assert (client.status, client.body) == (200, b'{"size":3276800}')


async def test_an_unread_body_is_not_buffered(app: FastAPI) -> None:
    """A route that rejects the request before reading the body must not pull the whole upload into memory."""
    client = HttpClient("POST", "/denied", [b"x" * 65536] * 100)
    await client.call(app)
    assert client.status == 401
    assert client.chunks_read <= 12, f"{client.chunks_read} of 100 chunks were read ahead"


async def test_yield_dependencies_see_a_failure_not_a_success_after_a_disconnect(
    app: FastAPI, events: list[str], postgres_dsn: str, app_name: str
) -> None:
    client = HttpClient(path="/in-transaction")
    task = asyncio.create_task(client.call(app))
    assert await wait_until(lambda: query_running(postgres_dsn, app_name))

    client.disconnect.set()
    await asyncio.wait_for(task, timeout=5)

    assert events == ["dependency saw HTTPException"]
    assert client.status == 499


async def test_a_cancellation_during_the_handlers_cleanup_is_not_swallowed(app: FastAPI, events: list[str]) -> None:
    client = HttpClient(path="/slow-cleanup")
    task = asyncio.create_task(client.call(app))
    await asyncio.sleep(0.2)
    client.disconnect.set()
    assert await wait_until(lambda: events == ["cleaning up"], timeout=3)

    task.cancel()  # e.g. server shutdown or a timeout, while the handler is still cleaning up
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)


async def test_a_handler_that_finished_wins_over_a_disconnect_that_arrives_with_it(app: FastAPI) -> None:
    client = HttpClient(path="/instant")
    client.gone_after_body = True
    await client.call(app)
    assert (client.status, client.body) == (200, b'{"done":true}')


async def test_context_variables_and_yield_dependencies_work_as_without_the_route(app: FastAPI) -> None:
    client = HttpClient(path="/context")
    await client.call(app)  # reset() in another context than set() would raise ValueError: 500
    assert (client.status, client.body) == (200, b'{"user":"me"}')


async def test_exception_handlers_can_still_read_the_body(app: FastAPI) -> None:
    client = HttpClient("POST", "/filter", b'{"n": "x"}')
    await asyncio.wait_for(client.call(app), timeout=3)  # used to wait for a body the reader had already taken
    assert client.status == 422
    assert json.loads(client.body)["body"] == '{"n": "x"}'
