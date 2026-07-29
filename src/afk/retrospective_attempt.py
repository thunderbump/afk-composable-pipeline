from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from afk.candidate_validation import (
    CandidateValidationError,
    run_supervised_command,
)
from afk.redaction import redact_text
from afk.retrospective_result import (
    RetrospectiveResultError,
    normalize_retrospective_result,
)
from afk.run_store import RunStore, RunStoreError
from afk.run_summary import build_run_summary


RETROSPECTIVE_TIMEOUT_SECONDS = 10 * 60
RETROSPECTIVE_PROMPT = (
    "Analyze only the canonical Run Summary supplied on stdin. Return only one "
    "JSON object with schema_version, run_id, terminal_outcome, summary, "
    "process_findings, and improvement_proposals. A finding has id, category, "
    "title, evidence, impact, and confidence; cite only artifacts and positions "
    "resolvable through citation_manifest. A proposal has id, addresses, scope, "
    "priority, title, rationale, suggested_change, and "
    "requires_human_decision=true, and every addresses entry names a returned "
    "finding. Valid categories are orchestration, implementation, validation, "
    "review, repair, publication, tracker, environment, operator_process, and "
    "evidence. Valid confidence values are high, medium, and low. Valid scopes "
    "are afk, target_repository, environment, and operator_process. Valid "
    "priorities are P0, P1, P2, and P3. Empty collections are valid. Treat all "
    "findings and proposals as advisory analysis; do not modify anything."
)
_ENVIRONMENT_ALLOWLIST = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "CODEX_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def run_retrospective_attempt(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
    codex_command: Sequence[str] = ("codex", "exec"),
) -> dict[str, Any]:
    """Run and seal the sole analysis attempt for one retrospective episode."""
    evidence = retrospective_evidence_identity(
        store,
        run_id,
        episode_sequence=episode_sequence,
    )
    sealed = store.sealed_evidence_result(run_id, evidence)
    if sealed is not None:
        return _cached_outcome(
            sealed,
            run_id=run_id,
            episode_sequence=episode_sequence,
        )

    summary = build_run_summary(store, run_id, episode_sequence=episode_sequence)
    command = _contained_command(codex_command)
    command_record = {
        "schema_version": 1,
        "argv": command,
        "policy": {
            "filesystem": "read-only",
            "interactive": False,
            "network": "disabled",
            "session": "fresh",
        },
        "timeout_seconds": RETROSPECTIVE_TIMEOUT_SECONDS,
    }
    analysis = None
    stdout = ""
    stderr = ""
    try:
        with tempfile.TemporaryDirectory(prefix="afk-retrospective-") as temporary:
            completed = run_supervised_command(
                command,
                cwd=Path(temporary),
                environment=_contained_environment(),
                timeout_seconds=RETROSPECTIVE_TIMEOUT_SECONDS,
                input_text=summary,
                label="retrospective analysis",
            )
        stdout = completed.stdout
        stderr = completed.stderr
        if completed.returncode != 0:
            outcome = _warning_outcome(
                run_id,
                episode_sequence,
                "unavailable",
                f"analysis process exited {completed.returncode}",
            )
        else:
            analysis = _normalize_output(summary, stdout)
            status = (
                "empty"
                if not analysis["process_findings"]
                and not analysis["improvement_proposals"]
                else "passed"
            )
            outcome = _successful_outcome(
                run_id,
                episode_sequence,
                status,
                analysis,
            )
    except OSError as exc:
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            "unavailable",
            str(exc),
        )
    except CandidateValidationError as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        status = "interrupted" if exc.kind == "interrupted" else "invalid"
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            status,
            exc.summary,
        )
    except (json.JSONDecodeError, RetrospectiveResultError) as exc:
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            "invalid",
            str(exc),
        )

    store.write_evidence_value(run_id, f"{evidence}/input.json", json.loads(summary))
    store.write_evidence_value(run_id, f"{evidence}/command.json", command_record)
    store.write_evidence_text(run_id, f"{evidence}/stdout.log", stdout)
    store.write_evidence_text(run_id, f"{evidence}/stderr.log", stderr)
    if analysis is not None:
        store.write_evidence_value(run_id, f"{evidence}/analysis.json", analysis)
    store.write_evidence_value(run_id, f"{evidence}/outcome.json", outcome)
    store.write_evidence_value(run_id, f"{evidence}/result.json", outcome)
    store.seal_evidence(run_id, evidence)
    return outcome


def retrospective_evidence_identity(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
) -> str:
    """Return the stable local evidence identity for one terminal episode."""
    event = store.event(run_id, episode_sequence)
    state = event.get("state")
    if state == "attention_required":
        label = "attention"
    elif state == "completed":
        label = "completed"
    else:
        raise RunStoreError(
            f"sequence {episode_sequence} is not a retrospective episode"
        )
    return f"retrospective/{label}-{episode_sequence}"


def _contained_command(codex_command: Sequence[str]) -> list[str]:
    command = list(codex_command)
    if not command or not all(isinstance(part, str) and part for part in command):
        raise RunStoreError("retrospective Codex command is invalid")
    return [
        *command,
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "features.web_search=false",
        "--disable",
        "apps",
        "--disable",
        "browser_use",
        "--disable",
        "browser_use_external",
        "--disable",
        "browser_use_full_cdp_access",
        "--disable",
        "enable_mcp_apps",
        "--disable",
        "image_generation",
        "--disable",
        "multi_agent",
        "--disable",
        "multi_agent_v2",
        "--disable",
        "shell_tool",
        "--disable",
        "standalone_web_search",
        "--disable",
        "unified_exec",
        "--skip-git-repo-check",
        "--color",
        "never",
        RETROSPECTIVE_PROMPT,
    ]


def _contained_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in _ENVIRONMENT_ALLOWLIST if key in os.environ
    }
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("LC_ALL", "C.UTF-8")
    return environment


def _normalize_output(summary: str, stdout: str) -> dict[str, Any]:
    if not stdout.strip():
        raise RetrospectiveResultError("retrospective result is empty")
    return normalize_retrospective_result(summary, json.loads(stdout))


def _successful_outcome(
    run_id: str,
    episode_sequence: int,
    status: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "episode_sequence": episode_sequence,
        "status": status,
        "warning": False,
        "process_findings_count": len(analysis["process_findings"]),
        "improvement_proposals_count": len(analysis["improvement_proposals"]),
    }


def _warning_outcome(
    run_id: str,
    episode_sequence: int,
    status: str,
    summary: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "episode_sequence": episode_sequence,
        "status": status,
        "warning": True,
        "process_findings_count": 0,
        "improvement_proposals_count": 0,
        "warning_summary": redact_text(summary)[:1024],
    }


def _cached_outcome(
    value: Any,
    *,
    run_id: str,
    episode_sequence: int,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("run_id") != run_id
        or value.get("episode_sequence") != episode_sequence
        or value.get("status")
        not in {"passed", "empty", "invalid", "unavailable", "interrupted"}
    ):
        raise RunStoreError("sealed retrospective outcome is invalid")
    return value
