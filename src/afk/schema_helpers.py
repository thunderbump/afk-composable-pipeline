from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from afk.redaction import redact_artifact_value


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def string_list_field(value: dict[str, Any], key: str) -> list[str] | None:
    items = value.get(key, [])
    if not is_string_list(items):
        return None
    return list(items)


def string_field(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if isinstance(item, str) and item.strip():
        return item.strip()
    return None


def relation_list(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    return [redact_artifact_value(item) for item in value]


def build_selected_work_record(
    work_item: dict[str, Any],
    *,
    external_id: str,
    source_id: str,
    source_type: str,
    labels: list[str],
    acceptance_criteria: list[str],
    dependencies: list[Any],
    blockers: list[Any],
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_type": source_type,
        "external_id": external_id,
        "url": string_field(work_item, "url") or "",
        "title": string_field(work_item, "title") or "",
        "status": string_field(work_item, "status") or "",
        "labels": labels,
        "parent": work_item.get("parent"),
        "workstream": work_item.get("workstream"),
        "acceptance_criteria": acceptance_criteria,
        "dependencies": dependencies,
        "blockers": blockers,
        "dependency_status": string_field(work_item, "dependency_status") or "",
        "afk": dict(work_item.get("afk") or {}) if isinstance(work_item.get("afk") or {}, dict) else {},
    }


def copy_selected_work_items(selected_work: Any) -> list[dict[str, Any]]:
    if not isinstance(selected_work, list):
        return []
    return [dict(item) for item in selected_work if isinstance(item, dict)]


def scrub_selected_work_value(value: Any) -> Any:
    if isinstance(value, list):
        return [scrub_selected_work_value(item) for item in value]
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if not key.startswith("selector_")}
    return value


def first_selected_work_external_id(selected_work: Any) -> str:
    if not isinstance(selected_work, list) or not selected_work:
        return ""
    first_item = selected_work[0]
    if not isinstance(first_item, dict):
        return ""
    return string_field(first_item, "external_id") or ""


def normalize_prepared_checkout(
    checkout: Any,
    *,
    include_repo_url: bool = False,
    redact_repo_url: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    if not isinstance(checkout, dict):
        return {"status": "invalid", "message": "checkout must be an object"}
    if checkout.get("status") != "prepared":
        return {"status": "invalid", "message": "checkout.status must be prepared"}
    path = string_field(checkout, "checkout_path")
    start_commit = string_field(checkout, "start_commit")
    if not path:
        return {"status": "invalid", "message": "checkout.checkout_path is required"}
    if not start_commit:
        return {"status": "invalid", "message": "checkout.start_commit is required"}
    checkout_path = Path(path)
    if not checkout_path.is_absolute():
        return {"status": "invalid", "message": "checkout.checkout_path must be absolute"}
    if not _is_git_checkout(checkout_path):
        return {"status": "invalid", "message": "checkout.checkout_path must be a git checkout"}
    normalized = {
        "path": str(checkout_path),
        "review_branch": string_field(checkout, "review_branch") or "",
        "requested_ref": string_field(checkout, "requested_ref") or "",
        "start_commit": start_commit,
    }
    if include_repo_url:
        repo_url = string_field(checkout, "repo_url") or ""
        normalized["repo_url"] = redact_repo_url(repo_url) if redact_repo_url is not None else repo_url
    return {"status": "valid", "checkout": normalized}


def _is_git_checkout(checkout_path: Path) -> bool:
    try:
        completed = _run_git(
            checkout_path,
            ["rev-parse", "--show-toplevel", "--is-inside-work-tree"],
        )
        top_level, inside_work_tree = completed.stdout.splitlines()
        return (
            completed.returncode == 0
            and inside_work_tree == "true"
            and os.path.samefile(checkout_path, top_level)
        )
    except (OSError, ValueError):
        return False


def resolve_git_commit(checkout_path: Path, revision: str) -> str | None:
    try:
        completed = _run_git(
            checkout_path,
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None
    except OSError:
        return None


def _run_git(checkout_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(checkout_path), *args],
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        },
        text=True,
        capture_output=True,
        check=False,
    )


def validation_artifact_ref(
    *,
    index: int,
    name: str | None,
    step_result_path: str | None,
    worker_result_path: str | None,
) -> dict[str, str]:
    return {
        "name": name or f"validation-{index + 1}",
        "step_result_path": step_result_path or "",
        "worker_result_path": worker_result_path or "",
    }
