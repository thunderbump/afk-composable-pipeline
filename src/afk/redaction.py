from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

URL_PATTERN = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)")
TRAILING_URL_PUNCTUATION = ".,;:)]}"
SECRET_KEY_PATTERN = re.compile(
    r"(^|[._-])(auth|credentials?|password|secret|token|api[._-]?key|env)($|[._-])",
    re.IGNORECASE,
)
SECRET_KEY_COMPONENTS = {"auth", "credential", "credentials", "password", "secret", "token", "env"}
KEY_COMPONENT_PATTERN = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")
SECRET_FLAG_COMPONENTS = {"auth", "credential", "credentials", "password", "secret", "token"}
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
)
JSON_SECRET_STRING_PATTERN = re.compile(
    r"(?P<prefix>(?P<key_quote>[\"'])(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"(?P=key_quote)\s*:\s*(?P<value_quote>[\"']))"
    r"(?P<value>[^\"'\r\n]*)"
    r"(?P=value_quote)",
    re.IGNORECASE,
)
SECRET_TOKEN_VALUE_PATTERN = re.compile(
    r"\b("
    r"gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"sk-proj-[A-Za-z0-9_-]{20,}|"
    r"sk-ant-[A-Za-z0-9_-]{20,}|"
    r"AIza[A-Za-z0-9_-]{20,}"
    r")\b"
)
BEARER_SECRET_PATTERN = re.compile(
    r"(?P<prefix>\bBearer\s+)(?P<quote>[\"']?)(?P<value>[A-Za-z0-9._~+/=-]{12,})(?P=quote)",
    re.IGNORECASE,
)
SAFE_BEARER_WORDS = {"unauthorized", "authorizationfailed", "missingcredential"}
MIN_EXACT_SECRET_LENGTH = 4


def redact_artifact_value(value: Any, *, exact_secrets: set[str] | None = None) -> Any:
    return redact_artifact_value_for_key(None, value, exact_secrets=exact_secrets)


def redact_artifact_value_for_key(
    key: str | None,
    value: Any,
    *,
    exact_secrets: set[str] | None = None,
) -> Any:
    if key is not None and is_secret_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            item_key: redact_artifact_value_for_key(str(item_key), item, exact_secrets=exact_secrets)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        if key == "command":
            return redact_command_list(value, exact_secrets=exact_secrets)
        return [redact_artifact_value_for_key(key, item, exact_secrets=exact_secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, exact_secrets=exact_secrets)
    return value


def redact_command_list(value: list[Any], *, exact_secrets: set[str] | None = None) -> list[Any]:
    redacted: list[Any] = []
    redact_next = False
    for item in value:
        if not isinstance(item, str):
            redacted.append(redact_artifact_value_for_key(None, item, exact_secrets=exact_secrets))
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
        redacted.append(redact_text(item, exact_secrets=exact_secrets))
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


def is_secret_key(value: str) -> bool:
    if SECRET_KEY_PATTERN.search(value):
        return True
    components = key_components(value)
    if any(component in SECRET_KEY_COMPONENTS for component in components):
        return True
    if "apikey" in components:
        return True
    return any(left == "api" and right == "key" for left, right in zip(components, components[1:]))


def is_secret_value(value: str) -> bool:
    if SECRET_TOKEN_VALUE_PATTERN.search(value):
        return True
    if any(is_secret_key(match.group("key")) for match in JSON_SECRET_STRING_PATTERN.finditer(value)):
        return True
    if any(is_secret_key(match.group("key")) for match in SECRET_ASSIGNMENT_PATTERN.finditer(value)):
        return True
    return any(url_has_secret_value(match.group("url")) for match in URL_PATTERN.finditer(value))


def key_components(value: str) -> list[str]:
    components = []
    for chunk in re.split(r"[._-]+", value):
        components.extend(match.group(0).lower() for match in KEY_COMPONENT_PATTERN.finditer(chunk))
    return components


def redact_text(value: str, *, exact_secrets: set[str] | None = None) -> str:
    redacted = URL_PATTERN.sub(redact_url_match, value)
    redacted = JSON_SECRET_STRING_PATTERN.sub(redact_json_secret_string, redacted)
    redacted = SECRET_ASSIGNMENT_PATTERN.sub(redact_secret_assignment, redacted)
    redacted = SECRET_TOKEN_VALUE_PATTERN.sub("[REDACTED]", redacted)
    redacted = BEARER_SECRET_PATTERN.sub(redact_bearer_secret, redacted)
    return redact_exact_secret_values(redacted, exact_secrets=exact_secrets)


def redact_exact_secret_values(value: str, *, exact_secrets: set[str] | None = None) -> str:
    if not exact_secrets:
        return value
    redacted = value
    for secret in sorted(normalize_exact_secrets(exact_secrets), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def normalize_exact_secrets(values: set[str] | None) -> set[str]:
    if not values:
        return set()
    normalized = set()
    for value in values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if len(stripped) < MIN_EXACT_SECRET_LENGTH:
            continue
        normalized.add(stripped)
    return normalized


def bearer_secret_present(value: str) -> bool:
    return any(is_bearer_secret_value(match.group("value")) for match in BEARER_SECRET_PATTERN.finditer(value))


def is_bearer_secret_value(value: str) -> bool:
    normalized = normalize_bearer_secret_value(value)
    unpadded = normalized.rstrip("=")
    if len(unpadded) < 12:
        return False
    if "=" in unpadded:
        return False
    if unpadded.isalpha() and unpadded.islower() and unpadded in SAFE_BEARER_WORDS:
        return False
    return any(char.isdigit() for char in unpadded) or any(char.isupper() for char in unpadded) or any(
        char in "./+~_-" for char in unpadded
    ) or unpadded.isalpha()


def normalize_bearer_secret_value(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "\"'":
        return normalized[1:-1].strip()
    return normalized


def redact_bearer_secret(match: re.Match[str]) -> str:
    if not is_bearer_secret_value(match.group("value")):
        return match.group(0)
    return f"{match.group('prefix')}[REDACTED]"


def redact_json_secret_string(match: re.Match[str]) -> str:
    if not is_secret_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}[REDACTED]{match.group('value_quote')}"


def redact_secret_assignment(match: re.Match[str]) -> str:
    if not is_secret_key(match.group("key")):
        return match.group(0)
    return f"{match.group('key')}{match.group('separator')}[REDACTED]"


def redact_url_match(match: re.Match[str]) -> str:
    raw_url = match.group("url")
    suffix = ""
    while raw_url and raw_url[-1] in TRAILING_URL_PUNCTUATION:
        suffix = raw_url[-1] + suffix
        raw_url = raw_url[:-1]
    return redact_url(raw_url) + suffix


def url_has_secret_value(value: str) -> bool:
    while value and value[-1] in TRAILING_URL_PUNCTUATION:
        value = value[:-1]
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        return True
    query_keys = [key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    fragment_keys = [key for key, _ in parse_qsl(parsed.fragment, keep_blank_values=True)]
    return any(is_secret_key(key) for key in [*query_keys, *fragment_keys])


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
