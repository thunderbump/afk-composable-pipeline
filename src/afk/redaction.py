from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

URL_PATTERN = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)")
TRAILING_URL_PUNCTUATION = ".,;:)]}"
SECRET_KEY_PATTERN = re.compile(r"(auth|credential|password|secret|token|api[_-]?key|env)", re.IGNORECASE)
SECRET_FLAG_PATTERN = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9_-]*(=.*)?$")
SECRET_FLAG_NAME_PATTERN = re.compile(
    r"(auth|credential|password|secret|token|api[-_]?key)",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|AUTH|API_KEY|CREDENTIAL)[A-Za-z0-9_]*)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)


def redact_artifact_value(value: Any) -> Any:
    return redact_artifact_value_for_key(None, value)


def redact_artifact_value_for_key(key: str | None, value: Any) -> Any:
    if key is not None and SECRET_KEY_PATTERN.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            item_key: redact_artifact_value_for_key(str(item_key), item)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        if key == "command":
            return redact_command_list(value)
        return [redact_artifact_value_for_key(key, item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_command_list(value: list[Any]) -> list[Any]:
    redacted: list[Any] = []
    redact_next = False
    for item in value:
        if not isinstance(item, str):
            redacted.append(redact_artifact_value_for_key(None, item))
            redact_next = False
            continue
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if SECRET_FLAG_PATTERN.match(item):
            flag_name = item.split("=", 1)[0].lstrip("-")
            if SECRET_FLAG_NAME_PATTERN.search(flag_name):
                if "=" in item:
                    redacted.append(item.split("=", 1)[0] + "=[REDACTED]")
                else:
                    redacted.append(item)
                    redact_next = True
                continue
        redacted.append(redact_text(item))
    return redacted


def redact_text(value: str) -> str:
    redacted = URL_PATTERN.sub(redact_url_match, value)
    return SECRET_ASSIGNMENT_PATTERN.sub(redact_secret_assignment, redacted)


def redact_secret_assignment(match: re.Match[str]) -> str:
    return f"{match.group('key')}{match.group('separator')}[REDACTED]"


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
