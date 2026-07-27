from __future__ import annotations

import json
import re
from typing import Any

from afk.jsonutil import canonical_json, sha256_json
from afk.redaction import redact_artifact_value, redact_text
from afk.run_store import RunStore, RunStoreError


MAX_RUN_SUMMARY_BYTES = 64 * 1024
MAX_EVENTS = 64
MAX_EFFECTS = 32
MAX_EVIDENCE_UNITS = 32
MAX_EVIDENCE_FILES = 32
MAX_NESTED_ITEMS = 16
MAX_STRING_CHARACTERS = 512
TRUNCATION_SUFFIX = "…[TRUNCATED]"
PROJECTION_FIELDS = (
    "state",
    "checkpoint",
    "unit",
    "branch",
    "candidate_sha",
    "pr_number",
    "pr_url",
    "pr_head_sha",
    "pr_ready",
    "worker_exit_code",
    "previous_candidate_sha",
    "repair_attempts_used",
    "merge",
    "remote_branch_deleted",
    "bead_closure",
    "validation",
    "validation_attempt",
    "gate_retry",
    "completion",
    "attention",
    "lingering",
    "lifecycle_interruption",
    "interrupted_repair",
)


def build_run_summary(store: RunStore, run_id: str, *, episode_sequence: int) -> str:
    """Return bounded canonical JSON for one retrospective episode."""
    event = store.event(run_id, episode_sequence)
    _validate_episode(event, episode_sequence)
    identity = store.identity(run_id)
    summary_evidence = f"retrospective/run-summary-{episode_sequence:020d}"
    cached = store.sealed_evidence_result(run_id, summary_evidence)
    if cached is not None:
        return _validate_cached_summary(
            cached,
            run_id=run_id,
            episode_sequence=episode_sequence,
            event=event,
            identity=identity,
        )

    snapshot = store.read_run_snapshot(run_id, through_sequence=episode_sequence)
    identity = snapshot["identity"]
    projection = snapshot["projection"]
    events = snapshot["events"]
    effects = snapshot["effects"]
    evidence = snapshot["evidence"]
    selected_events = events[-MAX_EVENTS:]
    selected_effects = effects[:MAX_EFFECTS]
    selected_evidence = evidence[:MAX_EVIDENCE_UNITS]
    omitted = {
        "events": len(events) - len(selected_events),
        "effects": len(effects) - len(selected_effects),
        "evidence_units": len(evidence) - len(selected_evidence),
        "evidence_files": 0,
    }

    evidence_references = []
    for record in selected_evidence:
        manifest = record["manifest"]
        files = manifest["files"][:MAX_EVIDENCE_FILES]
        omitted["evidence_files"] += len(manifest["files"]) - len(files)
        evidence_references.append(
            {
                "unit": _bounded_text(record["unit"]),
                "manifest_sha256": sha256_json(manifest),
                "total_bytes": manifest["total_bytes"],
                "files": files,
            }
        )

    summary = redact_artifact_value(
        {
            "schema_version": 1,
            "run": {
                "run_id": _bounded_text(identity["run_id"]),
                "bead_id": _bounded_text(identity["bead_id"]),
                "repository": _bounded_text(identity["repository"]),
                "base_branch": _bounded_text(identity["base_branch"]),
                "base_sha": identity["base_sha"],
                "created_at": _bounded_text(identity["created_at"]),
            },
            "episode": {
                "sequence": event["sequence"],
                "event": _bounded_text(event["event"]),
                "recorded_at": _bounded_text(event["recorded_at"]),
                "state": _bounded_text(event["state"]),
                "checkpoint": _bounded_value(projection["checkpoint"]),
            },
            "projection": {
                key: _bounded_value(projection[key])
                for key in PROJECTION_FIELDS
                if key in projection
            },
            "events": [
                {
                    "sequence": item["sequence"],
                    "event": _bounded_text(item["event"]),
                    "recorded_at": _bounded_text(item["recorded_at"]),
                    **(
                        {"state": _bounded_text(item["state"])}
                        if "state" in item
                        else {}
                    ),
                }
                for item in selected_events
            ],
            "effects": [
                {
                    "effect_id": _bounded_text(item["effect_id"]),
                    "kind": _bounded_text(item["kind"]),
                    "status": item["status"],
                }
                for item in selected_effects
            ],
            "evidence": evidence_references,
            "omitted": omitted,
        }
    )
    _fit_summary(summary)
    rendered = canonical_json(summary)
    if len(rendered.encode("utf-8")) > MAX_RUN_SUMMARY_BYTES:
        raise RunStoreError("Run Summary identity exceeds its byte limit")
    sealed = store.reconcile_evidence_result(
        run_id,
        summary_evidence,
        {
            "schema_version": 1,
            "run_id": run_id,
            "episode_sequence": episode_sequence,
            "episode_event": event["event"],
            "episode_state": event["state"],
            "summary": rendered,
        },
    )
    return _validate_cached_summary(
        sealed,
        run_id=run_id,
        episode_sequence=episode_sequence,
        event=event,
        identity=identity,
    )


def _validate_episode(event: dict[str, Any], episode_sequence: int) -> None:
    expected_state = {
        "run.attention_required": "attention_required",
        "run.completed": "completed",
    }.get(event["event"])
    if expected_state is None or event.get("state") != expected_state:
        raise RunStoreError(
            f"sequence {episode_sequence} is not a retrospective episode"
        )


def _validate_cached_summary(
    cached: Any,
    *,
    run_id: str,
    episode_sequence: int,
    event: dict[str, Any],
    identity: dict[str, Any],
) -> str:
    if (
        not isinstance(cached, dict)
        or set(cached)
        != {
            "schema_version",
            "run_id",
            "episode_sequence",
            "episode_event",
            "episode_state",
            "summary",
        }
        or type(cached["schema_version"]) is not int
        or cached["schema_version"] != 1
        or cached["run_id"] != run_id
        or type(cached["episode_sequence"]) is not int
        or cached["episode_sequence"] != episode_sequence
        or cached["episode_event"] != event["event"]
        or cached["episode_state"] != event["state"]
        or not isinstance(cached["summary"], str)
    ):
        raise RunStoreError("sealed Run Summary identity is invalid")
    rendered = cached["summary"]
    if len(rendered.encode("utf-8")) > MAX_RUN_SUMMARY_BYTES:
        raise RunStoreError("sealed Run Summary exceeds its byte limit")
    try:
        summary = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise RunStoreError("sealed Run Summary is invalid") from exc
    if canonical_json(summary) != rendered:
        raise RunStoreError("sealed Run Summary is not canonical")
    if not _valid_summary_content(
        summary,
        episode_sequence=episode_sequence,
        event=event,
        identity=identity,
    ):
        raise RunStoreError("sealed Run Summary content is invalid")
    return rendered


def _valid_summary_content(
    summary: Any,
    *,
    episode_sequence: int,
    event: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    if (
        not _has_keys(
            summary,
            {
                "schema_version",
                "run",
                "episode",
                "projection",
                "events",
                "effects",
                "evidence",
                "omitted",
            },
        )
        or type(summary["schema_version"]) is not int
        or summary["schema_version"] != 1
        or redact_artifact_value(summary) != summary
    ):
        return False

    run = summary["run"]
    episode = summary["episode"]
    projection = summary["projection"]
    events = summary["events"]
    effects = summary["effects"]
    evidence = summary["evidence"]
    omitted = summary["omitted"]
    if (
        not _has_keys(
            run,
            {
                "run_id",
                "bead_id",
                "repository",
                "base_branch",
                "base_sha",
                "created_at",
            },
        )
        or run
        != {
            "run_id": _bounded_text(identity["run_id"]),
            "bead_id": _bounded_text(identity["bead_id"]),
            "repository": _bounded_text(identity["repository"]),
            "base_branch": _bounded_text(identity["base_branch"]),
            "base_sha": identity["base_sha"],
            "created_at": _bounded_text(identity["created_at"]),
        }
        or any(
            not _valid_bounded_text(run[key])
            for key in ("run_id", "bead_id", "repository", "base_branch", "created_at")
        )
        or not isinstance(run["base_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", run["base_sha"]) is None
        or not _has_keys(
            episode,
            {"sequence", "event", "recorded_at", "state", "checkpoint"},
        )
        or type(episode["sequence"]) is not int
        or episode["sequence"] != episode_sequence
        or episode["event"] != event["event"]
        or episode["recorded_at"] != _bounded_text(event["recorded_at"])
        or episode["state"] != event["state"]
        or not all(
            _valid_bounded_text(episode[key])
            for key in ("event", "recorded_at", "state")
        )
        or not _valid_bounded_value(episode["checkpoint"])
        or not isinstance(projection, dict)
        or not {"state", "checkpoint"} <= set(projection) <= set(PROJECTION_FIELDS)
        or projection["state"] != episode["state"]
        or projection["checkpoint"] != episode["checkpoint"]
        or any(not _valid_bounded_value(value) for value in projection.values())
    ):
        return False

    if (
        not isinstance(events, list)
        or not 1 <= len(events) <= MAX_EVENTS
        or any(not _valid_summary_event(item) for item in events)
        or [item["sequence"] for item in events]
        != list(range(episode_sequence - len(events) + 1, episode_sequence + 1))
        or events[-1]["event"] != episode["event"]
        or events[-1]["recorded_at"] != episode["recorded_at"]
        or events[-1].get("state") != episode["state"]
    ):
        return False

    if (
        not isinstance(effects, list)
        or len(effects) > MAX_EFFECTS
        or any(
            not _has_keys(item, {"effect_id", "kind", "status"})
            or not _valid_bounded_text(item["effect_id"])
            or not _valid_bounded_text(item["kind"])
            or not isinstance(item["status"], str)
            or item["status"] not in {"prepared", "confirmed"}
            for item in effects
        )
        or not isinstance(evidence, list)
        or len(evidence) > MAX_EVIDENCE_UNITS
        or any(not _valid_evidence_reference(item) for item in evidence)
        or not _has_keys(
            omitted,
            {"events", "effects", "evidence_units", "evidence_files"},
        )
        or any(not _nonnegative_int(value) for value in omitted.values())
    ):
        return False
    return True


def _valid_summary_event(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value)
        in (
            {"sequence", "event", "recorded_at"},
            {"sequence", "event", "recorded_at", "state"},
        )
        and type(value["sequence"]) is int
        and value["sequence"] >= 1
        and _valid_bounded_text(value["event"])
        and _valid_bounded_text(value["recorded_at"])
        and ("state" not in value or _valid_bounded_text(value["state"]))
    )


def _valid_evidence_reference(value: Any) -> bool:
    if (
        not _has_keys(
            value,
            {"unit", "manifest_sha256", "total_bytes", "files"},
        )
        or not _valid_bounded_text(value["unit"])
        or re.fullmatch(r"(attempts|gates|retrospective)/[^/]+", value["unit"]) is None
        or not _sha256(value["manifest_sha256"])
        or not _nonnegative_int(value["total_bytes"])
        or not isinstance(value["files"], list)
        or len(value["files"]) > MAX_EVIDENCE_FILES
    ):
        return False
    return all(
        _has_keys(item, {"path", "bytes", "sha256"})
        and isinstance(item["path"], str)
        and bool(item["path"])
        and _nonnegative_int(item["bytes"])
        and _sha256(item["sha256"])
        for item in value["files"]
    )


def _valid_bounded_value(value: Any, *, depth: int = 0) -> bool:
    if isinstance(value, str):
        return _valid_bounded_text(value)
    if value is None or type(value) in {bool, int, float}:
        return True
    if depth >= 4:
        return False
    if isinstance(value, list):
        return len(value) <= MAX_NESTED_ITEMS and all(
            _valid_bounded_value(item, depth=depth + 1) for item in value
        )
    if isinstance(value, dict):
        return (
            len(value) <= MAX_NESTED_ITEMS
            and all(isinstance(key, str) for key in value)
            and all(
                _valid_bounded_value(item, depth=depth + 1) for item in value.values()
            )
        )
    return False


def _valid_bounded_text(value: Any) -> bool:
    return isinstance(value, str) and (
        len(value) <= MAX_STRING_CHARACTERS
        or (
            len(value) == MAX_STRING_CHARACTERS + len(TRUNCATION_SUFFIX)
            and value.endswith(TRUNCATION_SUFFIX)
        )
    )


def _has_keys(value: Any, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    value = redact_artifact_value(value)
    if isinstance(value, str):
        return _bounded_text(value)
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        keys = sorted(value, key=str)[:MAX_NESTED_ITEMS]
        return {str(key): _bounded_value(value[key], depth=depth + 1) for key in keys}
    if isinstance(value, list):
        return [
            _bounded_value(item, depth=depth + 1) for item in value[:MAX_NESTED_ITEMS]
        ]
    return value


def _bounded_text(value: str) -> str:
    redacted = redact_text(value)
    if len(redacted) <= MAX_STRING_CHARACTERS:
        return redacted
    return f"{redacted[:MAX_STRING_CHARACTERS]}{TRUNCATION_SUFFIX}"


def _fit_summary(summary: dict[str, Any]) -> None:
    omitted = summary["omitted"]
    while len(canonical_json(summary).encode("utf-8")) > MAX_RUN_SUMMARY_BYTES:
        evidence_with_files = next(
            (record for record in reversed(summary["evidence"]) if record["files"]),
            None,
        )
        if evidence_with_files is not None:
            evidence_with_files["files"].pop()
            omitted["evidence_files"] += 1
            continue
        if len(summary["events"]) > 1:
            summary["events"].pop(0)
            omitted["events"] += 1
            continue
        if summary["effects"]:
            summary["effects"].pop()
            omitted["effects"] += 1
            continue
        if summary["evidence"]:
            summary["evidence"].pop()
            omitted["evidence_units"] += 1
            continue
        removable = sorted(
            set(summary["projection"]) - {"state", "checkpoint"}, reverse=True
        )
        if removable:
            del summary["projection"][removable[0]]
            continue
        return
