from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

import pgdevkit.lakebase as lakebase


@pytest.fixture(autouse=True)
def _clear_caches():
    lakebase._credential_cache.clear()
    lakebase._dns_cache.clear()
    yield
    lakebase._credential_cache.clear()
    lakebase._dns_cache.clear()


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_get_lakebase_password_fetches_and_caches(monkeypatch):
    monkeypatch.setattr(lakebase, "_databricks_resource_token", lambda: "ENTRA_TOKEN")
    calls = []

    def fake_urlopen(req, timeout=10):
        calls.append(req)
        return _FakeResponse({"token": "PGTOKEN", "expiration_time": "2099-01-01T00:00:00Z"})

    monkeypatch.setattr(lakebase.urllib_request, "urlopen", fake_urlopen)

    token = lakebase.get_lakebase_password("https://adb-123.azuredatabricks.net", "myinstance")
    assert token == "PGTOKEN"
    assert len(calls) == 1
    assert calls[0].full_url == "https://adb-123.azuredatabricks.net/api/2.0/database/credentials"
    assert calls[0].get_header("Authorization") == "Bearer ENTRA_TOKEN"
    body = json.loads(calls[0].data.decode("utf-8"))
    assert body["instance_names"] == ["myinstance"]

    # Second call within validity window must use the cache, not call urlopen again.
    token2 = lakebase.get_lakebase_password("https://adb-123.azuredatabricks.net", "myinstance")
    assert token2 == "PGTOKEN"
    assert len(calls) == 1


def test_get_lakebase_password_refreshes_when_near_expiry(monkeypatch):
    monkeypatch.setattr(lakebase, "_databricks_resource_token", lambda: "ENTRA_TOKEN")
    responses = iter(
        [
            {"token": "FIRST", "expiration_time": "2020-01-01T00:00:01Z"},
            {"token": "SECOND", "expiration_time": "2099-01-01T00:00:00Z"},
        ]
    )

    def fake_urlopen(req, timeout=10):
        return _FakeResponse(next(responses))

    monkeypatch.setattr(lakebase.urllib_request, "urlopen", fake_urlopen)

    first = lakebase.get_lakebase_password("https://adb-123.azuredatabricks.net", "myinstance")
    assert first == "FIRST"
    second = lakebase.get_lakebase_password("https://adb-123.azuredatabricks.net", "myinstance")
    assert second == "SECOND"


def test_get_lakebase_password_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(lakebase, "_databricks_resource_token", lambda: "ENTRA_TOKEN")

    def fake_urlopen(req, timeout=10):
        raise HTTPError(req.full_url, 403, "Forbidden", {}, BytesIO(b'{"message": "no access"}'))

    monkeypatch.setattr(lakebase.urllib_request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="403"):
        lakebase.get_lakebase_password("https://adb-123.azuredatabricks.net", "myinstance")


def test_resolve_read_write_dns_caches(monkeypatch):
    monkeypatch.setattr(lakebase, "_databricks_resource_token", lambda: "ENTRA_TOKEN")
    calls = []

    def fake_urlopen(req, timeout=10):
        calls.append(req)
        return _FakeResponse({"read_write_dns": "instance-abc.database.azuredatabricks.net"})

    monkeypatch.setattr(lakebase.urllib_request, "urlopen", fake_urlopen)

    dns = lakebase.resolve_read_write_dns("https://adb-123.azuredatabricks.net", "myinstance")
    assert dns == "instance-abc.database.azuredatabricks.net"
    assert calls[0].full_url == "https://adb-123.azuredatabricks.net/api/2.0/database/instances/myinstance"

    lakebase.resolve_read_write_dns("https://adb-123.azuredatabricks.net", "myinstance")
    assert len(calls) == 1  # cached, no second request
