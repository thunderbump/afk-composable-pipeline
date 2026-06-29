from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.pi_workers import non_openai_pi_mount_error, openai_codex_pi_mount_error, validate_absolute_dir
from afk.redaction import is_secret_command_flag, redact_artifact_value, redact_text


SCHEMA_VERSION = 1


class ReviewerRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        timed_out: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out


def review_step(context: Any) -> dict[str, Any]:
    return review(
        context.input_data,
        run_id=context.run_id,
        run_dir=context.run_dir,
    )


def review(input_data: Any, *, run_id: str, run_dir: Path | None) -> dict[str, Any]:
    request = normalize_request(input_data, run_id=run_id)
    if request["status"] != "valid":
        return request
    if run_dir is None:
        return invalid_request("run_dir is required")

    evidence_pack = build_evidence_pack(request)
    write_json(
        run_dir / "evidence-pack.json",
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "step": "review",
            "artifact_type": "evidence-pack",
            "evidence_pack": evidence_pack,
        },
    )
    reviewer_request = build_reviewer_request(evidence_pack, run_id=run_id)
    write_json(run_dir / "reviewer-request.json", reviewer_request)

    validation_failures = required_validation_failures(evidence_pack)
    if validation_failures:
        summary = validation_gate_summary(validation_failures)
        normalized = normalized_reviewer_result(
            status="failed_validation_evidence",
            classification="validation_evidence_incomplete",
            summary=summary,
            findings=validation_failures,
            adapter=adapter_record(request["reviewer"], None, False),
            stdout="",
            stderr="",
        )
        write_review_artifacts(run_dir, run_id, normalized)
        return review_output(request, evidence_pack, normalized)

    try:
        adapter_result, raw_result = run_fake_reviewer_command(
            request["reviewer"],
            checkout_path=Path(request["checkout"]["path"]),
            reviewer_request=reviewer_request,
            request_path=run_dir / "reviewer-request.json",
        )
    except ReviewerRuntimeError as exc:
        stdout = redact_text(exc.stdout)
        stderr = redact_text(exc.stderr or exc.message)
        write_adapter_logs(stdout, stderr)
        normalized = normalized_reviewer_result(
            status="failed_runtime",
            classification="runtime_failure",
            summary=exc.message,
            findings=[],
            adapter=adapter_record(request["reviewer"], exc.returncode, exc.timed_out),
            stdout=stdout,
            stderr=stderr,
        )
        write_review_artifacts(run_dir, run_id, normalized)
        return review_output(request, evidence_pack, normalized)

    raw_payload = read_reviewer_payload(raw_result, stdout=adapter_result["stdout"])
    stdout = redact_text(adapter_result["stdout"])
    stderr = redact_text(adapter_result["stderr"])
    write_adapter_logs(stdout, stderr)

    if raw_payload["status"] != "valid":
        normalized = normalized_reviewer_result(
            status="failed_protocol",
            classification="protocol_failure",
            summary=raw_payload["message"],
            findings=[],
            adapter=adapter_record(request["reviewer"], adapter_result["returncode"], False),
            stdout=stdout,
            stderr=stderr,
            result_source=raw_payload.get("result_source"),
            result_file_present=raw_payload.get("result_file_present"),
        )
        write_review_artifacts(run_dir, run_id, normalized)
        return review_output(request, evidence_pack, normalized)

    normalized = normalize_reviewer_payload(
        raw_payload["payload"],
        adapter=adapter_record(request["reviewer"], adapter_result["returncode"], False),
        stdout=stdout,
        stderr=stderr,
        result_source=raw_payload["result_source"],
        result_file_present=raw_payload["result_file_present"],
    )
    write_review_artifacts(run_dir, run_id, normalized)
    return review_output(request, evidence_pack, normalized)


def normalize_request(input_data: Any, *, run_id: str) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        return invalid_request("request must be an object")

    work_item = normalize_work_item(input_data.get("work_item"))
    if work_item["status"] != "valid":
        return invalid_request(work_item["message"])
    work_selection = normalize_work_selection(input_data.get("work_selection"), work_item["work_item"])
    if work_selection["status"] != "valid":
        return invalid_request(work_selection["message"])

    checkout = normalize_checkout(input_data.get("checkout"))
    if checkout["status"] != "valid":
        return invalid_request(checkout["message"])

    implementation = normalize_implementation(
        input_data.get("implementation"),
        Path(checkout["checkout"]["path"]),
    )
    if implementation["status"] != "valid":
        return invalid_request(implementation["message"])

    validation = normalize_validation(input_data.get("validation"))
    if validation["status"] != "valid":
        return invalid_request(validation["message"])

    guardrails = normalize_guardrails(input_data.get("guardrails", []))
    if guardrails["status"] != "valid":
        return invalid_request(guardrails["message"])

    cleanup = normalize_cleanup(input_data.get("cleanup", {}))
    if cleanup["status"] != "valid":
        return invalid_request(cleanup["message"])

    reviewer = normalize_reviewer(
        input_data.get("reviewer"),
        checkout_path=Path(checkout["checkout"]["path"]),
    )
    if reviewer["status"] != "valid":
        return invalid_request(reviewer["message"])

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "run_id": run_id,
        "work_item": work_item["work_item"],
        "work_selection": work_selection["work_selection"],
        "checkout": checkout["checkout"],
        "implementation": implementation["implementation"],
        "validation": validation["validation"],
        "guardrails": guardrails["guardrails"],
        "cleanup": cleanup["cleanup"],
        "reviewer": reviewer["reviewer"],
    }


def invalid_request(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed_invalid_payload",
        "message": message,
    }


def normalize_work_item(work_item: Any) -> dict[str, Any]:
    if not isinstance(work_item, dict):
        return {"status": "invalid", "message": "work_item must be an object"}
    external_id = string_field(work_item, "external_id")
    source_id = string_field(work_item, "source_id")
    source_type = string_field(work_item, "source_type")
    if not external_id:
        return {"status": "invalid", "message": "work_item.external_id is required"}
    if not source_id or not source_type:
        return {"status": "invalid", "message": "work_item source identity is required"}
    labels = string_list_field(work_item, "labels")
    acceptance_criteria = string_list_field(work_item, "acceptance_criteria")
    if labels is None:
        return {"status": "invalid", "message": "work_item.labels must be a list of strings"}
    if acceptance_criteria is None:
        return {"status": "invalid", "message": "work_item.acceptance_criteria must be a list of strings"}
    normalized = {
        "source_id": source_id,
        "source_type": source_type,
        "external_id": external_id,
        "url": string_field(work_item, "url") or "",
        "title": string_field(work_item, "title") or "",
        "status": string_field(work_item, "status") or "",
        "labels": labels,
        "parent": work_item.get("parent"),
        "workstream": work_item.get("workstream"),
        "acceptance_criteria": acceptance_criteria,
        "dependencies": (
            list(work_item.get("dependencies", []))
            if isinstance(work_item.get("dependencies", []), list)
            else []
        ),
        "blockers": list(work_item.get("blockers", [])) if isinstance(work_item.get("blockers", []), list) else [],
        "dependency_status": string_field(work_item, "dependency_status") or "",
        "afk": dict(work_item.get("afk") or {}) if isinstance(work_item.get("afk") or {}, dict) else {},
    }
    return {"status": "valid", "work_item": redact_artifact_value(normalized)}


def normalize_work_selection(work_selection: Any, fallback_work_item: dict[str, Any]) -> dict[str, Any]:
    if work_selection is None:
        return {
            "status": "valid",
            "work_selection": {"schema_version": SCHEMA_VERSION, "selected_work": [fallback_work_item]},
        }
    if not isinstance(work_selection, dict):
        return {"status": "invalid", "message": "work_selection must be an object"}
    selected_work = work_selection.get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return {"status": "invalid", "message": "work_selection.selected_work must be a non-empty list"}
    normalized_items = []
    for item in selected_work:
        normalized = normalize_work_item(item)
        if normalized["status"] != "valid":
            return normalized
        normalized_items.append(normalized["work_item"])
    return {
        "status": "valid",
        "work_selection": {
            "schema_version": SCHEMA_VERSION,
            "selected_work": redact_artifact_value(normalized_items),
        },
    }


def normalize_checkout(checkout: Any) -> dict[str, Any]:
    if not isinstance(checkout, dict):
        return {"status": "invalid", "message": "checkout must be an object"}
    if checkout.get("status") != "prepared":
        return {"status": "invalid", "message": "checkout.status must be prepared"}
    path = string_field(checkout, "checkout_path")
    start_commit = string_field(checkout, "start_commit")
    if not path:
        return {"status": "invalid", "message": "checkout.checkout_path is required"}
    if not start_commit:
        return {"status": "invalid", "message": "checkout.start_commit is required"}
    checkout_path = Path(path)
    if not checkout_path.is_absolute():
        return {"status": "invalid", "message": "checkout.checkout_path must be absolute"}
    if not (checkout_path / ".git").is_dir():
        return {"status": "invalid", "message": "checkout.checkout_path must be a git checkout"}
    return {
        "status": "valid",
        "checkout": {
            "path": str(checkout_path),
            "review_branch": string_field(checkout, "review_branch") or "",
            "requested_ref": string_field(checkout, "requested_ref") or "",
            "start_commit": start_commit,
        },
    }


def normalize_implementation(implementation: Any, checkout_path: Path) -> dict[str, Any]:
    if not isinstance(implementation, dict):
        return {"status": "invalid", "message": "implementation must be an object"}
    git_metadata = implementation.get("git")
    if not isinstance(git_metadata, dict):
        git_metadata = infer_git_metadata(checkout_path)
    changed_files = string_list_field(git_metadata, "changed_files")
    if changed_files is None:
        return {"status": "invalid", "message": "implementation.git.changed_files must be a list of strings"}
    commits = git_metadata.get("commits", [])
    if not isinstance(commits, list):
        return {"status": "invalid", "message": "implementation.git.commits must be a list"}
    normalized = {
        "status": string_field(implementation, "status") or "",
        "summary": string_field(implementation, "summary") or "",
        "git": {
            "before_commit": string_field(git_metadata, "before_commit") or "",
            "after_commit": string_field(git_metadata, "after_commit") or "",
            "changed_files": changed_files,
            "commits": redact_artifact_value(commits),
            "dirty": bool(git_metadata.get("dirty", False)),
            "dirty_status": string_list_field(git_metadata, "dirty_status") or [],
        },
    }
    return {"status": "valid", "implementation": redact_artifact_value(normalized)}


def infer_git_metadata(checkout_path: Path) -> dict[str, Any]:
    try:
        head = git(checkout_path, ["rev-parse", "HEAD"])
    except ReviewerRuntimeError:
        head = ""
    return {
        "before_commit": "",
        "after_commit": head,
        "changed_files": [],
        "commits": [],
        "dirty": False,
        "dirty_status": [],
    }


def normalize_validation(validation: Any) -> dict[str, Any]:
    if not isinstance(validation, dict):
        return {"status": "invalid", "message": "validation must be an object"}
    required_artifacts = validation.get("required_artifacts")
    if not isinstance(required_artifacts, list) or not required_artifacts:
        return {"status": "invalid", "message": "validation.required_artifacts must be a non-empty list"}
    normalized = []
    for index, artifact in enumerate(required_artifacts):
        if not isinstance(artifact, dict):
            return {"status": "invalid", "message": "validation.required_artifacts entries must be objects"}
        name = string_field(artifact, "name") or f"validation-{index + 1}"
        step_result_path = string_field(artifact, "step_result_path")
        worker_result_path = string_field(artifact, "worker_result_path")
        step_path = Path(step_result_path) if step_result_path else None
        worker_path = Path(worker_result_path) if worker_result_path else None
        step_path_errors = (
            ["step-result.json path is required"]
            if step_path is None
            else validation_artifact_path_errors(step_path, "step-result.json")
        )
        worker_path_errors = ["worker-result.json path is required"] if worker_path is None else []
        pair_path_errors = []
        if step_path is not None and worker_path is not None:
            worker_path_errors = validation_artifact_path_errors(worker_path, "worker-result.json")
            pair_path_errors = validation_artifact_pair_path_errors(step_path, worker_path)
        path_errors = step_path_errors + worker_path_errors + pair_path_errors
        if path_errors:
            step_result = invalid_validation_artifact_path_result(
                step_path,
                step_path_errors or pair_path_errors or path_errors,
            )
            worker_result = invalid_validation_artifact_path_result(
                worker_path,
                worker_path_errors or pair_path_errors or path_errors,
            )
        else:
            step_result = read_json_artifact(step_path)
            worker_result = read_json_artifact(worker_path)
        output = step_result.get("output") if isinstance(step_result.get("output"), dict) else {}
        worker_normalized = worker_validation_result(worker_result)
        evidence_errors = list(path_errors)
        if not path_errors:
            evidence_errors.extend(
                validation_evidence_errors(
                    step_result,
                    worker_result,
                    worker_result_path=worker_result_path,
                )
            )
        normalized.append(
            redact_artifact_value(
                {
                    "name": name,
                    "step_result_path": step_result_path or "",
                    "worker_result_path": worker_result_path or "",
                    "evidence_status": "invalid" if evidence_errors else "valid",
                    "evidence_errors": evidence_errors,
                    "status": string_field(output, "status") or "missing",
                    "classification": string_field(output, "classification") or "",
                    "summary": string_field(output, "summary") or "",
                    "worker_status": worker_normalized["status"],
                    "worker_classification": worker_normalized["classification"],
                    "worker_summary": worker_normalized["summary"],
                    "step_result": step_result,
                    "worker_result": worker_result,
                }
            )
        )
    return {"status": "valid", "validation": {"required": normalized}}


def read_validation_artifact(path: Path, expected_filename: str) -> dict[str, Any]:
    errors = validation_artifact_path_errors(path, expected_filename)
    if errors:
        return invalid_validation_artifact_path_result(path, errors)
    return read_json_artifact(path)


def invalid_validation_artifact_path_result(path: Path | None, errors: list[str]) -> dict[str, Any]:
    return {
        "status": "invalid_path",
        "path": str(path) if path is not None else "",
        "message": "; ".join(errors),
    }


def validation_artifact_path_errors(path: Path, expected_filename: str) -> list[str]:
    errors = []
    if not path.is_absolute():
        errors.append(f"{expected_filename} path must be absolute")
    if ".." in path.parts:
        errors.append(f"{expected_filename} path must not contain traversal")
    if path.name != expected_filename:
        errors.append(f"validation artifact filename must be {expected_filename}")
    try:
        for item in (path, *path.parents):
            if item.exists() and item.is_symlink():
                errors.append(f"{expected_filename} path must not use symlinks")
                break
        if not path.is_file():
            errors.append(f"{expected_filename} path must be a regular JSON file")
    except OSError:
        errors.append(f"{expected_filename} path could not be inspected")
    return errors


def validation_artifact_pair_path_errors(step_path: Path, worker_path: Path) -> list[str]:
    if not step_path.is_absolute() or not worker_path.is_absolute():
        return []
    if step_path.parent == worker_path.parent:
        return []
    if step_path.parent.parent == worker_path.parent.parent:
        return []
    return ["validation artifacts must be in the same or sibling ledger run directory"]


def validation_evidence_errors(
    step_result: dict[str, Any],
    worker_result: dict[str, Any],
    *,
    worker_result_path: str | None,
) -> list[str]:
    errors = []
    output = step_result.get("output") if isinstance(step_result.get("output"), dict) else None
    if step_result.get("step") != "validate":
        errors.append("step_result step must be validate")
    if step_result.get("status") != "succeeded":
        errors.append("step_result status must be succeeded")
    if output is None:
        errors.append("step_result output must be an object")
    else:
        if output.get("status") != "validated":
            errors.append("step_result output status must be validated")
        if worker_result_path:
            artifacts = output.get("artifacts") if isinstance(output.get("artifacts"), dict) else {}
            if artifacts.get("worker_result") != Path(worker_result_path).name:
                errors.append("step_result output artifacts must reference worker_result")

    worker_normalized = {}
    worker_result_result = worker_result.get("result") if isinstance(worker_result.get("result"), dict) else {}
    if isinstance(worker_result_result.get("normalized"), dict):
        worker_normalized = worker_result_result["normalized"]
    if worker_result.get("step") != "validate":
        errors.append("worker_result step must be validate")
    if worker_result.get("artifact_type") != "worker-result":
        errors.append("worker_result artifact_type must be worker-result")
    if not worker_normalized:
        errors.append("worker_result result.normalized must be an object")
    elif worker_normalized.get("status") != "validated":
        errors.append("worker_result normalized status must be validated")

    step_run_id = string_field(step_result, "run_id")
    worker_run_id = string_field(worker_result, "run_id")
    if not step_run_id:
        errors.append("step_result run_id is required")
    if not worker_run_id:
        errors.append("worker_result run_id is required")
    if step_run_id and worker_run_id and step_run_id != worker_run_id:
        errors.append("step_result and worker_result run_id must match")
    return errors


def worker_validation_result(worker_result: dict[str, Any]) -> dict[str, str]:
    if worker_result.get("status") in {"missing", "invalid_json", "invalid"}:
        return {
            "status": string_field(worker_result, "status") or "missing",
            "classification": "",
            "summary": "",
        }
    result = worker_result.get("result") if isinstance(worker_result.get("result"), dict) else {}
    normalized = result.get("normalized") if isinstance(result.get("normalized"), dict) else {}
    return {
        "status": string_field(normalized, "status") or "missing",
        "classification": string_field(normalized, "classification") or "",
        "summary": string_field(normalized, "summary") or "",
    }


def read_json_artifact(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "missing", "path": str(path)}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "invalid_json", "path": str(path)}
    if not isinstance(payload, dict):
        return {"status": "invalid", "path": str(path)}
    return redact_artifact_value(payload)


def normalize_guardrails(guardrails: Any) -> dict[str, Any]:
    if not isinstance(guardrails, list):
        return {"status": "invalid", "message": "guardrails must be a list"}
    return {"status": "valid", "guardrails": redact_artifact_value(guardrails)}


def normalize_cleanup(cleanup: Any) -> dict[str, Any]:
    if cleanup is None:
        cleanup = {}
    if not isinstance(cleanup, dict):
        return {"status": "invalid", "message": "cleanup must be an object"}
    normalized = {
        "status": string_field(cleanup, "status") or "unknown",
        "resources": cleanup.get("resources", []) if isinstance(cleanup.get("resources", []), list) else [],
    }
    return {"status": "valid", "cleanup": redact_artifact_value(normalized)}


def normalize_reviewer(reviewer: Any, *, checkout_path: Path) -> dict[str, Any]:
    if not isinstance(reviewer, dict):
        return {"status": "invalid", "message": "reviewer must be an object"}
    if reviewer.get("type") != "fake-reviewer-command":
        return {"status": "invalid", "message": "reviewer.type must be fake-reviewer-command"}
    for forbidden_key in ("credentials_path", "auth_file", "token", "api_key"):
        if forbidden_key in reviewer:
            return {"status": "invalid", "message": f"reviewer.{forbidden_key} is not supported"}
    command = reviewer.get("command")
    if not is_string_list(command):
        return {"status": "invalid", "message": "reviewer.command must be a list of strings"}
    command_secret_error = command_secret_error_message(command)
    if command_secret_error:
        return {"status": "invalid", "message": command_secret_error}
    timeout_seconds = reviewer.get("timeout_seconds", reviewer.get("timeoutSeconds", 120))
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return {"status": "invalid", "message": "reviewer.timeout_seconds must be a positive number"}
    normalized_reviewer = {
        "type": "fake-reviewer-command",
        "command": list(command),
        "timeout_seconds": float(timeout_seconds),
    }
    for field_name in ("codex_home", "config_home"):
        raw_value = reviewer.get(field_name)
        if raw_value is None:
            continue
        if not isinstance(raw_value, str) or not raw_value.strip():
            return {"status": "invalid", "message": f"reviewer.{field_name} must be an absolute directory path"}
        try:
            normalized_reviewer[field_name] = validate_absolute_dir(
                raw_value,
                f"reviewer.{field_name}",
                checkout_path=checkout_path,
            )
        except ValueError as exc:
            return {"status": "invalid", "message": str(exc)}
    raw_env = reviewer.get("env")
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            return {"status": "invalid", "message": "reviewer.env must be an object"}
        normalized_env: dict[str, str] = {}
        for key, value in raw_env.items():
            if key not in {"PI_CONFIG_HOME", "PI_CODING_AGENT_DIR"}:
                return {
                    "status": "invalid",
                    "message": "reviewer.env only supports PI_CONFIG_HOME and PI_CODING_AGENT_DIR",
                }
            if not isinstance(value, str) or not value.strip():
                return {"status": "invalid", "message": f"reviewer.env.{key} must be an absolute directory path"}
            try:
                normalized_env[key] = validate_absolute_dir(
                    value,
                    f"reviewer.env.{key}",
                    checkout_path=checkout_path,
                )
            except ValueError as exc:
                return {"status": "invalid", "message": str(exc)}
        normalized_reviewer["env"] = normalized_env
    mount_error = openai_codex_pi_mount_error(
        command=normalized_reviewer["command"],
        codex_home=normalized_reviewer.get("codex_home"),
        config_home=normalized_reviewer.get("config_home"),
        env=normalized_reviewer.get("env"),
        field_prefix="reviewer",
    )
    if mount_error:
        return {"status": "invalid", "message": mount_error}
    mount_rejection = non_openai_pi_mount_error(
        command=normalized_reviewer["command"],
        codex_home=normalized_reviewer.get("codex_home"),
        config_home=normalized_reviewer.get("config_home"),
        env=normalized_reviewer.get("env"),
        field_prefix="reviewer",
    )
    if mount_rejection:
        return {"status": "invalid", "message": mount_rejection}
    return {
        "status": "valid",
        "reviewer": normalized_reviewer,
    }


def build_evidence_pack(request: dict[str, Any]) -> dict[str, Any]:
    pack = {
        "schema_version": SCHEMA_VERSION,
        "run_id": request["run_id"],
        "work_item": request["work_item"],
        "work_selection": request["work_selection"],
        "acceptance_criteria": request["work_item"]["acceptance_criteria"],
        "checkout": request["checkout"],
        "implementation": request["implementation"],
        "validation": request["validation"],
        "guardrails": request["guardrails"],
        "cleanup": request["cleanup"],
        "redaction": {
            "applied": True,
            "artifact_values": "redact_artifact_value",
            "text": "redact_text",
            "secret_placeholder": "[REDACTED]",
        },
    }
    return redact_artifact_value(pack)


def build_reviewer_request(evidence_pack: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "step": "review",
        "artifact_type": "reviewer-request",
        "evidence_pack": evidence_pack,
        "expected_result_schema": {
            "status": "pass|fail|request_revision",
            "summary": "string",
            "findings": "list[object]",
        },
    }


def required_validation_failures(evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    validation = evidence_pack.get("validation") if isinstance(evidence_pack.get("validation"), dict) else {}
    required = validation.get("required") if isinstance(validation.get("required"), list) else []
    failures = []
    for item in required:
        if not isinstance(item, dict):
            failures.append(
                {
                    "status": "fail",
                    "title": "Malformed validation evidence",
                    "summary": "required validation evidence entry is not an object",
                }
            )
            continue
        status = string_field(item, "status") or "missing"
        name = string_field(item, "name") or "validation"
        worker_status = string_field(item, "worker_status") or "missing"
        evidence_status = string_field(item, "evidence_status") or "invalid"
        if status != "validated" or worker_status != "validated" or evidence_status != "valid":
            evidence_errors = item.get("evidence_errors") if isinstance(item.get("evidence_errors"), list) else []
            failure_summary = "; ".join(str(error) for error in evidence_errors if isinstance(error, str))
            if not failure_summary:
                failure_summary = string_field(item, "summary") or f"{name} status is {status}"
            if status == "validated" and worker_status != "validated":
                failure_summary = (
                    string_field(item, "worker_summary")
                    or f"{name} worker result status is {worker_status}"
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
                    },
                }
            )
    if not required:
        failures.append(
            {
                "status": "fail",
                "title": "No required validation evidence",
                "summary": "review requires at least one final validation artifact",
            }
        )
    return failures


def validation_gate_summary(validation_failures: list[dict[str, Any]]) -> str:
    names = []
    for failure in validation_failures:
        validation = failure.get("validation") if isinstance(failure.get("validation"), dict) else {}
        name = string_field(validation, "name")
        status = string_field(validation, "status")
        evidence_status = string_field(validation, "evidence_status")
        worker_status = string_field(validation, "worker_status")
        if name and evidence_status and evidence_status != "valid":
            names.append(f"{name} ({evidence_status} evidence)")
        elif name and status and worker_status and status == "validated" and worker_status != "validated":
            names.append(f"{name} (worker {worker_status})")
        elif name and status:
            names.append(f"{name} ({status})")
        elif name:
            names.append(name)
    suffix = ", ".join(names) if names else "required validation"
    return f"required final validation evidence is not validated: {suffix}"


def run_fake_reviewer_command(
    reviewer: dict[str, Any],
    *,
    checkout_path: Path,
    reviewer_request: dict[str, Any],
    request_path: Path,
) -> tuple[dict[str, Any], str | None]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        result_path = temp_path / "reviewer-result.json"
        env = minimal_reviewer_environment(temp_path, config_home=reviewer.get("config_home") or "")
        env.update(reviewer.get("env") or {})
        if reviewer.get("codex_home"):
            env["CODEX_HOME"] = reviewer["codex_home"]
        env["AFK_REVIEWER_REQUEST"] = str(request_path)
        env["AFK_REVIEWER_RESULT"] = str(result_path)
        command = render_command(
            reviewer["command"],
            reviewer_request=reviewer_request,
            request_path=request_path,
            result_path=result_path,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=checkout_path,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=reviewer["timeout_seconds"],
            )
        except OSError as exc:
            raise ReviewerRuntimeError(str(exc), stderr=str(exc), returncode=None) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            raise ReviewerRuntimeError(
                "reviewer command timed out",
                stdout=stdout,
                stderr=stderr or "reviewer command timed out",
                returncode=None,
                timed_out=True,
            ) from exc
        raw_payload = result_path.read_text(encoding="utf-8") if result_path.exists() else None
    if completed.returncode != 0:
        raise ReviewerRuntimeError(
            "reviewer command failed",
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }, raw_payload


def render_command(
    command: list[str],
    *,
    reviewer_request: dict[str, Any],
    request_path: Path,
    result_path: Path,
) -> list[str]:
    replacements = {
        "{prompt}": canonical_json(reviewer_request),
        "{request_path}": str(request_path),
        "{result_path}": str(result_path),
    }
    pattern = re.compile("|".join(sorted((re.escape(token) for token in replacements), key=len, reverse=True)))
    rendered = []
    for part in command:
        rendered.append(pattern.sub(lambda match: replacements[match.group(0)], part))
    return rendered


def minimal_reviewer_environment(temp_path: Path, *, config_home: str = "") -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "PATH",
        "LANG",
        "LC_ALL",
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


def read_reviewer_payload(raw: str | None, *, stdout: str) -> dict[str, Any]:
    if raw is None:
        return read_reviewer_payload_from_stdout(stdout)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "status": "invalid",
            "message": "reviewer result file is not valid JSON",
            "result_source": "reviewer_result_file",
            "result_file_present": True,
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "message": "reviewer result file must contain an object",
            "result_source": "reviewer_result_file",
            "result_file_present": True,
        }
    return {
        "status": "valid",
        "payload": redact_artifact_value(payload),
        "result_source": "reviewer_result_file",
        "result_file_present": True,
    }


def read_reviewer_payload_from_stdout(stdout: str) -> dict[str, Any]:
    stripped_stdout = stdout.strip()
    if not stripped_stdout:
        return {
            "status": "missing",
            "message": "reviewer result file was not produced",
            "result_source": "stdout_fallback",
            "result_file_present": False,
        }
    try:
        payload = json.loads(stripped_stdout)
    except json.JSONDecodeError:
        return {
            "status": "invalid",
            "message": "reviewer stdout is not valid JSON",
            "result_source": "stdout_fallback",
            "result_file_present": False,
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "message": "reviewer stdout must contain a JSON object",
            "result_source": "stdout_fallback",
            "result_file_present": False,
        }
    if not stdout_payload_matches_schema(payload):
        return {
            "status": "invalid",
            "message": "reviewer stdout JSON must match the reviewer result schema",
            "result_source": "stdout_fallback",
            "result_file_present": False,
        }
    return {
        "status": "valid",
        "payload": redact_artifact_value(payload),
        "result_source": "stdout_fallback",
        "result_file_present": False,
    }


def stdout_payload_matches_schema(payload: dict[str, Any]) -> bool:
    if string_field(payload, "artifact_type") != "reviewer-result":
        return False
    raw_status = string_field(payload, "status") or ""
    if raw_status not in {"pass", "fail", "request_revision"}:
        return False
    if string_field(payload, "summary") is None:
        return False
    return isinstance(payload.get("findings"), list)


def normalize_reviewer_payload(
    payload: dict[str, Any],
    *,
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
    result_source: str,
    result_file_present: bool,
) -> dict[str, Any]:
    raw_status = string_field(payload, "status") or ""
    if raw_status in {"pass", "passed", "success", "succeeded"}:
        status = "passed"
        classification = "success"
    elif raw_status in {"fail", "failed"}:
        status = "failed"
        classification = "review_failure"
    elif raw_status in {"request_revision", "request-revision", "request_changes", "needs_revision"}:
        status = "request_revision"
        classification = "review_revision_requested"
    else:
        status = "failed_protocol"
        classification = "protocol_failure"
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    summary = string_field(payload, "summary") or status
    return normalized_reviewer_result(
        status=status,
        classification=classification,
        summary=summary,
        findings=findings,
        adapter=adapter,
        stdout=stdout,
        stderr=stderr,
        result_source=result_source,
        result_file_present=result_file_present,
    )


def normalized_reviewer_result(
    *,
    status: str,
    classification: str,
    summary: str,
    findings: list[Any],
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
    result_source: str | None = None,
    result_file_present: bool | None = None,
) -> dict[str, Any]:
    evidence = {
        "stdout_path": "stdout.log",
        "stderr_path": "stderr.log",
        "stdout_excerpt": stdout[-2000:],
        "stderr_excerpt": stderr[-2000:],
    }
    if result_source is not None:
        evidence["result_source"] = result_source
    if result_file_present is not None:
        evidence["result_file_present"] = result_file_present
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "classification": classification,
        "summary": redact_text(summary),
        "findings": redact_artifact_value(findings),
        "adapter": adapter,
        "evidence": evidence,
    }


def adapter_record(reviewer: dict[str, Any], returncode: int | None, timed_out: bool) -> dict[str, Any]:
    return {
        "type": reviewer["type"],
        "returncode": returncode,
        "timed_out": timed_out,
    }


def write_review_artifacts(run_dir: Path, run_id: str, reviewer_result: dict[str, Any]) -> None:
    write_json(
        run_dir / "reviewer-result.json",
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "step": "review",
            "artifact_type": "reviewer-result",
            "result": reviewer_result,
        },
    )
    (run_dir / "review-summary.md").write_text(review_summary_markdown(reviewer_result), encoding="utf-8")


def review_summary_markdown(reviewer_result: dict[str, Any]) -> str:
    lines = [
        "# Final Review",
        "",
        f"Status: {reviewer_result['status']}",
        f"Classification: {reviewer_result['classification']}",
        "",
        reviewer_result["summary"],
        "",
        "## Findings",
        "",
    ]
    findings = reviewer_result.get("findings", [])
    if not findings:
        lines.append("- None")
    for finding in findings:
        if not isinstance(finding, dict):
            lines.append(f"- {redact_text(str(finding))}")
            continue
        status = string_field(finding, "status") or "unknown"
        title = string_field(finding, "title") or string_field(finding, "summary") or "Finding"
        lines.append(f"- [{redact_text(status)}] {redact_text(title)}")
    lines.append("")
    return "\n".join(lines)


def review_output(
    request: dict[str, Any],
    evidence_pack: dict[str, Any],
    reviewer_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": reviewer_result["status"],
        "classification": reviewer_result["classification"],
        "summary": reviewer_result["summary"],
        "work_item": request["work_item"],
        "work_selection": request["work_selection"],
        "checkout": request["checkout"],
        "implementation": request["implementation"],
        "validation": request["validation"],
        "guardrails": request["guardrails"],
        "cleanup": request["cleanup"],
        "redaction": evidence_pack["redaction"],
        "reviewer_result": reviewer_result,
        "artifacts": review_artifacts(),
    }


def review_artifacts() -> dict[str, str]:
    return {
        "evidence_pack": "evidence-pack.json",
        "reviewer_request": "reviewer-request.json",
        "reviewer_result": "reviewer-result.json",
        "review_summary": "review-summary.md",
    }


def git(checkout_path: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=checkout_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReviewerRuntimeError(
            "git metadata command failed",
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
    return completed.stdout.strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(canonical_json(redact_artifact_value(payload)) + "\n", encoding="utf-8")


def write_adapter_logs(stdout: str, stderr: str) -> None:
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)


def command_secret_error_message(command: list[str]) -> str | None:
    for part in command:
        if is_secret_command_flag(part):
            flag = part.strip().split("=", 1)[0].lower()
            return f"reviewer.command must not include credential flag {flag}"
    return None


def string_list_field(value: dict[str, Any], key: str) -> list[str] | None:
    items = value.get(key, [])
    if not is_string_list(items):
        return None
    return list(items)


def string_field(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if isinstance(item, str) and item.strip():
        return item.strip()
    return None


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
