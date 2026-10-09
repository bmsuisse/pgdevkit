"""Drive an ASGI response by hand, with a client that can disconnect at will (httpx's ASGITransport can't)."""

import asyncio
import time
from collections.abc import Awaitable, Callable

import psycopg


class FakeClient:
    def __init__(self) -> None:
        self.disconnect = asyncio.Event()
        self.sent: list[dict] = []
        self.fail_sends_after: int | None = None  # simulate ASGI spec >= 2.4: send() raises OSError once gone
        self.disconnect_when_send_fails = False  # ... and http.disconnect arrives at that very moment

    async def receive(self) -> dict:
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        if self.fail_sends_after is not None and len(self.sent) >= self.fail_sends_after:
            if self.disconnect_when_send_fails:
                self.disconnect.set()
            raise OSError("client went away")
        self.sent.append(message)

    async def call(self, app: Callable[..., Awaitable[None]]) -> None:
        await app({"type": "http", "asgi": {"spec_version": "2.3"}}, self.receive, self.send)

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


async def active_queries(dsn: str, application_name: str) -> int:
    """Number of backends of the given application_name that are executing a query right now."""
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as con:
        cur = await con.execute(
            "select pid from pg_stat_activity where state = 'active' and application_name = %(app)s",
            {"app": application_name},
        )
        return len(await cur.fetchall())


async def wait_until(condition: Callable[[], Awaitable[bool] | bool], *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if (await result) if isinstance(result, Awaitable) else result:
            return True
        await asyncio.sleep(0.05)
    return False
