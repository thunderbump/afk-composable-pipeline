from __future__ import annotations

from typing import Any

from afk.durable_id import is_durable_id
from afk.run_store import RunStoreError


def normalize_retrospective_outcome(
    value: Any,
    *,
    run_id: str,
    episode_sequence: int,
) -> dict[str, Any]:
    base_keys = {
        "schema_version",
        "run_id",
        "episode_sequence",
        "status",
        "warning",
        "process_findings_count",
        "improvement_proposals_count",
    }
    status = value.get("status") if isinstance(value, dict) else None
    warning_status = type(status) is str and status in {
        "invalid",
        "unavailable",
        "interrupted",
    }
    expected_keys = base_keys | ({"warning_summary"} if warning_status else set())
    findings = value.get("process_findings_count") if isinstance(value, dict) else None
    proposals = (
        value.get("improvement_proposals_count") if isinstance(value, dict) else None
    )
    warning_summary = value.get("warning_summary") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not is_durable_id(value.get("run_id"))
        or value.get("run_id") != run_id
        or type(value.get("episode_sequence")) is not int
        or value.get("episode_sequence") != episode_sequence
        or type(status) is not str
        or status not in {"passed", "empty", "invalid", "unavailable", "interrupted"}
        or type(value.get("warning")) is not bool
        or type(findings) is not int
        or findings < 0
        or type(proposals) is not int
        or proposals < 0
        or (status == "passed" and (value["warning"] or findings + proposals == 0))
        or (status == "empty" and (value["warning"] or findings != 0 or proposals != 0))
        or (
            warning_status
            and (
                not value["warning"]
                or findings != 0
                or proposals != 0
                or not _valid_warning_summary(warning_summary)
            )
        )
    ):
        raise RunStoreError("sealed retrospective outcome is invalid")
    return value


def _valid_warning_summary(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
