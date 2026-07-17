from __future__ import annotations

from typing import Literal
from urllib.parse import quote, urlparse, urlunparse

_LAKEBASE_HOST_SUFFIXES = (
    ".database.azuredatabricks.net",
    ".database.cloud.databricks.com",
)
_AZURE_POSTGRES_HOST_SUFFIXES = (
    ".postgres.database.azure.com",
    ".postgres.cosmos.azure.com",
)

_default_credential = None
_managed_identity_credential = None


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
    except ImportError:
        raise ImportError("Install azure-identity extra: pip install pgdevkit[azure]")
    global _default_credential, _managed_identity_credential
    if managed_identity:
        if _managed_identity_credential is None:
            _managed_identity_credential = ManagedIdentityCredential()
        credential = _managed_identity_credential
    else:
        if _default_credential is None:
            _default_credential = DefaultAzureCredential(
                exclude_interactive_browser_credential=exclude_interactive_browser_credential
            )
        credential = _default_credential
    token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default")
    return token.token


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
            raise ValueError(
                "Lakebase host detected — pass --databricks-workspace-host and --databricks-instance"
            )
        from .lakebase import get_lakebase_password

        password = get_lakebase_password(databricks_workspace_host, databricks_instance)
    else:
        password = get_azure_postgres_password(
            managed_identity=managed_identity,
            exclude_interactive_browser_credential=exclude_interactive_browser_credential,
        )

    netloc = f"{quote(entra_user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunparse(parsed._replace(netloc=netloc))
