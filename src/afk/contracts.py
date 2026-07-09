from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_TERMINAL_CLASSIFY_TIMEOUT_SECONDS = 3600
DEFAULT_TERMINAL_MERGE_TIMEOUT_SECONDS = 300
DEFAULT_TERMINAL_POLICY_POLL_SECONDS = 300
ALLOWED_TERMINAL_POLICY_ACTIONS = {"allow", "block"}
ALLOWED_TERMINAL_MERGE_METHODS = {"merge", "squash", "rebase"}
ALLOWED_VALIDATION_MODES = {"fake", "project-worker"}


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectContractIdentity:
    path: str
    sha256: str

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class ProjectContract:
    project_slug: str
    repo_url: str
    base_branch: str
    beads_labels: tuple[str, ...]
    validation_profiles: tuple[str, ...]
    validation_profile_requests: dict[str, dict[str, Any]]
    artifact_retention: dict[str, Any]
    pr_target: dict[str, str]
    terminal_integration: dict[str, Any]
    identity: ProjectContractIdentity


def load_project_contract(
    project_slug: str,
    contracts_dir: Path,
    *,
    cwd: Path | None = None,
) -> ProjectContract:
    if "/" in project_slug or "\\" in project_slug or project_slug in {"", ".", ".."}:
        raise ContractError(f"invalid project slug: {project_slug!r}")

    path = contracts_dir / f"{project_slug}.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ContractError(f"project contract not found: {path}") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid project contract {path}: expected UTF-8 JSON") from exc

    contract = validate_project_contract(data, path=path, expected_slug=project_slug)
    return ProjectContract(
        project_slug=contract["project_slug"],
        repo_url=contract["repo_url"],
        base_branch=contract["base_branch"],
        beads_labels=tuple(contract["beads_labels"]),
        validation_profiles=tuple(contract["validation_profiles"]),
        validation_profile_requests=dict(contract["validation_profile_requests"]),
        artifact_retention=dict(contract["artifact_retention"]),
        pr_target=dict(contract["pr_target"]),
        terminal_integration=dict(contract["terminal_integration"]),
        identity=ProjectContractIdentity(
            path=ledger_path(path, cwd=cwd),
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
    )


def validate_project_contract(
    data: Any,
    *,
    path: Path,
    expected_slug: str,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError(f"invalid project contract {path}: root must be an object")

    require_equal(data, "schema_version", SCHEMA_VERSION, path)
    require_string(data, "project_slug", path)
    require_string(data, "repo_url", path)
    require_string(data, "base_branch", path)
    beads_labels = require_string_list(data, "beads_labels", path)
    validation_profiles = require_string_list(data, "validation_profiles", path)
    validation_profile_requests = optional_profile_request_map(data, "validation_profile_requests", path)
    artifact_retention = require_object(data, "artifact_retention", path)
    pr_target = require_object(data, "pr_target", path)
    terminal_integration = optional_terminal_integration(
        data,
        "terminal_integration",
        path,
        validation_profiles=validation_profiles,
    )

    if data["project_slug"] != expected_slug:
        raise ContractError(
            f"invalid project contract {path}: project_slug must be {expected_slug!r}"
        )
    ledger_days = require_positive_int(
        artifact_retention,
        "ledger_days",
        path,
        prefix="artifact_retention.",
    )
    log_days = require_positive_int(
        artifact_retention,
        "log_days",
        path,
        prefix="artifact_retention.",
    )
    require_string(pr_target, "remote", path)
    require_string(pr_target, "branch", path)

    return {
        "project_slug": data["project_slug"],
        "repo_url": data["repo_url"],
        "base_branch": data["base_branch"],
        "beads_labels": beads_labels,
        "validation_profiles": validation_profiles,
        "validation_profile_requests": validation_profile_requests,
        "artifact_retention": {"ledger_days": ledger_days, "log_days": log_days},
        "pr_target": {
            "remote": pr_target["remote"],
            "branch": pr_target["branch"],
        },
        "terminal_integration": terminal_integration,
    }


def require_equal(data: dict[str, Any], key: str, expected: Any, path: Path) -> None:
    if data.get(key) != expected:
        raise ContractError(f"invalid project contract {path}: {key} must be {expected!r}")


def require_string(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"invalid project contract {path}: {key} must be a non-empty string")
    return value


def require_string_list(data: dict[str, Any], key: str, path: Path) -> list[str]:
    value = data.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ContractError(
            f"invalid project contract {path}: {key} must be a non-empty string list"
        )
    return list(value)


def require_object(data: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"invalid project contract {path}: {key} must be an object")
    return dict(value)


def optional_profile_request_map(data: dict[str, Any], key: str, path: Path) -> dict[str, dict[str, Any]]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ContractError(f"invalid project contract {path}: {key} must be an object")
    mapping: dict[str, dict[str, Any]] = {}
    for profile, request in value.items():
        if not isinstance(profile, str) or not profile:
            raise ContractError(f"invalid project contract {path}: {key} keys must be non-empty strings")
        if not isinstance(request, dict):
            raise ContractError(f"invalid project contract {path}: {key}.{profile} must be an object")
        if "profile" in request and (not isinstance(request["profile"], str) or not request["profile"]):
            raise ContractError(
                f"invalid project contract {path}: {key}.{profile}.profile must be a non-empty string"
            )
        mapping[profile] = dict(request)
    return mapping


def require_positive_int(
    data: dict[str, Any],
    key: str,
    path: Path,
    *,
    prefix: str = "",
) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(
            f"invalid project contract {path}: {prefix}{key} must be a positive integer"
        )
    return value


def materialize_terminal_integration_policy(project_contract: ProjectContract) -> dict[str, Any]:
    terminal_integration = dict(project_contract.terminal_integration)
    validation = terminal_integration.get("validation", {})
    if not isinstance(validation, dict):
        validation = {}
    poll_seconds = int(terminal_integration["poll_seconds"])
    close_tracker_on_merge = bool(terminal_integration["close_tracker_on_merge"])
    return {
        "required_checks": list(terminal_integration["required_checks"]),
        "required_check_patterns": list(terminal_integration["required_check_patterns"]),
        "optional_checks": list(terminal_integration["optional_checks"]),
        "optional_check_patterns": list(terminal_integration["optional_check_patterns"]),
        "neutral_policy": terminal_integration["neutral_policy"],
        "skipped_policy": terminal_integration["skipped_policy"],
        "merge_method": terminal_integration["merge_method"],
        "classify_timeout_seconds": int(terminal_integration["classify_timeout_seconds"]),
        "merge_timeout_seconds": int(terminal_integration["merge_timeout_seconds"]),
        "poll_seconds": poll_seconds,
        "poll_interval_seconds": poll_seconds,
        "close_tracker_on_merge": close_tracker_on_merge,
        "closeTrackerOnMerge": close_tracker_on_merge,
        "validation": {
            "default_mode": validation["default_mode"],
            "recommended_profiles": list(validation["recommended_profiles"]),
        },
    }


def default_validation_mode(project_contract: ProjectContract) -> str:
    return materialize_terminal_integration_policy(project_contract)["validation"]["default_mode"]


def optional_terminal_integration(
    data: dict[str, Any],
    key: str,
    path: Path,
    *,
    validation_profiles: list[str],
) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ContractError(f"invalid project contract {path}: {key} must be an object")
    validation = value.get("validation", {})
    if not isinstance(validation, dict):
        raise ContractError(f"invalid project contract {path}: {key}.validation must be an object")
    default_mode = validation.get("default_mode", "fake")
    if default_mode not in ALLOWED_VALIDATION_MODES:
        allowed = ", ".join(sorted(ALLOWED_VALIDATION_MODES))
        raise ContractError(f"invalid project contract {path}: {key}.validation.default_mode must be one of: {allowed}")
    recommended_profiles = optional_string_list(validation, "recommended_profiles", path, prefix=f"{key}.validation.")
    unknown_profiles = [profile for profile in recommended_profiles if profile not in validation_profiles]
    if unknown_profiles:
        raise ContractError(
            f"invalid project contract {path}: {key}.validation.recommended_profiles must be declared in validation_profiles"
        )
    return {
        "required_checks": optional_string_list(value, "required_checks", path, prefix=f"{key}."),
        "required_check_patterns": optional_string_list(value, "required_check_patterns", path, prefix=f"{key}."),
        "optional_checks": optional_string_list(value, "optional_checks", path, prefix=f"{key}."),
        "optional_check_patterns": optional_string_list(value, "optional_check_patterns", path, prefix=f"{key}."),
        "neutral_policy": optional_enum(
            value,
            "neutral_policy",
            "block",
            path,
            prefix=f"{key}.",
            allowed=ALLOWED_TERMINAL_POLICY_ACTIONS,
        ),
        "skipped_policy": optional_enum(
            value,
            "skipped_policy",
            "block",
            path,
            prefix=f"{key}.",
            allowed=ALLOWED_TERMINAL_POLICY_ACTIONS,
        ),
        "merge_method": optional_enum(
            value,
            "merge_method",
            "merge",
            path,
            prefix=f"{key}.",
            allowed=ALLOWED_TERMINAL_MERGE_METHODS,
        ),
        "classify_timeout_seconds": optional_positive_int(
            value,
            "classify_timeout_seconds",
            DEFAULT_TERMINAL_CLASSIFY_TIMEOUT_SECONDS,
            path,
            prefix=f"{key}.",
        ),
        "merge_timeout_seconds": optional_positive_int(
            value,
            "merge_timeout_seconds",
            DEFAULT_TERMINAL_MERGE_TIMEOUT_SECONDS,
            path,
            prefix=f"{key}.",
        ),
        "poll_seconds": optional_positive_int(
            value,
            "poll_seconds",
            DEFAULT_TERMINAL_POLICY_POLL_SECONDS,
            path,
            prefix=f"{key}.",
        ),
        "close_tracker_on_merge": optional_bool_or_alias(
            value,
            "close_tracker_on_merge",
            "closeTrackerOnMerge",
            True,
            path,
            prefix=f"{key}.",
        ),
        "validation": {
            "default_mode": default_mode,
            "recommended_profiles": recommended_profiles,
        },
    }


def optional_bool_or_alias(
    data: dict[str, Any],
    key: str,
    alias: str,
    default: bool,
    path: Path,
    *,
    prefix: str = "",
) -> bool:
    candidates = [name for name in (key, alias) if name in data]
    if not candidates:
        return default
    for name in candidates:
        if not isinstance(data[name], bool):
            raise ContractError(
                f"invalid project contract {path}: {prefix}{name} must be a boolean"
            )
    if len(candidates) == 2 and data[key] != data[alias]:
        raise ContractError(f"invalid project contract {path}: {prefix}{key} and {alias} must agree when both are set")
    value = data[candidates[0]]
    return value


def optional_enum(
    data: dict[str, Any],
    key: str,
    default: str,
    path: Path,
    *,
    prefix: str = "",
    allowed: set[str],
) -> str:
    value = data.get(key, default)
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ContractError(f"invalid project contract {path}: {prefix}{key} must be one of: {allowed_text}")
    return value


def optional_positive_int(
    data: dict[str, Any],
    key: str,
    default: int,
    path: Path,
    *,
    prefix: str = "",
) -> int:
    if key not in data:
        return default
    return require_positive_int(data, key, path, prefix=prefix)


def optional_string_list(data: dict[str, Any], key: str, path: Path, *, prefix: str = "") -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ContractError(f"invalid project contract {path}: {prefix}{key} must be a string list")
    if any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"invalid project contract {path}: {prefix}{key} must be a string list")
    return list(value)


def ledger_path(path: Path, *, cwd: Path | None) -> str:
    resolved = path.resolve()
    base = (cwd or Path.cwd()).resolve()
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return str(resolved)
