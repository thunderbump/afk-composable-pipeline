from __future__ import annotations

from pathlib import Path
from typing import Any

from afk.redaction import redact_artifact_value


def required_validation_gate(
    required: list[dict[str, Any]],
    *,
    implemented_commit: str = "",
) -> dict[str, Any]:
    failures = validation_failures(required, implemented_commit=implemented_commit)
    return {
        "passed": not failures,
        "reason": validation_gate_reason(failures),
        "failures": failures,
        "artifacts": validation_artifacts(required),
    }


def publication_gate(
    *,
    validations: list[dict[str, Any]],
    review: dict[str, Any] | None,
    implemented_commit: str,
    incomplete_selected_work: list[str],
) -> dict[str, Any]:
    validation_gate = required_validation_gate(validations, implemented_commit=implemented_commit)
    if not validation_gate["passed"]:
        return validation_gate
    if not isinstance(review, dict):
        return {"passed": False, "reason": "required final review evidence is missing"}
    if review.get("status") != "passed":
        return {"passed": False, "reason": f"final review did not pass: {review.get('status') or 'missing status'}"}
    review_commit = string_field(review, "checkout_commit")
    if implemented_commit and review_commit != implemented_commit:
        return {"passed": False, "reason": "final review evidence is stale for implemented HEAD"}
    if incomplete_selected_work:
        return {
            "passed": False,
            "reason": "selected work items lack passed implementation, validation, and review evidence: "
            + ", ".join(incomplete_selected_work),
        }
    return {"passed": True, "reason": "", "artifacts": validation_gate["artifacts"]}


def validation_summary_lines(validations: list[dict[str, Any]]) -> list[str]:
    return [validation_summary_line(validation, index) for index, validation in enumerate(validations)]


def validation_failures(required: list[dict[str, Any]], *, implemented_commit: str = "") -> list[dict[str, Any]]:
    failures = []
    for item in required:
        name = string_field(item, "name") or "validation"
        status = string_field(item, "status") or "missing"
        worker_status = string_field(item, "worker_status") or worker_result_status(item)
        evidence_status = string_field(item, "evidence_status") or "valid"
        checkout_commit = string_field(item, "checkout_commit")
        if implemented_commit and checkout_commit != implemented_commit:
            validated_for = checkout_commit or "missing checkout_commit"
            failures.append(
                {
                    "status": "fail",
                    "title": f"{name} validation evidence is stale",
                    "summary": f"{name} was validated for {validated_for}, not {implemented_commit}",
                    "validation": {"name": name, "status": status, "checkout_commit": checkout_commit},
                    "gate_reason": f"{name}",
                    "gate_reason_kind": "stale",
                }
            )
            continue
        if status == "validated" and worker_status == "validated" and evidence_status == "valid":
            continue
        evidence_errors = item.get("evidence_errors") if isinstance(item.get("evidence_errors"), list) else []
        failure_summary = "; ".join(str(error) for error in evidence_errors if isinstance(error, str))
        if not failure_summary:
            failure_summary = (
                string_field(item, "worker_summary")
                or string_field(item, "summary")
                or f"{name} status is {status}"
            )
        failures.append(
            {
                "status": "fail",
                "title": f"{name} validation evidence is not validated",
                "summary": failure_summary,
                "validation": {
                    "name": name,
                    "status": status,
                    "classification": string_field(item, "classification") or "",
                    "evidence_status": evidence_status,
                    "evidence_errors": evidence_errors,
                    "worker_status": worker_status,
                    "worker_classification": string_field(item, "worker_classification") or "",
                    "step_result_path": string_field(item, "step_result_path") or "",
                    "worker_result_path": string_field(item, "worker_result_path") or "",
                    "checkout_commit": checkout_commit or "",
                },
                "gate_reason": f"{name} ({gate_reason_status(status, worker_status, evidence_status)})",
                "gate_reason_kind": "invalid",
            }
        )
    if not required:
        failures.append(
            {
                "status": "fail",
                "title": "No required validation evidence",
                "summary": "review requires at least one final validation artifact",
                "gate_reason": "required validation",
                "gate_reason_kind": "missing",
            }
        )
    return failures


def validation_gate_reason(failures: list[dict[str, Any]]) -> str:
    if not failures:
        return ""
    if all(failure.get("gate_reason_kind") == "stale" for failure in failures):
        return "required final validation evidence is stale for implemented HEAD: " + ", ".join(
            str(failure.get("gate_reason") or "validation") for failure in failures
        )
    return "required final validation evidence is not validated: " + ", ".join(
        str(failure.get("gate_reason") or "validation") for failure in failures
    )


def validation_artifacts(required: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "name": string_field(item, "name") or f"validation-{index + 1}",
            "step_result_path": string_field(item, "step_result_path") or "",
            "worker_result_path": string_field(item, "worker_result_path") or "",
        }
        for index, item in enumerate(required)
        if isinstance(item, dict)
    ]


def validation_summary_line(validation: dict[str, Any], index: int) -> str:
    profile = string_field(validation, "name") or f"validation-{index + 1}"
    status = string_field(validation, "status") or output_status(validation) or "missing"
    evidence = validation_worker_evidence(validation)
    step_ref = ledger_relative_path(string_field(validation, "step_result_path") or "")
    worker_ref = ledger_relative_path(string_field(validation, "worker_result_path") or "")
    path_evidence = "; ".join(item for item in [step_ref, worker_ref] if item)
    parts = [f"- {profile}: {status}"]
    if evidence:
        parts.append(evidence)
    if path_evidence:
        parts.append(f"evidence: {path_evidence}")
    return " - ".join(parts)


def validation_worker_evidence(validation: dict[str, Any]) -> str:
    worker_result = validation.get("worker_result") if isinstance(validation.get("worker_result"), dict) else {}
    raw_result = worker_result.get("raw") if isinstance(worker_result.get("raw"), dict) else {}
    normalized = worker_result.get("normalized") if isinstance(worker_result.get("normalized"), dict) else {}
    result = validation_worker_result_summary(raw_result) or "missing"
    command = validation_worker_command_summary(normalized) or "missing"
    summary = string_field(validation, "summary") or string_field(normalized, "summary") or "missing"
    return f"result: {result} - command: {command} - summary: {summary}"


def validation_worker_result_summary(raw_result: dict[str, Any]) -> str:
    steps = raw_result.get("steps")
    if isinstance(steps, list):
        labels = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            name = string_field(step, "name")
            status = string_field(step, "status")
            if name and status:
                labels.append(f"{name}={status}")
        if labels:
            return ", ".join(labels[:5])
    status = string_field(raw_result, "status")
    return f"worker_status={status}" if status else ""


def validation_worker_command_summary(normalized: dict[str, Any]) -> str:
    adapter = normalized.get("adapter") if isinstance(normalized.get("adapter"), dict) else {}
    command = adapter.get("command")
    if not isinstance(command, list) or not command:
        return ""
    redacted = redact_artifact_value({"command": command}).get("command", [])
    if not isinstance(redacted, list):
        return ""
    rendered = " ".join(str(part) for part in redacted if str(part).strip())
    if len(rendered) > 160:
        return rendered[:157].rstrip() + "..."
    return rendered


def ledger_relative_path(path: str) -> str:
    for marker, prefix in (("/runs/", "runs/"), ("/workstreams/", "workstreams/")):
        if marker in path:
            return prefix + path.split(marker, 1)[1]
    return path


def output_status(validation: dict[str, Any]) -> str:
    output = validation.get("output")
    if isinstance(output, dict):
        return string_field(output, "status") or ""
    return ""


def worker_result_status(validation: dict[str, Any]) -> str:
    worker_result = validation.get("worker_result") if isinstance(validation.get("worker_result"), dict) else {}
    normalized = worker_result.get("normalized") if isinstance(worker_result.get("normalized"), dict) else {}
    return string_field(normalized, "status") or ""


def gate_reason_status(status: str, worker_status: str, evidence_status: str) -> str:
    if evidence_status != "valid":
        return f"{evidence_status} evidence"
    if status == "validated" and worker_status and worker_status != "validated":
        return f"worker {worker_status}"
    return status


def string_field(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if isinstance(item, str) and item.strip():
        return item.strip()
    return None
