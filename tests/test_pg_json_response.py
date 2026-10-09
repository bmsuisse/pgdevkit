"""PostgresJsonResponse: output shape, connections and error handling, through a real FastAPI app and real Postgres."""

from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager

import json

import httpx
import psycopg
import pytest
from fastapi import Depends, FastAPI
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.sql import SQL, Literal
from psycopg_pool import AsyncConnectionPool
from sqlglot import exp, select
from starlette.exceptions import HTTPException

from pgdevkit.db import fetch, set_default_pool
from pgdevkit.fastapi import PostgresJsonResponse
from tests._asgi import FakeClient


class VerboseResponse(PostgresJsonResponse):
    expose_errors = True


class DetailResponse(PostgresJsonResponse):
    error_key = "detail"


class ForbiddenPool:
    def connection(self) -> AbstractAsyncContextManager[AsyncConnection]:
        raise HTTPException(status_code=403, detail="no access", headers={"WWW-Authenticate": "Bearer"})


@pytest.fixture
async def pool(postgres_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(postgres_dsn, min_size=1, max_size=1, open=False) as pool:
        yield pool


@pytest.fixture
def default_pool(pool: AsyncConnectionPool) -> Iterator[AsyncConnectionPool]:
    set_default_pool(pool)
    yield pool
    set_default_pool(None)


@pytest.fixture
def no_default_pool() -> Iterator[None]:
    previous = fetch._default_source  # process-wide state: put back whatever was registered
    set_default_pool(None)
    yield
    set_default_pool(previous)


@pytest.fixture
async def client(pool: AsyncConnectionPool) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()

    @app.get("/series")
    async def series(n: int, batch: int = 1000):
        return PostgresJsonResponse(
            "select i as id, 'name ' || i as name from generate_series(1, %(n)s) i order by i;",
            {"n": n},
            pool=pool,
            batch_size=batch,
        )

    @app.get("/composable")
    async def composable():
        return PostgresJsonResponse(SQL("select {} as v").format(Literal(5)), pool=pool)

    @app.get("/line-comment")
    async def line_comment():
        return PostgresJsonResponse("select 1 as a -- trailing comment", pool=pool)

    @app.get("/own-json")
    async def own_json():
        return PostgresJsonResponse(
            "select json_build_object('a', i)::text from generate_series(1, 2) i",
            pool=pool,
            query_produces_json=True,
        )

    @app.get("/headers")
    async def headers():
        return PostgresJsonResponse(
            "select 1 as a", pool=pool, headers={"Cache-Control": "max-age=60"}, status_code=203
        )

    async def own_connection() -> AsyncIterator[AsyncConnection]:
        async with pool.connection() as conn:
            yield conn

    @app.get("/caller-owned")
    async def caller_owned(conn: AsyncConnection = Depends(own_connection)):
        return PostgresJsonResponse("select pg_backend_pid() as pid", con=conn)

    @app.get("/pid")
    async def pid():
        return PostgresJsonResponse("select pg_backend_pid() as pid", pool=pool)

    @app.get("/t-string")
    async def t_string(n: int, evil: str = "x"):
        column = "id"
        return PostgresJsonResponse(
            t"select i as {column:i}, {evil} as note from generate_series(1, {n}) i order by i", pool=pool
        )

    @app.get("/t-string-json")
    async def t_string_json():
        return PostgresJsonResponse(
            t"select json_build_object('a', i)::text from generate_series(1, {2}) i", pool=pool, query_produces_json=True
        )

    @app.get("/default-pool")
    async def default_pool_route():
        return PostgresJsonResponse("select 1 as a")

    @app.get("/no-pool")
    async def no_pool_route():
        return VerboseResponse("select 1 as a")

    @app.get("/stacked")
    async def stacked():
        return VerboseResponse("select 1 as a) s; select 2 as b from (select 1", pool=pool)

    @app.get("/syntax-error-detail")
    async def syntax_error_detail():
        return DetailResponse("selec 1", pool=pool)

    @app.get("/forbidden-detail")
    async def forbidden_detail():
        return DetailResponse("select 1", pool=ForbiddenPool())

    @app.get("/sqlglot-like")
    async def sqlglot_like(prefix: str):
        query = (
            select("name")
            .from_("(select 'a%' as name union all select 'ab' union all select 'b') s")
            .where(exp.column("name").like("a%"))
            .where(exp.column("name").neq(exp.Placeholder(this="prefix")))
        )
        return PostgresJsonResponse(query, {"prefix": prefix}, pool=pool)

    @app.get("/syntax-error")
    async def syntax_error():
        return PostgresJsonResponse("selec 1", pool=pool)

    @app.get("/syntax-error-verbose")
    async def syntax_error_verbose():
        return VerboseResponse("selec 1", pool=pool)

    @app.get("/forbidden")
    async def forbidden_route():
        return PostgresJsonResponse("select 1", pool=ForbiddenPool())

    @app.get("/fails-midway")
    async def fails_midway():
        return PostgresJsonResponse(
            "select i, repeat('x', 100) as pad, 1 / (case when i = 1500 then 0 else 1 end) as boom "
            "from generate_series(1, 2000) i",  # no ORDER BY: it would make Postgres compute every row up front
            pool=pool,
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


async def test_caller_owned_connection_is_not_closed(client: httpx.AsyncClient) -> None:
    first = (await client.get("/caller-owned")).json()[0]["pid"]
    assert (await client.get("/pid")).json()[0]["pid"] == first  # same pooled connection, still open


async def test_pool_connection_is_returned_and_reused(client: httpx.AsyncClient, pool: AsyncConnectionPool) -> None:
    first = (await client.get("/pid")).json()[0]["pid"]
    second = (await client.get("/pid")).json()[0]["pid"]
    assert first == second
    assert pool.get_stats()["pool_available"] == 1


@pytest.mark.usefixtures("default_pool")
async def test_uses_the_default_pool(client: httpx.AsyncClient) -> None:
    assert (await client.get("/default-pool")).json() == [{"a": 1}]


@pytest.mark.usefixtures("no_default_pool")
async def test_without_any_connection_source_the_error_is_reported(client: httpx.AsyncClient) -> None:
    response = await client.get("/no-pool")
    assert response.status_code == 500
    assert "No connection" in response.json()["error"]


async def test_stacked_statements_are_rejected(client: httpx.AsyncClient) -> None:
    """The query is only wrapped, not parsed: the extended protocol (cursor.stream) is what refuses stacking."""
    response = await client.get("/stacked")
    assert response.status_code == 500
    assert "multiple commands" in response.json()["error"]


def test_con_and_pool_are_exclusive(pool: AsyncConnectionPool) -> None:
    with pytest.raises(TypeError):
        PostgresJsonResponse("select 1", con=object(), pool=pool)  # ty: ignore[invalid-argument-type]


async def test_query_error_before_first_byte_is_a_500_without_details(client: httpx.AsyncClient) -> None:
    response = await client.get("/syntax-error")
    assert response.status_code == 500
    assert response.json() == {"error": "Internal Server Error"}


async def test_query_error_details_when_exposed(client: httpx.AsyncClient) -> None:
    response = await client.get("/syntax-error-verbose")
    assert response.status_code == 500
    assert "syntax error" in response.json()["error"]


async def test_http_exception_from_the_pool_keeps_its_status(client: httpx.AsyncClient) -> None:
    response = await client.get("/forbidden")
    assert response.status_code == 403
    assert response.json() == {"error": "no access"}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_error_after_the_first_byte_is_raised_not_hidden(
    client: httpx.AsyncClient, pool: AsyncConnectionPool
) -> None:
    with pytest.raises(psycopg.errors.DivisionByZero):
        await client.get("/fails-midway")
    async with pool.connection() as conn:  # the pool is still healthy
        assert await (await conn.execute("select 1")).fetchone() == (1,)


async def test_connection_with_a_dict_row_factory(postgres_dsn: str) -> None:
    """The response must not depend on the row factory the connection (or a pool's kwargs) came configured with."""
    async with await AsyncConnection.connect(postgres_dsn, row_factory=dict_row) as conn:  # ty: ignore[invalid-argument-type]
        client = FakeClient()
        await client.call(PostgresJsonResponse("select 1 as a, 2 as b", con=conn))
    assert json.loads(client.body) == [{"a": 1, "b": 2}]


@pytest.mark.parametrize("batch_size", [0, -1])
def test_batch_size_must_be_positive(pool: AsyncConnectionPool, batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        PostgresJsonResponse("select 1", pool=pool, batch_size=batch_size)


async def test_t_string_query_binds_values_and_quotes_identifiers(client: httpx.AsyncClient) -> None:
    response = await client.get("/t-string", params={"n": 3})
    assert response.json() == [{"id": i, "note": "x"} for i in (1, 2, 3)]


async def test_t_string_value_stays_a_bound_parameter(client: httpx.AsyncClient) -> None:
    evil = "x') from generate_series(1, 1) i; drop table t; --"
    response = await client.get("/t-string", params={"n": 2, "evil": evil})
    assert [r["note"] for r in response.json()] == [evil, evil]


async def test_t_string_with_query_produces_json(client: httpx.AsyncClient) -> None:
    assert (await client.get("/t-string-json")).json() == [{"a": 1}, {"a": 2}]


def test_t_string_takes_no_params(pool: AsyncConnectionPool) -> None:
    with pytest.raises(TypeError, match="t-string"):
        PostgresJsonResponse(t"select {1}", {"x": 1}, pool=pool)


async def test_literal_percent_in_a_sqlglot_query_with_params(client: httpx.AsyncClient) -> None:
    response = await client.get("/sqlglot-like", params={"prefix": "ab"})
    assert response.json() == [{"name": "a%"}]


async def test_error_key_can_be_switched_to_detail(client: httpx.AsyncClient) -> None:
    response = await client.get("/syntax-error-detail")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    response = await client.get("/forbidden-detail")
    assert response.status_code == 403
    assert response.json() == {"detail": "no access"}
    assert response.headers["www-authenticate"] == "Bearer"
