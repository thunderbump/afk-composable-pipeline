from __future__ import annotations

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
EPISODE_EVENTS = {"run.attention_required", "run.completed"}
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
    snapshot = store.read_run_snapshot(run_id, through_sequence=episode_sequence)
    event = snapshot["events"][-1]
    expected_state = {
        "run.attention_required": "attention_required",
        "run.completed": "completed",
    }.get(event["event"])
    if expected_state is None or event.get("state") != expected_state:
        raise RunStoreError(
            f"sequence {episode_sequence} is not a retrospective episode"
        )

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
