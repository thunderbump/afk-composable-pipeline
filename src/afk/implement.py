from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.redaction import redact_artifact_value, redact_text


SCHEMA_VERSION = 1


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def implement_step(context: Any) -> dict[str, Any]:
    return implement(
        context.input_data,
        project_contract=context.project_contract,
        run_id=context.run_id,
    )


def implement(
    input_data: Any,
    *,
    project_contract: Any = None,
    run_id: str,
) -> dict[str, Any]:
    request = normalize_request(input_data, project_contract=project_contract, run_id=run_id)
    if request["status"] != "valid":
        return request

    checkout_path = Path(request["checkout"]["path"])
    agent_result_path = checkout_path / request["agent"]["result_path"]
    stale_result = remove_existing_agent_result(agent_result_path, checkout_path)
    capsule = build_job_capsule(request, run_id=run_id)
    if stale_result["status"] != "valid":
        normalized = normalized_agent_result(
            status="failed_protocol",
            classification="protocol_failure",
            summary=stale_result["message"],
            notes=[],
            failures=[{"type": "protocol", "message": stale_result["message"]}],
            adapter={"type": request["agent"]["type"], "returncode": None},
            stdout="",
            stderr="",
        )
        return implement_output(capsule, normalized, fallback_git_metadata(request["checkout"]["start_commit"]))

    checkout_preflight = validate_checkout_provenance(checkout_path, request["checkout"]["start_commit"])
    if checkout_preflight["status"] != "valid":
        normalized = normalized_agent_result(
            status="failed_protocol",
            classification="protocol_failure",
            summary=checkout_preflight["message"],
            notes=[],
            failures=[{"type": "protocol", "message": checkout_preflight["message"]}],
            adapter={"type": request["agent"]["type"], "returncode": None},
            stdout="",
            stderr="",
        )
        return implement_output(capsule, normalized, checkout_preflight["metadata"])

    try:
        adapter_result = run_fake_pi_command(request["agent"], checkout_path, capsule)
    except AgentRuntimeError as exc:
        stdout = redact_text(exc.stdout)
        stderr = redact_text(exc.stderr or exc.message)
        write_adapter_logs(stdout, stderr)
        after_metadata = git_metadata(checkout_path, request["checkout"]["start_commit"])
        normalized = normalized_agent_result(
            status="failed_runtime",
            classification="runtime_failure",
            summary=exc.message,
            notes=[],
            failures=[{"type": "runtime", "message": exc.message}],
            adapter={"type": request["agent"]["type"], "returncode": exc.returncode},
            stdout=stdout,
            stderr=stderr,
        )
        return implement_output(capsule, normalized, after_metadata)

    stdout = redact_text(adapter_result["stdout"])
    stderr = redact_text(adapter_result["stderr"])
    write_adapter_logs(stdout, stderr)

    agent_payload = read_agent_payload(
        checkout_path,
        request["agent"]["result_path"],
        cleanup=True,
    )
    if agent_payload["status"] != "valid":
        after_metadata = git_metadata(checkout_path, request["checkout"]["start_commit"])
        normalized = normalized_agent_result(
            status="failed_protocol",
            classification="protocol_failure",
            summary=agent_payload["message"],
            notes=[],
            failures=[{"type": "protocol", "message": agent_payload["message"]}],
            adapter={"type": request["agent"]["type"], "returncode": adapter_result["returncode"]},
            stdout=stdout,
            stderr=stderr,
        )
        return implement_output(capsule, normalized, after_metadata)

    after_metadata = git_metadata(checkout_path, request["checkout"]["start_commit"])
    normalized = normalize_agent_payload(
        agent_payload["payload"],
        adapter={"type": request["agent"]["type"], "returncode": adapter_result["returncode"]},
        stdout=stdout,
        stderr=stderr,
    )
    return implement_output(capsule, normalized, after_metadata)


def normalize_request(input_data: Any, *, project_contract: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        return invalid_request("request must be an object")

    work_item = selected_work_item(input_data.get("work_selection"), input_data.get("work_index", 0))
    if work_item["status"] != "valid":
        return invalid_request(work_item["message"])

    checkout = normalize_checkout(input_data.get("checkout"))
    if checkout["status"] != "valid":
        return invalid_request(checkout["message"])

    guardrails = normalize_string_list(input_data.get("guardrails", []), "guardrails")
    if guardrails["status"] != "valid":
        return invalid_request(guardrails["message"])

    validation = normalize_validation(input_data.get("validation", {}), project_contract)
    if validation["status"] != "valid":
        return invalid_request(validation["message"])

    agent = normalize_agent(input_data.get("agent"))
    if agent["status"] != "valid":
        return invalid_request(agent["message"])

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "work_item": work_item["work_item"],
        "checkout": checkout["checkout"],
        "guardrails": guardrails["items"],
        "validation": validation["validation"],
        "agent": agent["agent"],
        "run_id": run_id,
    }


def invalid_request(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed_invalid_payload",
        "message": message,
    }


def selected_work_item(work_selection: Any, work_index: Any) -> dict[str, Any]:
    if not isinstance(work_selection, dict):
        return {"status": "invalid", "message": "work_selection must be an object"}
    selected_work = work_selection.get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return {"status": "invalid", "message": "work_selection.selected_work must be a non-empty list"}
    if isinstance(work_index, bool) or not isinstance(work_index, int):
        return {"status": "invalid", "message": "work_index must be an integer"}
    if work_index < 0 or work_index >= len(selected_work):
        return {"status": "invalid", "message": "work_index is outside selected_work"}
    work_item = selected_work[work_index]
    if not isinstance(work_item, dict):
        return {"status": "invalid", "message": "selected work item must be an object"}
    external_id = string_field(work_item, "external_id")
    title = string_field(work_item, "title")
    source_id = string_field(work_item, "source_id")
    source_type = string_field(work_item, "source_type")
    if not external_id:
        return {"status": "invalid", "message": "selected work item external_id is required"}
    if not source_id or not source_type:
        return {"status": "invalid", "message": "selected work item source identity is required"}
    labels = string_list_field(work_item, "labels")
    acceptance_criteria = string_list_field(work_item, "acceptance_criteria")
    dependencies = relation_list(work_item.get("dependencies", []))
    blockers = relation_list(work_item.get("blockers", []))
    if labels is None:
        return {"status": "invalid", "message": "selected work item labels must be a list of strings"}
    if acceptance_criteria is None:
        return {
            "status": "invalid",
            "message": "selected work item acceptance_criteria must be a list of strings",
        }
    if dependencies is None:
        return {"status": "invalid", "message": "selected work item dependencies must be a list"}
    if blockers is None:
        return {"status": "invalid", "message": "selected work item blockers must be a list"}
    normalized = {
        "source_id": source_id,
        "source_type": source_type,
        "external_id": external_id,
        "url": string_field(work_item, "url") or "",
        "title": title or "",
        "status": string_field(work_item, "status") or "",
        "labels": labels,
        "parent": work_item.get("parent"),
        "workstream": work_item.get("workstream"),
        "acceptance_criteria": acceptance_criteria,
        "dependencies": dependencies,
        "blockers": blockers,
        "dependency_status": string_field(work_item, "dependency_status") or "",
        "afk": dict(work_item.get("afk") or {}) if isinstance(work_item.get("afk") or {}, dict) else {},
    }
    return {"status": "valid", "work_item": redact_artifact_value(normalized)}


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
    normalized = {
        "path": str(checkout_path),
        "review_branch": string_field(checkout, "review_branch") or "",
        "requested_ref": string_field(checkout, "requested_ref") or "",
        "start_commit": start_commit,
    }
    return {"status": "valid", "checkout": normalized}


def normalize_validation(validation: Any, project_contract: Any) -> dict[str, Any]:
    if not isinstance(validation, dict):
        return {"status": "invalid", "message": "validation must be an object"}
    profile = string_field(validation, "profile") or ""
    commands = validation.get("commands", [])
    if not isinstance(commands, list):
        return {"status": "invalid", "message": "validation.commands must be a list"}
    normalized_commands = []
    for command in commands:
        if not is_string_list(command):
            return {"status": "invalid", "message": "validation.commands must contain command lists"}
        normalized_commands.append(list(command))
    available_profiles = []
    if project_contract is not None:
        available_profiles = list(project_contract.validation_profiles)
    return {
        "status": "valid",
        "validation": {
            "profile": profile,
            "commands": normalized_commands,
            "available_profiles": available_profiles,
        },
    }


def normalize_agent(agent: Any) -> dict[str, Any]:
    if not isinstance(agent, dict):
        return {"status": "invalid", "message": "agent must be an object"}
    if agent.get("type") != "fake-pi-command":
        return {"status": "invalid", "message": "agent.type must be fake-pi-command"}
    for forbidden_key in ("credentials_path", "auth_file", "token", "api_key", "env"):
        if forbidden_key in agent:
            return {"status": "invalid", "message": f"agent.{forbidden_key} is not supported"}
    command = agent.get("command")
    if not is_string_list(command):
        return {"status": "invalid", "message": "agent.command must be a list of strings"}
    result_path = string_field(agent, "result_path") or "agent-result.json"
    if result_path_error(result_path) is not None:
        return {"status": "invalid", "message": result_path_error(result_path)}
    timeout_seconds = agent.get("timeout_seconds", 120)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return {"status": "invalid", "message": "agent.timeout_seconds must be a positive number"}
    return {
        "status": "valid",
        "agent": {
            "type": "fake-pi-command",
            "command": list(command),
            "result_path": result_path,
            "timeout_seconds": float(timeout_seconds),
        },
    }


def normalize_string_list(value: Any, field: str) -> dict[str, Any]:
    if not is_string_list(value):
        return {"status": "invalid", "message": f"{field} must be a list of strings"}
    return {"status": "valid", "items": list(value)}


def build_job_capsule(request: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "work_item": request["work_item"],
        "acceptance_criteria": request["work_item"]["acceptance_criteria"],
        "checkout": request["checkout"],
        "guardrails": request["guardrails"],
        "validation": request["validation"],
        "expected_result_schema": {
            "status": "completed|target_failed",
            "summary": "string",
            "notes": "list[string]",
            "failures": "list[object]",
        },
    }


def run_fake_pi_command(
    agent: dict[str, Any],
    checkout_path: Path,
    capsule: dict[str, Any],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        capsule_path = Path(temp_dir) / "job-capsule.json"
        capsule_path.write_text(canonical_json(capsule) + "\n", encoding="utf-8")
        env = minimal_agent_environment()
        env["AFK_JOB_CAPSULE"] = str(capsule_path)
        try:
            completed = subprocess.run(
                agent["command"],
                cwd=checkout_path,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=agent["timeout_seconds"],
            )
        except OSError as exc:
            raise AgentRuntimeError(str(exc), stderr=str(exc), returncode=None) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            raise AgentRuntimeError(
                "agent command timed out",
                stdout=stdout,
                stderr=stderr or "agent command timed out",
                returncode=None,
            ) from exc
    if completed.returncode != 0:
        raise AgentRuntimeError(
            "agent command failed",
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def minimal_agent_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def remove_existing_agent_result(agent_result_path: Path, checkout_path: Path) -> dict[str, Any]:
    try:
        agent_result_path.resolve(strict=False).relative_to(checkout_path.resolve())
    except ValueError:
        return {"status": "invalid", "message": "agent result_path escaped checkout"}
    if not agent_result_path.exists():
        return {"status": "valid"}
    try:
        if not agent_result_path.is_file():
            return {"status": "invalid", "message": "agent result_path exists but is not a file"}
        agent_result_path.unlink()
    except OSError:
        return {"status": "invalid", "message": "pre-existing agent result file could not be removed"}
    return {"status": "valid"}


def read_agent_payload(
    checkout_path: Path,
    result_path: str,
    *,
    cleanup: bool,
) -> dict[str, Any]:
    path = (checkout_path / result_path).resolve(strict=False)
    try:
        path.relative_to(checkout_path.resolve())
    except ValueError:
        return {"status": "invalid", "message": "agent result_path escaped checkout"}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "invalid", "message": "agent result file was not produced"}
    if cleanup:
        try:
            path.unlink()
        except OSError:
            pass
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "invalid", "message": "agent result file is not valid JSON"}
    if not isinstance(payload, dict):
        return {"status": "invalid", "message": "agent result file must contain an object"}
    return {"status": "valid", "payload": redact_artifact_value(payload)}


def normalize_agent_payload(
    payload: dict[str, Any],
    *,
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    raw_status = string_field(payload, "status") or ""
    failure_type = string_field(payload, "failure_type") or string_field(payload, "classification") or ""
    if raw_status in {"completed", "succeeded", "success"}:
        status = "implemented"
        classification = "success"
    elif raw_status in {"target_failed", "failed_target"} or failure_type == "target":
        status = "failed_target"
        classification = "target_failure"
    else:
        status = "failed_protocol"
        classification = "protocol_failure"
    summary = string_field(payload, "summary") or status
    notes = string_list_field(payload, "notes") or []
    failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
    return normalized_agent_result(
        status=status,
        classification=classification,
        summary=summary,
        notes=notes,
        failures=failures,
        adapter=adapter,
        stdout=stdout,
        stderr=stderr,
    )


def normalized_agent_result(
    *,
    status: str,
    classification: str,
    summary: str,
    notes: list[str],
    failures: list[Any],
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "classification": classification,
        "summary": summary,
        "notes": notes,
        "failures": redact_artifact_value(failures),
        "adapter": adapter,
        "evidence": {
            "stdout_path": "stdout.log",
            "stderr_path": "stderr.log",
            "stdout_excerpt": stdout[-2000:],
            "stderr_excerpt": stderr[-2000:],
        },
    }


def implement_output(
    capsule: dict[str, Any],
    agent_result: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": agent_result["status"],
        "classification": agent_result["classification"],
        "summary": agent_result["summary"],
        "work_item": capsule["work_item"],
        "checkout": capsule["checkout"],
        "git": metadata,
        "agent_result": agent_result,
        "job_capsule": capsule,
        "artifacts": {"job_capsule": "job-capsule.json", "agent_result": "agent-result.json"},
    }


def validate_checkout_provenance(checkout_path: Path, start_commit: str) -> dict[str, Any]:
    try:
        resolved_start = git(checkout_path, ["rev-parse", "--verify", f"{start_commit}^{{commit}}"])
        head = git(checkout_path, ["rev-parse", "HEAD"])
        dirty_lines = [line for line in git(checkout_path, ["status", "--porcelain=v1"]).splitlines() if line]
    except AgentRuntimeError as exc:
        return {
            "status": "invalid",
            "message": "checkout provenance could not be verified",
            "metadata": fallback_git_metadata(start_commit),
        }
    if head != resolved_start:
        return {
            "status": "invalid",
            "message": "checkout HEAD does not match checkout.start_commit",
            "metadata": {
                **fallback_git_metadata(start_commit),
                "after_commit": head,
            },
        }
    if dirty_lines:
        return {
            "status": "invalid",
            "message": "checkout has uncommitted changes before agent execution",
            "metadata": {
                **fallback_git_metadata(start_commit),
                "after_commit": head,
                "dirty": True,
                "dirty_status": dirty_lines,
                "changed_files": dirty_files(dirty_lines),
            },
        }
    return {
        "status": "valid",
        "metadata": {
            **fallback_git_metadata(start_commit),
            "after_commit": head,
        },
    }


def fallback_git_metadata(start_commit: str) -> dict[str, Any]:
    return {
        "before_commit": start_commit,
        "after_commit": start_commit,
        "changed_files": [],
        "dirty": False,
        "dirty_status": [],
        "commits": [],
        "diff_stat": "",
    }


def git_metadata(checkout_path: Path, before_commit: str) -> dict[str, Any]:
    after_commit = git(checkout_path, ["rev-parse", "HEAD"])
    dirty_lines = [line for line in git(checkout_path, ["status", "--porcelain=v1"]).splitlines() if line]
    changed_files = sorted(set(committed_files(checkout_path, before_commit, after_commit) + dirty_files(dirty_lines)))
    return {
        "before_commit": before_commit,
        "after_commit": after_commit,
        "changed_files": changed_files,
        "dirty": bool(dirty_lines),
        "dirty_status": dirty_lines,
        "commits": commit_records(checkout_path, before_commit, after_commit),
        "diff_stat": git(checkout_path, ["diff", "--stat", before_commit, after_commit]),
    }


def committed_files(checkout_path: Path, before_commit: str, after_commit: str) -> list[str]:
    if before_commit == after_commit:
        return []
    return [
        line
        for line in git(checkout_path, ["diff", "--name-only", before_commit, after_commit]).splitlines()
        if line
    ]


def dirty_files(dirty_lines: list[str]) -> list[str]:
    files = []
    for line in dirty_lines:
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            files.append(path)
    return files


def commit_records(checkout_path: Path, before_commit: str, after_commit: str) -> list[dict[str, str]]:
    if before_commit == after_commit:
        return []
    output = git(checkout_path, ["log", "--format=%H%x00%s", f"{before_commit}..{after_commit}"])
    records = []
    for line in output.splitlines():
        if "\0" in line:
            commit, subject = line.split("\0", 1)
            records.append({"commit": commit, "subject": subject})
    return records


def git(checkout_path: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=checkout_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentRuntimeError(
            "git metadata command failed",
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
    return completed.stdout.strip()


def write_adapter_logs(stdout: str, stderr: str) -> None:
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)


def result_path_error(result_path: str) -> str | None:
    path = Path(result_path)
    if path.is_absolute():
        return "agent.result_path must be relative"
    if any(part in {"", ".", ".."} for part in path.parts):
        return "agent.result_path must stay inside the checkout"
    return None


def relation_list(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    return [redact_artifact_value(item) for item in value]


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
