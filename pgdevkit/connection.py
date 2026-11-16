from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote, urlparse, urlunparse

_LAKEBASE_HOST_SUFFIXES = (
    ".database.azuredatabricks.net",
    ".database.cloud.databricks.com",
)
_AZURE_POSTGRES_HOST_SUFFIXES = (
    ".postgres.database.azure.com",
    ".postgres.cosmos.azure.com",
)

# Process-wide credential cache, keyed "default" / "managed_identity" (see get_azure_postgres_password)
_credentials: dict[str, Any] = {}


def detect_provider(host: str) -> Literal["azure_postgres", "databricks_lakebase"]:
    """Classify a Postgres hostname for Entra ID auth: Databricks Lakebase
    (needs credential exchange) or the default Azure Postgres AAD token
    flow — the latter is the fallback for any non-Lakebase host, since
    `entra_user` is itself the caller's assertion that Entra auth applies.
    For a strict "is this actually Azure Database for PostgreSQL" check
    (e.g. to decide PgBouncer-aware pooling), use `is_azure_postgres_host`
    instead."""
    if any(host.endswith(suffix) for suffix in _LAKEBASE_HOST_SUFFIXES):
        return "databricks_lakebase"
    return "azure_postgres"


def is_azure_postgres_host(host: str) -> bool:
    """True only for actual Azure Database for PostgreSQL hostnames —
    unlike `detect_provider`, this is not a fallback default."""
    return any(host.endswith(suffix) for suffix in _AZURE_POSTGRES_HOST_SUFFIXES)


def get_azure_postgres_password(
    *,
    managed_identity: bool = False,
    exclude_interactive_browser_credential: bool = True,
) -> str:
    """Fetch an Entra ID token to use as an Azure Postgres password.

    `managed_identity=True` uses `ManagedIdentityCredential` (for workloads
    running under an Azure-assigned identity); otherwise
    `DefaultAzureCredential`, whose credential chain already falls back to
    managed identity when no other credential is available. Both credential
    objects are process-cached so repeated calls (one per new pooled
    connection) don't re-probe the credential chain every time."""
    try:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    except ImportError as e:
        raise ImportError("Install azure-identity extra: pip install pgdevkit[azure]") from e
    key = "managed_identity" if managed_identity else "default"
    credential = _credentials.get(key)
    if credential is None:
        if managed_identity:
            credential = ManagedIdentityCredential()
        else:
            credential = DefaultAzureCredential(
                exclude_interactive_browser_credential=exclude_interactive_browser_credential
            )
        _credentials[key] = credential
    token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default")
    return token.token


def build_mssql_conninfo(conn_str: str, entra_user: str | None = None) -> str:
    """MSSQL counterpart of `build_conninfo`. Without `entra_user` the ODBC connection
    string is used as-is. With it, `Authentication=ActiveDirectoryDefault` is appended so
    mssql-python acquires an Entra ID token itself via azure-identity's
    `DefaultAzureCredential` (needs the `azure` extra); the identity is whatever that
    credential chain resolves, so `entra_user` only switches Entra auth on and is not
    sent to the server. A connection string that already sets `Authentication` is left
    to speak for itself and rejected here to avoid ambiguity."""
    if entra_user is None:
        return conn_str
    if re.search(r"(^|;)\s*Authentication\s*=", conn_str, re.IGNORECASE):
        raise ValueError("--entra-user can't be combined with an Authentication= setting in the connection string")
    return f"{conn_str.rstrip().rstrip(';')};Authentication=ActiveDirectoryDefault"


def build_conninfo(
    url: str,
    entra_user: str | None = None,
    *,
    databricks_workspace_host: str | None = None,
    databricks_instance: str | None = None,
    managed_identity: bool = False,
    exclude_interactive_browser_credential: bool = True,
) -> str:
    if entra_user is None:
        return url

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""

    if detect_provider(host) == "databricks_lakebase":
        if not databricks_workspace_host or not databricks_instance:
            raise ValueError("Lakebase host detected — pass --databricks-workspace-host and --databricks-instance")
        from pgdevkit.lakebase import get_lakebase_password

        password = get_lakebase_password(databricks_workspace_host, databricks_instance)
    else:
        password = get_azure_postgres_password(
            managed_identity=managed_identity,
            exclude_interactive_browser_credential=exclude_interactive_browser_credential,
        )

    netloc = f"{quote(entra_user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunparse(parsed._replace(netloc=netloc))
