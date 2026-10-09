"""PostgresJsonResponse: output shape, sources and error handling, through a real FastAPI app and real Postgres."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import psycopg
import pytest
from fastapi import Depends, FastAPI
from psycopg import AsyncConnection
from psycopg.sql import SQL, Literal
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException

from pgdevkit.fastapi import PostgresJsonResponse


class VerboseResponse(PostgresJsonResponse):
    expose_errors = True


@pytest.fixture
async def pool(postgres_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(postgres_dsn, min_size=1, max_size=1, open=False) as pool:
        yield pool


@pytest.fixture
async def client(pool: AsyncConnectionPool) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()

    @app.get("/series")
    async def series(n: int, batch: int = 1000):
        return PostgresJsonResponse(
            pool.connection,
            "select i as id, 'name ' || i as name from generate_series(1, %(n)s) i order by i;",
            parameters={"n": n},
            batch_size=batch,
        )

    @app.get("/composable")
    async def composable():
        return PostgresJsonResponse(pool.connection, SQL("select {} as v").format(Literal(5)))

    @app.get("/line-comment")
    async def line_comment():
        return PostgresJsonResponse(pool.connection, "select 1 as a -- trailing comment")

    @app.get("/own-json")
    async def own_json():
        return PostgresJsonResponse(
            pool.connection,
            "select json_build_object('a', i)::text from generate_series(1, 2) i",
            query_produces_json=True,
        )

    @app.get("/headers")
    async def headers():
        return PostgresJsonResponse(
            pool.connection, "select 1 as a", headers={"Cache-Control": "max-age=60"}, status_code=203
        )

    async def own_connection() -> AsyncIterator[AsyncConnection]:
        async with pool.connection() as conn:
            yield conn

    @app.get("/caller-owned")
    async def caller_owned(conn: AsyncConnection = Depends(own_connection)):
        return PostgresJsonResponse(conn, "select pg_backend_pid() as pid")

    @app.get("/pid")
    async def pid():
        return PostgresJsonResponse(pool.connection, "select pg_backend_pid() as pid")

    @app.get("/syntax-error")
    async def syntax_error():
        return PostgresJsonResponse(pool.connection, "selec 1")

    @app.get("/syntax-error-verbose")
    async def syntax_error_verbose():
        return VerboseResponse(pool.connection, "selec 1")

    @asynccontextmanager
    async def forbidden() -> AsyncIterator[AsyncConnection]:
        raise HTTPException(status_code=403, detail="no access")
        yield

    @app.get("/forbidden")
    async def forbidden_route():
        return PostgresJsonResponse(forbidden, "select 1")

    @app.get("/fails-midway")
    async def fails_midway():
        return PostgresJsonResponse(
            pool.connection,
            "select i, repeat('x', 100) as pad, 1 / (case when i = 1500 then 0 else 1 end) as boom "
            "from generate_series(1, 2000) i",  # no ORDER BY: it would make Postgres compute every row up front
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("n", [0, 1, 999, 1000, 1001, 2500])
async def test_series_is_a_complete_json_array(client: httpx.AsyncClient, n: int) -> None:
    response = await client.get("/series", params={"n": n})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == [{"id": i, "name": f"name {i}"} for i in range(1, n + 1)]


@pytest.mark.parametrize(("n", "batch"), [(4, 2), (5, 2), (1, 2)])
async def test_series_with_small_batches(client: httpx.AsyncClient, n: int, batch: int) -> None:
    response = await client.get("/series", params={"n": n, "batch": batch})
    assert [r["id"] for r in response.json()] == list(range(1, n + 1))


async def test_composable_query(client: httpx.AsyncClient) -> None:
    assert (await client.get("/composable")).json() == [{"v": 5}]


async def test_query_ending_in_line_comment(client: httpx.AsyncClient) -> None:
    assert (await client.get("/line-comment")).json() == [{"a": 1}]


async def test_query_produces_json(client: httpx.AsyncClient) -> None:
    assert (await client.get("/own-json")).json() == [{"a": 1}, {"a": 2}]


async def test_status_and_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/headers")
    assert response.status_code == 203
    assert response.headers["cache-control"] == "max-age=60"


async def test_caller_owned_connection_is_not_closed(client: httpx.AsyncClient, pool: AsyncConnectionPool) -> None:
    first = (await client.get("/caller-owned")).json()[0]["pid"]
    assert (await client.get("/pid")).json()[0]["pid"] == first  # same pooled connection, still open


async def test_pool_connection_is_returned_and_reused(client: httpx.AsyncClient, pool: AsyncConnectionPool) -> None:
    first = (await client.get("/pid")).json()[0]["pid"]
    second = (await client.get("/pid")).json()[0]["pid"]
    assert first == second
    assert pool.get_stats()["pool_available"] == 1


async def test_query_error_before_first_byte_is_a_500_without_details(client: httpx.AsyncClient) -> None:
    response = await client.get("/syntax-error")
    assert response.status_code == 500
    assert response.json() == {"error": "Internal Server Error"}


async def test_query_error_details_when_exposed(client: httpx.AsyncClient) -> None:
    response = await client.get("/syntax-error-verbose")
    assert response.status_code == 500
    assert "syntax error" in response.json()["error"]


async def test_http_exception_from_the_source_keeps_its_status(client: httpx.AsyncClient) -> None:
    response = await client.get("/forbidden")
    assert response.status_code == 403
    assert response.json() == {"error": "no access"}


async def test_error_after_the_first_byte_is_raised_not_hidden(
    client: httpx.AsyncClient, pool: AsyncConnectionPool
) -> None:
    with pytest.raises(psycopg.errors.DivisionByZero):
        await client.get("/fails-midway")
    async with pool.connection() as conn:  # the pool is still healthy
        assert await (await conn.execute("select 1")).fetchone() == (1,)
