from __future__ import annotations

from typing import Any

from afk.retrospective_attempt import normalize_retrospective_outcome
from afk.retrospective_contract import expected_episode_state
from afk.run_store import RunStore, RunStoreError


def build_retrospective_status(store: RunStore, run_id: str) -> dict[str, Any]:
    """Summarize immutable retrospective outcomes without triggering recovery."""
    episodes = []
    evidence_paths = []
    findings = 0
    proposals = 0
    warnings = 0
    sealed = 0
    for event in store.event_history(run_id):
        state = expected_episode_state(event["event"])
        if state is None:
            continue
        if event.get("state") != state:
            raise RunStoreError(
                f"sequence {event['sequence']} is not a retrospective episode"
            )
        sequence = event["sequence"]
        evidence = (
            f"retrospective/completed-{sequence}"
            if state == "completed"
            else f"retrospective/attention-{sequence}"
        )
        outcome = store.observe_sealed_evidence_result(run_id, evidence)
        if outcome is None:
            episode = {
                "episode_sequence": sequence,
                "event": event["event"],
                "state": state,
                "status": "absent",
                "warning": False,
                "process_findings_count": 0,
                "improvement_proposals_count": 0,
                "evidence_path": None,
            }
        else:
            normalized = normalize_retrospective_outcome(
                outcome,
                run_id=run_id,
                episode_sequence=sequence,
            )
            sealed += 1
            warnings += int(normalized["warning"])
            findings += normalized["process_findings_count"]
            proposals += normalized["improvement_proposals_count"]
            evidence_paths.append(evidence)
            episode = {
                "episode_sequence": sequence,
                "event": event["event"],
                "state": state,
                "status": normalized["status"],
                "warning": normalized["warning"],
                "process_findings_count": normalized["process_findings_count"],
                "improvement_proposals_count": normalized[
                    "improvement_proposals_count"
                ],
                "evidence_path": evidence,
            }
        episodes.append(episode)

    latest = episodes[-1] if episodes else None
    return {
        "schema_version": 1,
        "status": latest["status"] if latest is not None else "absent",
        "episode_counts": {
            "total": len(episodes),
            "sealed": sealed,
            "warning": warnings,
            "absent": len(episodes) - sealed,
        },
        "process_findings_count": findings,
        "improvement_proposals_count": proposals,
        "evidence_paths": evidence_paths,
        "latest": latest,
    }
