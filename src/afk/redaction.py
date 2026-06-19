from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


def redact_artifact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_artifact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_artifact_value(item) for item in value]
    if isinstance(value, str):
        return redact_url(value)
    return value


def redact_text(value: str) -> str:
    words = value.split()
    return " ".join(redact_url(word) for word in words)


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or "@" not in parsed.netloc:
        return value
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
