"""PostgresJsonResponse under Granian, the server our apps run on: real sockets, real aborted clients."""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests._asgi import active_queries, wait_until

pytest.importorskip("granian")


@pytest.fixture(scope="module")
def app_name() -> str:
    return f"pgjson-granian-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def server(postgres_dsn: str, app_name: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = tmp_path_factory.mktemp("granian") / "granian.log"
    env = {**os.environ, "PGJSON_TEST_DSN": f"{postgres_dsn} application_name={app_name}"}
    with log.open("w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "granian", "--interface", "asgi", "--port", str(port), "tests._granian_app:app"],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=Path(__file__).parent.parent,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if httpx.get(f"{url}/ok").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"granian did not start:\n{log.read_text()}")
                time.sleep(0.2)
            yield url
        finally:
            proc.terminate()
            proc.wait(timeout=10)


async def test_normal_response(server: str) -> None:
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{server}/ok")).json() == [{"i": 1}, {"i": 2}, {"i": 3}]


async def test_aborted_client_cancels_the_query(server: str, postgres_dsn: str, app_name: str) -> None:
    async def query_running() -> bool:
        return await active_queries(postgres_dsn, app_name) == 1

    async def query_stopped() -> bool:
        return await active_queries(postgres_dsn, app_name) == 0

    async with httpx.AsyncClient(timeout=1) as client:
        with pytest.raises(httpx.ReadTimeout):  # the client gives up and drops the connection
            await client.get(f"{server}/sleep")

    assert await wait_until(query_stopped, timeout=5), "the query kept running after the client left"
    assert await wait_until(query_running, timeout=0.2) is False


async def test_error_after_the_first_byte_ends_the_response_without_a_valid_json_array(server: str) -> None:
    """Granian ends the response when the app raises; the body lacks the closing bracket, so parsing fails.
    (The response must also end promptly: with an anyio task group around the body it used to hang.)"""
    async with httpx.AsyncClient(timeout=3) as client:
        started = time.monotonic()
        response = await client.get(f"{server}/fails-midway")
        assert time.monotonic() - started < 2
        assert response.status_code == 200
        assert response.content.startswith(b"[{")
        with pytest.raises(json.JSONDecodeError):
            response.json()
        assert (await client.get(f"{server}/ok")).status_code == 200  # the server and its pool are fine
