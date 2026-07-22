from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = 1
ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VALIDATION_TERMINAL_STATUSES = {
    "passed",
    "rejected",
    "inconclusive",
    "invalid",
    "head_mismatch",
    "interrupted",
}


def validate_open_attempts(
    projection: dict[str, Any], events: list[dict[str, Any]]
) -> str | None:
    (
        implementation_invalid,
        implementation_is_open,
        started_implementation,
        terminal_implementation,
    ) = _open_implementation_attempt(events)
    implementation = projection.get("implementation_attempt")
    if (
        implementation_invalid
        or implementation_is_open
        and implementation != started_implementation
        or not implementation_is_open
        and terminal_implementation is not None
        and implementation != terminal_implementation
        or isinstance(implementation, dict)
        and implementation.get("starting_sha") != projection.get("base_sha")
    ):
        return "implementation attempt lifecycle is invalid"

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
        if (
            not _valid_validation_attempt(validation, {"started"})
            or validation.get("candidate_sha") != projection.get("candidate_sha")
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
        not _valid_repair_brief(repair)
        or repair.get("candidate_sha") != projection.get("candidate_sha")
        or type(consumed_slot) is not int
        or repair_attempt != consumed_slot
        or type(projection.get("repair_attempts_used")) is not int
        or projection.get("repair_attempts_used") != consumed_slot
        or not isinstance(repair.get("blocking_findings"), list)
        or repair != started_repair
    ):
        return "open repair attempt is invalid"
    return None


def _open_implementation_attempt(
    events: list[dict[str, Any]],
) -> tuple[bool, bool, Any, Any]:
    is_open = False
    attempt: Any = None
    terminal: Any = None
    expected_attempt_id: str | None = "implementation-1"
    for event in events:
        if event["event"] == "implementation.attempt_started":
            started = event["data"].get("implementation_attempt")
            if (
                is_open
                or not _valid_implementation_attempt(started, {"started"})
                or started.get("attempt_id") != expected_attempt_id
            ):
                return True, is_open, attempt, terminal
            is_open = True
            attempt = started
            terminal = None
            expected_attempt_id = None
        elif event["event"] in {
            "implementation.attempt_finished",
            "implementation.attempt_interrupted",
        }:
            finished = event["data"].get("implementation_attempt")
            statuses = (
                {"completed"}
                if event["event"] == "implementation.attempt_finished"
                else {"interrupted"}
            )
            if not (
                is_open
                and _valid_implementation_attempt(finished, statuses)
                and finished.get("attempt_id") == attempt.get("attempt_id")
                and finished.get("starting_sha") == attempt.get("starting_sha")
                and finished.get("evidence") == attempt.get("evidence")
            ):
                return True, is_open, attempt, terminal
            is_open = False
            terminal = finished
            expected_attempt_id = (
                "implementation-2"
                if finished["status"] == "interrupted"
                and finished["retryable"] is True
                and finished["attempt_id"] == "implementation-1"
                else None
            )
            attempt = None
    return False, is_open, attempt, terminal


def _valid_implementation_attempt(value: Any, statuses: set[str]) -> bool:
    if not isinstance(value, dict):
        return False
    attempt_id = value.get("attempt_id")
    common = {
        "attempt_id",
        "starting_sha",
        "status",
        "evidence",
    }
    extra = (
        {"ending_sha"}
        if value.get("status") == "completed"
        else {"summary", "retryable"} if value.get("status") == "interrupted" else set()
    )
    return (
        set(value) == common | extra
        and isinstance(attempt_id, str)
        and attempt_id in {"implementation-1", "implementation-2"}
        and isinstance(value.get("starting_sha"), str)
        and bool(SHA_PATTERN.fullmatch(value["starting_sha"]))
        and value.get("status") in statuses
        and value.get("evidence") == f"attempts/{attempt_id}"
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
            and (value["retryable"] is False or attempt_id == "implementation-1")
        )
    )


def _open_validation_attempt(
    events: list[dict[str, Any]],
) -> tuple[bool, bool, Any]:
    is_open = False
    attempt: Any = None
    for event in events:
        if event["event"] == "validation.attempt_started":
            started = event["data"].get("validation_attempt")
            if is_open or not _valid_validation_attempt(started, {"started"}):
                return True, is_open, attempt
            is_open = True
            attempt = started
        elif event["event"] == "validation.attempt_finished":
            finished = event["data"].get("validation_attempt")
            if not (
                is_open
                and _valid_validation_attempt(finished, VALIDATION_TERMINAL_STATUSES)
                and finished.get("attempt_id") == attempt.get("attempt_id")
                and finished.get("candidate_sha") == attempt.get("candidate_sha")
                and finished.get("evidence") == attempt.get("evidence")
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
            started_brief = event["data"].get("repair_brief")
            if is_open or not _valid_repair_brief(started_brief):
                return True, is_open, brief, consumed_slot
            is_open = True
            brief = started_brief
            consumed_slot = event["data"].get("repair_attempts_used")
        elif event["event"] == "candidate.repaired" and (
            "repair_attempts_used" in event["data"]
        ):
            if not _valid_candidate_repaired_closure(
                event["data"], brief, consumed_slot
            ):
                return True, is_open, brief, consumed_slot
            is_open = False
            brief = None
            consumed_slot = None
        elif event["event"] == "repair.interrupted":
            if not _valid_repair_interrupted_closure(
                event["data"], brief, consumed_slot
            ):
                return True, is_open, brief, consumed_slot
            is_open = False
            brief = None
            consumed_slot = None
    return False, is_open, brief, consumed_slot


def _valid_validation_attempt(value: Any, statuses: set[str]) -> bool:
    if not isinstance(value, dict):
        return False
    attempt_id = value.get("attempt_id")
    return (
        set(value) == {"attempt_id", "candidate_sha", "status", "evidence"}
        and isinstance(attempt_id, str)
        and bool(ATTEMPT_ID_PATTERN.fullmatch(attempt_id))
        and isinstance(value.get("candidate_sha"), str)
        and bool(SHA_PATTERN.fullmatch(value["candidate_sha"]))
        and value.get("status") in statuses
        and value.get("evidence") == f"attempts/{attempt_id}"
    )


def _valid_repair_brief(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value)
        == {"schema_version", "candidate_sha", "repair_attempt", "blocking_findings"}
        and type(value.get("schema_version")) is int
        and value["schema_version"] == SCHEMA_VERSION
        and isinstance(value.get("candidate_sha"), str)
        and bool(SHA_PATTERN.fullmatch(value["candidate_sha"]))
        and type(value.get("repair_attempt")) is int
        and 1 <= value["repair_attempt"] <= 4
        and isinstance(value.get("blocking_findings"), list)
    )


def _valid_candidate_repaired_closure(data: Any, brief: Any, slot: Any) -> bool:
    if not isinstance(data, dict) or not isinstance(brief, dict):
        return False
    candidate = data.get("candidate_sha")
    return (
        set(data)
        == {
            "checkpoint",
            "previous_candidate_sha",
            "candidate_sha",
            "pr_number",
            "pr_url",
            "pr_head_sha",
            "repair_attempts_used",
            "repair_dispositions",
            "attention",
        }
        and data.get("checkpoint") == "candidate_ready"
        and type(slot) is int
        and type(data.get("repair_attempts_used")) is int
        and data["repair_attempts_used"] == slot
        and data.get("previous_candidate_sha") == brief.get("candidate_sha")
        and isinstance(candidate, str)
        and bool(SHA_PATTERN.fullmatch(candidate))
        and candidate != brief.get("candidate_sha")
        and type(data.get("pr_number")) is int
        and data["pr_number"] > 0
        and isinstance(data.get("pr_url"), str)
        and bool(data["pr_url"])
        and data.get("pr_head_sha") == candidate
        and isinstance(data.get("repair_dispositions"), list)
        and data.get("attention") == {}
    )


def _valid_repair_interrupted_closure(data: Any, brief: Any, slot: Any) -> bool:
    if not isinstance(data, dict) or not isinstance(brief, dict):
        return False
    interruption = data.get("interrupted_repair")
    next_brief = data.get("repair_brief")
    return (
        set(data)
        == {
            "checkpoint",
            "repair_attempts_used",
            "repair_brief",
            "interrupted_repair",
        }
        and data.get("checkpoint") in {"validated", "candidate_ready"}
        and type(slot) is int
        and type(data.get("repair_attempts_used")) is int
        and data["repair_attempts_used"] == slot
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
        and interruption.get("repair_attempt") == slot
        and isinstance(interruption.get("summary"), str)
        and bool(interruption["summary"])
        and (
            next_brief == {}
            if slot == 4
            else _valid_repair_brief(next_brief)
            and next_brief.get("candidate_sha") == brief.get("candidate_sha")
            and next_brief.get("repair_attempt") == slot + 1
        )
    )
