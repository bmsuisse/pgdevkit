"""CancelOnDisconnectRoute cancels the handler (and with it the Postgres query) when the client goes away."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from psycopg_pool import AsyncConnectionPool

from pgdevkit.db import fetch_all
from pgdevkit.fastapi import CancelOnDisconnectRoute
from tests._asgi import active_queries, wait_until


class HttpClient:
    """Drives an ASGI app by hand: sends one request, then stays connected until `disconnect` is set."""

    def __init__(self, method: str = "GET", path: str = "/", body: bytes = b"") -> None:
        self.method, self.path, self.request_body = method, path, body
        self.disconnect = asyncio.Event()
        self.sent: list[dict] = []
        self._body_sent = False
        self.gone_at_start = False  # the client disconnected before the body arrived

    async def receive(self) -> dict:
        if self.gone_at_start:
            return {"type": "http.disconnect"}
        if not self._body_sent:
            self._body_sent = True
            return {"type": "http.request", "body": self.request_body, "more_body": False}
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
            "headers": [(b"content-type", b"application/json")] if self.request_body else [],
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

    @cancellable.get("/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403, detail="no")

    @cancellable.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    app = FastAPI()
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
    client = HttpClient(path="/ok")
    client.gone_at_start = True
    await client.call(app)  # no ClientDisconnect / 500 / traceback
    assert client.status == 499
    assert events == []
