from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = 1
ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def validate_open_attempts(
    projection: dict[str, Any], events: list[dict[str, Any]]
) -> str | None:
    lifecycle_invalid, validation_is_open, started_validation = (
        _open_validation_attempt(events)
    )
    validation = projection.get("validation_attempt")
    if lifecycle_invalid or (
        not validation_is_open
        and isinstance(validation, dict)
        and validation.get("status") == "started"
    ):
        return "validation attempt lifecycle is invalid"
    if validation_is_open:
        attempt_id = (
            validation.get("attempt_id") if isinstance(validation, dict) else None
        )
        if (
            not isinstance(validation, dict)
            or set(validation) != {"attempt_id", "candidate_sha", "status", "evidence"}
            or validation.get("status") != "started"
            or not isinstance(attempt_id, str)
            or not ATTEMPT_ID_PATTERN.fullmatch(attempt_id)
            or not isinstance(validation.get("candidate_sha"), str)
            or not SHA_PATTERN.fullmatch(validation["candidate_sha"])
            or validation.get("candidate_sha") != projection.get("candidate_sha")
            or validation.get("evidence") != f"attempts/{attempt_id}"
            or validation != started_validation
        ):
            return "open validation attempt is invalid"

    repair_is_open, started_repair, consumed_slot = _open_repair_attempt(events)
    repair = projection.get("repair_brief")
    repair_attempt = repair.get("repair_attempt") if isinstance(repair, dict) else None
    if repair_is_open and (
        not isinstance(repair, dict)
        or not repair
        or set(repair)
        not in (
            {"candidate_sha", "repair_attempt", "blocking_findings"},
            {
                "schema_version",
                "candidate_sha",
                "repair_attempt",
                "blocking_findings",
            },
        )
        or (
            "schema_version" in repair
            and (
                type(repair["schema_version"]) is not int
                or repair["schema_version"] != SCHEMA_VERSION
            )
        )
        or not isinstance(repair.get("candidate_sha"), str)
        or not SHA_PATTERN.fullmatch(repair["candidate_sha"])
        or repair.get("candidate_sha") != projection.get("candidate_sha")
        or type(repair_attempt) is not int
        or not 1 <= repair_attempt <= 4
        or repair_attempt != consumed_slot
        or projection.get("repair_attempts_used") != consumed_slot
        or not isinstance(repair.get("blocking_findings"), list)
        or repair != started_repair
    ):
        return "open repair attempt is invalid"
    return None


def _open_validation_attempt(
    events: list[dict[str, Any]],
) -> tuple[bool, bool, Any]:
    is_open = False
    attempt: Any = None
    for event in events:
        if event["event"] == "validation.attempt_started":
            if is_open:
                return True, is_open, attempt
            is_open = True
            attempt = event["data"].get("validation_attempt")
        elif event["event"] == "validation.attempt_finished":
            finished = event["data"].get("validation_attempt")
            if not (
                is_open
                and isinstance(attempt, dict)
                and isinstance(finished, dict)
                and set(finished)
                == {"attempt_id", "candidate_sha", "status", "evidence"}
                and finished.get("attempt_id") == attempt.get("attempt_id")
                and finished.get("candidate_sha") == attempt.get("candidate_sha")
                and finished.get("evidence") == attempt.get("evidence")
                and isinstance(finished.get("status"), str)
                and finished["status"] != "started"
            ):
                return True, is_open, attempt
            is_open = False
            attempt = None
    return False, is_open, attempt


def _open_repair_attempt(events: list[dict[str, Any]]) -> tuple[bool, Any, Any]:
    is_open = False
    brief: Any = None
    consumed_slot: Any = None
    for event in events:
        if event["event"] == "repair.started":
            is_open = True
            brief = event["data"].get("repair_brief")
            consumed_slot = event["data"].get("repair_attempts_used")
        elif event["event"] in {"candidate.repaired", "repair.interrupted"} and (
            is_open and event["data"].get("repair_attempts_used") == consumed_slot
        ):
            is_open = False
            brief = None
            consumed_slot = None
    return is_open, brief, consumed_slot
