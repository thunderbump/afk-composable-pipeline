from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


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
    artifact_retention: dict[str, Any]
    pr_target: dict[str, str]
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
        artifact_retention=dict(contract["artifact_retention"]),
        pr_target=dict(contract["pr_target"]),
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
    artifact_retention = require_object(data, "artifact_retention", path)
    pr_target = require_object(data, "pr_target", path)

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
        "artifact_retention": {"ledger_days": ledger_days, "log_days": log_days},
        "pr_target": {
            "remote": pr_target["remote"],
            "branch": pr_target["branch"],
        },
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


def ledger_path(path: Path, *, cwd: Path | None) -> str:
    resolved = path.resolve()
    base = (cwd or Path.cwd()).resolve()
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return str(resolved)
