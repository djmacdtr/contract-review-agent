from urllib.parse import urlsplit, urlunsplit


def sanitize_url(url: str) -> str:
    """Remove credentials, query and fragment from a URL used outside the worker boundary."""
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))

