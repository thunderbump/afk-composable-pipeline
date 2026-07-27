from __future__ import annotations

import json
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
    if type(episode_sequence) is not int or episode_sequence < 1:
        raise RunStoreError("episode_sequence must be a positive integer")
    event = store.event(run_id, episode_sequence)
    _validate_episode(event, episode_sequence)
    summary_evidence = f"retrospective/run-summary-{episode_sequence:020d}"
    cached = store.sealed_evidence_result(run_id, summary_evidence)
    if cached is not None:
        return _validate_cached_summary(
            cached,
            run_id=run_id,
            episode_sequence=episode_sequence,
            event=event,
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
    if (
        not isinstance(summary, dict)
        or not isinstance(summary.get("run"), dict)
        or summary["run"].get("run_id") != run_id
        or not isinstance(summary.get("episode"), dict)
        or summary["episode"].get("sequence") != episode_sequence
        or summary["episode"].get("event") != event["event"]
        or summary["episode"].get("state") != event["state"]
    ):
        raise RunStoreError("sealed Run Summary content identity is invalid")
    return rendered


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
    return f"{redacted[:MAX_STRING_CHARACTERS]}…[TRUNCATED]"


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
