from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afk.implement import runtime_failure_excerpt
from afk.jsonutil import canonical_json, sha256_json
from afk.redaction import redact_artifact_value, redact_text
from afk.tracking import effective_tracker_terminal_decision, redact_retrospective
from afk.workstream_lifecycle import repair_stop_record, workstream_status_from_publication


SCHEMA_VERSION = 1
PI_JUDGE_PROMPT_PLACEHOLDER = "{prompt}"
PI_JUDGE_REQUEST_PATH_PLACEHOLDER = "{request_path}"
PI_JUDGE_RESULT_PATH_PLACEHOLDER = "{result_path}"


@dataclass(frozen=True)
class RetrospectiveContext:
    state: dict[str, Any]
    publication: dict[str, Any]
    tracker: dict[str, Any]
    normalized: dict[str, Any] | None = None


@dataclass(frozen=True)
class TerminalIntegrationRetrospectiveContext:
    workstream: dict[str, Any]
    integration: dict[str, Any]
    follow_up: dict[str, Any] | None = None
    output_dir: Path | None = None


class _RetrospectiveJudgeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: list[str],
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class _RetrospectiveFollowUpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: list[str],
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


def build_pipeline_retrospective(context: RetrospectiveContext) -> dict[str, Any]:
    return pipeline_retrospective_record(
        context.state,
        context.publication,
        context.tracker,
        context.normalized,
    )


def build_terminal_integration_retrospective(
    context: TerminalIntegrationRetrospectiveContext,
) -> dict[str, Any]:
    signals = _terminal_integration_retrospective_signals(context.integration)
    follow_up = _retrospective_follow_up_record(signals, None, {})
    record = {
        "schema_version": SCHEMA_VERSION,
        "status": redact_text(string_field(context.integration, "decision") or "unknown"),
        "health": _retrospective_health(_process_retrospective_signals(signals)),
        "integration_decision": redact_text(string_field(context.integration, "decision") or ""),
        "pr_url": redact_text(string_field(context.integration, "pr_url") or ""),
        "signals": signals,
        "recommended_follow_up": _legacy_recommended_follow_up(follow_up["recommended"]),
        "follow_up": follow_up,
    }
    if context.follow_up and context.output_dir is not None:
        output_dir = context.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        ledger = _RetrospectiveOutputLedger(
            run_id=(string_field(context.workstream, "workstream_id") or "terminal-integration") + "-integrate-pr",
            path=output_dir,
        )
        normalized = {
            "workstream_id": string_field(context.workstream, "workstream_id") or "terminal-integration",
            "parent": string_field(context.workstream, "parent") or "",
            "review_branch": string_field(context.workstream, "review_branch") or "",
            "retrospective_follow_up": context.follow_up,
        }
        creation = _run_retrospective_follow_up(
            normalized=normalized,
            state={},
            publication=context.workstream.get("publication") if isinstance(context.workstream.get("publication"), dict) else {},
            tracker=context.workstream.get("tracker") if isinstance(context.workstream.get("tracker"), dict) else {},
            pipeline_retrospective=record,
            ledger=ledger,
        )
        record = _apply_retrospective_follow_up_creation(record, creation)
    return record


@dataclass(frozen=True)
class _RetrospectiveOutputLedger:
    run_id: str
    path: Path

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        (self.path / name).write_text(canonical_json(payload) + "\n", encoding="utf-8")


def redacted_terminal_retrospective(normalized: dict[str, Any], publication: dict[str, Any]) -> dict[str, Any]:
    return redact_retrospective(effective_retrospective(normalized, publication))


def effective_retrospective(normalized: dict[str, Any], publication: dict[str, Any]) -> dict[str, Any]:
    retrospective = normalized.get("retrospective") if isinstance(normalized, dict) else {}
    if not isinstance(retrospective, dict) or not retrospective:
        return {}
    if _publisher_mode(normalized) != "close":
        return retrospective
    decision_status = effective_tracker_terminal_decision(normalized, publication).get("status")
    if decision_status not in {"merged", "no-merge"}:
        return {}
    return retrospective


def retrospective_follow_up_allowed(normalized: dict[str, Any], publication: dict[str, Any]) -> bool:
    if _publisher_mode(normalized) != "close":
        return True
    decision_status = effective_tracker_terminal_decision(normalized, publication).get("status")
    return decision_status in {"merged", "no-merge"}


def _publisher_mode(normalized: dict[str, Any]) -> str:
    publisher = normalized.get("publisher") if isinstance(normalized, dict) else {}
    if not isinstance(publisher, dict):
        publisher = {}
    return string_field(publisher, "mode") or "create"


def checkout_path_from_state(state: dict[str, Any]) -> Path:
    checkout = state.get("checkout") if isinstance(state.get("checkout"), dict) else {}
    path = checkout.get("checkout_path") or checkout.get("path")
    if isinstance(path, str) and path:
        return Path(path)
    return Path.cwd()


def latest_validation_record(state: dict[str, Any]) -> dict[str, Any] | None:
    validations = state.get("validations")
    if not isinstance(validations, list) or not validations:
        return None
    latest = validations[-1]
    return latest if isinstance(latest, dict) else None


def first_validation_failure(output: dict[str, Any]) -> dict[str, Any] | None:
    failures = output.get("actionable_failures")
    if not isinstance(failures, list):
        return None
    for failure in failures:
        if isinstance(failure, dict):
            return failure
    return None


def selected_work_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    items = state.get("selected_work")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def selected_work_external_id(selected_work: list[dict[str, Any]]) -> str:
    for item in selected_work:
        external_id = string_field(item, "external_id")
        if external_id:
            return external_id
    return ""


def selected_work_target_project_labels(selected_work: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for item in selected_work:
        item_labels = item.get("labels")
        if not isinstance(item_labels, list):
            continue
        for label in item_labels:
            if (
                isinstance(label, str)
                and label.startswith("project:")
                and label != "project:afk-composable-pipeline"
                and label not in labels
            ):
                labels.append(label)
    return labels


def inferred_target_project_labels(state: dict[str, Any]) -> list[str]:
    labels = selected_work_target_project_labels(selected_work_items(state))
    if labels:
        return labels
    labels = selected_work_target_project_labels(_persisted_selected_work_items(state))
    if labels:
        return labels
    project_slug = _persisted_run_project_slug(state)
    if project_slug and project_slug != "afk-composable-pipeline":
        return [f"project:{project_slug}"]
    return []


def _persisted_selected_work_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    for step in state.get("steps", []):
        if not isinstance(step, dict) or string_field(step, "name") != "implement":
            continue
        input_data = _persisted_step_input(step)
        work_selection = input_data.get("work_selection") if isinstance(input_data.get("work_selection"), dict) else {}
        selected_work = work_selection.get("selected_work")
        if isinstance(selected_work, list):
            return [item for item in selected_work if isinstance(item, dict)]
    return []


def _persisted_run_project_slug(state: dict[str, Any]) -> str:
    for step in state.get("steps", []):
        if not isinstance(step, dict):
            continue
        project_slug = _persisted_step_option(step, "--project")
        if project_slug:
            return project_slug
    return ""


def _persisted_step_input(step: dict[str, Any]) -> dict[str, Any]:
    raw_input = _persisted_step_option(step, "--input")
    if not raw_input:
        return {}
    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persisted_step_option(step: dict[str, Any], option: str) -> str:
    command = step.get("equivalent_command")
    if not isinstance(command, list):
        return ""
    for index, part in enumerate(command[:-1]):
        if part == option and isinstance(command[index + 1], str):
            return command[index + 1]
    return ""


def review_feedback_pipeline_follow_up(review: dict[str, Any]) -> list[dict[str, Any]]:
    reviewer_result = review.get("reviewer_result") if isinstance(review.get("reviewer_result"), dict) else {}
    findings = reviewer_result.get("findings")
    if not isinstance(findings, list):
        return []
    follow_up = []
    for finding in findings:
        if not isinstance(finding, dict) or not review_finding_is_pipeline_failure(finding):
            continue
        follow_up.append(
            {
                "classification": string_field(finding, "classification") or string_field(finding, "category") or "pipeline_failure",
                "severity": review_finding_severity(finding),
                "summary": string_field(finding, "summary") or string_field(finding, "title") or "",
            }
        )
    return follow_up


def review_finding_is_pipeline_failure(finding: dict[str, Any]) -> bool:
    classification = (string_field(finding, "classification") or string_field(finding, "category") or "").lower()
    return classification in {
        "pipeline_failure",
        "tool_failure",
        "validation_evidence_incomplete",
        "runtime_failure",
        "protocol_failure",
    }


def review_finding_severity(finding: dict[str, Any]) -> str:
    severity = string_field(finding, "severity")
    if severity:
        return severity
    status = string_field(finding, "status") or ""
    return "high" if status in {"request_revision", "fail", "failed"} else "medium"


def string_field(input_data: dict[str, Any], key: str) -> str | None:
    value = input_data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _subprocess_output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def pipeline_retrospective_record(
    state: dict[str, Any],
    publication: dict[str, Any],
    tracker: dict[str, Any],
    normalized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signals = (
        _validation_retrospective_signals(state, normalized)
        + _publication_retrospective_signals(publication)
        + _blocked_retrospective_signals(state, publication)
        + _cleanup_retrospective_signals(state)
    )
    follow_up = _retrospective_follow_up_record(signals, normalized, publication)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": workstream_status_from_publication(publication, tracker),
        "health": _retrospective_health(_process_retrospective_signals(signals)),
        "publication_status": redact_text(str(publication.get("status") or "")),
        "tracker_status": redact_text(str(tracker.get("status") or "")),
        "repair_stop": redact_artifact_value(repair_stop_record(state, publication)),
        "signals": signals,
        "recommended_follow_up": _legacy_recommended_follow_up(follow_up["recommended"]),
        "follow_up": follow_up,
        "judge": _disabled_retrospective_judge_record(),
    }


def _disabled_retrospective_judge_record() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "disabled",
    }


def _skipped_retrospective_judge_record(summary: str, *, classification: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "status": "skipped",
        "classification": classification,
        "summary": redact_text(summary),
        "findings": [],
    }


def _apply_retrospective_judge(
    pipeline_retrospective: dict[str, Any],
    judge: dict[str, Any],
    *,
    normalized: dict[str, Any] | None,
    publication: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = dict(pipeline_retrospective)
    if not judge:
        record["judge"] = _disabled_retrospective_judge_record()
        return record
    signals = list(record.get("signals", [])) if isinstance(record.get("signals"), list) else []
    judge_signals = _retrospective_judge_signals(
        judge,
        existing_signals=signals,
        publication=publication,
        state=state,
    )
    if judge_signals:
        signals.extend(judge_signals)
    record["signals"] = signals
    record["health"] = _retrospective_health(_process_retrospective_signals(signals))
    follow_up = _retrospective_follow_up_record(signals, normalized, publication)
    record["recommended_follow_up"] = _legacy_recommended_follow_up(follow_up["recommended"])
    record["follow_up"] = follow_up
    record["judge"] = judge
    return record


def _retrospective_judge_signals(
    judge: dict[str, Any],
    *,
    existing_signals: list[dict[str, Any]],
    publication: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(judge, dict) or not judge.get("enabled"):
        return []
    status = string_field(judge, "status") or ""
    if status == "skipped":
        return []
    if status == "passed":
        return []
    severity = "warning" if status == "warning" else "error"
    evidence = judge.get("evidence") if isinstance(judge.get("evidence"), dict) else {}
    return [
        {
            "kind": "retrospective-judge",
            "scope": _retrospective_judge_signal_scope(
                judge,
                existing_signals=existing_signals,
                publication=publication,
                state=state,
            ),
            "severity": severity,
            "summary": redact_text(string_field(judge, "summary") or status or "retrospective judge reported an issue"),
            "evidence_paths": _retrospective_evidence_paths(
                string_field(evidence, "request_path") or "",
                string_field(evidence, "result_path") or "",
                string_field(evidence, "stdout_path") or "",
                string_field(evidence, "stderr_path") or "",
            ),
        }
    ]


def _retrospective_judge_signal_scope(
    judge: dict[str, Any],
    *,
    existing_signals: list[dict[str, Any]],
    publication: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> str:
    if publication.get("status") != "blocked":
        return "pipeline-process"
    reason = string_field(publication, "reason") or ""
    repair_stop = repair_stop_record(state or {}, publication)
    if not (
        reason.startswith("review did not reach passed: request_revision")
        or reason.startswith("review feedback retry budget exhausted:")
        or reason.startswith("review requested changes:")
        or string_field(repair_stop, "scope") == "target-work"
    ):
        return "pipeline-process"
    review = state.get("review") if isinstance(state, dict) and isinstance(state.get("review"), dict) else {}
    if review_feedback_pipeline_follow_up(review):
        return "pipeline-process"
    if any(
        isinstance(signal, dict)
        and string_field(signal, "scope") != "target-work"
        for signal in existing_signals
    ):
        return "pipeline-process"
    if not any(
        isinstance(signal, dict)
        and string_field(signal, "kind") in {"retry-or-blocked", "repair-stop"}
        and string_field(signal, "scope") == "target-work"
        for signal in existing_signals
    ):
        return "pipeline-process"
    if string_field(judge, "classification") not in {"judge_failure", "judge_warning"}:
        return "pipeline-process"
    return "target-work"


def _run_retrospective_judge(
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    publication: dict[str, Any],
    tracker: dict[str, Any],
    selected_work: list[dict[str, Any]],
    pipeline_retrospective: dict[str, Any],
    ledger: "WorkstreamLedger",
    skip_reason: str = "",
    skip_classification: str = "",
) -> dict[str, Any]:
    judge = normalized.get("retrospective_judge") if isinstance(normalized, dict) else {}
    if not isinstance(judge, dict) or not judge.get("enabled"):
        return _disabled_retrospective_judge_record()
    if skip_reason:
        return _skipped_retrospective_judge_record(skip_reason, classification=skip_classification)
    evidence_pack = _build_retrospective_judge_evidence_pack(
        normalized=normalized,
        state=state,
        publication=publication,
        tracker=tracker,
        selected_work=selected_work,
        pipeline_retrospective=pipeline_retrospective,
    )
    request = _build_retrospective_judge_request(evidence_pack, run_id=ledger.run_id)
    request_prompt = _retrospective_judge_prompt_request(request)
    request_path = ledger.path / "retrospective-judge-request.json"
    result_path = ledger.path / "retrospective-judge-result.json"
    stdout_path = ledger.path / "retrospective-judge-stdout.log"
    stderr_path = ledger.path / "retrospective-judge-stderr.log"
    ledger.write_json("retrospective-judge-evidence.json", evidence_pack)
    ledger.write_json("retrospective-judge-request.json", request)
    checkout_path = checkout_path_from_state(state)
    try:
        adapter_result, raw_payload, raw_payload_source = _run_retrospective_judge_command(
            judge,
            checkout_path=checkout_path,
            request=request,
            request_prompt=request_prompt,
            request_path=request_path,
            result_path=result_path,
        )
        raw = _read_retrospective_judge_payload(raw_payload, source=raw_payload_source)
        if raw["status"] != "valid":
            normalized_result = _normalized_retrospective_judge_result(
                enabled=True,
                status="failed_protocol",
                classification="protocol_failure",
                summary=raw["message"],
                findings=[],
                adapter=_retrospective_judge_adapter_record(judge, adapter_result["returncode"], False),
                stdout=adapter_result["stdout"],
                stderr=adapter_result["stderr"],
                request_path=request_path,
                result_path=result_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            _write_retrospective_judge_result(result_path, ledger.run_id, normalized_result)
            return normalized_result
        normalized_result = _normalize_retrospective_judge_payload(
            raw["payload"],
            adapter=_retrospective_judge_adapter_record(judge, adapter_result["returncode"], False),
            stdout=adapter_result["stdout"],
            stderr=adapter_result["stderr"],
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        _write_retrospective_judge_result(result_path, ledger.run_id, normalized_result)
        return normalized_result
    except _RetrospectiveJudgeError as exc:
        normalized_result = _normalized_retrospective_judge_result(
            enabled=True,
            status="failed",
            classification="judge_adapter_failure",
            summary=exc.message,
            findings=[],
            adapter=_retrospective_judge_adapter_record(judge, exc.returncode, exc.timed_out),
            stdout=exc.stdout,
            stderr=exc.stderr,
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        _write_retrospective_judge_result(result_path, ledger.run_id, normalized_result)
        return normalized_result


def _build_retrospective_judge_evidence_pack(
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    publication: dict[str, Any],
    tracker: dict[str, Any],
    selected_work: list[dict[str, Any]],
    pipeline_retrospective: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workstream": {
            "workstream_id": normalized["workstream_id"],
            "parent": normalized["parent"],
            "review_branch": normalized["review_branch"],
            "status": workstream_status_from_publication(publication, tracker),
        },
        "selected_work": _retrospective_judge_prompt_selected_work(selected_work),
        "retrospective": redact_retrospective(effective_retrospective(normalized, publication)),
        "publication": {
            "status": redact_text(str(publication.get("status") or "")),
            "reason": redact_text(str(publication.get("reason") or "")),
            "url": redact_text(str(publication.get("url") or "")),
        },
        "tracker": {
            "status": redact_text(str(tracker.get("status") or "")),
            "comment": redact_text(str(tracker.get("comment") or "")),
            "close_source_item": bool(tracker.get("close_source_item")),
            "close_reason": redact_text(str(tracker.get("close_reason") or "")),
            "pr_url": redact_text(str(tracker.get("pr_url") or "")),
            "merge_commit": redact_text(str(tracker.get("merge_commit") or "")),
            "review_findings": redact_artifact_value(tracker.get("review_findings", [])),
        },
        "cleanup": redact_artifact_value(state.get("cleanup", {})),
        "pipeline_retrospective": pipeline_retrospective,
        "redaction": {
            "applied": True,
            "artifact_values": "redact_artifact_value",
            "text": "redact_text",
            "secret_placeholder": "[REDACTED]",
            "raw_logs_included": False,
        },
    }


def _build_retrospective_judge_request(evidence_pack: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "step": "retrospective-judge",
        "artifact_type": "retrospective-judge-request",
        "evidence_pack": evidence_pack,
        "expected_result_schema": {
            "status": "pass|warn|fail",
            "summary": "string",
            "findings": "list[object]",
        },
    }


def _run_retrospective_judge_command(
    judge: dict[str, Any],
    *,
    checkout_path: Path,
    request: dict[str, Any],
    request_prompt: dict[str, Any],
    request_path: Path,
    result_path: Path,
) -> tuple[dict[str, Any], str | None, str]:
    with tempfile.TemporaryDirectory(prefix="afk-retrospective-judge-") as temp_dir:
        temp_path = Path(temp_dir)
        env = _minimal_retrospective_judge_environment(temp_path, config_home=judge.get("config_home") or "")
        env.update(judge.get("env") or {})
        if judge.get("codex_home"):
            env["CODEX_HOME"] = judge["codex_home"]
        env["AFK_RETROSPECTIVE_JUDGE_REQUEST"] = str(request_path)
        env["AFK_RETROSPECTIVE_JUDGE_RESULT"] = str(result_path)
        command = _render_retrospective_judge_command(
            judge["command"],
            judge_prompt=canonical_json(request_prompt),
            request_path=request_path,
            result_path=result_path,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=checkout_path,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=judge["timeout_seconds"],
            )
        except OSError as exc:
            raise _RetrospectiveJudgeError(
                str(exc),
                command=command,
                returncode=None,
                stderr=str(exc),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            raise _RetrospectiveJudgeError(
                "retrospective judge command timed out",
                command=command,
                returncode=None,
                stdout=stdout,
                stderr=stderr or "retrospective judge command timed out",
                timed_out=True,
            ) from exc
        raw_payload = result_path.read_text(encoding="utf-8", errors="replace") if result_path.exists() else None
        raw_payload_source = "file" if raw_payload is not None else "stdout"
    if completed.returncode != 0:
        raise _RetrospectiveJudgeError(
            "retrospective judge command failed",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    if raw_payload is None and completed.stdout.strip():
        raw_payload = completed.stdout
        raw_payload_source = "stdout"
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }, raw_payload, raw_payload_source


def _render_retrospective_judge_command(
    command: list[str],
    *,
    judge_prompt: str,
    request_path: Path,
    result_path: Path,
) -> list[str]:
    rendered = _render_retrospective_command(
        command,
        {
            PI_JUDGE_REQUEST_PATH_PLACEHOLDER: str(request_path),
            PI_JUDGE_RESULT_PATH_PLACEHOLDER: str(result_path),
        },
    )
    return [
        judge_prompt if part == PI_JUDGE_PROMPT_PLACEHOLDER else part for part in rendered
    ]


def _render_retrospective_follow_up_command(
    command: list[str],
    *,
    request_path: Path,
    result_path: Path,
) -> list[str]:
    return _render_retrospective_command(
        command,
        {
            PI_JUDGE_REQUEST_PATH_PLACEHOLDER: str(request_path),
            PI_JUDGE_RESULT_PATH_PLACEHOLDER: str(result_path),
        },
    )


def _render_retrospective_command(
    command: list[str],
    replacements: dict[str, str],
) -> list[str]:
    if not replacements:
        return list(command)
    pattern = re.compile(
        "|".join(sorted((re.escape(token) for token in replacements), key=len, reverse=True))
    )

    def replace_placeholder(match: re.Match[str]) -> str:
        return replacements[match.group(0)]

    rendered: list[str] = []
    for part in command:
        rendered.append(pattern.sub(replace_placeholder, part))
    return rendered


def _retrospective_judge_prompt_request(request: dict[str, Any]) -> dict[str, Any]:
    evidence_pack = request.get("evidence_pack")
    if not isinstance(evidence_pack, dict):
        return request
    selected_work = evidence_pack.get("selected_work")
    if not isinstance(selected_work, list):
        return request
    sanitized = dict(request)
    sanitized_evidence_pack = dict(evidence_pack)
    sanitized_evidence_pack["selected_work"] = _retrospective_judge_prompt_selected_work(selected_work)
    sanitized["evidence_pack"] = sanitized_evidence_pack
    return sanitized


def _retrospective_judge_prompt_selected_work(selected_work: list[Any]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for item in selected_work:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "external_id": str(item.get("external_id") or ""),
                "source_id": str(item.get("source_id") or ""),
                "source_type": str(item.get("source_type") or ""),
            }
        )
    return records


def _minimal_retrospective_judge_environment(temp_path: Path, *, config_home: str = "") -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    home_path = temp_path / "home"
    home_path.mkdir()
    env["HOME"] = str(home_path)
    if config_home:
        env["XDG_CONFIG_HOME"] = config_home
    else:
        xdg_config_home = temp_path / "xdg-config"
        xdg_config_home.mkdir()
        env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    return env


def _read_retrospective_judge_payload(raw: str | None, *, source: str) -> dict[str, Any]:
    if raw is None:
        return {"status": "missing", "message": "retrospective judge result payload was not produced"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "invalid", "message": f"retrospective judge result {source} is not valid JSON"}
    if not isinstance(payload, dict):
        return {"status": "invalid", "message": f"retrospective judge result {source} must contain an object"}
    return {"status": "valid", "payload": redact_artifact_value(payload)}


def _normalize_retrospective_judge_payload(
    payload: dict[str, Any],
    *,
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
    request_path: Path,
    result_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    raw_status = string_field(payload, "status") or ""
    if raw_status in {"pass", "passed", "success", "succeeded"}:
        status = "passed"
        classification = "success"
    elif raw_status in {"warn", "warning"}:
        status = "warning"
        classification = "judge_warning"
    elif raw_status in {"fail", "failed"}:
        status = "failed"
        classification = "judge_failure"
    else:
        status = "failed_protocol"
        classification = "protocol_failure"
    return _normalized_retrospective_judge_result(
        enabled=True,
        status=status,
        classification=classification,
        summary=string_field(payload, "summary") or status,
        findings=payload.get("findings") if isinstance(payload.get("findings"), list) else [],
        adapter=adapter,
        stdout=stdout,
        stderr=stderr,
        request_path=request_path,
        result_path=result_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _normalized_retrospective_judge_result(
    *,
    enabled: bool,
    status: str,
    classification: str,
    summary: str,
    findings: list[Any],
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
    request_path: Path,
    result_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    stdout_path.write_text(redact_text(stdout), encoding="utf-8")
    stderr_path.write_text(redact_text(stderr), encoding="utf-8")
    return {
        "enabled": enabled,
        "status": status,
        "classification": classification,
        "summary": redact_text(summary),
        "findings": redact_artifact_value(findings),
        "adapter": adapter,
        "evidence": {
            "request_path": request_path.name,
            "result_path": result_path.name,
            "stdout_path": stdout_path.name,
            "stderr_path": stderr_path.name,
        },
    }


def _retrospective_judge_adapter_record(
    judge: dict[str, Any],
    returncode: int | None,
    timed_out: bool,
) -> dict[str, Any]:
    return {
        "type": judge["type"],
        "command": redact_artifact_value({"command": judge["command"]})["command"],
        "returncode": returncode,
        "timed_out": timed_out,
    }


def _write_retrospective_judge_result(path: Path, run_id: str, judge_result: dict[str, Any]) -> None:
    path.write_text(
        canonical_json(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "step": "retrospective-judge",
                "artifact_type": "retrospective-judge-result",
                "result": redact_artifact_value(judge_result),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _run_retrospective_follow_up(
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    publication: dict[str, Any],
    tracker: dict[str, Any],
    pipeline_retrospective: dict[str, Any],
    ledger: "WorkstreamLedger",
) -> dict[str, Any]:
    follow_up_config = normalized.get("retrospective_follow_up") if isinstance(normalized, dict) else {}
    if not isinstance(follow_up_config, dict) or not follow_up_config.get("enabled"):
        return _disabled_retrospective_follow_up_creation_record()
    if not retrospective_follow_up_allowed(normalized, publication):
        return _disabled_retrospective_follow_up_creation_record()
    follow_up = (
        pipeline_retrospective.get("follow_up") if isinstance(pipeline_retrospective.get("follow_up"), dict) else {}
    )
    recommended = follow_up.get("recommended") if isinstance(follow_up.get("recommended"), list) else []
    existing_created = follow_up.get("created") if isinstance(follow_up.get("created"), list) else []
    request_path = ledger.path / "retrospective-follow-up-request.json"
    result_path = ledger.path / "retrospective-follow-up-result.json"
    stdout_path = ledger.path / "retrospective-follow-up-stdout.log"
    stderr_path = ledger.path / "retrospective-follow-up-stderr.log"
    request = _build_retrospective_follow_up_request(
        normalized=normalized,
        publication=publication,
        tracker=tracker,
        pipeline_retrospective=pipeline_retrospective,
        recommended=recommended,
        created=existing_created,
        run_id=ledger.run_id,
    )
    ledger.write_json("retrospective-follow-up-request.json", request)
    if not recommended:
        normalized_result = _normalized_retrospective_follow_up_result(
            enabled=True,
            status="skipped",
            classification="no_recommendations",
            summary="No retrospective follow-up recommendations required.",
            created=[],
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        normalized_result.update(_retrospective_follow_up_runtime_record(follow_up_config, None, False))
        _write_retrospective_follow_up_result(result_path, ledger.run_id, normalized_result)
        return normalized_result
    if follow_up_config.get("creator") == "beads":
        normalized_result = _run_retrospective_follow_up_beads_creator(
            follow_up_config=follow_up_config,
            normalized=normalized,
            pipeline_retrospective=pipeline_retrospective,
            recommended=recommended,
            existing_created=existing_created,
            ledger=ledger,
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        _write_retrospective_follow_up_result(result_path, ledger.run_id, normalized_result)
        return normalized_result
    checkout_path = checkout_path_from_state(state)
    try:
        adapter_result, raw_payload = _run_retrospective_follow_up_command(
            follow_up_config,
            checkout_path=checkout_path,
            request_path=request_path,
            result_path=result_path,
        )
        raw = _read_retrospective_follow_up_payload(raw_payload)
        if raw["status"] != "valid":
            normalized_result = _normalized_retrospective_follow_up_result(
                enabled=True,
                status="failed_protocol",
                classification="protocol_failure",
                summary=raw["message"],
                created=[],
                adapter=_retrospective_follow_up_adapter_record(
                    follow_up_config,
                    adapter_result["returncode"],
                    False,
                ),
                stdout=adapter_result["stdout"],
                stderr=adapter_result["stderr"],
                request_path=request_path,
                result_path=result_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            _write_retrospective_follow_up_result(result_path, ledger.run_id, normalized_result)
            return normalized_result
        normalized_result = _normalize_retrospective_follow_up_payload(
            raw["payload"],
            recommended=recommended,
            adapter=_retrospective_follow_up_adapter_record(
                follow_up_config,
                adapter_result["returncode"],
                False,
            ),
            stdout=adapter_result["stdout"],
            stderr=adapter_result["stderr"],
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        _write_retrospective_follow_up_result(result_path, ledger.run_id, normalized_result)
        return normalized_result
    except _RetrospectiveFollowUpError as exc:
        normalized_result = _normalized_retrospective_follow_up_result(
            enabled=True,
            status="failed",
            classification="creation_adapter_failure",
            summary=exc.message,
            created=[],
            adapter=_retrospective_follow_up_adapter_record(follow_up_config, exc.returncode, exc.timed_out),
            stdout=exc.stdout,
            stderr=exc.stderr,
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        _write_retrospective_follow_up_result(result_path, ledger.run_id, normalized_result)
        return normalized_result


def _apply_retrospective_follow_up_creation(
    pipeline_retrospective: dict[str, Any],
    creation: dict[str, Any],
) -> dict[str, Any]:
    record = dict(pipeline_retrospective)
    follow_up = dict(record.get("follow_up")) if isinstance(record.get("follow_up"), dict) else {}
    recommended = follow_up.get("recommended") if isinstance(follow_up.get("recommended"), list) else []
    existing_created = follow_up.get("created") if isinstance(follow_up.get("created"), list) else []
    created = creation.get("created") if isinstance(creation.get("created"), list) else []
    follow_up["created"] = _merge_retrospective_created_follow_up(existing_created, created)
    follow_up["recommended"] = _retrospective_uncreated_recommendations(recommended, follow_up["created"])
    follow_up["creation"] = _retrospective_follow_up_creation_public_record(creation)
    record["follow_up"] = follow_up
    record["recommended_follow_up"] = _legacy_recommended_follow_up(follow_up["recommended"])
    return record


def _build_retrospective_follow_up_request(
    *,
    normalized: dict[str, Any],
    publication: dict[str, Any],
    tracker: dict[str, Any],
    pipeline_retrospective: dict[str, Any],
    recommended: list[dict[str, Any]],
    created: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "step": "retrospective-follow-up",
        "artifact_type": "retrospective-follow-up-request",
        "workstream": {
            "workstream_id": normalized["workstream_id"],
            "parent": normalized["parent"],
            "review_branch": normalized["review_branch"],
            "status": workstream_status_from_publication(publication, tracker),
        },
        "retrospective": redact_retrospective(effective_retrospective(normalized, publication)),
        "publication": {
            "status": redact_text(str(publication.get("status") or "")),
            "reason": redact_text(str(publication.get("reason") or "")),
            "url": redact_text(str(publication.get("url") or "")),
        },
        "tracker": {
            "status": redact_text(str(tracker.get("status") or "")),
            "comment": redact_text(str(tracker.get("comment") or "")),
            "close_source_item": bool(tracker.get("close_source_item")),
            "close_reason": redact_text(str(tracker.get("close_reason") or "")),
            "pr_url": redact_text(str(tracker.get("pr_url") or "")),
            "merge_commit": redact_text(str(tracker.get("merge_commit") or "")),
        },
        "pipeline_retrospective": pipeline_retrospective,
        "follow_up": {
            "recommended": redact_artifact_value(recommended),
            "created": redact_artifact_value(created),
        },
        "expected_result_schema": {
            "status": "created|recorded|skipped|failed",
            "summary": "string",
            "created": "list[object]",
        },
    }


def _run_retrospective_follow_up_beads_creator(
    *,
    follow_up_config: dict[str, Any],
    normalized: dict[str, Any],
    pipeline_retrospective: dict[str, Any],
    recommended: list[dict[str, Any]],
    existing_created: list[dict[str, Any]],
    ledger: "WorkstreamLedger",
    request_path: Path,
    result_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    workspace = Path(follow_up_config["beads_workspace"])
    password = _read_retrospective_follow_up_beads_password(workspace / "secrets" / "dolt_beads_password.txt")
    env = _retrospective_follow_up_beads_environment(password)
    exact_secrets = {password}
    created_items: list[dict[str, Any]] = []
    try:
        existing_by_fingerprint, list_stdout = _load_existing_retrospective_follow_up_beads(
            workspace=workspace,
            env=env,
            exact_secrets=exact_secrets,
            recommended=recommended,
        )
        created_count = 0
        duplicate_count = 0
        for recommendation in recommended:
            if not isinstance(recommendation, dict):
                continue
            fingerprint = string_field(recommendation, "fingerprint") or ""
            if not fingerprint:
                continue
            if fingerprint in existing_by_fingerprint:
                existing_item = existing_by_fingerprint[fingerprint]
                created_items.append(
                    _retrospective_follow_up_existing_bead_item(existing_item, recommendation)
                )
                duplicate_count += 1
                continue
            if fingerprint in {
                string_field(item, "fingerprint") or ""
                for item in existing_created
                if isinstance(item, dict)
            }:
                created_item = _retrospective_follow_up_existing_created_item(
                    fingerprint=fingerprint,
                    recommendation=recommendation,
                    existing_created=existing_created,
                )
                if created_item is not None:
                    created_items.append(created_item)
                duplicate_count += 1
                continue
            bead_id, create_stdout = _create_retrospective_follow_up_bead(
                workspace=workspace,
                env=env,
                exact_secrets=exact_secrets,
                follow_up_config=follow_up_config,
                normalized=normalized,
                pipeline_retrospective=pipeline_retrospective,
                recommendation=recommendation,
                ledger=ledger,
                request_path=request_path,
                result_path=result_path,
            )
            list_stdout += create_stdout
            created_item = _retrospective_created_follow_up_item(
                {
                    "id": bead_id,
                    "summary": string_field(recommendation, "summary") or "",
                    "labels": recommendation.get("labels"),
                    "fingerprint": fingerprint,
                },
                kind=string_field(recommendation, "kind") or "beads-created",
            )
            if created_item is not None:
                created_item["fingerprint"] = fingerprint
                created_items.append(created_item)
            created_count += 1
        status = "created" if created_count else "recorded"
        classification = "success_created" if created_count else "success_recorded"
        summary = _retrospective_follow_up_beads_summary(created_count=created_count, duplicate_count=duplicate_count)
        return _normalized_retrospective_follow_up_result(
            enabled=True,
            status=status,
            classification=classification,
            summary=summary,
            created=_merge_retrospective_created_follow_up([], created_items),
            stdout=list_stdout,
            stderr="",
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        ) | {
            "creator": {
                "type": "beads",
                "workspace": str(workspace),
                "labels": redact_artifact_value(follow_up_config.get("labels", [])),
                "dedupe": "fingerprint",
            }
        }
    except _RetrospectiveFollowUpError as exc:
        return _normalized_retrospective_follow_up_result(
            enabled=True,
            status="failed",
            classification="beads_creation_failure",
            summary=exc.message,
            created=_merge_retrospective_created_follow_up([], created_items),
            stdout=exc.stdout,
            stderr=exc.stderr,
            request_path=request_path,
            result_path=result_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        ) | {
            "creator": {
                "type": "beads",
                "workspace": str(workspace),
                "labels": redact_artifact_value(follow_up_config.get("labels", [])),
                "dedupe": "fingerprint",
            }
        }


def _read_retrospective_follow_up_beads_password(credentials_path: Path) -> str:
    try:
        lines = credentials_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise _RetrospectiveFollowUpError(
            "Beads credentials are not available for retrospective follow-up creation",
            command=["bd", "create"],
            returncode=None,
            stderr=str(exc),
        ) from exc
    if not lines or not lines[0]:
        raise _RetrospectiveFollowUpError(
            "Beads credentials are not available for retrospective follow-up creation",
            command=["bd", "create"],
            returncode=None,
        )
    return lines[0]


def _retrospective_follow_up_beads_environment(password: str) -> dict[str, str]:
    env = {key: value for key in ("PATH", "LANG", "LC_ALL") if (value := os.environ.get(key)) is not None}
    env["BEADS_DOLT_PASSWORD"] = password
    return env


def _load_existing_retrospective_follow_up_beads(
    *,
    workspace: Path,
    env: dict[str, str],
    exact_secrets: set[str],
    recommended: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str]:
    command = ["bd", "list", "--json", "--no-pager", "--limit", "0"]
    project_label = _retrospective_follow_up_project_label(recommended)
    if project_label:
        command.extend(["--label", project_label])
    completed = _run_retrospective_follow_up_beads_command(
        command,
        workspace=workspace,
        env=env,
        exact_secrets=exact_secrets,
        message_on_failure="bd list failed",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise _RetrospectiveFollowUpError(
            "bd list returned invalid JSON payload",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        ) from exc
    if not isinstance(payload, list):
        raise _RetrospectiveFollowUpError(
            "bd list must return a JSON list payload",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    existing_by_fingerprint: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        fingerprint = string_field(metadata, "afk.retrospective_follow_up.fingerprint") or ""
        if fingerprint and fingerprint not in existing_by_fingerprint:
            existing_by_fingerprint[fingerprint] = item
    return existing_by_fingerprint, _retrospective_follow_up_beads_list_audit(
        payload,
        project_label=project_label,
        exact_secrets=exact_secrets,
    )


def _retrospective_follow_up_existing_bead_item(
    existing_bead: dict[str, Any],
    recommendation: dict[str, Any],
) -> dict[str, Any]:
    normalized = _retrospective_created_follow_up_item(
        {
            "id": string_field(existing_bead, "id") or "",
            "summary": string_field(existing_bead, "title") or string_field(recommendation, "summary") or "",
            "labels": existing_bead.get("labels") or recommendation.get("labels"),
        },
        kind=string_field(recommendation, "kind") or "beads-existing",
    ) or {
        "kind": string_field(recommendation, "kind") or "beads-existing",
        "summary": string_field(recommendation, "summary") or "",
        "labels": _retrospective_follow_up_labels(recommendation.get("labels")),
    }
    normalized["fingerprint"] = string_field(recommendation, "fingerprint") or ""
    return normalized


def _retrospective_follow_up_existing_created_item(
    *,
    fingerprint: str,
    recommendation: dict[str, Any],
    existing_created: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for item in existing_created:
        if not isinstance(item, dict) or string_field(item, "fingerprint") != fingerprint:
            continue
        normalized = _retrospective_created_follow_up_item(
            item,
            kind=string_field(item, "kind") or string_field(recommendation, "kind") or "beads-existing",
        )
        if normalized is None:
            normalized = _retrospective_created_follow_up_item(
                {
                    "summary": string_field(recommendation, "summary") or "",
                    "labels": recommendation.get("labels"),
                },
                kind=string_field(recommendation, "kind") or "beads-existing",
            )
        if normalized is not None:
            normalized["fingerprint"] = fingerprint
        return normalized
    return None


def _create_retrospective_follow_up_bead(
    *,
    workspace: Path,
    env: dict[str, str],
    exact_secrets: set[str],
    follow_up_config: dict[str, Any],
    normalized: dict[str, Any],
    pipeline_retrospective: dict[str, Any],
    recommendation: dict[str, Any],
    ledger: "WorkstreamLedger",
    request_path: Path,
    result_path: Path,
) -> tuple[str, str]:
    fingerprint = string_field(recommendation, "fingerprint") or ""
    metadata = _retrospective_follow_up_bead_metadata(
        normalized=normalized,
        pipeline_retrospective=pipeline_retrospective,
        recommendation=recommendation,
        fingerprint=fingerprint,
    )
    command = [
        "bd",
        "create",
        "--silent",
        "--title",
        string_field(recommendation, "summary") or "AFK retrospective follow-up",
        "--description",
        _retrospective_follow_up_bead_description(
            normalized=normalized,
            pipeline_retrospective=pipeline_retrospective,
            recommendation=recommendation,
            ledger=ledger,
            request_path=request_path,
            result_path=result_path,
        ),
        "--labels",
        ",".join(_retrospective_follow_up_bead_labels(recommendation, follow_up_config)),
        "--metadata",
        canonical_json(metadata),
    ]
    completed = _run_retrospective_follow_up_beads_command(
        command,
        workspace=workspace,
        env=env,
        exact_secrets=exact_secrets,
        message_on_failure="bd create failed",
    )
    bead_id = completed.stdout.strip()
    if not bead_id:
        raise _RetrospectiveFollowUpError(
            "bd create did not return a Bead ID",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return bead_id, _retrospective_follow_up_beads_create_audit(
        bead_id=bead_id,
        recommendation=recommendation,
        exact_secrets=exact_secrets,
    )


def _retrospective_follow_up_beads_list_audit(
    payload: list[Any],
    *,
    project_label: str,
    exact_secrets: set[str],
) -> str:
    lines = [f"Scanned {len(payload)} Beads for retrospective follow-up dedupe."]
    if project_label:
        lines.append(f"Label filter: {redact_text(project_label, exact_secrets=exact_secrets)}")
    matched_items = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        fingerprint = string_field(metadata, "afk.retrospective_follow_up.fingerprint") or ""
        if not fingerprint:
            continue
        matched_items.append(
            {
                "id": string_field(item, "id") or "",
                "fingerprint": fingerprint,
                "category": string_field(metadata, "afk.retrospective_follow_up.category") or "",
                "severity": string_field(metadata, "afk.retrospective_follow_up.severity") or "",
            }
        )
    lines.append(f"Matched {len(matched_items)} existing retrospective follow-up Beads by fingerprint.")
    for item in matched_items:
        parts = []
        if item["id"]:
            parts.append(f"id={redact_text(item['id'], exact_secrets=exact_secrets)}")
        parts.append(f"fingerprint={redact_text(item['fingerprint'], exact_secrets=exact_secrets)}")
        if item["category"]:
            parts.append(f"category={redact_text(item['category'], exact_secrets=exact_secrets)}")
        if item["severity"]:
            parts.append(f"severity={redact_text(item['severity'], exact_secrets=exact_secrets)}")
        lines.append("- " + " ".join(parts))
    return "\n".join(lines) + "\n"


def _retrospective_follow_up_beads_create_audit(
    *,
    bead_id: str,
    recommendation: dict[str, Any],
    exact_secrets: set[str],
) -> str:
    parts = [f"Created retrospective follow-up Bead {redact_text(bead_id, exact_secrets=exact_secrets)}."]
    fingerprint = string_field(recommendation, "fingerprint") or ""
    if fingerprint:
        parts.append(f"fingerprint={redact_text(fingerprint, exact_secrets=exact_secrets)}")
    summary = string_field(recommendation, "summary") or ""
    if summary:
        parts.append(f"summary={redact_text(summary, exact_secrets=exact_secrets)}")
    return " ".join(parts) + "\n"


def _retrospective_follow_up_bead_labels(
    recommendation: dict[str, Any],
    follow_up_config: dict[str, Any],
) -> list[str]:
    labels = _retrospective_follow_up_labels(recommendation.get("labels"))
    configured_labels: list[str] = []
    if isinstance(follow_up_config.get("labels"), list):
        for label in follow_up_config["labels"]:
            if isinstance(label, str) and label and label not in configured_labels:
                configured_labels.append(label)
    configured_project_labels = [label for label in configured_labels if label.startswith("project:")]
    if configured_project_labels:
        labels = [label for label in labels if not label.startswith("project:")]
    elif not any(label.startswith("project:") for label in labels):
        labels.append(_retrospective_follow_up_project_label([recommendation]))
    for label in configured_labels:
        if label not in labels:
            labels.append(label)
    return labels


def _retrospective_follow_up_bead_metadata(
    *,
    normalized: dict[str, Any],
    pipeline_retrospective: dict[str, Any],
    recommendation: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    signal_details = _retrospective_follow_up_signal_details(pipeline_retrospective, recommendation)
    metadata: dict[str, Any] = {
        "afk.retrospective_follow_up.fingerprint": fingerprint,
        "afk.retrospective_follow_up.category": signal_details["category"],
        "afk.retrospective_follow_up.severity": signal_details["severity"],
        "afk.retrospective_follow_up.workstream_id": normalized["workstream_id"],
        "afk.retrospective_follow_up.parent": normalized["parent"],
    }
    if signal_details["evidence_paths"]:
        metadata["afk.retrospective_follow_up.evidence_paths"] = signal_details["evidence_paths"]
    return metadata


def _retrospective_follow_up_bead_description(
    *,
    normalized: dict[str, Any],
    pipeline_retrospective: dict[str, Any],
    recommendation: dict[str, Any],
    ledger: "WorkstreamLedger",
    request_path: Path,
    result_path: Path,
) -> str:
    signal_details = _retrospective_follow_up_signal_details(pipeline_retrospective, recommendation)
    evidence_paths = [
        str(_retrospective_follow_up_resolve_evidence_path(ledger, path))
        for path in signal_details["evidence_paths"]
    ]
    evidence_paths.extend(
        [
            str(request_path),
            str(result_path),
        ]
    )
    lines = [
        "AFK retrospective follow-up recommendation.",
        "",
        f"Workstream: {normalized['workstream_id']}",
        f"Parent: {normalized['parent']}",
        f"Category: {signal_details['category']}",
        f"Severity: {signal_details['severity']}",
        f"Fingerprint: {string_field(recommendation, 'fingerprint') or ''}",
    ]
    if signal_details["step"]:
        lines.append(f"Step: {signal_details['step']}")
    if signal_details["classification"]:
        lines.append(f"Classification: {signal_details['classification']}")
    if signal_details["excerpt"]:
        lines.extend(["", "Root failure excerpt:", signal_details["excerpt"]])
    if evidence_paths:
        lines.extend(["", "Evidence paths:"])
        for path in evidence_paths:
            if path and path not in lines:
                lines.append(f"- {path}")
    return "\n".join(lines)


def _retrospective_follow_up_signal_details(
    pipeline_retrospective: dict[str, Any],
    recommendation: dict[str, Any],
) -> dict[str, Any]:
    kind = string_field(recommendation, "kind") or "retrospective-follow-up"
    signals = pipeline_retrospective.get("signals") if isinstance(pipeline_retrospective.get("signals"), list) else []
    severity = ""
    evidence_paths: list[str] = []
    step = ""
    classification = ""
    excerpt = ""
    for signal in signals:
        if not isinstance(signal, dict) or string_field(signal, "kind") != kind:
            continue
        if not severity:
            severity = string_field(signal, "severity") or ""
        if not step:
            step = string_field(signal, "step") or ""
        if not classification:
            classification = string_field(signal, "classification") or ""
        if not excerpt:
            excerpt = string_field(signal, "excerpt") or string_field(signal, "summary") or ""
        for path in signal.get("evidence_paths") if isinstance(signal.get("evidence_paths"), list) else []:
            if isinstance(path, str) and path and path not in evidence_paths:
                evidence_paths.append(path)
    return {
        "category": kind,
        "severity": severity or "unknown",
        "step": step,
        "classification": classification,
        "excerpt": excerpt,
        "evidence_paths": evidence_paths,
    }


def _retrospective_follow_up_resolve_evidence_path(ledger: "WorkstreamLedger", path: str) -> Path:
    evidence_path = Path(path)
    if evidence_path.is_absolute():
        return evidence_path
    return ledger.path / path


def _retrospective_follow_up_project_label(recommended: list[dict[str, Any]]) -> str:
    for recommendation in recommended:
        if not isinstance(recommendation, dict):
            continue
        for label in _retrospective_follow_up_labels(recommendation.get("labels")):
            if label.startswith("project:"):
                return label
    return "project:afk-composable-pipeline"


def _retrospective_follow_up_beads_summary(*, created_count: int, duplicate_count: int) -> str:
    if created_count and duplicate_count:
        return f"Created {created_count} retrospective follow-up Beads and suppressed {duplicate_count} duplicates."
    if created_count:
        return f"Created {created_count} retrospective follow-up Beads."
    if duplicate_count:
        return f"Suppressed {duplicate_count} duplicate retrospective follow-up recommendations."
    return "No retrospective follow-up Beads were created."


def _run_retrospective_follow_up_beads_command(
    command: list[str],
    *,
    workspace: Path,
    env: dict[str, str],
    exact_secrets: set[str],
    message_on_failure: str,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise _RetrospectiveFollowUpError(
            str(exc),
            command=command,
            returncode=None,
            stderr=redact_text(str(exc), exact_secrets=exact_secrets),
        ) from exc
    if completed.returncode != 0:
        raise _RetrospectiveFollowUpError(
            message_on_failure,
            command=command,
            returncode=completed.returncode,
            stdout=redact_text(completed.stdout, exact_secrets=exact_secrets),
            stderr=redact_text(completed.stderr, exact_secrets=exact_secrets),
        )
    return completed


def _run_retrospective_follow_up_command(
    follow_up_config: dict[str, Any],
    *,
    checkout_path: Path,
    request_path: Path,
    result_path: Path,
) -> tuple[dict[str, Any], str | None]:
    with tempfile.TemporaryDirectory(prefix="afk-retrospective-follow-up-") as temp_dir:
        temp_path = Path(temp_dir)
        env = _minimal_retrospective_judge_environment(temp_path)
        env["AFK_RETROSPECTIVE_FOLLOW_UP_REQUEST"] = str(request_path)
        env["AFK_RETROSPECTIVE_FOLLOW_UP_RESULT"] = str(result_path)
        command = _render_retrospective_follow_up_command(
            follow_up_config["command"],
            request_path=request_path,
            result_path=result_path,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=checkout_path,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=follow_up_config["timeout_seconds"],
            )
        except OSError as exc:
            raise _RetrospectiveFollowUpError(
                str(exc),
                command=command,
                returncode=None,
                stderr=str(exc),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = _subprocess_output_text(exc.stdout)
            stderr = _subprocess_output_text(exc.stderr)
            raise _RetrospectiveFollowUpError(
                "retrospective follow-up command timed out",
                command=command,
                returncode=None,
                stdout=stdout,
                stderr=stderr or "retrospective follow-up command timed out",
                timed_out=True,
            ) from exc
        raw_payload = result_path.read_text(encoding="utf-8", errors="replace") if result_path.exists() else None
    if completed.returncode != 0:
        raise _RetrospectiveFollowUpError(
            "retrospective follow-up command failed",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }, raw_payload


def _read_retrospective_follow_up_payload(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {"status": "missing", "message": "retrospective follow-up result file was not produced"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "invalid", "message": "retrospective follow-up result file is not valid JSON"}
    if not isinstance(payload, dict):
        return {"status": "invalid", "message": "retrospective follow-up result file must contain an object"}
    return {"status": "valid", "payload": redact_artifact_value(payload)}


def _normalize_retrospective_follow_up_payload(
    payload: dict[str, Any],
    *,
    recommended: list[dict[str, Any]],
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
    request_path: Path,
    result_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    raw_status = string_field(payload, "status") or ""
    if raw_status in {"create", "created"}:
        status = "created"
        classification = "success_created"
    elif raw_status in {"record", "recorded"}:
        status = "recorded"
        classification = "success_recorded"
    elif raw_status in {"skip", "skipped", "noop"}:
        status = "skipped"
        classification = "skipped"
    elif raw_status in {"fail", "failed"}:
        status = "failed"
        classification = "creation_failure"
    else:
        status = "failed_protocol"
        classification = "protocol_failure"
    return _normalized_retrospective_follow_up_result(
        enabled=True,
        status=status,
        classification=classification,
        summary=string_field(payload, "summary") or status,
        created=_normalize_retrospective_follow_up_created(
            payload.get("created"),
            recommended=recommended,
        ),
        adapter=adapter,
        stdout=stdout,
        stderr=stderr,
        request_path=request_path,
        result_path=result_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _normalize_retrospective_follow_up_created(
    items: Any,
    *,
    recommended: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    recommended_by_fingerprint = {
        string_field(item, "fingerprint") or "": item
        for item in recommended
        if isinstance(item, dict) and string_field(item, "fingerprint")
    }
    recommended_by_identity = {
        _retrospective_follow_up_identity(
            string_field(item, "summary") or "",
            _retrospective_follow_up_labels(item.get("labels")),
        ): item
        for item in recommended
        if isinstance(item, dict)
    }
    recommended_by_summary: dict[str, dict[str, Any]] = {}
    ambiguous_summaries: set[str] = set()
    for item in recommended:
        if not isinstance(item, dict):
            continue
        summary_key = string_field(item, "summary") or ""
        if not summary_key:
            continue
        if summary_key in recommended_by_summary:
            ambiguous_summaries.add(summary_key)
            continue
        recommended_by_summary[summary_key] = item
    for summary_key in ambiguous_summaries:
        recommended_by_summary.pop(summary_key, None)
    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fingerprint = string_field(item, "fingerprint") or ""
        summary = string_field(item, "summary") or ""
        labels = item.get("labels")
        identity = _retrospective_follow_up_identity(redact_text(summary), _retrospective_follow_up_labels(labels))
        matched = recommended_by_fingerprint.get(fingerprint) or recommended_by_identity.get(identity) or recommended_by_summary.get(redact_text(summary)) or {}
        kind = string_field(item, "kind") or string_field(matched, "kind") or "created-follow-up"
        summary = summary or string_field(matched, "summary") or ""
        labels = labels if labels is not None else matched.get("labels")
        normalized_item = _retrospective_created_follow_up_item(
            {
                "id": string_field(item, "id") or "",
                "summary": summary,
                "labels": labels,
            },
            kind=kind,
        )
        if normalized_item is None:
            continue
        if string_field(matched, "fingerprint"):
            normalized_item["fingerprint"] = matched["fingerprint"]
        normalized_items.append(normalized_item)
    return _merge_retrospective_created_follow_up([], normalized_items)


def _normalized_retrospective_follow_up_result(
    *,
    enabled: bool,
    status: str,
    classification: str,
    summary: str,
    created: list[dict[str, Any]],
    adapter: dict[str, Any] | None = None,
    stdout: str = "",
    stderr: str = "",
    request_path: Path | None = None,
    result_path: Path | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> dict[str, Any]:
    normalized_result = {
        "enabled": enabled,
        "status": status,
        "classification": classification,
        "summary": redact_text(summary),
        "created": redact_artifact_value(created),
    }
    if adapter is not None:
        normalized_result["adapter"] = adapter
    if stdout_path is not None and stderr_path is not None:
        stdout_path.write_text(redact_text(stdout), encoding="utf-8")
        stderr_path.write_text(redact_text(stderr), encoding="utf-8")
    if request_path is not None and result_path is not None and stdout_path is not None and stderr_path is not None:
        normalized_result["evidence"] = {
            "request_path": request_path.name,
            "result_path": result_path.name,
            "stdout_path": stdout_path.name,
            "stderr_path": stderr_path.name,
        }
    return normalized_result


def _retrospective_follow_up_adapter_record(
    follow_up_config: dict[str, Any],
    returncode: int | None,
    timed_out: bool,
) -> dict[str, Any]:
    return {
        "type": follow_up_config["type"],
        "command": redact_artifact_value({"command": follow_up_config["command"]})["command"],
        "returncode": returncode,
        "timed_out": timed_out,
    }


def _retrospective_follow_up_runtime_record(
    follow_up_config: dict[str, Any],
    returncode: int | None,
    timed_out: bool,
) -> dict[str, Any]:
    if follow_up_config.get("creator") == "beads":
        return {
            "creator": {
                "type": "beads",
                "workspace": follow_up_config["beads_workspace"],
                "labels": redact_artifact_value(follow_up_config.get("labels", [])),
                "dedupe": "fingerprint",
            }
        }
    return {
        "adapter": _retrospective_follow_up_adapter_record(
            follow_up_config,
            returncode,
            timed_out,
        )
    }


def _retrospective_follow_up_creation_public_record(creation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in creation.items()
        if key != "created"
    }


def _write_retrospective_follow_up_result(path: Path, run_id: str, creation: dict[str, Any]) -> None:
    path.write_text(
        canonical_json(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "step": "retrospective-follow-up",
                "artifact_type": "retrospective-follow-up-result",
                "result": redact_artifact_value(creation),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _validation_retrospective_signals(
    state: dict[str, Any],
    normalized: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    validations = state.get("validations")
    if not isinstance(validations, list) or not validations:
        return _persisted_validation_retrospective_signals(state)
    selected_work = selected_work_items(state)
    signals = []
    for index, validation in enumerate(validations):
        if not isinstance(validation, dict):
            continue
        output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
        smoke_signal = _dry_run_smoke_validation_retrospective_signal(validation, output, normalized)
        if smoke_signal is not None:
            signals.append(smoke_signal)
        actionable_failures = output.get("actionable_failures")
        if output.get("status") == "validated" or not isinstance(actionable_failures, list):
            continue
        for failure in actionable_failures:
            if not isinstance(failure, dict):
                continue
            signal = _validation_failure_retrospective_signal(
                validation,
                output,
                failure,
                selected_work=selected_work,
            )
            if signal is not None:
                if (
                    string_field(signal, "scope") == "target-work"
                    and _validation_failure_consumed_by_repair(validations, index)
                ):
                    signal["consumed_by_repair"] = True
                signals.append(signal)
                break
    return signals


def _persisted_validation_retrospective_signals(state: dict[str, Any]) -> list[dict[str, Any]]:
    pipeline_retrospective = (
        state.get("pipeline_retrospective") if isinstance(state.get("pipeline_retrospective"), dict) else {}
    )
    persisted_signals = pipeline_retrospective.get("signals")
    if not isinstance(persisted_signals, list):
        return []
    selected_work = selected_work_items(state)
    signals: list[dict[str, Any]] = []
    for signal in persisted_signals:
        if not isinstance(signal, dict):
            continue
        kind = string_field(signal, "kind") or ""
        if kind not in {"validation-failure", "missing-tool-or-config"}:
            continue
        evidence_paths = [
            path for path in signal.get("evidence_paths", []) if isinstance(path, str) and path
        ]
        scope = _validation_failure_retrospective_scope(
            {"classification": string_field(signal, "classification") or ""},
            {
                "category": string_field(signal, "classification") or "",
                "excerpt": string_field(signal, "excerpt") or string_field(signal, "summary") or "",
                "log_path": evidence_paths[0] if evidence_paths else "",
            },
            kind=kind,
            selected_work=selected_work,
            evidence_paths=evidence_paths,
            target_project_labels=inferred_target_project_labels(state),
        )
        rebuilt = dict(signal)
        rebuilt["scope"] = scope
        if scope == "target-work":
            external_id = selected_work_external_id(selected_work)
            labels = inferred_target_project_labels(state)
            if external_id:
                rebuilt["external_id"] = redact_text(external_id)
            if labels:
                rebuilt["labels"] = labels
        signals.append(rebuilt)
    return signals


def _dry_run_smoke_validation_retrospective_signal(
    validation: dict[str, Any],
    output: dict[str, Any],
    normalized: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if output.get("status") != "validated":
        return None
    if _generated_smoke_dry_run_expected(normalized):
        return None
    validation_info = output.get("validation") if isinstance(output.get("validation"), dict) else {}
    if validation_info.get("dry_run") is not True:
        return None
    worker_result = output.get("worker_result") if isinstance(output.get("worker_result"), dict) else {}
    raw_result = worker_result.get("raw") if isinstance(worker_result.get("raw"), dict) else {}
    steps = raw_result.get("steps")
    if not isinstance(steps, list) or not any(_is_generated_smoke_validation_step(step) for step in steps):
        return None
    step = string_field(validation_info, "requested_profile") or "validation"
    excerpt = "Validation used dry-run generated smoke coverage instead of project worker evidence."
    return {
        "kind": "validation-smoke",
        "scope": "pipeline-process",
        "severity": "warning",
        "summary": excerpt,
        "step": redact_text(step),
        "classification": "dry-run-smoke-validation",
        "excerpt": excerpt,
        "evidence_paths": _retrospective_evidence_paths(
            string_field(validation, "step_result_path") or "",
            string_field(validation, "worker_result_path") or "",
        ),
    }


def _is_generated_smoke_validation_step(step: Any) -> bool:
    return isinstance(step, dict) and string_field(step, "name") == "generated-recipe-smoke"


def _generated_smoke_dry_run_expected(normalized: dict[str, Any] | None) -> bool:
    if not isinstance(normalized, dict):
        return False
    validation_expectations = normalized.get("validation_expectations")
    return (
        isinstance(validation_expectations, dict)
        and validation_expectations.get("generated_smoke_dry_run_expected") is True
    )


def _validation_failure_consumed_by_repair(validations: list[Any], index: int) -> bool:
    for later in validations[index + 1 :]:
        if not isinstance(later, dict):
            continue
        output = later.get("output") if isinstance(later.get("output"), dict) else {}
        if output.get("status") == "validated":
            return True
    return False


def _validation_failure_retrospective_signal(
    validation: dict[str, Any],
    output: dict[str, Any],
    failure: dict[str, Any],
    *,
    selected_work: list[dict[str, Any]],
) -> dict[str, Any] | None:
    excerpt = (
        string_field(failure, "excerpt") or string_field(failure, "reason") or string_field(output, "summary") or ""
    )
    log_path = string_field(failure, "log_path") or ""
    kind = "missing-tool-or-config" if _retrospective_text_has_missing_tool_or_config(excerpt) else "validation-failure"
    if not log_path and kind != "missing-tool-or-config":
        return None
    evidence_paths = _retrospective_evidence_paths(
        log_path,
        string_field(validation, "step_result_path") or "",
        string_field(validation, "worker_result_path") or "",
    )
    scope = _validation_failure_retrospective_scope(
        output,
        failure,
        kind=kind,
        selected_work=selected_work,
        evidence_paths=evidence_paths,
    )
    validation_info = output.get("validation") if isinstance(output.get("validation"), dict) else {}
    step = string_field(failure, "name") or string_field(validation_info, "requested_profile") or "validation"
    classification = string_field(failure, "category") or kind
    signal = {
        "kind": kind,
        "scope": scope,
        "severity": "error",
        "summary": redact_text(excerpt),
        "step": redact_text(step),
        "classification": redact_text(classification),
        "excerpt": redact_text(excerpt),
        "evidence_paths": evidence_paths,
    }
    if scope == "target-work":
        external_id = selected_work_external_id(selected_work)
        labels = selected_work_target_project_labels(selected_work)
        if external_id:
            signal["external_id"] = redact_text(external_id)
        if labels:
            signal["labels"] = labels
    return signal


def _validation_failure_retrospective_scope(
    output: dict[str, Any],
    failure: dict[str, Any],
    *,
    kind: str,
    selected_work: list[dict[str, Any]] | None = None,
    evidence_paths: list[str] | None = None,
    target_project_labels: list[str] | None = None,
) -> str:
    if kind == "missing-tool-or-config":
        return "pipeline-process"
    category = string_field(failure, "category") or string_field(output, "classification") or ""
    if category in {"runtime", "protocol", "timeout", "missing_result", "prerequisite_skip"}:
        return "pipeline-process"
    if category == "worker_failure":
        effective_target_project_labels = target_project_labels
        if effective_target_project_labels is None:
            effective_target_project_labels = selected_work_target_project_labels(selected_work or [])
        if not effective_target_project_labels:
            return "pipeline-process"
        if _validation_worker_failure_is_pipeline_infrastructure(failure, evidence_paths or []):
            return "pipeline-process"
        if not _validation_failure_has_target_repo_evidence(evidence_paths or []):
            return "pipeline-process"
    return "target-work"


def _validation_worker_failure_is_pipeline_infrastructure(
    failure: dict[str, Any],
    evidence_paths: list[str],
) -> bool:
    log_path = string_field(failure, "log_path") or ""
    excerpt = string_field(failure, "excerpt") or ""
    if any(path.endswith("/validation-evidence/logs/stack.log") for path in evidence_paths if isinstance(path, str)):
        return True
    if log_path.endswith("/validation-evidence/logs/stack.log"):
        return True
    return "binding validation stack " in excerpt.lower()


def _validation_failure_has_target_repo_evidence(evidence_paths: list[str]) -> bool:
    return any(
        isinstance(path, str)
        and (
            "validation-evidence/" in path
            or path.endswith("/worker-result.json")
            or path.endswith("/step-result.json")
        )
        for path in evidence_paths
    )


def _publication_retrospective_signals(publication: dict[str, Any]) -> list[dict[str, Any]]:
    if publication.get("status") != "failed-needs-human":
        return []
    reason = string_field(publication, "reason") or ""
    stdout_excerpt = string_field(publication, "stdout_excerpt") or ""
    stderr_excerpt = string_field(publication, "stderr_excerpt") or ""
    failure_text = "\n".join(part for part in (reason, stderr_excerpt, stdout_excerpt) if part)
    if not failure_text:
        return []
    excerpt = runtime_failure_excerpt(stderr_excerpt) or runtime_failure_excerpt(stdout_excerpt) or reason
    step = _publication_failure_step(publication)
    evidence_paths = _retrospective_evidence_paths("publication-result.json")
    if _publisher_auth_failure_reason(failure_text):
        kind = "publisher-auth"
    elif _retrospective_text_has_missing_tool_or_config(failure_text):
        kind = "missing-tool-or-config"
    else:
        kind = "publisher-failure"
    return [
        {
            "kind": kind,
            "scope": "pipeline-process",
            "severity": "error",
            "summary": redact_text(excerpt),
            "step": redact_text(step),
            "classification": redact_text(kind),
            "excerpt": redact_text(excerpt),
            "evidence_paths": evidence_paths,
        }
    ]


def _blocked_retrospective_signals(state: dict[str, Any], publication: dict[str, Any]) -> list[dict[str, Any]]:
    reason = string_field(publication, "reason") or ""
    if publication.get("status") != "blocked" or not reason:
        return []
    repair_stop = repair_stop_record(state, publication)
    if repair_stop:
        return [
            {
                "kind": "repair-stop",
                "scope": repair_stop["scope"],
                "severity": "error",
                "summary": repair_stop["reason"],
                "classification": repair_stop["classification"],
                "evidence_paths": repair_stop["evidence_paths"],
            }
        ]
    implementation_auth_signal = _implementation_auth_retrospective_signal(state, reason)
    if implementation_auth_signal is not None:
        return [implementation_auth_signal]
    reviewer_timeout_signal = _reviewer_timeout_retrospective_signal(state, reason)
    if reviewer_timeout_signal is not None:
        return [reviewer_timeout_signal]
    dirty_checkout_signal = _dirty_checkout_retrospective_signal(state, reason)
    if dirty_checkout_signal is not None:
        return [dirty_checkout_signal]
    return [
        {
            "kind": "retry-or-blocked",
            "scope": "target-work" if _blocked_reason_targets_work_item(state, reason) else "pipeline-process",
            "severity": "error",
            "summary": redact_text(reason),
            "evidence_paths": [],
        }
    ]


def _implementation_auth_retrospective_signal(state: dict[str, Any], reason: str) -> dict[str, Any] | None:
    if reason != "implement did not reach implemented: failed_runtime":
        return None
    implementation = state.get("implementation")
    if not isinstance(implementation, dict):
        return None
    summary = string_field(implementation, "summary") or ""
    agent_result = implementation.get("agent_result") if isinstance(implementation.get("agent_result"), dict) else {}
    evidence = agent_result.get("evidence") if isinstance(agent_result.get("evidence"), dict) else {}
    stderr_excerpt = string_field(evidence, "stderr_excerpt") or ""
    stdout_excerpt = string_field(evidence, "stdout_excerpt") or ""
    auth_excerpt = _implementation_auth_failure_excerpt(summary, stderr_excerpt, stdout_excerpt)
    classification = _implementation_auth_failure_classification(summary, stderr_excerpt, stdout_excerpt)
    if not classification:
        return None
    excerpt = auth_excerpt or runtime_failure_excerpt(stderr_excerpt) or runtime_failure_excerpt(stdout_excerpt) or summary
    implementation_result_path = string_field(state, "implementation_result_path") or ""
    agent_result_path = str(Path(implementation_result_path).with_name("agent-result.json")) if implementation_result_path else ""
    return {
        "kind": "implementation-auth",
        "scope": "pipeline-process",
        "severity": "error",
        "summary": redact_text(excerpt),
        "step": "implement",
        "classification": classification,
        "excerpt": redact_text(excerpt),
        "evidence_paths": _retrospective_evidence_paths(
            implementation_result_path,
            agent_result_path,
        ),
    }


def _implementation_auth_failure_excerpt(*texts: str) -> str:
    explicit_excerpts: list[str] = []
    for text in texts[1:]:
        explicit_excerpts.extend(_explicit_auth_failure_lines(text))
    for text in texts:
        explicit_excerpts.extend(_explicit_auth_failure_lines(text))
    for excerpt in explicit_excerpts:
        if "openai-codex" in excerpt.lower():
            return excerpt
    return explicit_excerpts[0] if explicit_excerpts else ""


def _implementation_auth_failure_classification(*texts: str) -> str:
    excerpt = _implementation_auth_failure_excerpt(*texts)
    if not excerpt:
        return ""
    lowered = excerpt.lower()
    if "openai-codex" in lowered:
        return "openai-codex-auth"
    return "agent-auth"


def _is_explicit_auth_failure_excerpt(excerpt: str) -> bool:
    if not excerpt:
        return False
    lowered = excerpt.lower()
    return (
        "no api key" in lowered
        or "api key not found" in lowered
        or "missing api key" in lowered
        or "oauth refresh failed" in lowered
        or "expired oauth credential" in lowered
        or "credential expired" in lowered
        or "expired credential" in lowered
        or "authentication failed" in lowered
        or "login failed" in lowered
        or ("credential" in lowered and ("missing" in lowered or "failed" in lowered or "expired" in lowered))
    )


def _explicit_auth_failure_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _is_explicit_auth_failure_excerpt(line):
            lines.append(line)
    return lines


def _reviewer_timeout_retrospective_signal(state: dict[str, Any], reason: str) -> dict[str, Any] | None:
    if reason != "review did not reach passed: failed_runtime":
        return None
    review = state.get("review")
    if not isinstance(review, dict):
        return None
    reviewer_result = review.get("reviewer_result")
    if not isinstance(reviewer_result, dict):
        return None
    adapter = reviewer_result.get("adapter")
    if not isinstance(adapter, dict) or adapter.get("timed_out") is not True:
        return None
    evidence = reviewer_result.get("evidence") if isinstance(reviewer_result.get("evidence"), dict) else {}
    excerpt = (
        string_field(reviewer_result, "summary")
        or string_field(evidence, "stderr_excerpt")
        or string_field(evidence, "stdout_excerpt")
        or "reviewer command timed out"
    )
    return {
        "kind": "reviewer-timeout",
        "scope": "pipeline-process",
        "severity": "error",
        "summary": redact_text(excerpt),
        "step": "review",
        "classification": "reviewer-timeout",
        "excerpt": redact_text(excerpt),
        "evidence_paths": _retrospective_evidence_paths(string_field(state, "review_result_path") or ""),
    }


def _dirty_checkout_retrospective_signal(state: dict[str, Any], reason: str) -> dict[str, Any] | None:
    if reason != "prepare-checkout did not reach prepared: failed_dirty_checkout":
        return None
    checkout = state.get("checkout")
    if not isinstance(checkout, dict) or checkout.get("status") != "failed_dirty_checkout":
        return None
    examples = _dirty_checkout_path_examples(checkout.get("dirty_status"))
    example_text = _dirty_checkout_examples_text(examples, checkout.get("dirty_status"))
    message = string_field(checkout, "message") or "existing checkout is dirty"
    summary = f"prepare-checkout blocked by dirty checkout; {message}."
    if example_text:
        summary += f" Dirty paths include {example_text}."
    return {
        "kind": "dirty-checkout",
        "scope": "pipeline-process",
        "severity": "error",
        "summary": redact_text(summary),
        "step": "prepare-checkout",
        "classification": "failed_dirty_checkout",
        "excerpt": redact_text(summary),
        "dirty_paths": redact_text(example_text),
        "evidence_paths": _retrospective_evidence_paths(string_field(checkout, "checkout_path") or ""),
    }


def _dirty_checkout_path_examples(dirty_status: Any, *, limit: int = 2) -> list[str]:
    if not isinstance(dirty_status, list):
        return []
    examples: list[str] = []
    for line in dirty_status:
        if not isinstance(line, str):
            continue
        text = line.strip()
        if not text:
            continue
        match = re.match(r"^[ MADRCU?!]{1,3}\s+(.*)$", text)
        path = match.group(1).strip() if match else text
        if path and path not in examples:
            examples.append(path)
        if len(examples) >= limit:
            break
    return examples


def _dirty_checkout_examples_text(examples: list[str], dirty_status: Any) -> str:
    if not examples:
        return ""
    total = len(dirty_status) if isinstance(dirty_status, list) else len(examples)
    if total > len(examples):
        return f"{', '.join(examples)}, and {total - len(examples)} more"
    return ", ".join(examples)


def _blocked_reason_targets_work_item(state: dict[str, Any], reason: str) -> bool:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    if reason.startswith("review feedback retry budget exhausted:"):
        return string_field(review, "status") == "request_revision"
    if reason.startswith("review requested changes:"):
        return True
    if (
        reason.startswith("validate did not reach validated:")
        or reason.startswith("required final validation evidence did not pass:")
        or reason.startswith("required final validation evidence is not validated:")
    ):
        validation = latest_validation_record(state)
        if validation is None:
            return _persisted_validation_targets_work_item(state)
        output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
        failure = first_validation_failure(output) or {}
        return _validation_failure_retrospective_scope(
            output,
            failure,
            kind="validation-failure",
            selected_work=selected_work_items(state),
            evidence_paths=_retrospective_evidence_paths(
                string_field(failure, "log_path") or "",
                string_field(validation, "step_result_path") or "",
                string_field(validation, "worker_result_path") or "",
            ),
        ) == "target-work"
    if reason.startswith("review did not reach passed: request_revision"):
        return True
    if _reason_is_repair_budget_exhausted(reason):
        if string_field(review, "status") == "request_revision":
            return True
        validation = latest_validation_record(state)
        if validation is None:
            return _persisted_validation_targets_work_item(state)
        output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
        failure = first_validation_failure(output) or {}
        return _validation_failure_retrospective_scope(
            output,
            failure,
            kind="validation-failure",
            selected_work=selected_work_items(state),
            evidence_paths=_retrospective_evidence_paths(
                string_field(failure, "log_path") or "",
                string_field(validation, "step_result_path") or "",
                string_field(validation, "worker_result_path") or "",
            ),
        ) == "target-work"
    return False


def _persisted_validation_targets_work_item(state: dict[str, Any]) -> bool:
    return any(
        isinstance(signal, dict) and string_field(signal, "scope") == "target-work"
        for signal in _persisted_validation_retrospective_signals(state)
    )


def _reason_is_repair_budget_exhausted(reason: str) -> bool:
    return reason.startswith("retry budget exhausted:") or reason.startswith("repair budget exhausted:")


def _cleanup_retrospective_signals(state: dict[str, Any]) -> list[dict[str, Any]]:
    cleanup = state.get("cleanup")
    if not isinstance(cleanup, dict):
        return []
    resources = cleanup.get("resources")
    if cleanup.get("status") == "clean" or not isinstance(resources, list) or not resources:
        return []
    return [
        {
            "kind": "dirty-cleanup",
            "scope": "pipeline-process",
            "severity": "warning",
            "summary": redact_text(f"Cleanup left resources behind: {cleanup.get('status') or 'unknown'}"),
            "evidence_paths": _retrospective_evidence_paths(
                *[
                    string_field(resource, "path") or ""
                    for resource in resources
                    if isinstance(resource, dict)
                ]
            ),
        }
    ]


def _terminal_integration_retrospective_signals(integration: dict[str, Any]) -> list[dict[str, Any]]:
    decision = string_field(integration, "decision") or ""
    remediation = string_field(integration, "remediation") or ""
    classification = _terminal_integration_classification(integration)
    if not classification:
        return []
    return [
        {
            "kind": "terminal-integration",
            "scope": "pipeline-process",
            "severity": "error",
            "summary": redact_text(remediation),
            "step": "integrate-pr",
            "classification": classification,
            "excerpt": redact_text(remediation),
            "evidence_paths": _retrospective_evidence_paths("integration-result.json"),
        }
    ]


def _terminal_integration_classification(integration: dict[str, Any]) -> str:
    decision = string_field(integration, "decision") or ""
    remediation = string_field(integration, "remediation") or ""
    if decision == "checks_pending":
        return ""
    if decision == "checks_inconclusive":
        if _terminal_integration_policy_blocks_inconclusive(integration):
            return "checks_inconclusive_policy"
        return ""
    if "Exact head mismatch" in remediation:
        return "exact_head_mismatch"
    if "cannot integrate blocked workstream artifact" in remediation:
        return "blocked_artifact_misuse"
    if remediation.startswith("could not determine "):
        return "missing_artifact_metadata"
    if "gh auth status failed" in remediation or "config_dir" in remediation:
        return "integration_auth_or_config"
    return ""


def _terminal_integration_policy_blocks_inconclusive(integration: dict[str, Any]) -> bool:
    neutral_blocks = string_field(integration, "neutral_policy") == "block"
    skipped_blocks = string_field(integration, "skipped_policy") == "block"
    if not neutral_blocks and not skipped_blocks:
        return False
    snapshots = integration.get("check_snapshots")
    if not isinstance(snapshots, list):
        return neutral_blocks
    for item in snapshots:
        if not isinstance(item, dict):
            continue
        if string_field(item, "status") != "inconclusive":
            continue
        if string_field(item, "bucket") == "skipping":
            if skipped_blocks:
                return True
            continue
        if neutral_blocks:
            return True
    return False


def _retrospective_follow_up_record(
    signals: list[dict[str, Any]],
    normalized: dict[str, Any] | None,
    publication: dict[str, Any],
) -> dict[str, Any]:
    process_signals = _process_retrospective_signals(signals)
    recommended = []
    created = []
    recommended_fingerprints: set[str] = set()
    recommended_identities: set[str] = set()
    created_fingerprints: set[str] = set()
    created_identities: set[str] = set()
    if normalized is not None:
        retrospective = effective_retrospective(normalized, publication)
        configured = retrospective.get("follow_up") if isinstance(retrospective, dict) else {}
        configured_recommended = configured.get("recommended") if isinstance(configured, dict) else []
        if isinstance(configured_recommended, list):
            for item in configured_recommended:
                if not isinstance(item, dict):
                    continue
                recommendation = _retrospective_follow_up_item(
                    kind="configured-recommendation",
                    summary=string_field(item, "summary") or "",
                    labels=item.get("labels"),
                )
                if (
                    recommendation
                    and recommendation["fingerprint"] not in recommended_fingerprints
                    and _retrospective_follow_up_identity_for_item(recommendation) not in recommended_identities
                ):
                    recommended.append(recommendation)
                    recommended_fingerprints.add(recommendation["fingerprint"])
                    recommended_identities.add(_retrospective_follow_up_identity_for_item(recommendation))
        configured_created = configured.get("created") if isinstance(configured, dict) else []
        if isinstance(configured_created, list):
            for item in configured_created:
                if not isinstance(item, dict):
                    continue
                created_item = _retrospective_created_follow_up_item(item, kind="configured-created")
                if created_item:
                    created.append(created_item)
                    if string_field(created_item, "fingerprint"):
                        created_fingerprints.add(created_item["fingerprint"])
                    created_identities.add(_retrospective_follow_up_identity_for_item(created_item))
    suppress_generic_retry_follow_up = any(_signal_replaces_generic_retry_follow_up(signal) for signal in signals)
    suppress_judge_follow_up = any(_signal_replaces_judge_follow_up(signal) for signal in signals)
    for signal in process_signals:
        if suppress_judge_follow_up and string_field(signal, "kind") == "retrospective-judge":
            continue
        if suppress_generic_retry_follow_up and string_field(signal, "kind") == "retry-or-blocked":
            continue
        follow_up_item = _follow_up_for_signal(signal)
        if (
            follow_up_item
            and follow_up_item["fingerprint"] not in recommended_fingerprints
            and follow_up_item["fingerprint"] not in created_fingerprints
            and _retrospective_follow_up_identity_for_item(follow_up_item) not in recommended_identities
            and _retrospective_follow_up_identity_for_item(follow_up_item) not in created_identities
        ):
            recommended.append(follow_up_item)
            recommended_fingerprints.add(follow_up_item["fingerprint"])
            recommended_identities.add(_retrospective_follow_up_identity_for_item(follow_up_item))
    repeated_target_validation_follow_up = _repeated_target_validation_follow_up(signals)
    if (
        repeated_target_validation_follow_up
        and repeated_target_validation_follow_up["fingerprint"] not in recommended_fingerprints
        and repeated_target_validation_follow_up["fingerprint"] not in created_fingerprints
        and _retrospective_follow_up_identity_for_item(repeated_target_validation_follow_up) not in recommended_identities
        and _retrospective_follow_up_identity_for_item(repeated_target_validation_follow_up) not in created_identities
    ):
        recommended.append(repeated_target_validation_follow_up)
        recommended_fingerprints.add(repeated_target_validation_follow_up["fingerprint"])
        recommended_identities.add(_retrospective_follow_up_identity_for_item(repeated_target_validation_follow_up))
    created = _merge_retrospective_created_follow_up([], created)
    return {
        "recommended": _retrospective_uncreated_recommendations(recommended, created),
        "created": created,
        "creation": _disabled_retrospective_follow_up_creation_record(),
    }


def _legacy_recommended_follow_up(recommended: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legacy = []
    for item in recommended:
        if not isinstance(item, dict):
            continue
        legacy.append(
            {
                "summary": redact_text(string_field(item, "summary") or ""),
                "labels": _retrospective_follow_up_labels(item.get("labels")),
            }
        )
    return legacy


def _disabled_retrospective_follow_up_creation_record() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "recommendation-only",
    }


def _retrospective_follow_up_item(kind: str, summary: str, labels: Any) -> dict[str, Any] | None:
    redacted_summary = redact_text(summary)
    normalized_labels = _retrospective_follow_up_labels(labels)
    if not redacted_summary:
        return None
    if not any(label.startswith("project:") for label in normalized_labels):
        normalized_labels.append("project:afk-composable-pipeline")
    return {
        "kind": kind,
        "summary": redacted_summary,
        "labels": normalized_labels,
        "fingerprint": _retrospective_follow_up_fingerprint(kind, redacted_summary, normalized_labels),
    }


def _retrospective_created_follow_up_item(item: dict[str, Any], *, kind: str) -> dict[str, Any] | None:
    item_id = redact_text(string_field(item, "id") or "")
    summary = string_field(item, "summary") or ""
    labels = item.get("labels")
    normalized = _retrospective_follow_up_item(kind, summary, labels)
    if normalized is None and not item_id:
        return None
    created_item = dict(normalized or {"kind": kind, "summary": "", "labels": []})
    if item_id:
        created_item["id"] = item_id
    if not created_item.get("fingerprint") and (created_item["summary"] or created_item["labels"]):
        created_item["fingerprint"] = _retrospective_follow_up_fingerprint(
            created_item["kind"],
            created_item["summary"],
            created_item["labels"],
        )
    return created_item


def _merge_retrospective_created_follow_up(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = []
    seen: dict[str, int] = {}
    for item in list(existing) + list(new):
        if not isinstance(item, dict):
            continue
        aliases = _retrospective_created_follow_up_aliases(item)
        matched_indexes = [seen[alias] for alias in aliases if alias in seen]
        if matched_indexes:
            index = min(matched_indexes)
            merged[index] = _merge_retrospective_created_follow_up_item(merged[index], item)
            for duplicate_index in sorted(set(matched_indexes) - {index}, reverse=True):
                merged[index] = _merge_retrospective_created_follow_up_item(merged[index], merged[duplicate_index])
                del merged[duplicate_index]
                seen = _retrospective_created_follow_up_seen_map(merged)
            seen.update({alias: index for alias in _retrospective_created_follow_up_aliases(merged[index])})
            continue
        merged.append(dict(item))
        seen.update({alias: len(merged) - 1 for alias in aliases})
    return merged


def _retrospective_created_follow_up_aliases(item: dict[str, Any]) -> list[str]:
    aliases = []
    item_id = string_field(item, "id") or ""
    fingerprint = string_field(item, "fingerprint") or ""
    summary = string_field(item, "summary") or ""
    if item_id:
        aliases.append(f"id:{item_id}")
    if fingerprint:
        aliases.append(f"fingerprint:{fingerprint}")
    if summary:
        aliases.append(f"identity:{_retrospective_follow_up_identity_for_item(item)}")
    return aliases


def _retrospective_created_follow_up_seen_map(items: list[dict[str, Any]]) -> dict[str, int]:
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        seen.update({alias: index for alias in _retrospective_created_follow_up_aliases(item)})
    return seen


def _merge_retrospective_created_follow_up_item(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in ("kind", "summary"):
        if string_field(new, key):
            merged[key] = new[key]
    if string_field(new, "id"):
        merged["id"] = new["id"]
    new_labels = new.get("labels")
    if isinstance(new_labels, list) and new_labels:
        merged["labels"] = new_labels
    fingerprint = string_field(new, "fingerprint") or string_field(existing, "fingerprint") or ""
    if fingerprint:
        merged["fingerprint"] = fingerprint
    elif string_field(merged, "summary"):
        merged["fingerprint"] = _retrospective_follow_up_fingerprint(
            string_field(merged, "kind") or "created-follow-up",
            string_field(merged, "summary") or "",
            _retrospective_follow_up_labels(merged.get("labels")),
        )
    return merged


def _retrospective_uncreated_recommendations(
    recommended: list[dict[str, Any]],
    created: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    created_aliases = set()
    for item in created:
        if isinstance(item, dict):
            created_aliases.update(_retrospective_created_follow_up_aliases(item))
    return [
        item
        for item in recommended
        if isinstance(item, dict)
        and not any(alias in created_aliases for alias in _retrospective_created_follow_up_aliases(item))
    ]


def _retrospective_follow_up_labels(labels: Any) -> list[str]:
    normalized_labels: list[str] = []
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, str) and label:
                redacted_label = redact_text(label)
                if redacted_label not in normalized_labels:
                    normalized_labels.append(redacted_label)
    return normalized_labels


def _retrospective_follow_up_fingerprint(kind: str, summary: str, labels: list[str]) -> str:
    return "retro-follow-up:" + sha256_json(
        {
            "kind": kind,
            "summary": summary,
            "labels": sorted(set(labels)),
        }
    )[:12]


def _retrospective_follow_up_identity(summary: str, labels: list[str]) -> str:
    return sha256_json(
        {
            "summary": summary,
            "labels": sorted(set(labels)),
        }
    )


def _retrospective_follow_up_identity_for_item(item: dict[str, Any]) -> str:
    return _retrospective_follow_up_identity(
        string_field(item, "summary") or "",
        _retrospective_follow_up_labels(item.get("labels")),
    )


def _subprocess_output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def _signal_replaces_generic_retry_follow_up(signal: dict[str, Any]) -> bool:
    if string_field(signal, "scope") == "target-work":
        return False
    kind = string_field(signal, "kind") or ""
    if kind in {"validation-failure", "missing-tool-or-config"}:
        return bool(string_field(signal, "excerpt") and signal.get("evidence_paths"))
    if kind == "publisher-auth":
        return bool(signal.get("evidence_paths"))
    return False


def _signal_replaces_judge_follow_up(signal: dict[str, Any]) -> bool:
    if string_field(signal, "scope") == "target-work":
        return False
    kind = string_field(signal, "kind") or ""
    return kind in {
        "validation-failure",
        "missing-tool-or-config",
    } and bool(
        string_field(signal, "excerpt") and signal.get("evidence_paths")
    )


def _follow_up_summary_for_signal(signal: dict[str, Any], prefix: str) -> str:
    step = string_field(signal, "step") or ""
    classification = string_field(signal, "classification") or string_field(signal, "kind") or "failure"
    excerpt = string_field(signal, "excerpt") or string_field(signal, "summary") or ""
    if step and excerpt:
        return f"{prefix} {step} [{classification}]: {excerpt}"
    if excerpt:
        return f"{prefix} [{classification}]: {excerpt}"
    return prefix


def _signal_targets_publication(signal: dict[str, Any]) -> bool:
    evidence_paths = signal.get("evidence_paths")
    return isinstance(evidence_paths, list) and "publication-result.json" in evidence_paths


def _follow_up_for_signal(signal: dict[str, Any]) -> dict[str, Any] | None:
    if string_field(signal, "scope") == "target-work":
        return None
    kind = string_field(signal, "kind") or ""
    if kind == "missing-tool-or-config":
        summary = "Fix the missing tool or configuration in validation evidence before rerunning the workstream."
        labels = ["afk:follow-up", "area:validation"]
        if _signal_targets_publication(signal):
            summary = "Fix the missing tool or configuration in publication evidence before rerunning the workstream."
            labels = ["afk:follow-up", "area:publication"]
        if _signal_replaces_judge_follow_up(signal):
            summary = _follow_up_summary_for_signal(signal, "Fix")
        return _retrospective_follow_up_item(
            kind=kind,
            summary=summary,
            labels=labels,
        )
    if kind == "validation-failure":
        return _retrospective_follow_up_item(
            kind=kind,
            summary=_follow_up_summary_for_signal(signal, "Fix"),
            labels=["afk:follow-up", "area:validation"],
        )
    if kind == "validation-smoke":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Switch validation to project-worker or another non-dry-run adapter before treating the run as honest dogfood evidence.",
            labels=["afk:follow-up", "area:validation"],
        )
    if kind == "terminal-integration":
        return _retrospective_follow_up_item(
            kind=kind,
            summary=_follow_up_summary_for_signal(signal, "Fix terminal integration"),
            labels=["afk:follow-up", "area:integration"],
        )
    if kind == "publisher-auth":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Repair GitHub publisher authentication evidence before rerunning terminal publication.",
            labels=["afk:follow-up", "area:publication"],
        )
    if kind == "implementation-auth":
        return _retrospective_follow_up_item(
            kind=kind,
            summary=_follow_up_summary_for_signal(signal, "Fix"),
            labels=["afk:follow-up", "area:implementation"],
        )
    if kind == "reviewer-timeout":
        excerpt = string_field(signal, "excerpt") or "reviewer command timed out"
        return _retrospective_follow_up_item(
            kind=kind,
            summary=f"Increase or override the reviewer timeout before rerunning the workstream; {excerpt}.",
            labels=["afk:follow-up", "area:review"],
        )
    if kind == "dirty-checkout":
        dirty_paths = string_field(signal, "dirty_paths") or "the reported checkout artifacts"
        return _retrospective_follow_up_item(
            kind=kind,
            summary=(
                "Clean the target checkout before rerunning prepare-checkout; move pipeline artifacts "
                "outside the checkout or remove/stash dirty paths such as "
                f"{dirty_paths}."
            ),
            labels=["afk:follow-up", "area:cleanup"],
        )
    if kind == "publisher-failure":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Address the blocked publication or retry evidence before rerunning the workstream.",
            labels=["afk:follow-up", "area:workstream"],
        )
    if kind == "retry-or-blocked":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Address the blocked publication or retry evidence before rerunning the workstream.",
            labels=["afk:follow-up", "area:workstream"],
        )
    if kind == "dirty-cleanup":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Clean up leftover workstream resources before starting another retry or publication attempt.",
            labels=["afk:follow-up", "area:cleanup"],
        )
    if kind == "retrospective-judge":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Review and address retrospective judge findings before treating the run as complete.",
            labels=["afk:follow-up", "area:workstream"],
        )
    return None


def _repeated_target_validation_follow_up(signals: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not any(
        isinstance(signal, dict)
        and string_field(signal, "kind") == "retry-or-blocked"
        and string_field(signal, "scope") == "target-work"
        and _reason_is_repair_budget_exhausted(string_field(signal, "summary") or "")
        for signal in signals
    ):
        return None
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        if string_field(signal, "kind") != "validation-failure" or string_field(signal, "scope") != "target-work":
            continue
        if signal.get("consumed_by_repair"):
            continue
        external_id = string_field(signal, "external_id") or "selected work"
        return _retrospective_follow_up_item(
            kind="target-validation-failure",
            summary=f"{external_id}: {_follow_up_summary_for_signal(signal, 'Fix')}",
            labels=["afk:follow-up", "area:validation", *(_retrospective_follow_up_labels(signal.get("labels")))],
        )
    return None


def _retrospective_health(signals: list[dict[str, Any]]) -> str:
    severities = {string_field(signal, "severity") or "" for signal in signals if isinstance(signal, dict)}
    if "error" in severities:
        return "failing"
    if "warning" in severities:
        return "warning"
    return "healthy"


def _process_retrospective_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        signal
        for signal in signals
        if isinstance(signal, dict) and string_field(signal, "scope") != "target-work"
    ]


def _retrospective_missing_tool_or_config_summary(text: str) -> str:
    if not _retrospective_text_has_missing_tool_or_config(text):
        return ""
    return redact_text(text)


def _validation_feedback_text_has_infra_or_setup_failure(text: str) -> bool:
    lowered = text.lower()
    if "permission denied" in lowered and "starting zone harness" in lowered:
        return True
    if "fatal: chdir" in lowered and "no such file or directory" in lowered:
        return True
    if "bash:" in lowered and "no such file or directory" in lowered:
        return True
    if "source directory" in lowered and "does not exist" in lowered:
        return True
    return False


def _retrospective_text_has_missing_tool_or_config(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "command not found",
            "executable file not found",
            "config_dir",
            "not installed",
            "missing tool",
            "missing config",
        )
    )


def _publisher_auth_failure_reason(reason: str) -> bool:
    lowered = reason.lower()
    return "gh auth status failed" in lowered or "authentication failed" in lowered


def _publication_failure_step(publication: dict[str, Any]) -> str:
    command = publication.get("command")
    if not isinstance(command, list):
        return "publisher"
    max_parts = 3 if command and command[0] == "gh" else 2
    parts = []
    for part in command:
        if not isinstance(part, str) or not part:
            continue
        if part.startswith("-"):
            break
        parts.append(part)
        if len(parts) == max_parts:
            break
    return " ".join(parts) or "publisher"


def _retrospective_evidence_paths(*paths: str) -> list[str]:
    evidence = []
    for path in paths:
        if not path:
            continue
        redacted = redact_text(path)
        if redacted not in evidence:
            evidence.append(redacted)
    return evidence
