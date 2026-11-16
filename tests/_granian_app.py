"""The app served by Granian in test_pg_json_granian.py (its DSN comes from the environment)."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from psycopg_pool import AsyncConnectionPool
from starlette.middleware.base import RequestResponseEndpoint

from pgdevkit.db import fetch_all
from pgdevkit.fastapi import CancelOnDisconnectRoute, PostgresJsonResponse

pool = AsyncConnectionPool(os.environ["PGJSON_TEST_DSN"], min_size=1, max_size=2, open=False)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    async with pool:
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")  # a BaseHTTPMiddleware, as in our apps: it must cope with a handler that was cancelled
async def passthrough(request: Request, call_next: RequestResponseEndpoint) -> Response:
    return await call_next(request)


cancellable = APIRouter(route_class=CancelOnDisconnectRoute)


@cancellable.post("/cancellable-sleep")
async def cancellable_sleep() -> list[dict]:
    return await fetch_all("select pg_sleep(30) as slept", pool=pool)


@cancellable.post("/cancellable-ok")
async def cancellable_ok() -> list[dict]:
    return await fetch_all("select 1 as one", pool=pool)


app.include_router(cancellable)


@app.post("/plain-sleep")  # not opted in: runs to completion even if the client is gone
async def plain_sleep() -> list[dict]:
    return await fetch_all("select pg_sleep(3) as slept", pool=pool)


@app.get("/ok")
async def ok() -> PostgresJsonResponse:
    return PostgresJsonResponse("select i from generate_series(1, 3) i", pool=pool)


@app.get("/sleep")
async def sleep() -> PostgresJsonResponse:
    return PostgresJsonResponse("select pg_sleep(30) as slept", pool=pool)


@app.get("/fails-at-once")  # before the first byte: a 500 JSON error
async def fails_at_once() -> PostgresJsonResponse:
    return PostgresJsonResponse("select 1 / 0 as boom", pool=pool)


@app.get("/fails-midway")
async def fails_midway() -> PostgresJsonResponse:
    return PostgresJsonResponse(
        "select i, repeat('x', 100) as pad, 1 / (case when i = 1500 then 0 else 1 end) as boom "
        "from generate_series(1, 2000) i",
        pool=pool,
    )
