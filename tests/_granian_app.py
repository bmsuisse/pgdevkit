"""The app served by Granian in test_pg_json_granian.py (its DSN comes from the environment)."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from pgdevkit.fastapi import PostgresJsonResponse

pool = AsyncConnectionPool(os.environ["PGJSON_TEST_DSN"], min_size=1, max_size=2, open=False)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with pool:
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/ok")
async def ok():
    return PostgresJsonResponse(pool.connection, "select i from generate_series(1, 3) i")


@app.get("/sleep")
async def sleep():
    return PostgresJsonResponse(pool.connection, "select pg_sleep(30) as slept")


@app.get("/fails-midway")
async def fails_midway():
    return PostgresJsonResponse(
        pool.connection,
        "select i, repeat('x', 100) as pad, 1 / (case when i = 1500 then 0 else 1 end) as boom "
        "from generate_series(1, 2000) i",
    )
