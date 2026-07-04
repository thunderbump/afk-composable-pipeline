from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.redaction import (
    is_secret_command_flag,
    is_secret_key,
    is_secret_value,
    key_components,
    normalize_exact_secrets,
    redact_artifact_value,
    redact_text,
)
from afk.role_adapters import (
    RoleAdapterRuntimeError,
    execute_role_command,
    minimal_command_environment,
    read_json_result_file,
    redact_adapter_streams,
    render_command,
    write_adapter_logs,
)


SCHEMA_VERSION = 1
WRAPPER_SECRET_FILE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SECRET_REF_LOGICAL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
MIN_RUNTIME_SECRET_LENGTH = 4
PI_JOB_PROMPT_PLACEHOLDER = "{prompt}"


AgentRuntimeError = RoleAdapterRuntimeError


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
            adapter=adapter_metadata(request["agent"]["type"], returncode=None),
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
            adapter=adapter_metadata(request["agent"]["type"], returncode=None),
            stdout="",
            stderr="",
        )
        return implement_output(capsule, normalized, checkout_preflight["metadata"])

    runtime_redaction = read_wrapper_secret_redaction_set(request["agent"].get("wrapper_secret_files", {}))
    if runtime_redaction["status"] != "valid":
        normalized = normalized_agent_result(
            status="failed_protocol",
            classification="protocol_failure",
            summary=runtime_redaction["message"],
            notes=[],
            failures=[{"type": "protocol", "message": runtime_redaction["message"]}],
            adapter=adapter_metadata(request["agent"]["type"], returncode=None),
            stdout="",
            stderr="",
        )
        return implement_output(capsule, normalized, fallback_git_metadata(request["checkout"]["start_commit"]))
    exact_secrets = runtime_redaction["exact_secrets"]

    try:
        adapter_result = run_agent_command(request["agent"], checkout_path, capsule)
    except AgentRuntimeError as exc:
        stdout, stderr = redact_adapter_streams(
            stdout=exc.stdout,
            stderr=exc.stderr or exc.message,
            exact_secrets=exact_secrets,
        )
        write_adapter_logs(stdout, stderr)
        after_metadata = safe_git_metadata(checkout_path, request["checkout"]["start_commit"])
        summary = runtime_failure_summary(exc.message, stdout=stdout, stderr=stderr)
        normalized = normalized_agent_result(
            status="failed_runtime",
            classification="runtime_failure",
            summary=summary,
            notes=[],
            failures=[{"type": "runtime", "message": summary}],
            adapter=adapter_metadata(
                request["agent"]["type"],
                returncode=exc.returncode,
                timed_out=exc.timed_out,
                configured_timeout_seconds=exc.configured_timeout_seconds,
                elapsed_seconds=exc.elapsed_seconds,
            ),
            stdout=stdout,
            stderr=stderr,
        )
        return implement_output(capsule, normalized, after_metadata)

    stdout, stderr = redact_adapter_streams(
        stdout=adapter_result["stdout"],
        stderr=adapter_result["stderr"],
        exact_secrets=exact_secrets,
    )
    write_adapter_logs(stdout, stderr)

    agent_payload = read_agent_payload(
        checkout_path,
        request["agent"]["result_path"],
        cleanup=True,
        exact_secrets=exact_secrets,
    )
    if agent_payload["status"] != "valid":
        after_metadata = safe_git_metadata(checkout_path, request["checkout"]["start_commit"])
        normalized = normalized_agent_result(
            status="failed_protocol",
            classification="protocol_failure",
            summary=agent_payload["message"],
            notes=[],
            failures=[{"type": "protocol", "message": agent_payload["message"]}],
            adapter=adapter_metadata(
                request["agent"]["type"],
                returncode=adapter_result["returncode"],
                timed_out=adapter_result.get("timed_out", False),
                configured_timeout_seconds=adapter_result.get("configured_timeout_seconds"),
                elapsed_seconds=adapter_result.get("elapsed_seconds"),
            ),
            stdout=stdout,
            stderr=stderr,
        )
        return implement_output(capsule, normalized, after_metadata)

    after_metadata = safe_git_metadata(checkout_path, request["checkout"]["start_commit"])
    normalized = normalize_agent_payload(
        agent_payload["payload"],
        adapter=adapter_metadata(
            request["agent"]["type"],
            returncode=adapter_result["returncode"],
            timed_out=adapter_result.get("timed_out", False),
            configured_timeout_seconds=adapter_result.get("configured_timeout_seconds"),
            elapsed_seconds=adapter_result.get("elapsed_seconds"),
        ),
        stdout=stdout,
        stderr=stderr,
    )
    normalized = require_commit_for_implemented_result(
        normalized,
        after_metadata,
        adapter=adapter_metadata(
            request["agent"]["type"],
            returncode=adapter_result["returncode"],
            timed_out=adapter_result.get("timed_out", False),
            configured_timeout_seconds=adapter_result.get("configured_timeout_seconds"),
            elapsed_seconds=adapter_result.get("elapsed_seconds"),
        ),
        stdout=stdout,
        stderr=stderr,
    )
    return implement_output(capsule, normalized, after_metadata)


def adapter_metadata(
    agent_type: str,
    *,
    returncode: int | None,
    timed_out: bool = False,
    configured_timeout_seconds: float | None = None,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    adapter = {
        "type": agent_type,
        "returncode": returncode,
    }
    if configured_timeout_seconds is not None:
        adapter["configured_timeout_seconds"] = configured_timeout_seconds
    if elapsed_seconds is not None:
        adapter["elapsed_seconds"] = elapsed_seconds
    if timed_out:
        adapter["timed_out"] = True
    return adapter


def normalize_request(input_data: Any, *, project_contract: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        return invalid_request("request must be an object")

    work_scope = string_field(input_data, "work_scope") or "item"
    if work_scope not in {"item", "selection"}:
        return invalid_request("work_scope must be item or selection")
    if work_scope == "selection":
        work_item = selected_work_item_for_selection(input_data.get("work_selection"))
    else:
        work_item = selected_work_item(input_data.get("work_selection"), input_data.get("work_index", 0))
    if work_item["status"] != "valid":
        return invalid_request(work_item["message"])

    checkout = normalize_checkout(input_data.get("checkout"))
    if checkout["status"] != "valid":
        return invalid_request(checkout["message"])

    guardrails = normalize_string_list(input_data.get("guardrails", []), "guardrails")
    if guardrails["status"] != "valid":
        return invalid_request(guardrails["message"])

    validation = normalize_validation(
        input_data.get("validation", {}),
        project_contract,
        checkout_path=Path(checkout["checkout"]["path"]),
    )
    if validation["status"] != "valid":
        return invalid_request(validation["message"])

    agent = normalize_agent(input_data.get("agent"), checkout_path=Path(checkout["checkout"]["path"]))
    if agent["status"] != "valid":
        return invalid_request(agent["message"])

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "work_item": work_item["work_item"],
        "selected_work": work_item["selected_work"],
        "checkout": checkout["checkout"],
        "guardrails": guardrails["items"],
        "validation": validation["validation"],
        "agent": agent["agent"],
        "repair_context": normalize_repair_context(input_data.get("repair_context")),
        "run_id": run_id,
    }


def invalid_request(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed_invalid_payload",
        "message": message,
    }


def normalize_repair_context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return redact_artifact_value(value)


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
    redacted = redact_artifact_value(normalized)
    return {"status": "valid", "work_item": redacted, "selected_work": [redacted]}


def selected_work_item_for_selection(work_selection: Any) -> dict[str, Any]:
    if not isinstance(work_selection, dict):
        return {"status": "invalid", "message": "work_selection must be an object"}
    selected_work = work_selection.get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return {"status": "invalid", "message": "work_selection.selected_work must be a non-empty list"}
    normalized_items = []
    for index in range(len(selected_work)):
        item = selected_work_item(work_selection, index)
        if item["status"] != "valid":
            return item
        normalized_items.append(item["work_item"])
    combined = combined_selected_work_item(normalized_items)
    return {
        "status": "valid",
        "work_item": redact_artifact_value(combined),
        "selected_work": redact_artifact_value(normalized_items),
    }


def combined_selected_work_item(selected_work: list[dict[str, Any]]) -> dict[str, Any]:
    external_ids = [string_field(item, "external_id") or "" for item in selected_work]
    source_types = sorted({string_field(item, "source_type") or "" for item in selected_work})
    source_ids = sorted({string_field(item, "source_id") or "" for item in selected_work})
    acceptance_criteria = []
    for item in selected_work:
        item_id = string_field(item, "external_id") or "selected item"
        for criterion in item.get("acceptance_criteria", []):
            if isinstance(criterion, str):
                acceptance_criteria.append(f"{item_id}: {criterion}")
    return {
        "source_id": ",".join(source_ids),
        "source_type": ",".join(source_types),
        "external_id": ",".join(external_ids),
        "url": "",
        "title": f"Combined selected work ({len(selected_work)} items)",
        "status": "",
        "labels": sorted({label for item in selected_work for label in item.get("labels", []) if isinstance(label, str)}),
        "parent": selected_work[0].get("parent"),
        "workstream": selected_work[0].get("workstream"),
        "acceptance_criteria": acceptance_criteria,
        "dependencies": [],
        "blockers": [],
        "dependency_status": "",
        "afk": {"combined_selection": True},
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
    normalized = {
        "path": str(checkout_path),
        "review_branch": string_field(checkout, "review_branch") or "",
        "requested_ref": string_field(checkout, "requested_ref") or "",
        "start_commit": start_commit,
    }
    return {"status": "valid", "checkout": normalized}


def normalize_validation(validation: Any, project_contract: Any, *, checkout_path: Path) -> dict[str, Any]:
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
    run_commands_marker = validation.get("run_commands_during_implementation")
    if run_commands_marker is not None and not isinstance(run_commands_marker, bool):
        return {
            "status": "invalid",
            "message": "validation.run_commands_during_implementation must be a boolean",
        }
    if run_commands_marker is False and normalized_commands:
        return {
            "status": "invalid",
            "message": "validation.run_commands_during_implementation=false contradicts non-empty validation.commands",
        }
    if run_commands_marker is True and not normalized_commands:
        return {
            "status": "invalid",
            "message": "validation.run_commands_during_implementation=true requires non-empty validation.commands",
        }
    normalized_run_commands = run_commands_marker if isinstance(run_commands_marker, bool) else bool(normalized_commands)
    worker_home = normalize_optional_validation_path(
        string_field(validation, "worker_home") or string_field(validation, "workerHome"),
        field="validation.worker_home",
        checkout_path=checkout_path,
    )
    if worker_home["status"] != "valid":
        return {"status": "invalid", "message": worker_home["message"]}
    stack = normalize_validation_stack(validation.get("stack"), checkout_path=checkout_path)
    if stack["status"] != "valid":
        return {"status": "invalid", "message": stack["message"]}
    available_profiles = []
    if project_contract is not None:
        available_profiles = list(project_contract.validation_profiles)
    has_pipeline_stack = stack["stack"] is not None
    normalized_validation = {
        "status": "valid",
        "validation": {
            "profile": profile,
            "commands": normalized_commands,
            "available_profiles": available_profiles,
            "run_commands_during_implementation": normalized_run_commands,
            "pipeline_validate_step_runs_stack": has_pipeline_stack,
            "implementation_instructions": validation_implementation_instructions(
                commands=normalized_commands,
                has_pipeline_stack=has_pipeline_stack,
            ),
        },
    }
    if worker_home["path"]:
        normalized_validation["validation"]["worker_home"] = worker_home["path"]
    if stack["stack"] is not None:
        normalized_validation["validation"]["stack"] = stack["stack"]
    return normalized_validation


def normalize_optional_validation_path(
    value: Any,
    *,
    field: str,
    checkout_path: Path,
) -> dict[str, Any]:
    if value is None or value == "":
        return {"status": "valid", "path": ""}
    if not isinstance(value, str) or not value.strip():
        return {"status": "invalid", "message": f"{field} must be an absolute path outside checkout"}
    path = Path(value)
    if not path.is_absolute():
        return {"status": "invalid", "message": f"{field} must be absolute"}
    if path_is_equal_to_or_inside(path, checkout_path):
        return {"status": "invalid", "message": f"{field} must be outside checkout"}
    return {"status": "valid", "path": str(path)}


def normalize_validation_stack(value: Any, *, checkout_path: Path) -> dict[str, Any]:
    if value is None:
        return {"status": "valid", "stack": None}
    if not isinstance(value, dict):
        return {"status": "invalid", "message": "validation.stack must be an object"}
    role = string_field(value, "role") or "validation"
    path = normalize_optional_validation_path(value.get("path"), field="validation.stack.path", checkout_path=checkout_path)
    if path["status"] != "valid":
        return path
    if not path["path"]:
        return {"status": "invalid", "message": "validation.stack.path is required"}
    return {"status": "valid", "stack": {"role": role, "path": path["path"]}}


def validation_implementation_instructions(*, commands: list[list[str]], has_pipeline_stack: bool) -> list[str]:
    instructions = []
    if commands:
        instructions.append("Run validation.commands during implementation before finishing when your changes affect them.")
    else:
        instructions.append("No implementation-time validation commands were provided.")
    if has_pipeline_stack:
        instructions.append(
            "Leave stack validation to the pipeline validate step; do not guess alternate validation stack paths."
        )
    return instructions


def normalize_agent(agent: Any, *, checkout_path: Path) -> dict[str, Any]:
    if not isinstance(agent, dict):
        return {"status": "invalid", "message": "agent must be an object"}
    agent_type = agent.get("type")
    if agent_type not in {"fake-pi-command", "real-agent-command"}:
        return {"status": "invalid", "message": "agent.type must be fake-pi-command or real-agent-command"}
    fake_agent = agent_type == "fake-pi-command"
    forbidden_keys = ("credentials_path", "auth_file", "token", "api_key")
    if fake_agent:
        forbidden_keys = (
            *forbidden_keys,
            "env",
            "codex_home",
            "config_home",
            "provider",
            "wrapper_secret_files",
            "secret_refs",
        )
    for forbidden_key in forbidden_keys:
        if forbidden_key in agent:
            return {"status": "invalid", "message": f"agent.{forbidden_key} is not supported"}
    command = agent.get("command")
    if not is_string_list(command):
        return {"status": "invalid", "message": "agent.command must be a list of strings"}
    command_secret_error = agent_command_secret_error(command)
    if command_secret_error:
        return {"status": "invalid", "message": command_secret_error}
    result_path = string_field(agent, "result_path") or "agent-result.json"
    if result_path_error(result_path) is not None:
        return {"status": "invalid", "message": result_path_error(result_path)}
    timeout_seconds = agent.get("timeout_seconds", 120)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return {"status": "invalid", "message": "agent.timeout_seconds must be a positive number"}
    normalized_agent: dict[str, Any] = {
        "type": agent_type,
        "command": list(command),
        "result_path": result_path,
        "timeout_seconds": float(timeout_seconds),
        "provider": "",
        "env": {},
        "codex_home": "",
        "config_home": "",
        "wrapper_secret_files": {},
        "secret_refs": {},
    }
    if not fake_agent:
        provider = agent.get("provider")
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            return {"status": "invalid", "message": "agent.provider must be a non-empty string"}
        normalized_provider = provider.strip() if isinstance(provider, str) else ""
        required_env_keys = {"PI_CONFIG_HOME"}
        if normalized_provider == "openai-codex":
            required_env_keys.add("PI_CODING_AGENT_DIR")
        env = normalize_agent_env(
            agent.get("env", {}),
            checkout_path=checkout_path,
            required_keys=required_env_keys,
        )
        if env["status"] != "valid":
            return {"status": "invalid", "message": env["message"]}
        codex_home = normalize_absolute_dir(
            agent.get("codex_home"),
            "agent.codex_home",
            checkout_path=checkout_path,
            required=True,
        )
        if codex_home["status"] != "valid":
            return {"status": "invalid", "message": codex_home["message"]}
        config_home = normalize_absolute_dir(
            agent.get("config_home"),
            "agent.config_home",
            checkout_path=checkout_path,
            required=True,
        )
        if config_home["status"] != "valid":
            return {"status": "invalid", "message": config_home["message"]}
        wrapper_secret_files = normalize_wrapper_secret_files(
            agent.get("wrapper_secret_files", {}),
            checkout_path=checkout_path,
        )
        if wrapper_secret_files["status"] != "valid":
            return {"status": "invalid", "message": wrapper_secret_files["message"]}
        secret_refs = normalize_secret_refs(agent.get("secret_refs", {}))
        if secret_refs["status"] != "valid":
            return {"status": "invalid", "message": secret_refs["message"]}
        normalized_agent["provider"] = normalized_provider
        normalized_agent["env"] = env["env"]
        normalized_agent["codex_home"] = codex_home["path"]
        normalized_agent["config_home"] = config_home["path"]
        normalized_agent["wrapper_secret_files"] = wrapper_secret_files["files"]
        normalized_agent["secret_refs"] = secret_refs["secret_refs"]
    return {
        "status": "valid",
        "agent": normalized_agent,
    }


def normalize_agent_env(
    env: Any,
    *,
    checkout_path: Path,
    required_keys: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(env, dict):
        return {"status": "invalid", "message": "agent.env must be an object"}
    normalized = {}
    required = set(required_keys or ())
    seen = set()
    reserved = {"AFK_JOB_CAPSULE", "AFK_AGENT_RESULT_PATH", "HOME", "XDG_CONFIG_HOME"}
    for key, value in env.items():
        if not isinstance(key, str) or not key:
            return {"status": "invalid", "message": "agent.env keys must be non-empty strings"}
        if key in required:
            seen.add(key)
        if key in reserved:
            return {"status": "invalid", "message": f"agent.env must not override {key}"}
        if is_secret_key(key):
            return {"status": "invalid", "message": f"agent.env must not include secret variable {key}"}
        if not isinstance(value, str):
            return {"status": "invalid", "message": f"agent.env.{key} must be a string"}
        if is_secret_value(value):
            return {"status": "invalid", "message": f"agent.env.{key} must not include a secret-looking value"}
        unsafe_path = unsafe_agent_env_path(
            key,
            value,
            checkout_path,
            required=(key in required),
        )
        if unsafe_path is not None:
            return {"status": "invalid", "message": unsafe_path}
        normalized[key] = value
    missing = sorted(required.difference(seen))
    if missing:
        return {
            "status": "invalid",
            "message": "agent.env must include " + ", ".join(missing),
        }
    return {"status": "valid", "env": normalized}


def unsafe_agent_env_path(
    key: str,
    value: str,
    checkout_path: Path,
    required: bool = False,
) -> str | None:
    if not is_config_state_env_key(key):
        return None
    if not required and "://" in value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return f"agent.env.{key} must be an absolute path outside checkout"
    if path_is_equal_to_or_inside(path, checkout_path):
        return f"agent.env.{key} must be outside checkout"
    if path_is_required_existing_directory_mount(key) and not path.is_dir():
        return f"agent.env.{key} must be an existing directory"
    return None


def is_config_state_env_key(key: str) -> bool:
    if key.upper() == "PI_CODING_AGENT_DIR":
        return True
    components = key_components(key)
    return any(component in {"cache", "config", "home", "path", "session", "state"} for component in components)


def path_is_required_existing_directory_mount(key: str) -> bool:
    return key.upper() in {"PI_CONFIG_HOME", "PI_CODING_AGENT_DIR"}


def normalize_absolute_dir(
    value: Any,
    field: str,
    *,
    checkout_path: Path | None = None,
    required: bool = False,
) -> dict[str, Any]:
    if value is None:
        if required:
            return {"status": "invalid", "message": f"{field} is required"}
        return {"status": "valid", "path": ""}
    if not isinstance(value, str) or not value.strip():
        return {"status": "invalid", "message": f"{field} must be an absolute directory path"}
    path = Path(value)
    if not path.is_absolute():
        return {"status": "invalid", "message": f"{field} must be absolute"}
    if not path.is_dir():
        return {"status": "invalid", "message": f"{field} must be an existing directory"}
    if checkout_path is not None and path_is_equal_to_or_inside(path, checkout_path):
        return {"status": "invalid", "message": f"{field} must be outside checkout"}
    return {"status": "valid", "path": str(path)}


def normalize_wrapper_secret_files(
    value: Any,
    *,
    checkout_path: Path,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "invalid", "message": "agent.wrapper_secret_files must be an object"}
    normalized: dict[str, str] = {}
    for key, raw_path in value.items():
        if not isinstance(key, str) or not WRAPPER_SECRET_FILE_NAME_PATTERN.match(key):
            return {
                "status": "invalid",
                "message": "agent.wrapper_secret_files keys must match [A-Za-z][A-Za-z0-9_-]*",
            }
        if is_secret_key(key):
            return {
                "status": "invalid",
                "message": f"agent.wrapper_secret_files.{key} must use a non-secret logical name",
            }
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {
                "status": "invalid",
                "message": f"agent.wrapper_secret_files.{key} must be an absolute file path outside checkout",
            }
        path = Path(raw_path)
        if not path.is_absolute():
            return {
                "status": "invalid",
                "message": f"agent.wrapper_secret_files.{key} must be an absolute file path outside checkout",
            }
        if path_is_equal_to_or_inside(path, checkout_path):
            return {
                "status": "invalid",
                "message": f"agent.wrapper_secret_files.{key} must be outside checkout",
            }
        if not path.is_file():
            return {
                "status": "invalid",
                "message": f"agent.wrapper_secret_files.{key} must be an existing file",
            }
        normalized[key] = str(path)
    return {"status": "valid", "files": normalized}


def read_wrapper_secret_redaction_set(wrapper_secret_files: dict[str, str]) -> dict[str, Any]:
    exact_secrets: set[str] = set()
    for key, raw_path in wrapper_secret_files.items():
        path = Path(raw_path)
        try:
            raw_value = path.read_text(encoding="utf-8")
        except OSError as exc:
            detail = exc.strerror or str(exc)
            return {
                "status": "invalid",
                "message": f"agent.wrapper_secret_files.{key} could not be read at runtime from {path}: {detail}",
            }
        except UnicodeDecodeError as exc:
            return {
                "status": "invalid",
                "message": (
                    f"agent.wrapper_secret_files.{key} could not be read at runtime from {path}: "
                    f"expected UTF-8 text ({exc})"
                ),
            }
        exact_secrets.update(wrapper_secret_redaction_candidates(raw_value))
    return {"status": "valid", "exact_secrets": normalize_exact_secrets(exact_secrets)}


def normalize_secret_refs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "invalid", "message": "agent.secret_refs must be an object"}
    normalized: dict[str, dict[str, dict[str, str]]] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not SECRET_REF_LOGICAL_NAME_PATTERN.match(key):
            return {
                "status": "invalid",
                "message": "agent.secret_refs keys must match [A-Za-z][A-Za-z0-9_-]*",
            }
        if is_secret_key(key):
            return {
                "status": "invalid",
                "message": f"agent.secret_refs.{key} must use a non-secret logical name",
            }
        if not isinstance(entry, dict):
            return {
                "status": "invalid",
                "message": f"agent.secret_refs.{key} must be an object containing only secretRef",
            }
        if "value" in entry:
            return {
                "status": "invalid",
                "message": f"agent.secret_refs.{key} must not include plaintext secret fields",
            }
        if set(entry) != {"secretRef"}:
            return {
                "status": "invalid",
                "message": f"agent.secret_refs.{key} must be an object containing only secretRef",
            }
        secret_ref = normalize_secret_ref(entry["secretRef"], field=f"agent.secret_refs.{key}.secretRef")
        if secret_ref["status"] != "valid":
            return {"status": "invalid", "message": secret_ref["message"]}
        normalized[key] = {"secretRef": secret_ref["secret_ref"]}
    return {"status": "valid", "secret_refs": normalized}


def normalize_secret_ref(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "invalid", "message": f"{field} must be an object"}
    allowed_fields = {"provider", "name", "key"}
    extra_fields = set(value).difference(allowed_fields)
    if extra_fields:
        return {
            "status": "invalid",
            "message": f"{field} must only contain provider, name, and key",
        }
    normalized: dict[str, str] = {}
    for required_field in ("provider", "name", "key"):
        raw_value = value.get(required_field)
        if raw_value is None:
            return {"status": "invalid", "message": f"{field}.{required_field} is required"}
        if not isinstance(raw_value, str) or not raw_value.strip():
            return {
                "status": "invalid",
                "message": f"{field}.{required_field} must be a non-empty string",
            }
        if is_secret_value(raw_value):
            return {
                "status": "invalid",
                "message": f"{field}.{required_field} must not include a secret-looking value",
            }
        normalized[required_field] = raw_value
    return {"status": "valid", "secret_ref": normalized}


def wrapper_secret_redaction_candidates(raw_value: str) -> set[str]:
    candidates: set[str] = set()
    stripped = raw_value.strip()
    if len(stripped) >= MIN_RUNTIME_SECRET_LENGTH:
        candidates.add(stripped)
    for line in raw_value.splitlines():
        line_value = line.strip()
        if len(line_value) >= MIN_RUNTIME_SECRET_LENGTH:
            candidates.add(line_value)
    return candidates


def path_is_equal_to_or_inside(path: Path, parent: Path) -> bool:
    for candidate in (path, path.resolve()):
        try:
            candidate.relative_to(parent.resolve())
            return True
        except ValueError:
            pass
    return False


def normalize_string_list(value: Any, field: str) -> dict[str, Any]:
    if not is_string_list(value):
        return {"status": "invalid", "message": f"{field} must be a list of strings"}
    return {"status": "valid", "items": list(value)}


def build_job_capsule(request: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    agent_mounts = {
        "codex_home": request["agent"].get("codex_home", ""),
        "config_home": request["agent"].get("config_home", ""),
        "pi_config_home": request["agent"].get("env", {}).get("PI_CONFIG_HOME", ""),
    }
    wrapper_secret_files = request["agent"].get("wrapper_secret_files", {})
    if wrapper_secret_files:
        agent_mounts["wrapper_secret_files"] = wrapper_secret_files
    secret_refs = request["agent"].get("secret_refs", {})
    if secret_refs:
        agent_mounts["secret_refs"] = secret_refs
    commit_required = request["agent"].get("type") == "real-agent-command"
    instructions = [
        "Write a JSON object matching expected_result_schema to AFK_AGENT_RESULT_PATH before exiting.",
        "Use status=completed only when acceptance criteria are satisfied.",
        "Use status=target_failed with failures when the requested work cannot be completed.",
    ]
    if commit_required:
        instructions.append("Commit successful code changes on the review branch before exiting.")
    capsule = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "work_item": request["work_item"],
        "work_selection": {"schema_version": SCHEMA_VERSION, "selected_work": request["selected_work"]},
        "acceptance_criteria": request["work_item"]["acceptance_criteria"],
        "checkout": request["checkout"],
        "agent_mounts": agent_mounts,
        "guardrails": request["guardrails"],
        "validation": request["validation"],
        "expected_result_schema": {
            "status": "completed|target_failed",
            "summary": "string",
            "notes": "list[string]",
            "failures": "list[object]",
        },
        "completion_contract": {
            "result_path": request["agent"]["result_path"],
            "result_path_env": "AFK_AGENT_RESULT_PATH",
            "write_result_file_before_exit": True,
            "commit_required_for_success": commit_required,
            "instructions": instructions,
        },
    }
    if request.get("repair_context"):
        capsule["repair_context"] = request["repair_context"]
    return capsule


def run_agent_command(
    agent: dict[str, Any],
    checkout_path: Path,
    capsule: dict[str, Any],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        capsule_path = temp_path / "job-capsule.json"
        capsule_path.write_text(canonical_json(capsule) + "\n", encoding="utf-8")
        env = minimal_command_environment(temp_path, config_home=agent.get("config_home") or "")
        add_git_identity_fallback(env, checkout_path)
        env.update(agent.get("env") or {})
        if agent.get("codex_home"):
            env["CODEX_HOME"] = agent["codex_home"]
        env["AFK_JOB_CAPSULE"] = str(capsule_path)
        env["AFK_AGENT_RESULT_PATH"] = agent["result_path"]
        command = render_command(agent["command"], {PI_JOB_PROMPT_PLACEHOLDER: canonical_json(capsule)})
        return execute_role_command(
            command=command,
            cwd=checkout_path,
            env=env,
            timeout_seconds=agent["timeout_seconds"],
            runtime_failure_message="agent command failed",
            timeout_message="agent command timed out",
        )


def add_git_identity_fallback(env: dict[str, str], checkout_path: Path) -> None:
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        if key in env and not env[key]:
            env.pop(key)
    if env.get("GIT_AUTHOR_NAME") and env.get("GIT_AUTHOR_EMAIL"):
        env["GIT_COMMITTER_NAME"] = env.get("GIT_COMMITTER_NAME") or env["GIT_AUTHOR_NAME"]
        env["GIT_COMMITTER_EMAIL"] = env.get("GIT_COMMITTER_EMAIL") or env["GIT_AUTHOR_EMAIL"]
        return
    if git_config_value(checkout_path, "user.name") and git_config_value(checkout_path, "user.email"):
        return
    env["GIT_AUTHOR_NAME"] = env.get("GIT_AUTHOR_NAME") or "AFK Pipeline"
    env["GIT_AUTHOR_EMAIL"] = env.get("GIT_AUTHOR_EMAIL") or "afk-pipeline@example.invalid"
    env["GIT_COMMITTER_NAME"] = env.get("GIT_COMMITTER_NAME") or env["GIT_AUTHOR_NAME"]
    env["GIT_COMMITTER_EMAIL"] = env.get("GIT_COMMITTER_EMAIL") or env["GIT_AUTHOR_EMAIL"]


def git_config_value(checkout_path: Path, key: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "config", "--get", key],
            cwd=checkout_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def remove_existing_agent_result(agent_result_path: Path, checkout_path: Path) -> dict[str, Any]:
    try:
        agent_result_path.parent.resolve(strict=False).relative_to(checkout_path.resolve())
    except ValueError:
        return {"status": "invalid", "message": "agent result_path escaped checkout"}
    if agent_result_path.is_symlink():
        return {"status": "invalid", "message": "agent result_path exists but is a symlink"}
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
    exact_secrets: set[str] | None = None,
) -> dict[str, Any]:
    path = checkout_path / result_path
    try:
        path.parent.resolve(strict=False).relative_to(checkout_path.resolve())
    except ValueError:
        return {"status": "invalid", "message": "agent result_path escaped checkout"}
    if path.is_symlink():
        return {"status": "invalid", "message": "agent result_path is a symlink"}
    result = read_json_result_file(
        path,
        missing_message="agent result file was not produced",
        invalid_json_message="agent result file is not valid JSON",
        invalid_type_message="agent result file must contain an object",
        exact_secrets=exact_secrets,
        cleanup=cleanup,
    )
    if result["status"] == "missing":
        return {"status": "invalid", "message": result["message"]}
    return result


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


def require_commit_for_implemented_result(
    agent_result: dict[str, Any],
    metadata: dict[str, Any],
    *,
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    if agent_result["status"] != "implemented":
        return agent_result
    if adapter.get("type") != "real-agent-command":
        return agent_result
    if metadata.get("metadata_status") == "failed":
        message = "agent reported success but post-run git metadata could not be verified"
        return normalized_agent_result(
            status="failed_protocol",
            classification="protocol_failure",
            summary=message,
            notes=agent_result["notes"],
            failures=[{"type": "protocol", "message": message}],
            adapter=adapter,
            stdout=stdout,
            stderr=stderr,
        )
    if metadata.get("before_commit") != metadata.get("after_commit") and metadata.get("commits"):
        return agent_result
    message = "agent reported success but produced no new commit"
    return normalized_agent_result(
        status="failed_protocol",
        classification="protocol_failure",
        summary=message,
        notes=agent_result["notes"],
        failures=[{"type": "protocol", "message": message}],
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


def runtime_failure_summary(message: str, *, stdout: str, stderr: str) -> str:
    if not runtime_summary_is_generic(message):
        return message
    for text in (stderr, stdout):
        candidate = runtime_failure_excerpt(text)
        if candidate:
            return candidate
    return message


def runtime_summary_is_generic(message: str) -> bool:
    return message.strip() in {"agent command failed", "agent command timed out"}


def runtime_failure_excerpt(text: str) -> str:
    informative_lines = []
    auth_related_lines = []
    error_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(token in lowered for token in ("error:", "failed", "exception", "timed out", "no api key")):
            error_lines.append(line)
            continue
        if any(token in lowered for token in ("oauth", "auth", "api key")):
            auth_related_lines.append(line)
        informative_lines.append(line)
    if error_lines:
        return auth_error_summary(error_lines) or error_lines[0]
    if auth_related_lines:
        return auth_related_lines[0]
    return informative_lines[0] if informative_lines else ""


def auth_error_summary(error_lines: list[str]) -> str:
    for line in error_lines:
        lowered = line.lower()
        if any(token in lowered for token in ("oauth", "auth", "api key", "no api key")):
            return line
    return ""


def implement_output(
    capsule: dict[str, Any],
    agent_result: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    output = {
        "schema_version": SCHEMA_VERSION,
        "status": agent_result["status"],
        "classification": agent_result["classification"],
        "summary": agent_result["summary"],
        "work_item": capsule["work_item"],
        "work_selection": capsule["work_selection"],
        "checkout": capsule["checkout"],
        "git": metadata,
        "agent_result": agent_result,
        "job_capsule": capsule,
        "artifacts": {"job_capsule": "job-capsule.json", "agent_result": "agent-result.json"},
    }
    if capsule.get("repair_context"):
        output["repair_context"] = capsule["repair_context"]
    return output


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


def safe_git_metadata(checkout_path: Path, before_commit: str) -> dict[str, Any]:
    try:
        return git_metadata(checkout_path, before_commit)
    except AgentRuntimeError as exc:
        return {
            **fallback_git_metadata(before_commit),
            "metadata_status": "failed",
            "metadata_error": redact_text(exc.message),
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


def result_path_error(result_path: str) -> str | None:
    if result_path != "agent-result.json":
        return "agent.result_path must be agent-result.json"
    path = Path(result_path)
    if path.is_absolute():
        return "agent.result_path must be relative"
    if any(part in {"", ".", ".."} for part in path.parts):
        return "agent.result_path must stay inside the checkout"
    return None


def agent_command_secret_error(command: list[str]) -> str | None:
    for part in command:
        if is_secret_command_flag(part):
            flag = part.strip().split("=", 1)[0].lower()
            return f"agent.command must not include credential flag {flag}"
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
