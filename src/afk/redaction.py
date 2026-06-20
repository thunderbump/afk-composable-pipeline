from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

URL_PATTERN = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)")
TRAILING_URL_PUNCTUATION = ".,;:)]}"


def redact_artifact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_artifact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_artifact_value(item) for item in value]
    if isinstance(value, str):
        return redact_url(value)
    return value


def redact_text(value: str) -> str:
    return URL_PATTERN.sub(redact_url_match, value)


def redact_url_match(match: re.Match[str]) -> str:
    raw_url = match.group("url")
    suffix = ""
    while raw_url and raw_url[-1] in TRAILING_URL_PUNCTUATION:
        suffix = raw_url[-1] + suffix
        raw_url = raw_url[:-1]
    return redact_url(raw_url) + suffix


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or "@" not in parsed.netloc:
        if parsed.scheme and parsed.netloc and (parsed.query or parsed.fragment):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return value
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
