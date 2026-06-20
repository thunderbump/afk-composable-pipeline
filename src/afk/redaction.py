from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

URL_PATTERN = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)")
TRAILING_URL_PUNCTUATION = ".,;:)]}"
SECRET_KEY_PATTERN = re.compile(
    r"(^|[._-])(auth|credentials?|password|secret|token|api[._-]?key|env)($|[._-])",
    re.IGNORECASE,
)
SECRET_FLAG_COMPONENTS = {"auth", "credential", "credentials", "password", "secret", "token"}
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|AUTH|API_KEY|CREDENTIAL)[A-Za-z0-9_]*"
    r"|TOKEN|SECRET|PASSWORD|AUTH|API_KEY|CREDENTIAL)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
JSON_SECRET_STRING_PATTERN = re.compile(
    r"(?P<prefix>(?P<key_quote>[\"'])(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"(?P=key_quote)\s*:\s*(?P<value_quote>[\"']))"
    r"(?P<value>[^\"'\r\n]*)"
    r"(?P=value_quote)",
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
        if is_secret_command_flag(item):
            if "=" in item:
                redacted.append(item.split("=", 1)[0] + "=[REDACTED]")
            else:
                redacted.append(item)
                redact_next = True
            continue
        redacted.append(redact_text(item))
    return redacted


def is_secret_command_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized.startswith("-"):
        return False
    flag = normalized.split("=", 1)[0]
    flag_name = flag.lstrip("-")
    components = [part for part in re.split(r"[._-]+", flag_name) if part]
    if any(part in SECRET_FLAG_COMPONENTS for part in components):
        return True
    if "apikey" in components:
        return True
    return any(left == "api" and right == "key" for left, right in zip(components, components[1:]))


def redact_text(value: str) -> str:
    redacted = URL_PATTERN.sub(redact_url_match, value)
    redacted = JSON_SECRET_STRING_PATTERN.sub(redact_json_secret_string, redacted)
    return SECRET_ASSIGNMENT_PATTERN.sub(redact_secret_assignment, redacted)


def redact_json_secret_string(match: re.Match[str]) -> str:
    if not SECRET_KEY_PATTERN.search(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}[REDACTED]{match.group('value_quote')}"


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
