from __future__ import annotations

import getpass
import json
import os
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afk.bead_spec import BEAD_SPEC_EVIDENCE, load_bead_spec, persist_bead_spec
from afk.candidate import (
    CandidateError,
    produce_candidate,
    produce_repair_candidate,
    reconcile_interrupted_repair_worktree,
    seal_interrupted_repair_attempt,
)
from afk.candidate_gate import GateError, complete_gate_cycle
from afk.candidate_validation import (
    CandidateValidationError,
    VALIDATION_ENVIRONMENT_ALLOWLIST,
    approve_bootstrap_contract,
    recover_candidate_validation,
    validate_candidate,
)
from afk.jsonutil import canonical_json
from afk.run_store import RunStore, RunStoreBusy, RunStoreError
from afk.validation_contract import ValidationContractError, parse_validation_contract


WORKER_LOCK_ATTEMPTS = 40
WORKER_LOCK_RETRY_SECONDS = 0.05
COMMAND_TIMEOUT_SECONDS = 30
CREDENTIAL_FILE_BYTE_LIMIT = 4096
BOOTSTRAP_VALIDATION_ADAPTER = "afk.builtin.bootstrap-validation/v1"


class StartError(RuntimeError):
    pass


class ExternalCommandError(StartError):
    def __init__(self, classification: str, summary: str):
        super().__init__(summary)
        self.classification = classification


@dataclass(frozen=True)
class StartContext:
    root: Path
    repository: str
    base_branch: str
    base_sha: str
    bead_id: str
    claimant: str
    beads_workspace: Path
    validation_contract: dict[str, str]


def start_run(
    bead_id: str,
    *,
    cwd: Path | None = None,
    bootstrap_contract: bool = False,
) -> tuple[str, int]:
    context = preflight(
        bead_id,
        cwd=cwd or Path.cwd(),
        bootstrap_contract=bootstrap_contract,
    )
    store = RunStore()
    with store.lock():
        projection = store.create_run(
            bead_id=bead_id,
            repository=context.repository,
            base_branch=context.base_branch,
            base_sha=context.base_sha,
            start_request={
                "repository_root": str(context.root),
                "beads_workspace": str(context.beads_workspace),
                "claimant": context.claimant,
                "validation_contract": context.validation_contract,
            },
        )
        run_id = projection["run_id"]
        try:
            bead = _show_bead(bead_id, context.beads_workspace)
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint="created",
                scope="bead_preflight",
                kind="unavailable",
                summary=str(exc),
                classification=_error_classification(exc),
                validation_contract=context.validation_contract,
            )
            return run_id, 2
        try:
            _validate_start_bead(bead, bead_id, context.repository)
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint="created",
                scope="bead_preflight",
                kind="invalid",
                summary=str(exc),
                classification=_error_classification(exc),
                validation_contract=context.validation_contract,
            )
            return run_id, 2
        persist_bead_spec(store, run_id, bead)
        unit = worker_unit(run_id)
        lingering = _lingering(context.claimant)
        store.prepare_effect(
            run_id,
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": unit},
        )
        store.append_event(
            run_id,
            "worker.launch_prepared",
            data={
                "unit": unit,
                "checkpoint": "created",
                "lingering": lingering,
                "validation_contract": context.validation_contract,
            },
        )
        try:
            _launch_worker(run_id, unit)
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint="created",
                scope="worker_launch",
                kind="unavailable",
                summary=str(exc),
                classification=_error_classification(exc),
                unit=unit,
            )
            return run_id, 2
    return run_id, 0


def resume_run(*, note: str | None = None) -> tuple[str, int]:
    store = RunStore()
    with store.lock():
        projection = store.status()
        run_id = projection["run_id"]
        if _validation_attempt_open(projection):
            return run_id, _recover_validation_attempt(store, run_id, projection)
        if _repair_interruption_pending(store, run_id, projection):
            return run_id, _recover_interrupted_repair(store, run_id, projection)
        if _interrupted_repair_terminal(projection):
            return run_id, 2
        if (
            _interrupted_repair_resume_ready(projection)
            or projection["last_event"] == "repair.interrupted"
        ):
            return run_id, _advance_interrupted_repair(store, run_id, projection)
        if _repair_resume_ready(projection):
            return run_id, _advance_completed_gate(store, run_id)
        attention = projection.get("attention")
        if isinstance(attention, dict) and attention.get("scope") == "gate":
            if _gate_attention_resume_ready(store, run_id, projection):
                return run_id, _advance_gate(store, run_id)
            return run_id, 2
        if projection["checkpoint"] == "reviewed":
            return run_id, 0
        if projection["checkpoint"] == "validated":
            return run_id, _advance_gate(store, run_id)
        if projection["last_event"] == "validation.rejected":
            return run_id, _advance_gate(store, run_id)
        if projection["last_event"] == "gate.cycle_completed":
            return run_id, _advance_completed_gate(store, run_id)
        if projection["last_event"] == "candidate.repaired":
            return run_id, _advance_repaired_candidate(store, run_id)
        if "worker_exit_code" in projection:
            if _candidate_resume_ready(projection):
                return run_id, _advance_candidate(store, run_id)
            if _validation_resume_ready(projection):
                return run_id, _advance_validation(store, run_id)
            return run_id, projection["worker_exit_code"]
        effect = store.effect(run_id, "worker-launch-1")
        unit = effect["intended"]["unit"]
        try:
            completed = _command(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState",
                    "--property=ActiveState",
                ],
                cwd=Path.cwd(),
                check=False,
            )
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint=projection["checkpoint"],
                scope="worker_launch",
                kind="unavailable",
                summary=str(exc),
                classification=_error_classification(exc),
                unit=unit,
            )
            return run_id, 2
        properties: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in properties:
                properties = {}
                break
            properties[key] = value
        active = completed.returncode == 0 and properties == {
            "LoadState": "loaded",
            "ActiveState": "active",
        }
        absent = properties == {
            "LoadState": "not-found",
            "ActiveState": "inactive",
        }
        if active:
            if effect["status"] != "confirmed":
                store.confirm_effect(run_id, "worker-launch-1", observed={"unit": unit})
                store.append_event(
                    run_id,
                    "worker.launch_reconciled",
                    data={
                        "unit": unit,
                        "checkpoint": projection["checkpoint"],
                        "note": note or "",
                    },
                )
            return run_id, 0
        if absent:
            if effect["status"] == "confirmed":
                if _candidate_resume_ready(projection):
                    return run_id, _advance_candidate(store, run_id)
                if _validation_resume_ready(projection):
                    return run_id, _advance_validation(store, run_id)
                _attention(
                    store,
                    run_id,
                    checkpoint=projection["checkpoint"],
                    scope="worker_launch",
                    kind="inconclusive",
                    summary=(
                        "confirmed worker launch was collected without a terminal "
                        "observation"
                    ),
                    unit=unit,
                )
                return run_id, 2
            try:
                _launch_worker(run_id, unit)
            except StartError as exc:
                _attention(
                    store,
                    run_id,
                    checkpoint=projection["checkpoint"],
                    scope="worker_launch",
                    kind="unavailable",
                    summary=str(exc),
                    classification=_error_classification(exc),
                    unit=unit,
                )
                return run_id, 2
            store.append_event(
                run_id,
                "worker.launch_retried",
                data={
                    "unit": unit,
                    "checkpoint": projection["checkpoint"],
                    "note": note or "",
                },
            )
            return run_id, 0
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="worker_launch",
            kind="inconclusive",
            summary="prepared worker launch could not be confirmed",
            unit=unit,
        )
        return run_id, 2


def approve_bootstrap_validation(
    harness_path: str,
    *,
    timeout_seconds: int,
    run_id: str | None = None,
) -> str:
    store = RunStore()
    with store.lock():
        projection = store.status(run_id)
        if projection["checkpoint"] != "candidate_ready":
            raise StartError("bootstrap approval requires candidate_ready")
        try:
            contract = approve_bootstrap_contract(
                Path(projection["worktree_path"]),
                projection["candidate_sha"],
                projection["validation_contract"],
                harness_path,
                timeout_seconds,
            )
        except (KeyError, CandidateValidationError) as exc:
            raise StartError(str(exc)) from exc
        store.append_event(
            projection["run_id"],
            "validation.bootstrap_approved",
            state="attention_required",
            data={
                "checkpoint": "candidate_ready",
                "validation_contract": contract,
                "attention": {
                    "scope": "validation",
                    "kind": "unavailable",
                    "summary": "approved bootstrap validation is ready",
                },
            },
        )
        return projection["run_id"]


def _candidate_resume_ready(projection: dict[str, Any]) -> bool:
    attention = projection.get("attention", {})
    return (
        projection["checkpoint"] in {"worktree_ready", "change_committed"}
        and isinstance(attention, dict)
        and (
            attention.get("scope") == "candidate"
            or (
                projection["checkpoint"] == "worktree_ready"
                and attention.get("scope") == "implementation"
                and attention.get("kind") == "unavailable"
            )
        )
    )


def _validation_resume_ready(projection: dict[str, Any]) -> bool:
    attention = projection.get("attention", {})
    return (
        projection["checkpoint"] == "candidate_ready"
        and isinstance(attention, dict)
        and attention.get("scope") == "validation"
        and attention.get("kind") in {"unavailable", "inconclusive", "interrupted"}
    )


def _gate_attention_resume_ready(
    store: RunStore, run_id: str, projection: dict[str, Any]
) -> bool:
    candidate_sha = projection.get("candidate_sha")
    used = projection.get("repair_attempts_used", 0)
    if (
        not isinstance(candidate_sha, str)
        or not _is_full_git_sha(candidate_sha)
        or type(used) is not int
        or not 0 <= used <= 4
    ):
        return False
    cycle = used + 1
    evidence = f"gates/gate-cycle-{cycle}-{candidate_sha[:12]}"
    outcome_path = store.root / "runs" / run_id / evidence / "outcome.json"
    try:
        if not store.verify_evidence(run_id, evidence):
            return False
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RunStoreError):
        return False
    validation = outcome.get("validation") if isinstance(outcome, dict) else None
    current_validation = projection.get("validation")
    cycles = projection.get("gate_cycles", [])
    if (
        not isinstance(outcome, dict)
        or outcome.get("schema_version") != 1
        or outcome.get("cycle") != cycle
        or outcome.get("candidate_sha") != candidate_sha
        or outcome.get("evidence") != evidence
        or not isinstance(validation, dict)
        or not isinstance(current_validation, dict)
        or validation.get("candidate_sha") != candidate_sha
        or validation.get("status") != current_validation.get("status")
        or validation.get("evidence") != current_validation.get("evidence")
        or not isinstance(outcome.get("reviews"), list)
        or outcome.get("prior_dispositions")
        != projection.get("repair_dispositions", [])
        or outcome.get("next_action") not in {"complete", "attention", "repair"}
        or not isinstance(cycles, list)
        or any(
            isinstance(item, dict)
            and item.get("cycle") == cycle
            and item.get("candidate_sha") == candidate_sha
            for item in cycles
        )
    ):
        return False
    brief = outcome.get("repair_brief")
    return outcome.get("next_action") != "repair" or (
        isinstance(brief, dict)
        and brief.get("candidate_sha") == candidate_sha
        and brief.get("repair_attempt") == cycle
    )


def _repair_resume_ready(projection: dict[str, Any]) -> bool:
    brief = projection.get("repair_brief")
    return (
        projection["checkpoint"] in {"candidate_ready", "validated"}
        and isinstance(brief, dict)
        and brief.get("candidate_sha") == projection.get("candidate_sha")
        and brief.get("repair_attempt") == projection.get("repair_attempts_used")
    )


def _repair_interruption_pending(
    store: RunStore, run_id: str, projection: dict[str, Any]
) -> bool:
    brief = projection.get("repair_brief")
    used = projection.get("repair_attempts_used")
    if (
        not isinstance(brief, dict)
        or type(used) is not int
        or brief.get("repair_attempt") != used
        or brief.get("candidate_sha") != projection.get("candidate_sha")
    ):
        return False
    attempt = store.root / "runs" / run_id / f"attempts/repair-{used}"
    return (
        not (attempt / "manifest.json").is_file()
        or (attempt / "interruption.json").is_file()
    )


def _interrupted_repair_resume_ready(projection: dict[str, Any]) -> bool:
    interruption = projection.get("interrupted_repair")
    brief = projection.get("repair_brief")
    used = projection.get("repair_attempts_used")
    return (
        isinstance(interruption, dict)
        and interruption.get("schema_version") == 1
        and interruption.get("status") == "interrupted"
        and interruption.get("candidate_sha") == projection.get("candidate_sha")
        and interruption.get("repair_attempt") == used
        and type(used) is int
        and 1 <= used <= 4
        and isinstance(brief, dict)
        and (
            brief == {}
            if used == 4
            else brief.get("candidate_sha") == projection.get("candidate_sha")
            and brief.get("repair_attempt") == used + 1
        )
    )


def _interrupted_repair_terminal(projection: dict[str, Any]) -> bool:
    interruption = projection.get("interrupted_repair")
    return (
        isinstance(interruption, dict)
        and interruption.get("schema_version") == 1
        and interruption.get("status") == "exhausted"
        and interruption.get("candidate_sha") == projection.get("candidate_sha")
        and interruption.get("repair_attempt") == 4
        and projection.get("repair_attempts_used") == 4
        and projection.get("repair_brief") == {}
    )


def _recover_interrupted_repair(
    store: RunStore, run_id: str, projection: dict[str, Any]
) -> int:
    brief = projection["repair_brief"]
    attempt_number = projection["repair_attempts_used"]
    try:
        interruption = seal_interrupted_repair_attempt(
            store,
            run_id,
            repair_brief=brief,
        )
    except (CandidateError, OSError, RunStoreError, ValueError) as exc:
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="repair",
            kind=exc.kind if isinstance(exc, CandidateError) else "unavailable",
            summary=exc.summary if isinstance(exc, CandidateError) else str(exc),
        )
        return 2
    next_brief = (
        {**brief, "repair_attempt": attempt_number + 1} if attempt_number < 4 else {}
    )
    projection = store.append_event(
        run_id,
        "repair.interrupted",
        data={
            "checkpoint": projection["checkpoint"],
            "repair_attempts_used": attempt_number,
            "repair_brief": next_brief,
            "interrupted_repair": interruption,
        },
    )
    return _advance_interrupted_repair(store, run_id, projection)


def _advance_interrupted_repair(
    store: RunStore, run_id: str, projection: dict[str, Any]
) -> int:
    used = projection.get("repair_attempts_used")
    if not _interrupted_repair_resume_ready(projection):
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="repair",
            kind="invalid",
            summary="interrupted repair continuation is invalid",
        )
        return 2
    if used == 4:
        interruption = projection.get("interrupted_repair", {})
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="repair",
            kind="exhausted",
            summary="repair budget exhausted after interrupted fourth attempt",
            interrupted_repair={**interruption, "status": "exhausted"},
        )
        return 2
    brief = projection["repair_brief"]
    try:
        reconcile_interrupted_repair_worktree(
            store,
            run_id,
            repair_brief=brief,
        )
    except (CandidateError, OSError, RunStoreError, ValueError) as exc:
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="repair",
            kind=exc.kind if isinstance(exc, CandidateError) else "unavailable",
            summary=exc.summary if isinstance(exc, CandidateError) else str(exc),
            interrupted_repair=projection["interrupted_repair"],
        )
        return 2
    return _advance_completed_gate(
        store,
        run_id,
        outcome={"next_action": "repair", "repair_brief": brief},
    )


def _validation_attempt_open(projection: dict[str, Any]) -> bool:
    attempt = projection.get("validation_attempt")
    return isinstance(attempt, dict) and attempt.get("status") == "started"


def _recover_validation_attempt(
    store: RunStore, run_id: str, projection: dict[str, Any]
) -> int:
    attempt = projection["validation_attempt"]
    validation = recover_candidate_validation(store, run_id, attempt)
    if validation is not None:
        attempt_evidence = store.root / "runs" / run_id / attempt["evidence"]
        if (attempt_evidence / "manifest.json").exists():
            store.verify_evidence(run_id, attempt["evidence"])
            attempt = {**attempt, "status": validation["status"]}
            store.append_event(
                run_id,
                "validation.attempt_finished",
                data={
                    "checkpoint": "candidate_ready",
                    "validation_attempt": attempt,
                },
            )
        else:
            attempt = _finish_validation_attempt(
                store,
                run_id,
                attempt,
                status=validation["status"],
                summary=validation["summary"],
            )
        return _record_validation_outcome(store, run_id, validation)
    summary = "validation attempt was interrupted before completion"
    evidence_path = store.root / "runs" / run_id / attempt["evidence"]
    if (evidence_path / "manifest.json").exists():
        store.verify_evidence(run_id, attempt["evidence"])
        attempt = {**attempt, "status": "interrupted"}
        store.append_event(
            run_id,
            "validation.attempt_finished",
            data={"checkpoint": "candidate_ready", "validation_attempt": attempt},
        )
    else:
        attempt = _finish_validation_attempt(
            store,
            run_id,
            attempt,
            status="interrupted",
            summary=summary,
        )
    _attention(
        store,
        run_id,
        checkpoint="candidate_ready",
        scope="validation",
        kind="interrupted",
        summary=summary,
        validation_attempt=attempt,
    )
    return 2


def _advance_validation(store: RunStore, run_id: str) -> int:
    projection = store.status(run_id)
    attempt = _start_validation_attempt(store, run_id, projection["candidate_sha"])
    try:
        validation = validate_candidate(
            store,
            run_id,
            attempt_id=attempt["attempt_id"],
            attempt_evidence=attempt["evidence"],
            gate_evidence=f"gates/{attempt['attempt_id']}",
        )
    except CandidateValidationError as exc:
        attempt = _finish_validation_attempt(
            store,
            run_id,
            attempt,
            status=exc.kind,
            summary=exc.summary,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )
        _attention(
            store,
            run_id,
            checkpoint="candidate_ready",
            scope="validation",
            kind=exc.kind,
            summary=exc.summary,
            validation_attempt=attempt,
        )
        return 2
    attempt = _finish_validation_attempt(
        store,
        run_id,
        attempt,
        status=validation["status"],
        summary=validation["summary"],
    )
    return _record_validation_outcome(store, run_id, validation)


def _record_validation_outcome(
    store: RunStore, run_id: str, validation: dict[str, Any]
) -> int:
    if validation["status"] == "passed":
        store.append_event(
            run_id,
            "validation.passed",
            state="validated",
            data={"checkpoint": "validated", "validation": validation},
        )
        return 0
    if validation["status"] == "rejected":
        store.append_event(
            run_id,
            "validation.rejected",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "attention": {},
                "validation": validation,
            },
        )
        return 0
    _attention(
        store,
        run_id,
        checkpoint="candidate_ready",
        scope="validation",
        kind=validation["status"],
        summary=validation["summary"],
        validation=validation,
    )
    return 2


def _start_validation_attempt(
    store: RunStore, run_id: str, candidate_sha: str
) -> dict[str, str]:
    projection = store.status(run_id)
    base_attempt_id = f"validation-{candidate_sha[:12]}"
    attempt_id = (
        f"{base_attempt_id}-{projection['last_sequence'] + 1}"
        if "validation_attempt" in projection
        else base_attempt_id
    )
    attempt = {
        "attempt_id": attempt_id,
        "candidate_sha": candidate_sha,
        "status": "started",
        "evidence": f"attempts/{attempt_id}",
    }
    store.append_event(
        run_id,
        "validation.attempt_started",
        data={"checkpoint": "candidate_ready", "validation_attempt": attempt},
    )
    return attempt


def _finish_validation_attempt(
    store: RunStore,
    run_id: str,
    attempt: dict[str, str],
    *,
    status: str,
    summary: str,
    stdout: str | None = None,
    stderr: str | None = None,
) -> dict[str, str]:
    evidence = attempt["evidence"]
    evidence_path = store.root / "runs" / run_id / evidence
    metadata = evidence_path / "afk"
    for name, value in (("stdout.log", stdout), ("stderr.log", stderr)):
        if not (metadata / name).exists():
            store.write_evidence_text(run_id, f"{evidence}/afk/{name}", value or "")
    if not (metadata / "outcome.json").exists():
        store.write_evidence_text(
            run_id,
            f"{evidence}/afk/outcome.json",
            canonical_json(
                {
                    "schema_version": 1,
                    "attempt_id": attempt["attempt_id"],
                    "candidate_sha": attempt["candidate_sha"],
                    "status": status,
                    "summary": summary,
                }
            )
            + "\n",
        )
    store.seal_evidence(run_id, evidence)
    finished = {**attempt, "status": status}
    store.append_event(
        run_id,
        "validation.attempt_finished",
        data={"checkpoint": "candidate_ready", "validation_attempt": finished},
    )
    return finished


def run_worker(run_id: str) -> int:
    store = RunStore()
    for attempt in range(WORKER_LOCK_ATTEMPTS):
        try:
            return _run_worker_with_lock(store, run_id)
        except RunStoreBusy:
            if attempt + 1 == WORKER_LOCK_ATTEMPTS:
                return 1
            time.sleep(WORKER_LOCK_RETRY_SECONDS)
    return 1


def run_worker_unit(run_id: str) -> int:
    exit_code = run_worker(run_id)
    worker_result = {
        0: "completed",
        2: "attention_required",
    }.get(exit_code, "failed")
    store = RunStore()
    while True:
        try:
            with store.lock():
                projection = store.status(run_id)
                terminal_keys = ("worker_exit_code", "worker_result")
                terminal_fields = [key for key in terminal_keys if key in projection]
                if terminal_fields:
                    if len(terminal_fields) != len(terminal_keys):
                        raise StartError("worker terminal observation is incomplete")
                    if (
                        type(projection["worker_exit_code"]) is int
                        and projection["worker_exit_code"] == exit_code
                        and projection["worker_result"] == worker_result
                    ):
                        return exit_code
                    raise StartError(
                        "worker terminal observation conflicts with result"
                    )
                store.append_event(
                    run_id,
                    "worker.terminal",
                    data={
                        "checkpoint": projection["checkpoint"],
                        "unit": worker_unit(run_id),
                        "worker_exit_code": exit_code,
                        "worker_result": worker_result,
                    },
                )
            return exit_code
        except (OSError, RunStoreError) as exc:
            print(
                f"worker terminal observation pending: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(WORKER_LOCK_RETRY_SECONDS)


def _run_worker_with_lock(store: RunStore, run_id: str) -> int:
    try:
        with store.lock():
            identity = store.identity(run_id)
            request = identity.get("start_request", {})
            bead_id = identity["bead_id"]
            beads_workspace = Path(request["beads_workspace"])
            claimant = request["claimant"]
            launch = store.effect(run_id, "worker-launch-1")
            unit = worker_unit(run_id)
            if launch["intended"] != {"unit": unit}:
                raise StartError("worker launch Effect does not match this worker")
            store.confirm_effect(run_id, "worker-launch-1", observed={"unit": unit})
            store.append_event(
                run_id,
                "worker.launched",
                data={"checkpoint": "created", "unit": unit},
            )
            claim = store.prepare_effect(
                run_id,
                "bead-claim",
                kind="bead-claim",
                intended={"bead_id": bead_id, "claimant": claimant},
            )
            if claim["status"] != "confirmed":
                observed = _claim_bead(bead_id, claimant, beads_workspace)
                store.confirm_effect(run_id, "bead-claim", observed=observed)
            store.append_event(
                run_id,
                "bead.claimed",
                state="claimed",
                data={"checkpoint": "claimed", "unit": worker_unit(run_id)},
            )
            worktree_path, branch = _prepare_worktree(store, identity)
            store.append_event(
                run_id,
                "worktree.ready",
                state="worktree_ready",
                data={
                    "checkpoint": "worktree_ready",
                    "unit": worker_unit(run_id),
                    "worktree_path": str(worktree_path),
                    "branch": branch,
                },
            )
            return _advance_candidate(store, run_id)
    except RunStoreBusy:
        raise
    except (KeyError, OSError, StartError, RunStoreError, ValueError) as exc:
        try:
            checkpoint = store.status(run_id)["checkpoint"]
            _attention(
                store,
                run_id,
                checkpoint=checkpoint,
                scope="worker",
                kind="unavailable",
                summary=str(exc),
                classification=(
                    _error_classification(exc) if isinstance(exc, StartError) else None
                ),
                unit=worker_unit(run_id),
            )
            return 2
        except (RunStoreError, OSError):
            return 1


def _advance_candidate(store: RunStore, run_id: str) -> int:
    try:
        bead = _bead_for_run(store, run_id)
        produce_candidate(store, run_id, bead=bead)
    except CandidateError as exc:
        checkpoint = store.status(run_id)["checkpoint"]
        _attention(
            store,
            run_id,
            checkpoint=checkpoint,
            scope="candidate",
            kind=exc.kind,
            summary=exc.summary,
        )
        return 2
    except (KeyError, OSError, StartError, RunStoreError, ValueError) as exc:
        checkpoint = store.status(run_id)["checkpoint"]
        _attention(
            store,
            run_id,
            checkpoint=checkpoint,
            scope="candidate",
            kind="unavailable",
            summary=str(exc),
            classification=(
                _error_classification(exc) if isinstance(exc, StartError) else None
            ),
        )
        return 2
    return _advance_validation_then_gate(store, run_id)


def _advance_gate(store: RunStore, run_id: str) -> int:
    try:
        bead = _bead_for_run(store, run_id)
        outcome = complete_gate_cycle(store, run_id, bead=bead)
    except GateError as exc:
        _attention(
            store,
            run_id,
            checkpoint=store.status(run_id)["checkpoint"],
            scope="gate",
            kind=exc.kind,
            summary=exc.summary,
        )
        return 2
    except (KeyError, OSError, StartError, RunStoreError, ValueError) as exc:
        _attention(
            store,
            run_id,
            checkpoint=store.status(run_id)["checkpoint"],
            scope="gate",
            kind="unavailable",
            summary=str(exc),
            classification=(
                _error_classification(exc) if isinstance(exc, StartError) else None
            ),
        )
        return 2
    return _advance_completed_gate(store, run_id, outcome=outcome, bead=bead)


def _advance_repaired_candidate(store: RunStore, run_id: str) -> int:
    validation_contract = store.status(run_id).get("validation_contract", {})
    if (
        isinstance(validation_contract, dict)
        and validation_contract.get("source") == "approved_bootstrap"
    ):
        _attention(
            store,
            run_id,
            checkpoint="candidate_ready",
            scope="validation",
            kind="unavailable",
            summary=(
                "repaired bootstrap Candidate requires explicit operator reapproval"
            ),
        )
        return 2
    return _advance_validation_then_gate(store, run_id)


def _advance_validation_then_gate(store: RunStore, run_id: str) -> int:
    exit_code = _advance_validation(store, run_id)
    if exit_code != 0:
        return exit_code
    return _advance_gate(store, run_id)


def _bead_for_run(store: RunStore, run_id: str) -> dict[str, Any]:
    evidence = store.root / "runs" / run_id / BEAD_SPEC_EVIDENCE
    if "bead_spec" in store.status(run_id) or evidence.exists():
        return load_bead_spec(store, run_id)
    identity = store.identity(run_id)
    request = identity.get("start_request", {})
    return _show_bead(identity["bead_id"], Path(request["beads_workspace"]))


def _advance_completed_gate(
    store: RunStore,
    run_id: str,
    *,
    outcome: dict[str, Any] | None = None,
    bead: dict[str, Any] | None = None,
) -> int:
    projection = store.status(run_id)
    if outcome is None:
        cycles = projection.get("gate_cycles", [])
        if (
            not isinstance(cycles, list)
            or not cycles
            or not isinstance(cycles[-1], dict)
        ):
            _attention(
                store,
                run_id,
                checkpoint=projection["checkpoint"],
                scope="gate",
                kind="invalid",
                summary="completed Gate Cycle outcome is missing",
            )
            return 2
        outcome = cycles[-1]
    next_action = outcome.get("next_action")
    if next_action == "complete":
        return 0
    if next_action == "attention":
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="gate",
            kind="exhausted" if outcome.get("stop_reason") else "inconclusive",
            summary=str(outcome.get("stop_reason", "Gate Cycle was inconclusive")),
        )
        return 2
    if next_action != "repair" or not isinstance(outcome.get("repair_brief"), dict):
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="gate",
            kind="invalid",
            summary="Gate Cycle next action is invalid",
        )
        return 2
    try:
        if bead is None:
            bead = _bead_for_run(store, run_id)
        produce_repair_candidate(
            store,
            run_id,
            bead=bead,
            repair_brief=outcome["repair_brief"],
        )
    except CandidateError as exc:
        _attention(
            store,
            run_id,
            checkpoint=store.status(run_id)["checkpoint"],
            scope="repair",
            kind=exc.kind,
            summary=exc.summary,
        )
        return 2
    except (KeyError, OSError, StartError, RunStoreError, ValueError) as exc:
        _attention(
            store,
            run_id,
            checkpoint=store.status(run_id)["checkpoint"],
            scope="repair",
            kind="unavailable",
            summary=str(exc),
            classification=(
                _error_classification(exc) if isinstance(exc, StartError) else None
            ),
        )
        return 2
    return _advance_repaired_candidate(store, run_id)


def preflight(
    bead_id: str, *, cwd: Path, bootstrap_contract: bool = False
) -> StartContext:
    root = Path(_required(["git", "rev-parse", "--show-toplevel"], cwd=cwd)).resolve()
    repository_data = _json_command(
        ["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"],
        cwd=root,
    )
    if not isinstance(repository_data, dict):
        raise StartError("GitHub repository or default branch is unavailable")
    repository = repository_data.get("nameWithOwner")
    default_branch_data = repository_data.get("defaultBranchRef")
    default_branch = (
        default_branch_data.get("name")
        if isinstance(default_branch_data, dict)
        else None
    )
    if (
        not isinstance(repository, str)
        or not repository
        or not isinstance(default_branch, str)
        or not default_branch
    ):
        raise StartError("GitHub repository or default branch is unavailable")
    remote_ref = f"refs/heads/{default_branch}"
    remote_line = _required(
        ["git", "ls-remote", "--exit-code", "origin", remote_ref], cwd=root
    )
    fields = remote_line.split()
    base_sha = fields[0] if len(fields) == 2 and fields[1] == remote_ref else ""
    if not _is_full_git_sha(base_sha):
        raise StartError("GitHub default branch does not resolve to a full Git SHA")
    _required(["git", "fetch", "--no-tags", "origin", remote_ref], cwd=root)
    fetched_sha = _required(["git", "rev-parse", "FETCH_HEAD"], cwd=root)
    if fetched_sha != base_sha:
        raise StartError("fetched default branch does not match the pinned GitHub SHA")
    validation_contract = _pinned_validation_contract(
        root,
        base_sha,
        bootstrap_contract=bootstrap_contract,
    )
    workspace = Path(
        os.environ.get("AFK_BEADS_WORKSPACE", "/home/bump/Projects/beads")
    ).resolve()
    if not workspace.is_dir():
        raise StartError(f"Beads workspace does not exist: {workspace}")
    claimant = (
        os.environ.get("BEADS_ACTOR") or os.environ.get("USER") or getpass.getuser()
    )
    return StartContext(
        root=root,
        repository=repository,
        base_branch=default_branch,
        base_sha=base_sha,
        bead_id=bead_id,
        claimant=claimant,
        beads_workspace=workspace,
        validation_contract=validation_contract,
    )


def worker_unit(run_id: str) -> str:
    return f"afk-{run_id}-worker-1"


def _launch_worker(run_id: str, unit: str) -> None:
    environment = []
    for name in (
        *VALIDATION_ENVIRONMENT_ALLOWLIST,
        "PYTHONPATH",
        "XDG_STATE_HOME",
        "AFK_BEADS_WORKSPACE",
    ):
        value = os.environ.get(name)
        if value is not None:
            environment.append(f"--setenv={name}={value}")
    command = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--property=Type=exec",
        "--property=Restart=no",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=30",
        "--property=UMask=0077",
        "--collect",
        *environment,
        sys.executable,
        "-m",
        "afk",
        "_worker_unit",
        run_id,
    ]
    completed = _command(command, cwd=Path.cwd(), check=False)
    if completed.returncode != 0:
        raise _external_failure(command[0], completed)


def _claim_bead(bead_id: str, claimant: str, workspace: Path) -> dict[str, str]:
    bead = _show_bead(bead_id, workspace)
    if bead.get("status") == "open" and not bead.get("assignee"):
        result = _bd_json(["bd", "update", bead_id, "--claim", "--json"], cwd=workspace)
        if isinstance(result, list):
            if len(result) != 1 or not isinstance(result[0], dict):
                raise _malformed_beads_output()
            result = result[0]
        elif not isinstance(result, dict):
            raise _malformed_beads_output()
        bead = result
    if bead.get("id") != bead_id:
        raise StartError(f"Bead claim returned unexpected Bead: {bead_id}")
    if bead.get("status") != "in_progress" or bead.get("assignee") != claimant:
        raise StartError(f"Bead claim conflicts with current owner: {bead_id}")
    return {"bead_id": bead_id, "claimant": claimant}


def _prepare_worktree(store: RunStore, identity: dict[str, Any]) -> tuple[Path, str]:
    run_id = identity["run_id"]
    root = Path(identity["start_request"]["repository_root"])
    worktree = store.root / "worktrees" / run_id
    branch = f"afk/{identity['bead_id'].replace('.', '-')}-{run_id}/candidate"
    if not worktree.exists():
        worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        completed = _command(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                identity["base_sha"],
            ],
            cwd=root,
            check=False,
        )
        if completed.returncode != 0:
            raise _external_failure("git", completed)
    listing = _required(["git", "worktree", "list", "--porcelain"], cwd=root)
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in [*listing.splitlines(), ""]:
        if not line:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, value = line.partition(" ")
        record[key] = value
    registered = next(
        (
            item
            for item in records
            if item.get("worktree")
            and Path(item["worktree"]).resolve() == worktree.resolve()
        ),
        None,
    )
    if registered is None:
        raise StartError("prepared worktree is not registered")
    if registered.get("HEAD") != identity["base_sha"]:
        raise StartError("prepared worktree is not at pinned base")
    if registered.get("branch") != f"refs/heads/{branch}":
        raise StartError("prepared worktree is not on the intended branch")
    dirty = _required(["git", "status", "--porcelain"], cwd=worktree)
    if dirty:
        raise StartError("prepared worktree is dirty")
    return worktree, branch


def _show_bead(bead_id: str, workspace: Path) -> dict[str, Any]:
    result = _bd_json(["bd", "show", bead_id, "--json"], cwd=workspace)
    if (
        not isinstance(result, list)
        or len(result) != 1
        or not isinstance(result[0], dict)
    ):
        raise _malformed_beads_output()
    return result[0]


def _validate_start_bead(bead: dict[str, Any], bead_id: str, repository: str) -> None:
    if bead.get("id") != bead_id or bead.get("status") != "open":
        raise StartError(f"Bead is not open and exact: {bead_id}")
    project_label = f"project:{repository.rsplit('/', 1)[-1]}"
    labels = bead.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        raise StartError(f"Bead labels are invalid: {bead_id}")
    if project_label not in labels:
        raise StartError(f"Bead does not belong to {project_label}")


def _pinned_validation_contract(
    root: Path, base_sha: str, *, bootstrap_contract: bool
) -> dict[str, str]:
    listing = _required(["git", "ls-tree", base_sha, "--", "afk.toml"], cwd=root)
    if not listing:
        if bootstrap_contract:
            return {
                "source": "approved_bootstrap",
                "base_sha": base_sha,
                "adapter_id": BOOTSTRAP_VALIDATION_ADAPTER,
            }
        raise StartError("pinned base does not contain afk.toml")
    fields = listing.split()
    if (
        len(fields) != 4
        or fields[0] != "100644"
        or fields[1] != "blob"
        or not _is_full_git_sha(fields[2])
    ):
        raise StartError("pinned afk.toml must be one regular file")
    if bootstrap_contract:
        raise StartError("pinned base already contains afk.toml")
    value = _required(["git", "cat-file", "blob", f"{base_sha}:afk.toml"], cwd=root)
    _validate_contract(value)
    return {
        "source": "pinned_base",
        "base_sha": base_sha,
        "blob_sha": fields[2],
    }


def _is_full_git_sha(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_contract(value: str) -> None:
    try:
        parse_validation_contract(value)
    except ValidationContractError as exc:
        raise StartError(f"invalid afk.toml: {exc}") from exc


def _lingering(claimant: str) -> str:
    completed = _command(
        ["loginctl", "show-user", claimant, "--property=Linger", "--value"],
        cwd=Path.cwd(),
        check=False,
    )
    if completed.returncode != 0:
        return "unknown"
    return "enabled" if completed.stdout.strip().lower() == "yes" else "disabled"


def _attention(
    store: RunStore,
    run_id: str,
    *,
    checkpoint: str,
    scope: str,
    kind: str,
    summary: str,
    classification: str | None = None,
    **details: Any,
) -> None:
    store.append_event(
        run_id,
        "run.attention_required",
        state="attention_required",
        data={
            "checkpoint": checkpoint,
            "attention": {
                "scope": scope,
                "kind": kind,
                "summary": summary,
                **({"classification": classification} if classification else {}),
            },
            **details,
        },
    )


def _required(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> str:
    completed = _command(command, cwd=cwd, check=False, env=env)
    if completed.returncode != 0:
        raise _external_failure(command[0], completed)
    return completed.stdout.strip()


def _json_command(command: list[str], *, cwd: Path) -> Any:
    output = _required(command, cwd=cwd)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError(
            "malformed_output", f"{_tool_name(command[0])} returned malformed output"
        ) from exc


def _bd_json(command: list[str], *, cwd: Path) -> Any:
    environment = os.environ.copy()
    environment["BEADS_DOLT_PASSWORD"] = _beads_password()
    completed = _command(command, cwd=cwd, check=False, env=environment)
    if completed.returncode != 0:
        raise _external_failure("bd", completed)
    output = completed.stdout.strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise _malformed_beads_output() from exc


def _malformed_beads_output() -> ExternalCommandError:
    return ExternalCommandError("malformed_output", "Beads returned malformed output")


def _error_classification(error: StartError) -> str | None:
    value = getattr(error, "classification", None)
    return value if isinstance(value, str) else None


def _external_failure(
    tool: str, completed: subprocess.CompletedProcess[str]
) -> ExternalCommandError:
    raw = f"{completed.stdout}\n{completed.stderr}".lower()
    authentication_markers = (
        "authentication denied",
        "authentication failed",
        "access denied",
        "unauthorized",
        "invalid password",
    )
    name = _tool_name(tool)
    if any(marker in raw for marker in authentication_markers):
        return ExternalCommandError(
            "authentication_denied", f"{name} authentication failed"
        )
    return ExternalCommandError("command_failed", f"{name} command failed")


def _tool_name(tool: str) -> str:
    return {"bd": "Beads", "gh": "GitHub"}.get(tool, tool)


def _beads_password() -> str:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    config_path = config_home / "afk" / "config.toml"
    try:
        config = tomllib.loads(_read_private_text(config_path))
        beads = config.get("beads")
        if (
            set(config) != {"schema_version", "beads"}
            or type(config.get("schema_version")) is not int
            or config["schema_version"] != 1
            or not isinstance(beads, dict)
            or set(beads) != {"password_file"}
            or not isinstance(beads["password_file"], str)
        ):
            raise ValueError
        password_path = Path(beads["password_file"])
        if not password_path.is_absolute():
            raise ValueError
        lines = _read_private_text(password_path).splitlines()
        if not lines or not lines[0]:
            raise ValueError
        return lines[0]
    except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise StartError(
            "Beads credential configuration is missing or invalid"
        ) from exc


def _read_private_text(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > CREDENTIAL_FILE_BYTE_LIMIT
        ):
            raise ValueError
        chunks = []
        remaining = CREDENTIAL_FILE_BYTE_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > CREDENTIAL_FILE_BYTE_LIMIT:
            raise ValueError
        return payload.decode("utf-8")
    finally:
        os.close(descriptor)


def _command(
    command: list[str],
    *,
    cwd: Path,
    check: bool,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
            env=env,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalCommandError(
            "command_timeout", f"{_tool_name(command[0])} command timed out"
        ) from exc
    except OSError as exc:
        raise ExternalCommandError(
            "command_unavailable", f"{_tool_name(command[0])} command is unavailable"
        ) from exc
