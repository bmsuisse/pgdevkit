from __future__ import annotations
from urllib.parse import urlparse, urlunparse, quote


def _get_entra_token() -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        raise ImportError("Install azure-identity extra: pip install pgdevkit[azure]")
    cred = DefaultAzureCredential()
    token = cred.get_token("https://ossrdbms-aad.database.windows.net/.default")
    return token.token


def build_conninfo(url: str, entra_user: str | None = None) -> str:
    if entra_user is None:
        return url
    token = _get_entra_token()
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(entra_user, safe='')}:{quote(token, safe='')}@{host}{port}"
    return urlunparse(parsed._replace(netloc=netloc))
