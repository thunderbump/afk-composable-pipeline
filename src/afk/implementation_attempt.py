from __future__ import annotations

import re
from typing import Any


FIRST_ATTEMPT_ID = "implementation-1"
SECOND_ATTEMPT_ID = "implementation-2"
ATTEMPT_IDS = {FIRST_ATTEMPT_ID, SECOND_ATTEMPT_ID}
BINDING_FIELDS = {
    "repository",
    "repository_common_dir",
    "repository_common_dir_identity",
    "origin",
    "branch",
    "worktree_path",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def started_attempt(
    attempt_id: str,
    *,
    starting_sha: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "starting_sha": starting_sha,
        "status": "started",
        "evidence": f"attempts/{attempt_id}",
        **binding,
    }


def completed_attempt(attempt: dict[str, Any], *, ending_sha: str) -> dict[str, Any]:
    return {**attempt, "status": "completed", "ending_sha": ending_sha}


def interrupted_attempt(
    attempt: dict[str, Any], *, summary: str, retryable: bool
) -> dict[str, Any]:
    return {
        **attempt,
        "status": "interrupted",
        "summary": summary,
        "retryable": retryable,
    }


def next_attempt_id(attempt: dict[str, Any]) -> str | None:
    if (
        attempt.get("status") == "interrupted"
        and attempt.get("retryable") is True
        and interruption_is_retryable(attempt)
    ):
        return SECOND_ATTEMPT_ID
    return None


def interruption_is_retryable(attempt: dict[str, Any]) -> bool:
    return attempt.get("attempt_id") == FIRST_ATTEMPT_ID


def valid_attempt(value: Any, statuses: set[str]) -> bool:
    if not isinstance(value, dict):
        return False
    attempt_id = value.get("attempt_id")
    common = {
        "attempt_id",
        "starting_sha",
        "status",
        "evidence",
        *BINDING_FIELDS,
    }
    extra = (
        {"ending_sha"}
        if value.get("status") == "completed"
        else {"summary", "retryable"} if value.get("status") == "interrupted" else set()
    )
    return (
        set(value) == common | extra
        and isinstance(attempt_id, str)
        and attempt_id in ATTEMPT_IDS
        and isinstance(value.get("starting_sha"), str)
        and bool(SHA_PATTERN.fullmatch(value["starting_sha"]))
        and value.get("status") in statuses
        and value.get("evidence") == f"attempts/{attempt_id}"
        and all(
            isinstance(value.get(key), str) and bool(value[key])
            for key in (
                "repository",
                "repository_common_dir",
                "origin",
                "branch",
                "worktree_path",
            )
        )
        and _valid_filesystem_identity(value.get("repository_common_dir_identity"))
        and (
            value.get("status") != "completed"
            or isinstance(value.get("ending_sha"), str)
            and bool(SHA_PATTERN.fullmatch(value["ending_sha"]))
            and value["ending_sha"] != value["starting_sha"]
        )
        and (
            value.get("status") != "interrupted"
            or isinstance(value.get("summary"), str)
            and bool(value["summary"])
            and type(value.get("retryable")) is bool
            and (value["retryable"] is False or interruption_is_retryable(value))
        )
    )


def _valid_filesystem_identity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"device", "inode"}
        and type(value.get("device")) is int
        and value["device"] >= 0
        and type(value.get("inode")) is int
        and value["inode"] > 0
    )
