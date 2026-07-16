from __future__ import annotations

import datetime
import json
import time
import uuid
from urllib import request as urllib_request
from urllib.error import HTTPError

_DATABRICKS_RESOURCE_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"
_REFRESH_MARGIN_SECONDS = 300

_credential: object | None = None
_credential_cache: dict[tuple[str, str], tuple[str, float]] = {}
_dns_cache: dict[tuple[str, str], str] = {}


def _databricks_resource_token() -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        raise ImportError("Install azure-identity extra: pip install pgdevkit[azure]")
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential.get_token(_DATABRICKS_RESOURCE_SCOPE).token


def _request_json(url: str, token: str, *, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib_request.Request(
        url,
        data=data,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Databricks API request failed [{e.code}]: {detail}") from e


def _parse_expiration(expiration_time: str) -> float:
    return datetime.datetime.fromisoformat(expiration_time).timestamp()


def get_lakebase_password(workspace_host: str, instance_name: str) -> str:
    """Return a valid short-lived Postgres password for the given Lakebase
    instance, fetching/refreshing the credential as needed."""
    key = (workspace_host, instance_name)
    cached = _credential_cache.get(key)
    if cached is not None:
        token, expires_at = cached
        if time.time() < expires_at - _REFRESH_MARGIN_SECONDS:
            return token

    entra_token = _databricks_resource_token()
    response = _request_json(
        f"{workspace_host.rstrip('/')}/api/2.0/database/credentials",
        entra_token,
        body={"instance_names": [instance_name], "request_id": str(uuid.uuid4())},
    )
    token = response["token"]
    expires_at = _parse_expiration(response["expiration_time"])
    _credential_cache[key] = (token, expires_at)
    return token


def resolve_read_write_dns(workspace_host: str, instance_name: str) -> str:
    """Resolve and cache the Postgres read/write DNS endpoint for a Lakebase instance."""
    key = (workspace_host, instance_name)
    if key in _dns_cache:
        return _dns_cache[key]
    entra_token = _databricks_resource_token()
    response = _request_json(
        f"{workspace_host.rstrip('/')}/api/2.0/database/instances/{instance_name}",
        entra_token,
    )
    dns = response["read_write_dns"]
    _dns_cache[key] = dns
    return dns
