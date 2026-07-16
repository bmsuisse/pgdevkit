from __future__ import annotations

from typing import Literal
from urllib.parse import quote, urlparse, urlunparse

_LAKEBASE_HOST_SUFFIXES = (
    ".database.azuredatabricks.net",
    ".database.cloud.databricks.com",
)


def detect_provider(host: str) -> Literal["azure_postgres", "databricks_lakebase"]:
    """Classify a Postgres hostname: Databricks Lakebase (needs credential
    exchange) or the default Azure Postgres AAD token flow."""
    if any(host.endswith(suffix) for suffix in _LAKEBASE_HOST_SUFFIXES):
        return "databricks_lakebase"
    return "azure_postgres"


def get_azure_postgres_password() -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        raise ImportError("Install azure-identity extra: pip install pgdevkit[azure]")
    cred = DefaultAzureCredential()
    token = cred.get_token("https://ossrdbms-aad.database.windows.net/.default")
    return token.token


def build_conninfo(
    url: str,
    entra_user: str | None = None,
    *,
    databricks_workspace_host: str | None = None,
    databricks_instance: str | None = None,
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
        password = get_azure_postgres_password()

    netloc = f"{quote(entra_user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunparse(parsed._replace(netloc=netloc))
