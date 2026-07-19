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

    repair_lifecycle_invalid, repair_is_open, started_repair, consumed_slot = (
        _open_repair_attempt(events)
    )
    repair = projection.get("repair_brief")
    repair_attempt = repair.get("repair_attempt") if isinstance(repair, dict) else None
    projected_repair_is_open = (
        isinstance(repair, dict)
        and bool(repair)
        and repair_attempt == projection.get("repair_attempts_used")
        and repair.get("candidate_sha") == projection.get("candidate_sha")
    )
    if repair_lifecycle_invalid or (not repair_is_open and projected_repair_is_open):
        return "repair attempt lifecycle is invalid"
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
        or type(consumed_slot) is not int
        or repair_attempt != consumed_slot
        or type(projection.get("repair_attempts_used")) is not int
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


def _open_repair_attempt(
    events: list[dict[str, Any]],
) -> tuple[bool, bool, Any, Any]:
    is_open = False
    brief: Any = None
    consumed_slot: Any = None
    for event in events:
        if event["event"] == "repair.started":
            if is_open:
                return True, is_open, brief, consumed_slot
            is_open = True
            brief = event["data"].get("repair_brief")
            consumed_slot = event["data"].get("repair_attempts_used")
        elif event["event"] == "candidate.repaired" and (
            "repair_attempts_used" in event["data"]
        ):
            candidate = event["data"].get("candidate_sha")
            if not (
                is_open
                and isinstance(brief, dict)
                and type(consumed_slot) is int
                and type(event["data"].get("repair_attempts_used")) is int
                and event["data"].get("repair_attempts_used") == consumed_slot
                and event["data"].get("previous_candidate_sha")
                == brief.get("candidate_sha")
                and isinstance(candidate, str)
                and SHA_PATTERN.fullmatch(candidate)
                and candidate != brief.get("candidate_sha")
            ):
                return True, is_open, brief, consumed_slot
            is_open = False
            brief = None
            consumed_slot = None
        elif event["event"] == "repair.interrupted":
            interruption = event["data"].get("interrupted_repair")
            if not (
                is_open
                and isinstance(brief, dict)
                and type(consumed_slot) is int
                and type(event["data"].get("repair_attempts_used")) is int
                and event["data"].get("repair_attempts_used") == consumed_slot
                and isinstance(interruption, dict)
                and set(interruption)
                == {
                    "schema_version",
                    "candidate_sha",
                    "repair_attempt",
                    "status",
                    "summary",
                }
                and type(interruption.get("schema_version")) is int
                and interruption["schema_version"] == SCHEMA_VERSION
                and interruption.get("status") == "interrupted"
                and interruption.get("candidate_sha") == brief.get("candidate_sha")
                and interruption.get("repair_attempt") == consumed_slot
                and isinstance(interruption.get("summary"), str)
                and bool(interruption["summary"])
            ):
                return True, is_open, brief, consumed_slot
            is_open = False
            brief = None
            consumed_slot = None
    return False, is_open, brief, consumed_slot
