from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from afk.contracts import ProjectContract
from afk.implement import runtime_failure_excerpt
from afk.jsonutil import canonical_json, sha256_json
from afk.pi_workers import (
    non_openai_pi_mount_error,
    pi_command_executable,
    openai_codex_pi_mount_error,
    pi_command_provider,
    pi_preflight_command,
    validate_absolute_dir,
)
from afk.redaction import is_secret_command_flag, is_secret_key, is_secret_value, redact_artifact_value, redact_text
from afk.recipes import branch_slug
from afk.registry import StepResult


SCHEMA_VERSION = 1
KNOWN_WORKSTREAM_STEPS = {"select-work", "prepare-checkout", "implement", "validate", "review"}
REVIEW_CYCLE_STATUSES = {"passed", "findings-open", "findings-addressed", "request-changes"}
REVIEW_CYCLE_OPEN_STATUSES = {"findings-open", "request-changes"}
REVIEW_CYCLE_RESPONSE_STATUSES = {"addressed", "findings-addressed"}
TERMINAL_REVIEW_FEEDBACK_STATUSES = {"resolved", "waived"}
COMMAND_BEARER_SECRET_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
PI_JUDGE_PROMPT_PLACEHOLDER = "{prompt}"
PI_JUDGE_REQUEST_PATH_PLACEHOLDER = "{request_path}"
PI_JUDGE_RESULT_PATH_PLACEHOLDER = "{result_path}"
PI_AUTH_PREFLIGHT_PROMPT = "Reply with OK only."
PI_AUTH_PREFLIGHT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class WorkstreamResult:
    run_id: str
    workstream_id: str
    parent: str
    status: str
    result_path: str
    publication_status: str


StepRunner = Callable[[str, Any, Path, ProjectContract | None], StepResult]


class WorkstreamError(ValueError):
    pass


class PublisherError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: list[str],
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.details = details or {}


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


def run_workstream(
    recipe: Any,
    *,
    ledger_dir: Path,
    step_runner: StepRunner,
    parent: str | None = None,
    workstream_id: str | None = None,
    project_contract: ProjectContract | None = None,
) -> WorkstreamResult:
    normalized = normalize_recipe(recipe, parent=parent, workstream_id=workstream_id)
    run_id = new_run_id()
    ledger = WorkstreamLedger(ledger_dir, run_id)
    ledger.prepare()
    ledger.write_json(
        "command.json",
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now(),
            "command": ["afk", "run-workstream"],
            "input": redact_artifact_value(recipe),
            "input_sha256": sha256_json(recipe),
            "workstream_id": normalized["workstream_id"],
            "parent": normalized["parent"],
        },
    )

    state: dict[str, Any] = {
        "selected_work": [],
        "checkout": None,
        "checkout_attempts": [],
        "implementation": None,
        "implementation_selection": [],
        "implementation_result_path": "",
        "validations": [],
        "review": None,
        "review_selection": [],
        "review_result_path": "",
        "cleanup": {"status": "unknown", "resources": []},
        "blocked_reason": "",
        "stop_reason": "",
        "next_allowed_command": "",
    }
    steps = []
    pi_auth_preflight = run_pi_auth_preflight(normalized)
    pi_auth_preflight_status = {
        item["target"]: item["status"]
        for item in pi_auth_preflight.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("target"), str) and isinstance(item.get("status"), str)
    }

    if pi_auth_preflight["status"] != "skipped":
        ledger.write_json("pi-auth-preflight.json", pi_auth_preflight)
    if pi_auth_preflight["status"] == "failed":
        state["blocked_reason"] = pi_auth_preflight["reason"]
    else:
        for index, step_spec in enumerate(normalized["steps"]):
            step_name = step_spec["name"]
            remaining_steps = normalized["steps"][index + 1 :]
            stop_reason = terminal_stop_reason(step_spec, state)
            if stop_reason:
                state["stop_reason"] = stop_reason
                state["next_allowed_command"] = next_allowed_command_for_terminal_stop(state, normalized)
                break
            blocked_reason = workflow_order_blocking_reason(step_name, state, normalized["retry_policy"])
            if blocked_reason:
                state["blocked_reason"] = blocked_reason
                break
            step_input = composed_step_input(step_spec, normalized, state, ledger_dir)
            auth_target = pi_auth_target_for_step(step_name, step_input)
            if auth_target is not None and pi_auth_preflight_status.get(auth_target["target"]) == "deferred":
                auth_result = run_pi_auth_preflight_target(auth_target, cwd=checkout_path_from_state(state))
                pi_auth_preflight["results"].append(auth_result)
                pi_auth_preflight = summarize_pi_auth_preflight(pi_auth_preflight["results"])
                pi_auth_preflight_status[auth_target["target"]] = auth_result["status"]
                ledger.write_json("pi-auth-preflight.json", pi_auth_preflight)
                if auth_result["status"] == "failed":
                    state["blocked_reason"] = pi_auth_preflight_failure_reason(auth_result)
                    break
            step_profile = step_spec.get("profile")
            equivalent_command = equivalent_run_step_command(
                step_name,
                step_input,
                ledger_dir,
                profile=step_profile,
                project_contract=project_contract,
            )
            result = step_runner(step_name, step_input, ledger_dir, project_contract)
            step_record = step_execution_record(
                step_name,
                result,
                equivalent_command,
                ledger_dir,
            )
            steps.append(step_record)
            update_state_from_step(state, step_name, result, ledger_dir)
            blocked_reason = blocking_reason_for_step(step_name, result, remaining_steps)
            if blocked_reason:
                state["blocked_reason"] = blocked_reason
                break

    state["cleanup"] = final_cleanup_state(state)

    publication: dict[str, Any]
    selected_work = []
    if state["blocked_reason"]:
        publication = blocked_publication(state["blocked_reason"], normalized, run_id)
        status = publication["status"]
    else:
        publication_gate = publication_gate_reason(state)
        if publication_gate:
            if state["stop_reason"] and has_current_validated_evidence(state):
                publication = validated_unpublished_publication(
                    state["stop_reason"],
                    next_allowed_command=state["next_allowed_command"] or rerun_workstream_command(normalized),
                )
            else:
                publication = blocked_publication(publication_gate, normalized, run_id)
            status = publication["status"]
        else:
            if tracker_terminal_decision_present(normalized):
                if tracker_terminal_decision_allows_close(normalized):
                    publication = tracker_terminal_decision_publication()
                else:
                    publication = tracker_close_blocked_publication()
            else:
                publication = publish_terminal_pr(
                    normalized["publisher"],
                    normalized=normalized,
                    state=state,
                    steps=steps,
                    selected_work=selected_work_records(state),
                    ledger=ledger,
                )
            status = workstream_status_from_publication(publication)
    tracker = tracker_record(normalized, state, publication)
    selected_work = selected_work_records(state)
    pipeline_retrospective = pipeline_retrospective_record(state, publication, tracker, normalized)
    retrospective_judge_skip_reason = ""
    retrospective_judge_skip_classification = "auth_preflight_failed"
    if state["blocked_reason"].startswith("Pi auth preflight failed for "):
        retrospective_judge_skip_reason = state["blocked_reason"]
    else:
        retrospective_judge_target = pi_auth_preflight_target("retrospective_judge", normalized.get("retrospective_judge"))
        if retrospective_judge_target is not None and pi_auth_preflight_status.get(retrospective_judge_target["target"]) == "deferred":
            checkout_path = prepared_checkout_path_from_state(state)
            if checkout_path is None:
                auth_result = deferred_pi_auth_preflight_result(
                    retrospective_judge_target,
                    summary="checkout-local Pi auth preflight remains deferred because no prepared checkout is available",
                )
                retrospective_judge_skip_reason = auth_result["summary"]
                retrospective_judge_skip_classification = "checkout_unavailable"
            else:
                auth_result = run_pi_auth_preflight_target(
                    retrospective_judge_target,
                    cwd=checkout_path,
                )
            pi_auth_preflight["results"].append(auth_result)
            pi_auth_preflight = summarize_pi_auth_preflight(pi_auth_preflight["results"])
            pi_auth_preflight_status[retrospective_judge_target["target"]] = auth_result["status"]
            ledger.write_json("pi-auth-preflight.json", pi_auth_preflight)
            if auth_result["status"] == "failed":
                retrospective_judge_skip_reason = pi_auth_preflight_failure_reason(auth_result)
                retrospective_judge_skip_classification = "auth_preflight_failed"
    retrospective_judge = _run_retrospective_judge(
        normalized=normalized,
        state=state,
        publication=publication,
        tracker=tracker,
        selected_work=selected_work,
        pipeline_retrospective=pipeline_retrospective,
        ledger=ledger,
        skip_reason=retrospective_judge_skip_reason,
        skip_classification=retrospective_judge_skip_classification,
    )
    pipeline_retrospective = _apply_retrospective_judge(
        pipeline_retrospective,
        retrospective_judge,
        normalized=normalized,
    )
    retrospective_follow_up_creation = _run_retrospective_follow_up(
        normalized=normalized,
        state=state,
        publication=publication,
        tracker=tracker,
        pipeline_retrospective=pipeline_retrospective,
        ledger=ledger,
    )
    pipeline_retrospective = _apply_retrospective_follow_up_creation(
        pipeline_retrospective,
        retrospective_follow_up_creation,
    )

    ledger.write_json("publication-result.json", publication)
    ledger.write_json("tracker-result.json", tracker)
    if normalized["retrospective"]:
        ledger.write_json("retrospective.json", redact_retrospective(normalized["retrospective"]))
    ledger.write_json("pipeline-retrospective.json", pipeline_retrospective)
    result_payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workstream_id": normalized["workstream_id"],
        "parent": normalized["parent"],
        "review_branch": normalized["review_branch"],
        "status": status,
        "review_cycles": redact_review_cycles(normalized["review_cycles"]),
        "retrospective": redact_retrospective(normalized["retrospective"]),
        "steps": steps,
        "selected_work": selected_work,
        "cleanup": state["cleanup"],
        "retry_budget": retry_budget_record(state, normalized["retry_policy"]),
        "retry_attempts": retry_attempt_records(state),
        "retry": publication.get("retry", ""),
        "terminal_reason": publication.get("reason", ""),
        "next_allowed_command": publication.get("next_allowed_command", ""),
        "publication": publication,
        "tracker": tracker,
        "pipeline_retrospective": pipeline_retrospective,
        "artifacts": workstream_artifacts(ledger),
    }
    ledger.write_json("workstream-result.json", result_payload)
    return WorkstreamResult(
        run_id=run_id,
        workstream_id=normalized["workstream_id"],
        parent=normalized["parent"],
        status=status,
        result_path=f"workstreams/{run_id}/workstream-result.json",
        publication_status=publication["status"],
    )


def normalize_recipe(
    recipe: Any,
    *,
    parent: str | None,
    workstream_id: str | None,
) -> dict[str, Any]:
    if not isinstance(recipe, dict):
        raise WorkstreamError("workstream input must be a JSON object")
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkstreamError("workstream input steps must be a non-empty list")
    normalized_steps = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise WorkstreamError("workstream steps must be objects")
        name = string_field(step, "name")
        if not name:
            raise WorkstreamError(f"workstream step {index + 1} name is required")
        if name not in KNOWN_WORKSTREAM_STEPS:
            known = ", ".join(sorted(KNOWN_WORKSTREAM_STEPS))
            raise WorkstreamError(f"unknown workstream step {name!r}; known steps: {known}")
        input_data = step.get("input", {})
        if not isinstance(input_data, dict):
            raise WorkstreamError(f"workstream step {name!r} input must be an object")
        normalized = {"name": name, "input": input_data}
        profile = string_field(step, "profile")
        if profile:
            normalized["profile"] = profile
        normalized_steps.append(normalized)

    resolved_workstream_id = workstream_id or string_field(recipe, "workstream_id")
    if not resolved_workstream_id:
        raise WorkstreamError("--workstream-id or input.workstream_id is required")
    resolved_parent = parent or string_field(recipe, "parent") or ""
    review_branch = string_field(recipe, "review_branch") or f"afk/{resolved_workstream_id}"
    publisher = recipe.get("publisher", {"enabled": False})
    retry_policy = normalize_retry_policy(recipe.get("retry_policy"))
    tracker = normalize_tracker_config(recipe.get("tracker"))
    review_cycles = normalize_review_cycles(recipe.get("review_cycles"))
    retrospective = normalize_retrospective(recipe.get("retrospective"))
    retrospective_follow_up = normalize_retrospective_follow_up_config(recipe.get("retrospective_follow_up"))
    retrospective_judge = normalize_retrospective_judge(
        recipe.get("retrospective_judge"),
        checkout_path=recipe_checkout_path(normalized_steps),
        checkout_paths=recipe_checkout_paths(normalized_steps),
    )
    validate_retrospective_terminal_decision(retrospective, tracker)
    return {
        "schema_version": SCHEMA_VERSION,
        "workstream_id": resolved_workstream_id,
        "parent": resolved_parent,
        "review_branch": review_branch,
        "steps": normalized_steps,
        "publisher": publisher,
        "retry_policy": retry_policy,
        "tracker": tracker,
        "review_cycles": review_cycles,
        "retrospective": retrospective,
        "retrospective_follow_up": retrospective_follow_up,
        "retrospective_judge": retrospective_judge,
    }


def composed_step_input(
    step_spec: dict[str, Any],
    normalized: dict[str, Any],
    state: dict[str, Any],
    ledger_dir: Path,
) -> dict[str, Any]:
    step_name = step_spec["name"]
    input_data = dict(step_spec["input"])
    if step_name == "prepare-checkout":
        input_data["review_branch"] = normalized["review_branch"]
        checkout = state.get("checkout")
        if isinstance(checkout, dict):
            current_checkout_path = string_field(checkout, "checkout_path")
            next_checkout_path = string_field(input_data, "checkout_path")
            requested_ref = string_field(checkout, "start_commit") or string_field(checkout, "requested_ref")
            has_explicit_requested_ref = bool(string_field(input_data, "requested_ref") or string_field(input_data, "ref"))
            if (
                requested_ref
                and current_checkout_path
                and current_checkout_path == next_checkout_path
                and not has_explicit_requested_ref
            ):
                input_data["requested_ref"] = requested_ref
    elif step_name == "implement":
        input_data["work_selection"] = {"schema_version": SCHEMA_VERSION, "selected_work": state["selected_work"]}
        if selected_work_count(state) > 1 and "work_index" not in input_data and "work_scope" not in input_data:
            input_data["work_scope"] = "selection"
        if state.get("checkout") is not None:
            input_data["checkout"] = state["checkout"]
        else:
            input_data.pop("checkout", None)
    elif step_name == "validate":
        if state.get("checkout") is not None:
            input_data["checkout"] = state["checkout"]
        profile = step_spec.get("profile")
        if profile:
            validation = input_data.get("validation", {})
            if not isinstance(validation, dict):
                validation = {}
            input_data["validation"] = {**validation, "profile": profile}
    elif step_name == "review":
        if state["selected_work"]:
            implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
            input_data["work_item"] = implementation.get("work_item") or state["selected_work"][0]
            input_data["work_selection"] = implementation_work_selection(implementation, state)
        if state.get("checkout") is not None:
            input_data["checkout"] = state["checkout"]
        if state.get("implementation") is not None:
            input_data["implementation"] = state["implementation"]
        validation = input_data.get("validation", {})
        if not isinstance(validation, dict):
            validation = {}
        input_data["validation"] = {
            **validation,
            "required_artifacts": validation_artifact_refs(state, ledger_dir),
        }
        input_data.setdefault("cleanup", {"status": "clean", "resources": []})
    return input_data


def recipe_checkout_path(steps: list[dict[str, Any]]) -> Path | None:
    checkout_paths = recipe_checkout_paths(steps)
    if checkout_paths:
        return checkout_paths[0]
    return None


def recipe_checkout_paths(steps: list[dict[str, Any]]) -> list[Path]:
    checkout_paths = []
    for step in steps:
        if step.get("name") != "prepare-checkout":
            continue
        input_data = step.get("input")
        if not isinstance(input_data, dict):
            continue
        checkout_path = string_field(input_data, "checkout_path")
        if checkout_path:
            checkout_paths.append(Path(checkout_path))
    return checkout_paths


def equivalent_run_step_command(
    step_name: str,
    input_data: Any,
    ledger_dir: Path,
    *,
    profile: str | None,
    project_contract: ProjectContract | None,
) -> list[str]:
    redacted_input = redact_artifact_value(input_data)
    command = ["afk", "run-step", step_name, "--input", canonical_json(redacted_input), "--ledger", str(ledger_dir)]
    if profile:
        command.extend(["--profile", profile])
    if project_contract is not None:
        command.extend(["--project", project_contract.project_slug])
    return command


def run_pi_auth_preflight(normalized: dict[str, Any]) -> dict[str, Any]:
    results = []
    for target in pi_auth_preflight_targets(normalized):
        result = run_pi_auth_preflight_target(target, defer_checkout_local=True)
        results.append(result)
        if result["status"] == "failed":
            return summarize_pi_auth_preflight(results)
    return summarize_pi_auth_preflight(results)


def summarize_pi_auth_preflight(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"schema_version": SCHEMA_VERSION, "status": "skipped", "results": []}
    failed_result = next((item for item in results if item.get("status") == "failed"), None)
    if failed_result is not None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "reason": pi_auth_preflight_failure_reason(failed_result),
            "results": results,
        }
    if any(item.get("status") == "passed" for item in results):
        status = "passed"
    elif any(item.get("status") == "deferred" for item in results):
        status = "deferred"
    else:
        status = "skipped"
    return {"schema_version": SCHEMA_VERSION, "status": status, "results": results}


def pi_auth_preflight_failure_reason(result: dict[str, Any]) -> str:
    return f"Pi auth preflight failed for {result['target']}: {result['summary']}"


def pi_auth_preflight_targets(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    targets = []
    for step in normalized["steps"]:
        step_name = step["name"]
        input_data = step["input"]
        if step_name == "implement":
            target = pi_auth_preflight_target("implement.agent", input_data.get("agent"))
            if target is not None:
                targets.append(target)
        elif step_name == "review":
            target = pi_auth_preflight_target("review.reviewer", input_data.get("reviewer"))
            if target is not None:
                targets.append(target)
    target = pi_auth_preflight_target("retrospective_judge", normalized.get("retrospective_judge"))
    if target is not None:
        targets.append(target)
    return targets


def pi_auth_preflight_target(target_name: str, config: Any) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        return None
    command = config.get("command")
    if not _is_string_list(command):
        return None
    command_list = list(command)
    if pi_command_provider(command_list) != "openai-codex":
        return None
    timeout_seconds = PI_AUTH_PREFLIGHT_TIMEOUT_SECONDS
    raw_timeout = config.get("timeout_seconds")
    if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) and raw_timeout > 0:
        timeout_seconds = min(float(raw_timeout), PI_AUTH_PREFLIGHT_TIMEOUT_SECONDS)
    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    target = {
        "target": target_name,
        "command": command_list,
        "codex_home": string_field(config, "codex_home") or "",
        "config_home": string_field(config, "config_home") or "",
        "env": {key: str(value) for key, value in env.items() if isinstance(value, str)},
        "timeout_seconds": timeout_seconds,
        "defer_until_checkout": pi_auth_preflight_requires_checkout(command_list),
    }
    if target_name == "implement.agent":
        target["job_capsule"] = _implement_preflight_job_capsule(config)
    return target


def pi_auth_preflight_requires_checkout(command: list[str]) -> bool:
    executable = pi_command_executable(command)
    if not executable:
        return False
    path = Path(executable)
    return not path.is_absolute() and ("/" in executable or executable.startswith("."))


def run_pi_auth_preflight_target(
    target: dict[str, Any],
    *,
    cwd: Path | None = None,
    defer_checkout_local: bool = False,
) -> dict[str, Any]:
    if defer_checkout_local and target.get("defer_until_checkout"):
        return {
            "target": target["target"],
            "provider": "openai-codex",
            "status": "deferred",
            "summary": "checkout-local Pi command deferred until prepare-checkout completes",
            "command": redact_artifact_value(target["command"]),
            "returncode": None,
            "stdout_excerpt": "",
            "stderr_excerpt": "",
        }
    with tempfile.TemporaryDirectory(prefix="afk-pi-auth-") as temp_dir:
        temp_path = Path(temp_dir)
        env = _minimal_retrospective_judge_environment(temp_path, config_home=target["config_home"])
        env.update(target["env"])
        if target["codex_home"]:
            env["CODEX_HOME"] = target["codex_home"]
        env["AFK_PI_AUTH_PREFLIGHT"] = "1"
        _seed_pi_auth_preflight_runtime_env(target, env=env, temp_path=temp_path)
        rendered_command = _render_pi_auth_preflight_command(target, temp_path=temp_path)
        command = pi_preflight_command(rendered_command, prompt=PI_AUTH_PREFLIGHT_PROMPT) or [
            part.replace(PI_JUDGE_PROMPT_PLACEHOLDER, PI_AUTH_PREFLIGHT_PROMPT)
            for part in rendered_command
        ]
        run_cwd = cwd or temp_path
        try:
            completed = subprocess.run(
                command,
                cwd=run_cwd,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=target["timeout_seconds"],
            )
        except OSError as exc:
            message = redact_text(str(exc))
            return {
                "target": target["target"],
                "provider": "openai-codex",
                "status": "failed",
                "summary": message,
                "command": redact_artifact_value(command),
                "returncode": None,
                "stdout_excerpt": "",
                "stderr_excerpt": message,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = redact_text(exc.stdout if isinstance(exc.stdout, str) else "")
            stderr = redact_text(exc.stderr if isinstance(exc.stderr, str) else "")
            return {
                "target": target["target"],
                "provider": "openai-codex",
                "status": "failed",
                "summary": "pi auth preflight timed out",
                "command": redact_artifact_value(command),
                "returncode": None,
                "stdout_excerpt": stdout[-2000:],
                "stderr_excerpt": stderr[-2000:],
            }
        stdout = redact_text(completed.stdout)
        stderr = redact_text(completed.stderr)
        summary = runtime_failure_excerpt(stderr) or runtime_failure_excerpt(stdout) or "pi auth preflight failed"
        return {
            "target": target["target"],
            "provider": "openai-codex",
            "status": "passed" if completed.returncode == 0 else "failed",
            "summary": "authenticated" if completed.returncode == 0 else summary,
            "command": redact_artifact_value(command),
            "returncode": completed.returncode,
            "stdout_excerpt": stdout[-2000:],
            "stderr_excerpt": stderr[-2000:],
        }


def deferred_pi_auth_preflight_result(target: dict[str, Any], *, summary: str) -> dict[str, Any]:
    return {
        "target": target["target"],
        "provider": "openai-codex",
        "status": "deferred",
        "summary": summary,
        "command": redact_artifact_value(target["command"]),
        "returncode": None,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }


def _render_pi_auth_preflight_command(target: dict[str, Any], *, temp_path: Path) -> list[str]:
    command = list(target["command"])
    if target["target"] == "review.reviewer":
        return _render_retrospective_command(
            command,
            {
                PI_JUDGE_REQUEST_PATH_PLACEHOLDER: str(temp_path / "reviewer-request.json"),
                PI_JUDGE_RESULT_PATH_PLACEHOLDER: str(temp_path / "reviewer-result.json"),
            },
        )
    if target["target"] == "retrospective_judge":
        return _render_retrospective_command(
            command,
            {
                PI_JUDGE_REQUEST_PATH_PLACEHOLDER: str(temp_path / "retrospective-judge-request.json"),
                PI_JUDGE_RESULT_PATH_PLACEHOLDER: str(temp_path / "retrospective-judge-result.json"),
            },
        )
    return command


def _implement_preflight_job_capsule(config: Any) -> dict[str, Any]:
    agent_mounts = {
        "codex_home": string_field(config, "codex_home") or "",
        "config_home": string_field(config, "config_home") or "",
        "pi_config_home": (
            string_field(config.get("env"), "PI_CONFIG_HOME") if isinstance(config, dict) else ""
        )
        or "",
    }
    if isinstance(config, dict):
        wrapper_secret_files = config.get("wrapper_secret_files")
        if isinstance(wrapper_secret_files, dict) and wrapper_secret_files:
            agent_mounts["wrapper_secret_files"] = {
                key: str(value) for key, value in wrapper_secret_files.items() if isinstance(value, str)
            }
        secret_refs = config.get("secret_refs")
        if isinstance(secret_refs, dict) and secret_refs:
            agent_mounts["secret_refs"] = json.loads(canonical_json(secret_refs))
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "job-capsule",
        "agent_mounts": agent_mounts,
        "completion_contract": {
            "result_path": "agent-result.json",
            "result_path_env": "AFK_AGENT_RESULT_PATH",
            "write_result_file_before_exit": True,
        },
    }


def _seed_pi_auth_preflight_runtime_env(target: dict[str, Any], *, env: dict[str, str], temp_path: Path) -> None:
    target_name = target["target"]
    if target_name == "implement.agent":
        capsule_path = temp_path / "job-capsule.json"
        capsule = target.get("job_capsule")
        if isinstance(capsule, dict):
            capsule_path.write_text(canonical_json(capsule) + "\n", encoding="utf-8")
        else:
            capsule_path.write_text('{"artifact_type":"job-capsule"}\n', encoding="utf-8")
        env["AFK_JOB_CAPSULE"] = str(capsule_path)
        env["AFK_AGENT_RESULT_PATH"] = "agent-result.json"
        return
    if target_name == "review.reviewer":
        request_path = temp_path / "reviewer-request.json"
        request_path.write_text('{"artifact_type":"reviewer-request"}\n', encoding="utf-8")
        env["AFK_REVIEWER_REQUEST"] = str(request_path)
        env["AFK_REVIEWER_RESULT"] = str(temp_path / "reviewer-result.json")
        return
    if target_name == "retrospective_judge":
        request_path = temp_path / "retrospective-judge-request.json"
        request_path.write_text('{"artifact_type":"retrospective-judge-request"}\n', encoding="utf-8")
        env["AFK_RETROSPECTIVE_JUDGE_REQUEST"] = str(request_path)
        env["AFK_RETROSPECTIVE_JUDGE_RESULT"] = str(temp_path / "retrospective-judge-result.json")


def step_execution_record(
    step_name: str,
    result: StepResult,
    equivalent_command: list[str],
    ledger_dir: Path,
) -> dict[str, Any]:
    output_status = ""
    if isinstance(result.output, dict):
        output_status = str(result.output.get("status") or "")
    run_result_path = f"runs/{result.run_id}/step-result.json"
    return {
        "name": step_name,
        "run_id": result.run_id,
        "status": result.status,
        "output_status": output_status,
        "result_path": run_result_path,
        "result_abspath": str((ledger_dir / run_result_path).resolve(strict=False)),
        "equivalent_command": redact_artifact_value(equivalent_command),
    }


def pi_auth_target_for_step(step_name: str, step_input: Any) -> dict[str, Any] | None:
    if step_name == "implement" and isinstance(step_input, dict):
        return pi_auth_preflight_target("implement.agent", step_input.get("agent"))
    if step_name == "review" and isinstance(step_input, dict):
        return pi_auth_preflight_target("review.reviewer", step_input.get("reviewer"))
    return None


def update_state_from_step(
    state: dict[str, Any],
    step_name: str,
    result: StepResult,
    ledger_dir: Path,
) -> None:
    output = result.output if isinstance(result.output, dict) else {}
    if step_name == "select-work":
        previous_identity = current_selected_work_selection_identity(state)
        selected = output.get("selected_work")
        state["selected_work"] = list(selected) if isinstance(selected, list) else []
        if previous_identity and current_selected_work_selection_identity(state) != previous_identity:
            reset_cycle_state_for_new_selection(state)
    elif step_name == "prepare-checkout":
        if output.get("status") == "prepared" or state.get("checkout") is None:
            state["checkout"] = output
        append_checkout_attempt(state, output)
        if output.get("status") == "prepared" and implemented_after_commit(state):
            state["validations"] = []
            state["review"] = None
            state["review_selection"] = []
            state["review_result_path"] = ""
    elif step_name == "implement":
        state["implementation"] = output
        state["implementation_selection"] = output_selected_work(output, state)
        state["implementation_result_path"] = f"runs/{result.run_id}/step-result.json"
        state["checkout"] = checkout_after_implementation(state.get("checkout"), output)
        update_checkout_attempt_after_implementation(state, output)
        state["review"] = None
        state["review_selection"] = []
        state["review_result_path"] = ""
        if output.get("status") == "implemented":
            state["validations"] = []
    elif step_name == "validate":
        state["validations"].append(
            {
                "run_id": result.run_id,
                "output": output,
                "selected_work": snapshot_work_selection(state.get("implementation_selection")),
                "step_result_path": str((ledger_dir / "runs" / result.run_id / "step-result.json").resolve(strict=False)),
                "worker_result_path": str((ledger_dir / "runs" / result.run_id / "worker-result.json").resolve(strict=False)),
            }
        )
        update_checkout_attempt_after_validation(state, output)
    elif step_name == "review":
        state["review"] = output
        state["review_selection"] = output_selected_work(output, state)
        state["review_result_path"] = f"runs/{result.run_id}/step-result.json"
        cleanup = output.get("cleanup")
        if isinstance(cleanup, dict):
            state["cleanup"] = cleanup


def workflow_order_blocking_reason(
    step_name: str,
    state: dict[str, Any],
    retry_policy: dict[str, int],
) -> str:
    if step_name == "prepare-checkout":
        retry_block = retry_prepare_checkout_blocking_reason(state, retry_policy)
        if retry_block:
            return retry_block
    if step_name == "validate" and not implemented_after_commit(state):
        return "validate requires implementation evidence before final validation"
    if step_name == "review":
        if not implemented_after_commit(state):
            return "review requires implementation evidence before final review"
        if not state["validations"]:
            return "review requires final validation evidence after implementation"
        implemented_commit = implemented_after_commit(state)
        if implemented_commit and not any(
            validation_checkout_commit(validation) == implemented_commit for validation in state["validations"]
        ):
            return "review requires final validation evidence for implemented HEAD"
    return ""


def terminal_stop_reason(step_spec: dict[str, Any], state: dict[str, Any]) -> str:
    step_name = step_spec["name"]
    if review_passed(state):
        return f"workstream already reached terminal review state before {step_name}; no further workstream steps are allowed"
    if step_name in {"select-work", "prepare-checkout", "implement"} and has_current_validated_evidence(state):
        if step_name == "prepare-checkout" and review_failed(state):
            return ""
        if step_name == "select-work" and select_work_proves_different_item(step_spec.get("input"), state):
            return ""
        return (
            f"workstream reached validated terminal state before {step_name}; "
            "do not start a fresh work cycle for the same work item"
        )
    return ""


def next_allowed_command_for_terminal_stop(state: dict[str, Any], normalized: dict[str, Any]) -> str:
    if review_passed(state) or has_current_validated_evidence(state):
        return rerun_workstream_command(normalized)
    return "none"


def selected_work_count(state: dict[str, Any]) -> int:
    selected_work = state.get("selected_work")
    return len(selected_work) if isinstance(selected_work, list) else 0


def snapshot_work_selection(selected_work: Any) -> list[dict[str, Any]]:
    if not isinstance(selected_work, list):
        return []
    return [dict(item) for item in selected_work if isinstance(item, dict)]


def snapshot_selected_work(state: dict[str, Any]) -> list[dict[str, Any]]:
    return snapshot_work_selection(state.get("selected_work"))


def output_selected_work(output: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    work_selection = output.get("work_selection") if isinstance(output.get("work_selection"), dict) else {}
    selected_work = work_selection.get("selected_work")
    if isinstance(selected_work, list) and selected_work:
        return snapshot_work_selection(selected_work)
    state_selection = snapshot_selected_work(state)
    if state_selection:
        return state_selection
    work_item = output.get("work_item")
    if isinstance(work_item, dict):
        return [dict(work_item)]
    return []


def implementation_work_selection(implementation: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    work_selection = implementation.get("work_selection") if isinstance(implementation.get("work_selection"), dict) else {}
    selected_work = work_selection.get("selected_work")
    if isinstance(selected_work, list) and selected_work:
        return {"schema_version": SCHEMA_VERSION, "selected_work": snapshot_work_selection(selected_work)}
    work_item = implementation.get("work_item")
    if isinstance(work_item, dict):
        return {"schema_version": SCHEMA_VERSION, "selected_work": [dict(work_item)]}
    return {"schema_version": SCHEMA_VERSION, "selected_work": snapshot_selected_work(state)}


def current_selected_work_selection_identity(state: dict[str, Any]) -> str:
    identities = selected_work_identity_set(state.get("selected_work"))
    if not identities:
        return ""
    return "|".join(sorted(identities))


def current_selected_work_identity(state: dict[str, Any]) -> str:
    selected_work = state.get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return ""
    first_item = selected_work[0]
    if not isinstance(first_item, dict):
        return ""
    return work_item_identity(first_item)


def work_item_identity(item: dict[str, Any]) -> str:
    source_id = string_field(item, "source_id") or ""
    source_type = string_field(item, "source_type") or ""
    external_id = string_field(item, "external_id") or ""
    if source_id and source_type and external_id:
        return f"{source_type}:{source_id}:{external_id}"
    return string_field(item, "url") or external_id


def work_item_aliases(item: dict[str, Any]) -> set[str]:
    aliases = set()
    identity = work_item_identity(item)
    if identity:
        aliases.add(identity)
    url = string_field(item, "url")
    if url:
        aliases.add(url)
    external_id = string_field(item, "external_id")
    if external_id:
        aliases.add(external_id)
    return aliases


def select_work_proves_different_item(input_data: Any, state: dict[str, Any]) -> bool:
    current_aliases = selected_work_aliases(state.get("selected_work"))
    if not current_aliases:
        return False
    if not isinstance(input_data, dict):
        return False

    target_ids = input_data.get("target_ids")
    if isinstance(target_ids, list) and target_ids and all(isinstance(item, str) and item.strip() for item in target_ids):
        return current_aliases.isdisjoint({item.strip() for item in target_ids})

    candidate_identities = select_work_candidate_identities(input_data)
    if not candidate_identities:
        return False
    return current_aliases.isdisjoint(candidate_identities)


def current_selected_work_external_id(state: dict[str, Any]) -> str:
    selected_work = state.get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return ""
    first_item = selected_work[0]
    if not isinstance(first_item, dict):
        return ""
    return string_field(first_item, "external_id") or ""


def select_work_candidate_identities(input_data: dict[str, Any]) -> set[str]:
    sources = input_data.get("sources")
    if not isinstance(sources, list):
        return set()
    identities: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            return set()
        if string_field(source, "type") != "fixture":
            continue
        items = source.get("items")
        if not isinstance(items, list):
            return set()
        for item in items:
            if not isinstance(item, dict):
                return set()
            aliases = work_item_aliases(item)
            if not aliases:
                return set()
            identities.update(aliases)
    return identities


def selected_work_identity_set(selected_work: Any) -> set[str]:
    if not isinstance(selected_work, list) or not selected_work:
        return set()
    identities = set()
    for item in selected_work:
        if not isinstance(item, dict):
            return set()
        identity = work_item_identity(item)
        if not identity:
            return set()
        identities.add(identity)
    return identities


def selected_work_aliases(selected_work: Any) -> set[str]:
    if not isinstance(selected_work, list) or not selected_work:
        return set()
    aliases = set()
    for item in selected_work:
        if not isinstance(item, dict):
            return set()
        item_aliases = work_item_aliases(item)
        if not item_aliases:
            return set()
        aliases.update(item_aliases)
    return aliases


def reset_cycle_state_for_new_selection(state: dict[str, Any]) -> None:
    state["checkout"] = None
    state["checkout_attempts"] = []
    state["implementation"] = None
    state["implementation_selection"] = []
    state["implementation_result_path"] = ""
    state["validations"] = []
    state["review"] = None
    state["review_selection"] = []
    state["review_result_path"] = ""
    state["cleanup"] = {"status": "unknown", "resources": []}


def checkout_after_implementation(checkout: Any, implementation: dict[str, Any]) -> Any:
    if not isinstance(checkout, dict) or implementation.get("status") != "implemented":
        return checkout
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    after_commit = string_field(git_info, "after_commit")
    if not after_commit:
        return checkout
    updated = dict(checkout)
    updated["start_commit"] = after_commit
    updated["requested_ref"] = after_commit
    return updated


def implemented_after_commit(state: dict[str, Any]) -> str:
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    if implementation.get("status") != "implemented":
        return ""
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    return string_field(git_info, "after_commit") or ""


def has_current_validated_evidence(state: dict[str, Any]) -> bool:
    implemented_commit = implemented_after_commit(state)
    if not implemented_commit:
        return False
    return any(
        validation["output"].get("status") == "validated"
        and validation_checkout_commit(validation) == implemented_commit
        for validation in state["validations"]
    )


def blocking_reason_for_step(
    step_name: str,
    result: StepResult,
    remaining_steps: list[dict[str, Any]],
) -> str:
    output = result.output if isinstance(result.output, dict) else {}
    status = output.get("status")
    if step_name == "select-work" and not output.get("selected_work"):
        return "select-work selected no work items"
    expected = {
        "prepare-checkout": "prepared",
        "implement": "implemented",
        "validate": "validated",
        "review": "passed",
    }.get(step_name)
    if step_name == "validate" and status != expected and validation_failure_allows_retry_follow_up(remaining_steps):
        return ""
    if step_name == "review" and status != expected and review_failure_allows_retry_follow_up(remaining_steps):
        return ""
    if expected and status != expected:
        return f"{step_name} did not reach {expected}: {status or 'missing status'}"
    return ""


def validation_artifact_refs(state: dict[str, Any], ledger_dir: Path) -> list[dict[str, str]]:
    refs = []
    for index, validation in enumerate(state["validations"]):
        output = validation["output"]
        validation_info = output.get("validation") if isinstance(output.get("validation"), dict) else {}
        name = string_field(validation_info, "requested_profile") or f"validation-{index + 1}"
        refs.append(
            {
                "name": name,
                "step_result_path": validation["step_result_path"],
                "worker_result_path": validation["worker_result_path"],
            }
        )
    return refs


def publication_gate_reason(state: dict[str, Any]) -> str:
    if not state["validations"]:
        return "required final validation evidence is missing"
    failed_validations = [
        validation for validation in state["validations"] if validation["output"].get("status") != "validated"
    ]
    if failed_validations:
        names = ", ".join(validation_name(item) for item in failed_validations)
        return f"required final validation evidence did not pass: {names}"
    implemented_commit = implemented_after_commit(state)
    stale_validations = [
        validation
        for validation in state["validations"]
        if implemented_commit and validation_checkout_commit(validation) != implemented_commit
    ]
    if stale_validations:
        names = ", ".join(validation_name(item) for item in stale_validations)
        return f"required final validation evidence is stale for implemented HEAD: {names}"
    review = state.get("review")
    if not isinstance(review, dict):
        return "required final review evidence is missing"
    if review.get("status") != "passed":
        return f"final review did not pass: {review.get('status') or 'missing status'}"
    if implemented_commit and review_checkout_commit(review) != implemented_commit:
        return "final review evidence is stale for implemented HEAD"
    incomplete = incomplete_selected_work_ids(state)
    if incomplete:
        return "selected work items lack passed implementation, validation, and review evidence: " + ", ".join(incomplete)
    return ""


def validation_checkout_commit(validation: dict[str, Any]) -> str:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    checkout = output.get("checkout") if isinstance(output.get("checkout"), dict) else {}
    return string_field(checkout, "start_commit") or ""


def review_checkout_commit(review: dict[str, Any]) -> str:
    checkout = review.get("checkout") if isinstance(review.get("checkout"), dict) else {}
    return string_field(checkout, "start_commit") or ""


def review_failed(state: dict[str, Any]) -> bool:
    review = state.get("review")
    return isinstance(review, dict) and review.get("status") not in {"", "passed"}


def publish_terminal_pr(
    publisher: Any,
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    steps: list[dict[str, Any]],
    selected_work: list[dict[str, Any]],
    ledger: "WorkstreamLedger",
) -> dict[str, Any]:
    if not isinstance(publisher, dict):
        return failed_publication_config("publisher must be an object", normalized)
    if not publisher.get("enabled", True):
        return validated_unpublished_publication(
            "workstream validated and reviewed, but publisher is disabled",
            next_allowed_command=rerun_workstream_command(normalized),
        )
    try:
        config = normalize_publisher_config(publisher, normalized)
    except WorkstreamError as exc:
        return failed_publication_config(str(exc), normalized)
    checkout_path = checkout_path_from_state(state)
    try:
        config["gh_auth"] = validate_publisher_auth_config(config["gh_auth"], checkout_path)
    except WorkstreamError as exc:
        return failed_publication_config(
            str(exc),
            normalized,
            auth=publisher_auth_artifact(config["gh_auth"]),
        )
    auth = config["gh_auth"]
    auth_artifact = publisher_auth_artifact(auth)
    body = pr_body_markdown(normalized, state, steps, selected_work, ledger)
    ledger.write_text("pr-body.md", body)
    pr_body_path = (ledger.path / "pr-body.md").resolve(strict=False)
    git_push_result: dict[str, Any] | None = None
    git_push_command: list[str] = []
    git_push_retry_command: list[str] = []
    try:
        run_publisher_command(
            [config["gh_path"], "auth", "status", "--hostname", "github.com"],
            cwd=checkout_path,
            tool="gh",
            auth=auth,
            message_on_failure="gh auth status failed",
        )
        if config["push"]:
            git_push = push_review_branch(
                config,
                normalized=normalized,
                state=state,
                checkout_path=checkout_path,
                auth=auth,
            )
            git_push_result = git_push["result"]
            git_push_command = git_push["command"]
            git_push_retry_command = git_push["retry_command"]
        if config["mode"] == "create":
            command = [
                config["gh_path"],
                "pr",
                "create",
                "--repo",
                config["repo"],
                "--base",
                config["base"],
                "--head",
                config["head"],
                "--title",
                config["title"],
                "--body-file",
                str(pr_body_path),
            ]
            completed = run_publisher_command(command, cwd=checkout_path, tool="gh", auth=auth)
        else:
            command = [
                config["gh_path"],
                "pr",
                "edit",
                config["pr"],
                "--repo",
                config["repo"],
                "--title",
                config["title"],
                "--body-file",
                str(pr_body_path),
            ]
            completed, command = run_pr_update_command(
                command,
                config=config,
                checkout_path=checkout_path,
                auth=auth,
                ledger=ledger,
                body=body,
            )
    except PublisherError as exc:
        if git_push_result is not None:
            details = dict(exc.details)
            details.setdefault("git_push", git_push_result)
            command_details = details.get("commands") if isinstance(details.get("commands"), dict) else {}
            if git_push_command and "git_push" not in command_details:
                command_details["git_push"] = git_push_command
            if git_push_retry_command and "git_push_retry" not in command_details:
                command_details["git_push_retry"] = git_push_retry_command
            if command_details:
                details["commands"] = command_details
            exc.details = details
        return failed_publication(exc, normalized, auth=auth_artifact)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "published",
        "enabled": True,
        "mode": config["mode"],
        "reason": "terminal PR published",
        "auth": auth_artifact,
        "url": successful_publisher_url(completed.stdout),
        "next_allowed_command": "none",
        "retry": "",
        "commands": {
            "gh": redact_artifact_value(command),
            "git_push": redact_artifact_value(git_push_command),
            "git_push_retry": redact_artifact_value(git_push_retry_command),
        },
        "body_path": str(pr_body_path),
    }
    if git_push_result is not None:
        result["git_push"] = redact_artifact_value(git_push_result)
    return result


def push_review_branch(
    config: dict[str, Any],
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    checkout_path: Path,
    auth: dict[str, Any],
) -> dict[str, Any]:
    push_ref = f"refs/heads/{config['head']}"
    push_command = [config["git_path"], "push", config["remote"], f"HEAD:{push_ref}"]
    git_push_result = {
        "branch": config["head"],
        "remote": config["remote"],
        "retry_handling": "not-needed",
        "lease_expected": "",
        "base_commit": checkout_start_commit(state),
        "attempts": [],
    }
    initial_error: PublisherError | None = None
    try:
        completed = run_publisher_command(
            push_command,
            cwd=checkout_path,
            tool="git",
            auth=auth,
        )
        git_push_result["attempts"].append(publisher_command_attempt(push_command, completed=completed, outcome="pushed"))
        return {"command": push_command, "retry_command": [], "result": git_push_result}
    except PublisherError as exc:
        initial_error = exc
        initial_outcome = "non-fast-forward" if publisher_error_is_non_fast_forward_push(exc) else "failed"
        git_push_result["attempts"].append(publisher_command_attempt(push_command, error=exc, outcome=initial_outcome))
        if not publisher_error_is_non_fast_forward_push(exc):
            exc.details = {"git_push": git_push_result}
            raise

    retry_context = afk_review_branch_retry_context(
        config,
        normalized=normalized,
        state=state,
        checkout_path=checkout_path,
        auth=auth,
    )
    git_push_result.update(
        {
            "lease_expected": retry_context["lease_expected"],
            "base_commit": retry_context["base_commit"],
            "local_head": retry_context["local_head"],
            "merge_base": retry_context["merge_base"],
            "owned_branch": retry_context["owned_branch"],
        }
    )
    if not retry_context["eligible"]:
        git_push_result["retry_handling"] = "not-eligible"
        raise PublisherError(
            f"git push rejected as non-fast-forward and AFK review-branch retry is not eligible: {retry_context['reason']}",
            command=push_command,
            returncode=initial_error.returncode if initial_error is not None else None,
            stdout=initial_error.stdout if initial_error is not None else "",
            stderr=initial_error.stderr if initial_error is not None else "",
            details={"git_push": git_push_result},
        )

    retry_command = [
        config["git_path"],
        "push",
        f"--force-with-lease={push_ref}:{retry_context['lease_expected']}",
        config["remote"],
        f"HEAD:{push_ref}",
    ]
    try:
        completed = run_publisher_command(
            retry_command,
            cwd=checkout_path,
            tool="git",
            auth=auth,
        )
    except PublisherError as exc:
        git_push_result["retry_handling"] = "force-with-lease-failed"
        git_push_result["attempts"].append(publisher_command_attempt(retry_command, error=exc, outcome="force-with-lease-failed"))
        raise PublisherError(
            "git push rejected as non-fast-forward and AFK review-branch retry with --force-with-lease failed",
            command=retry_command,
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
            details={"git_push": git_push_result},
        ) from exc

    git_push_result["retry_handling"] = "force-with-lease-replaced"
    git_push_result["attempts"].append(publisher_command_attempt(retry_command, completed=completed, outcome="pushed"))
    return {"command": push_command, "retry_command": retry_command, "result": git_push_result}


def publisher_command_attempt(
    command: list[str],
    *,
    completed: subprocess.CompletedProcess[str] | None = None,
    error: PublisherError | None = None,
    outcome: str,
) -> dict[str, Any]:
    if completed is not None:
        return {
            "command": redact_artifact_value(command),
            "returncode": completed.returncode,
            "stdout_excerpt": redact_text(completed.stdout[-2000:]),
            "stderr_excerpt": redact_text(completed.stderr[-2000:]),
            "outcome": outcome,
        }
    if error is None:
        raise ValueError("completed or error is required")
    return {
        "command": redact_artifact_value(command),
        "returncode": error.returncode,
        "stdout_excerpt": redact_text(error.stdout[-2000:]),
        "stderr_excerpt": redact_text(error.stderr[-2000:]),
        "outcome": outcome,
    }


def publisher_error_is_non_fast_forward_push(exc: PublisherError) -> bool:
    text = f"{exc.message}\n{exc.stdout}\n{exc.stderr}".lower()
    return "non-fast-forward" in text or ("[rejected]" in text and "fetch first" in text)


def afk_review_branch_retry_context(
    config: dict[str, Any],
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    checkout_path: Path,
    auth: dict[str, Any],
) -> dict[str, Any]:
    owned_branch = workstream_owned_afk_branch(normalized)
    lease_expected = publisher_remote_branch_oid(config, checkout_path=checkout_path, auth=auth)
    base_commit = publisher_resolved_commit(
        config["git_path"],
        checkout_start_commit(state),
        checkout_path=checkout_path,
        auth=auth,
    )
    local_head = publisher_resolved_commit(
        config["git_path"],
        "HEAD",
        checkout_path=checkout_path,
        auth=auth,
    )
    merge_base = publisher_merge_base(
        config["git_path"],
        local_head,
        lease_expected,
        checkout_path=checkout_path,
        auth=auth,
    )
    if not config["head"].startswith("afk/"):
        reason = "review branch retry is only allowed for afk/ branches"
    elif config["head"] != normalized["review_branch"]:
        reason = "publisher head does not match the normalized review branch"
    elif not owned_branch:
        reason = "workstream id is required to prove AFK review-branch ownership"
    elif config["head"] != owned_branch:
        reason = f"review branch does not match the workstream-owned AFK branch {owned_branch}"
    elif not lease_expected:
        reason = "remote review branch could not be resolved for retry"
    elif not base_commit:
        reason = "checkout start commit is required for retry safety"
    elif not local_head:
        reason = "local HEAD could not be resolved for retry safety"
    elif merge_base != base_commit:
        reason = "remote review branch does not match the checkout start commit"
    else:
        reason = ""
    return {
        "eligible": not reason,
        "reason": reason,
        "lease_expected": lease_expected,
        "base_commit": base_commit,
        "local_head": local_head,
        "merge_base": merge_base,
        "owned_branch": owned_branch,
    }


def workstream_owned_afk_branch(normalized: dict[str, Any]) -> str:
    workstream_id = string_field(normalized, "workstream_id") or ""
    if not workstream_id:
        return ""
    return f"afk/{branch_slug(workstream_id)}"


def checkout_start_commit(state: dict[str, Any]) -> str:
    checkout = state.get("checkout")
    if not isinstance(checkout, dict):
        return ""
    return string_field(checkout, "start_commit") or ""


def publisher_remote_branch_oid(config: dict[str, Any], *, checkout_path: Path, auth: dict[str, Any]) -> str:
    push_ref = f"refs/heads/{config['head']}"
    completed = run_publisher_diagnostic_command(
        [config["git_path"], "ls-remote", "--heads", config["remote"], push_ref],
        cwd=checkout_path,
        auth=auth,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return ""
    return completed.stdout.strip().split()[0]


def publisher_resolved_commit(git_path: str, ref: str, *, checkout_path: Path, auth: dict[str, Any]) -> str:
    if not ref:
        return ""
    completed = run_publisher_diagnostic_command(
        [git_path, "rev-parse", ref],
        cwd=checkout_path,
        auth=auth,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""


def publisher_merge_base(
    git_path: str,
    local_head: str,
    remote_head: str,
    *,
    checkout_path: Path,
    auth: dict[str, Any],
) -> str:
    if not local_head or not remote_head:
        return ""
    completed = run_publisher_diagnostic_command(
        [git_path, "merge-base", local_head, remote_head],
        cwd=checkout_path,
        auth=auth,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""


def run_publisher_diagnostic_command(
    command: list[str],
    *,
    cwd: Path,
    auth: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="afk-publisher-") as temp_dir:
        env = minimal_publisher_environment(Path(temp_dir), auth=auth)
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr=str(exc))


def successful_publisher_url(stdout: str) -> str:
    return redact_text(stdout.strip().splitlines()[-1]) if stdout.strip() else ""


def run_pr_update_command(
    command: list[str],
    *,
    config: dict[str, Any],
    checkout_path: Path,
    auth: dict[str, Any],
    ledger: "WorkstreamLedger",
    body: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    try:
        return run_publisher_command(command, cwd=checkout_path, tool="gh", auth=auth), command
    except PublisherError as exc:
        if not publisher_error_is_projects_classic_graphql_failure(exc):
            raise
    pr_number = pr_number_for_rest_update(config["pr"], config=config, checkout_path=checkout_path, auth=auth)
    ledger.write_json("pr-update.json", {"title": config["title"], "body": body})
    pr_update_path = (ledger.path / "pr-update.json").resolve(strict=False)
    fallback_command = [
        config["gh_path"],
        "api",
        "--method",
        "PATCH",
        f"repos/{config['repo']}/pulls/{pr_number}",
        "--input",
        str(pr_update_path),
        "--jq",
        ".html_url",
    ]
    return run_publisher_command(fallback_command, cwd=checkout_path, tool="gh", auth=auth), fallback_command


def publisher_error_is_projects_classic_graphql_failure(exc: PublisherError) -> bool:
    text = f"{exc.message}\n{exc.stdout}\n{exc.stderr}".lower()
    if "graphql" not in text:
        return False
    return (
        "projects (classic)" in text
        or "projects classic" in text
        or "projectcards" in text
        or "classic projects" in text
    )


def pr_number_for_rest_update(
    pr_ref: str,
    *,
    config: dict[str, Any],
    checkout_path: Path,
    auth: dict[str, Any],
) -> str:
    direct_number = string_field({"pr": pr_ref}, "pr")
    if direct_number and direct_number.isdigit():
        return direct_number
    command = [
        config["gh_path"],
        "pr",
        "view",
        pr_ref,
        "--repo",
        config["repo"],
        "--json",
        "number",
        "--jq",
        ".number",
    ]
    completed = run_publisher_command(command, cwd=checkout_path, tool="gh", auth=auth)
    resolved = string_field({"number": completed.stdout}, "number")
    if resolved and resolved.isdigit():
        return resolved
    raise PublisherError(
        "could not resolve PR number for REST update fallback",
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def normalize_publisher_config(publisher: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    mode = string_field(publisher, "mode") or "create"
    if mode not in {"create", "update"}:
        raise WorkstreamError("publisher.mode must be create or update")
    gh = publisher.get("gh", {})
    git = publisher.get("git", {})
    if not isinstance(gh, dict) or not isinstance(git, dict):
        raise WorkstreamError("publisher.gh and publisher.git must be objects when present")
    head = string_field(publisher, "head") or normalized["review_branch"]
    if head != normalized["review_branch"]:
        raise WorkstreamError("publisher.head must match review_branch")
    title = string_field(publisher, "title") or f"{normalized['workstream_id']}: workstream"
    repo = string_field(publisher, "repo") or ""
    base = string_field(publisher, "base") or ""
    pr = string_field(publisher, "pr") or head
    if not repo:
        raise WorkstreamError("publisher.repo is required")
    if mode == "create" and not base:
        raise WorkstreamError("publisher.base is required for create")
    gh_auth = normalize_publisher_gh_auth(gh)
    return {
        "mode": mode,
        "gh_path": string_field(gh, "path") or "gh",
        "gh_auth": gh_auth,
        "git_path": string_field(git, "path") or "git",
        "push": bool(git.get("push", False)),
        "remote": string_field(git, "remote") or "origin",
        "repo": repo,
        "base": base,
        "head": head,
        "title": title,
        "pr": pr,
    }


def normalize_publisher_gh_auth(gh: dict[str, Any]) -> dict[str, Any]:
    for key in gh:
        if key in {"path", "auth"}:
            continue
        if is_secret_key(key):
            raise WorkstreamError(f"publisher.gh.{key} is not supported; mount gh auth config instead")
    raw_auth = gh.get("auth")
    if raw_auth is None:
        return {"configured": False, "source": "minimal_env", "config_dir": ""}
    if not isinstance(raw_auth, dict):
        raise WorkstreamError("publisher.gh.auth must be an object")
    unsupported = [key for key in raw_auth.keys() if key != "config_dir"]
    if unsupported:
        raise WorkstreamError("publisher.gh.auth only supports config_dir")
    config_dir = string_field(raw_auth, "config_dir")
    if not config_dir:
        raise WorkstreamError("publisher.gh.auth.config_dir is required")
    return {
        "configured": True,
        "source": "gh_config_dir",
        "config_dir": config_dir,
    }


def validate_publisher_auth_config(auth: dict[str, Any], checkout_path: Path) -> dict[str, Any]:
    if not auth.get("configured"):
        return {"configured": False, "source": "minimal_env", "config_dir": ""}
    config_dir = Path(str(auth["config_dir"]))
    if not config_dir.is_absolute():
        raise WorkstreamError("publisher.gh.auth.config_dir must be absolute")
    if not config_dir.is_dir():
        raise WorkstreamError("publisher.gh.auth.config_dir must be an existing directory")
    if path_is_equal_to_or_inside(config_dir, checkout_path):
        raise WorkstreamError("publisher.gh.auth.config_dir must be outside checkout")
    return {
        "configured": True,
        "source": "gh_config_dir",
        "config_dir": str(config_dir),
    }


def publisher_auth_artifact(auth: dict[str, Any]) -> dict[str, Any]:
    artifact = {
        "configured": bool(auth.get("configured")),
        "source": str(auth.get("source") or "minimal_env"),
    }
    if auth.get("configured"):
        artifact["path"] = "[REDACTED]"
    return artifact


def run_publisher_command(
    command: list[str],
    *,
    cwd: Path,
    tool: str,
    auth: dict[str, Any],
    message_on_failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="afk-publisher-") as temp_dir:
        env = minimal_publisher_environment(Path(temp_dir), auth=auth)
        return run_publisher_command_once(
            command,
            cwd=cwd,
            tool=tool,
            env=env,
            message_on_failure=message_on_failure,
        )


def run_publisher_command_once(
    command: list[str],
    *,
    cwd: Path,
    tool: str,
    env: dict[str, str],
    message_on_failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise PublisherError(str(exc), command=command, returncode=None, stderr=str(exc)) from exc
    if completed.returncode != 0:
        raise PublisherError(
            message_on_failure or f"{tool} command failed",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def minimal_publisher_environment(temp_path: Path, *, auth: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    home_path = temp_path / "home"
    xdg_config_home = temp_path / "xdg-config"
    xdg_cache_home = temp_path / "xdg-cache"
    xdg_state_home = temp_path / "xdg-state"
    tmp_path = temp_path / "tmp"
    for path in (home_path, xdg_config_home, xdg_cache_home, xdg_state_home, tmp_path):
        path.mkdir()
    env["HOME"] = str(home_path)
    env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    env["XDG_CACHE_HOME"] = str(xdg_cache_home)
    env["XDG_STATE_HOME"] = str(xdg_state_home)
    env["TMPDIR"] = str(tmp_path)
    if auth.get("configured") and auth.get("source") == "gh_config_dir":
        env["GH_CONFIG_DIR"] = str(auth["config_dir"])
    return env


def pr_body_markdown(
    normalized: dict[str, Any],
    state: dict[str, Any],
    steps: list[dict[str, Any]],
    selected_work: list[dict[str, Any]],
    ledger: "WorkstreamLedger",
) -> str:
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    changed_files = list(git_info.get("changed_files") or [])
    commits = list(git_info.get("commits") or [])
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    lines = [
        f"# Workstream {pr_body_value(normalized['workstream_id'])}",
        "",
        f"Workstream: {pr_body_value(normalized['workstream_id'])}",
        f"Parent: {pr_body_value(normalized['parent'] or '(none)')}",
        f"Review branch: {pr_body_value(normalized['review_branch'])}",
        "",
        "## Selected Work",
        "",
    ]
    for item in selected_work:
        lines.append(
            f"- {pr_body_value(item['external_id'])} - "
            f"{pr_body_value(item['title'])} ({pr_body_value(item['result'])})"
        )
        artifact_lines = selected_work_artifact_lines_for_body(item, state)
        for artifact_line in artifact_lines:
            lines.append(f"  - {pr_body_value(artifact_line)}")
    lines.extend(["", "## Changed files", ""])
    if changed_files:
        lines.extend(f"- {pr_body_value(path)}" for path in changed_files)
    else:
        lines.append("- None recorded")
    lines.extend(["", "## Commits", ""])
    if commits:
        for commit in commits:
            if isinstance(commit, dict):
                lines.append(
                    f"- {pr_body_value(commit.get('commit', ''))} "
                    f"{pr_body_value(commit.get('subject', ''))}".rstrip()
                )
    else:
        lines.append("- None recorded")
    lines.extend(["", "## Validation", ""])
    validation_lines = [
        pr_body_validation_line(validation, index)
        for index, validation in enumerate(state.get("validations") or [])
        if isinstance(validation, dict)
    ]
    lines.extend(validation_lines or ["- None recorded"])
    lines.extend(["", f"Review: {pr_body_value(review.get('status', 'missing'))}", ""])
    if review.get("summary"):
        lines.extend(["## Review Summary", "", pr_body_value(review["summary"]), ""])
    lines.extend(
        [
            "## Cleanup",
            "",
            f"Cleanup: {pr_body_value(state['cleanup'].get('status', 'unknown'))}",
            "",
            "## Retry",
            "",
            "Retry: rerun the workstream if terminal publication fails",
            "",
            "## Artifacts",
            "",
            f"- Workstream result: {pr_body_value(f'workstreams/{ledger.run_id}/workstream-result.json')}",
        ]
    )
    cleanup_resources = state["cleanup"].get("resources")
    if isinstance(cleanup_resources, list) and cleanup_resources:
        lines.extend(["- Cleanup resources:"])
        for resource in cleanup_resources:
            if not isinstance(resource, dict):
                continue
            lines.append(
                "  - "
                f"{pr_body_value(resource.get('kind', 'resource'))}: "
                f"{pr_body_value(resource.get('path', ''))} "
                f"{pr_body_value(resource.get('branch', ''))} "
                f"{pr_body_value(resource.get('commit', ''))} "
                f"({pr_body_value(resource.get('status', 'unknown'))})"
            )
    for step in steps:
        lines.append(f"- {pr_body_value(step['name'])}: {pr_body_value(step['result_path'])}")
    lines.append("")
    return "\n".join(lines)


def normalize_retry_policy(retry_policy: Any) -> dict[str, int]:
    if retry_policy is None:
        return {"max_retries": 0}
    if not isinstance(retry_policy, dict):
        raise WorkstreamError("retry_policy must be an object")
    unsupported = [key for key in retry_policy if key != "max_retries"]
    if unsupported:
        raise WorkstreamError("retry_policy only supports max_retries")
    max_retries = retry_policy.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise WorkstreamError("retry_policy.max_retries must be a non-negative integer")
    return {"max_retries": max_retries}


def normalize_tracker_config(tracker: Any) -> dict[str, Any]:
    if tracker is None:
        return {
            "terminal_decision": {
                "status": "",
                "merge_commit": "",
                "reason": "",
                "pr_url": "",
                "review_feedback_status": "",
            }
        }
    if not isinstance(tracker, dict):
        raise WorkstreamError("tracker must be an object")
    unsupported = [key for key in tracker if key != "terminal_decision"]
    if unsupported:
        raise WorkstreamError("tracker only supports terminal_decision")
    return {"terminal_decision": normalize_tracker_terminal_decision(tracker.get("terminal_decision"))}


def normalize_tracker_terminal_decision(decision: Any) -> dict[str, str]:
    if decision is None:
        return {"status": "", "merge_commit": "", "reason": "", "pr_url": "", "review_feedback_status": ""}
    if not isinstance(decision, dict):
        raise WorkstreamError("tracker.terminal_decision must be an object")
    unsupported = [
        key for key in decision if key not in {"status", "merge_commit", "reason", "pr_url", "review_feedback_status"}
    ]
    if unsupported:
        raise WorkstreamError(
            "tracker.terminal_decision only supports status, merge_commit, reason, pr_url, review_feedback_status"
        )
    status = string_field(decision, "status") or ""
    merge_commit = string_field(decision, "merge_commit") or ""
    reason = string_field(decision, "reason") or ""
    pr_url = string_field(decision, "pr_url") or ""
    review_feedback_status = string_field(decision, "review_feedback_status") or ""
    if not status and not merge_commit and not reason and not pr_url and not review_feedback_status:
        return {"status": "", "merge_commit": "", "reason": "", "pr_url": "", "review_feedback_status": ""}
    if status not in {"merged", "no-merge"}:
        raise WorkstreamError("tracker.terminal_decision.status must be merged or no-merge")
    if status == "merged" and not merge_commit:
        raise WorkstreamError("tracker.terminal_decision.merge_commit is required for merged")
    if status == "merged" and not pr_url:
        raise WorkstreamError("tracker.terminal_decision.pr_url is required for merged")
    if status == "no-merge" and not reason:
        raise WorkstreamError("tracker.terminal_decision.reason is required for no-merge")
    if status == "no-merge" and not pr_url:
        raise WorkstreamError("tracker.terminal_decision.pr_url is required for no-merge")
    if review_feedback_status and review_feedback_status not in TERMINAL_REVIEW_FEEDBACK_STATUSES:
        allowed = ", ".join(sorted(TERMINAL_REVIEW_FEEDBACK_STATUSES))
        raise WorkstreamError(f"tracker.terminal_decision.review_feedback_status must be one of: {allowed}")
    if status == "merged":
        reason = ""
    if status == "no-merge":
        merge_commit = ""
    return {
        "status": status,
        "merge_commit": merge_commit,
        "reason": reason,
        "pr_url": pr_url,
        "review_feedback_status": review_feedback_status,
    }


def append_checkout_attempt(state: dict[str, Any], checkout: dict[str, Any]) -> None:
    if checkout.get("status") != "prepared":
        return
    attempt_number = len(state["checkout_attempts"]) + 1
    retry_number = max(0, attempt_number - 1)
    state["checkout_attempts"].append(
        {
            "attempt": attempt_number,
            "retry_number": retry_number,
            "repairing_failure_class": previous_failure_class_for_retry(state) if retry_number else "",
            "checkout_path": string_field(checkout, "checkout_path") or "",
            "review_branch": string_field(checkout, "review_branch") or "",
            "commit": string_field(checkout, "start_commit") or "",
            "status": "prepared",
            "failure_class": "",
            "dirty": False,
            "dirty_status": [],
        }
    )


def update_checkout_attempt_after_implementation(state: dict[str, Any], implementation: dict[str, Any]) -> None:
    attempt = latest_checkout_attempt(state)
    if attempt is None or implementation.get("status") != "implemented":
        return
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    after_commit = string_field(git_info, "after_commit")
    if after_commit:
        attempt["commit"] = after_commit
    dirty_status = git_info.get("dirty_status")
    attempt["dirty"] = bool(git_info.get("dirty"))
    attempt["dirty_status"] = list(dirty_status) if isinstance(dirty_status, list) else []
    attempt["status"] = "dirty" if attempt["dirty"] else "awaiting_validation"


def update_checkout_attempt_after_validation(state: dict[str, Any], validation: dict[str, Any]) -> None:
    attempt = latest_checkout_attempt(state)
    if attempt is None:
        return
    status = string_field(validation, "status") or ""
    if not status:
        return
    attempt["failure_class"] = "" if status == "validated" else status
    if not checkout_attempt_is_dirty(attempt):
        attempt["status"] = status


def latest_checkout_attempt(state: dict[str, Any]) -> dict[str, Any] | None:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    latest = attempts[-1]
    return latest if isinstance(latest, dict) else None


def latest_retry_attempt(state: dict[str, Any]) -> dict[str, Any] | None:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and integer_retry_number(attempt) > 0:
            return attempt
    return None


def previous_failure_class_for_retry(state: dict[str, Any]) -> str:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    review_status = string_field(review, "status")
    if review_status and review_status != "passed":
        return review_status
    validations = state.get("validations")
    if isinstance(validations, list) and validations:
        latest_validation = validations[-1]
        if isinstance(latest_validation, dict):
            output = latest_validation.get("output") if isinstance(latest_validation.get("output"), dict) else {}
            validation_status = string_field(output, "status")
            if validation_status and validation_status != "validated":
                return validation_status
    latest = latest_checkout_attempt(state)
    if latest is None:
        return ""
    return string_field(latest, "failure_class") or string_field(latest, "status") or ""


def checkout_attempt_is_dirty(attempt: dict[str, Any]) -> bool:
    return bool(attempt.get("dirty"))


def retry_prepare_checkout_blocking_reason(state: dict[str, Any], retry_policy: dict[str, int]) -> str:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list) or not attempts:
        return ""
    attempted_retries = retry_attempt_count(state)
    if attempted_retries >= retry_policy["max_retries"]:
        return (
            "retry budget exhausted: "
            f"{attempted_retries} retries attempted, max_retries={retry_policy['max_retries']}"
        )
    prior_retry = latest_retry_attempt(state)
    if prior_retry is None:
        return ""
    if checkout_attempt_is_dirty(prior_retry):
        return "retry checkout blocked: prior retry checkout is dirty and still needs cleanup"
    if string_field(prior_retry, "status") in {"prepared", "awaiting_validation"}:
        return "retry checkout blocked: prior retry checkout is still running validation"
    return ""


def validation_failure_allows_retry_follow_up(remaining_steps: list[dict[str, Any]]) -> bool:
    for step in remaining_steps:
        if not isinstance(step, dict):
            continue
        name = step.get("name")
        if name in {"prepare-checkout", "select-work"}:
            return True
        if name in {"review", "validate"}:
            return False
    return False


def review_failure_allows_retry_follow_up(remaining_steps: list[dict[str, Any]]) -> bool:
    for step in remaining_steps:
        if not isinstance(step, dict):
            continue
        name = step.get("name")
        if name == "prepare-checkout":
            return True
        if name in {"select-work", "implement"}:
            return False
    return False


def retry_attempt_count(state: dict[str, Any]) -> int:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list):
        return 0
    return sum(1 for attempt in attempts if isinstance(attempt, dict) and integer_retry_number(attempt) > 0)


def retry_budget_record(state: dict[str, Any], retry_policy: dict[str, int]) -> dict[str, int]:
    attempted_retries = retry_attempt_count(state)
    max_retries = retry_policy["max_retries"]
    return {
        "max_retries": max_retries,
        "attempted_retries": attempted_retries,
        "remaining_retries": max(0, max_retries - attempted_retries),
    }


def retry_attempt_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list):
        return []
    records = []
    for attempt in attempts:
        if not isinstance(attempt, dict) or integer_retry_number(attempt) <= 0:
            continue
        records.append(
            {
                "attempt": int(attempt["attempt"]),
                "repairing_failure_class": string_field(attempt, "repairing_failure_class") or "",
                "checkout_path": string_field(attempt, "checkout_path") or "",
                "review_branch": string_field(attempt, "review_branch") or "",
                "commit": string_field(attempt, "commit") or "",
                "status": "dirty" if checkout_attempt_is_dirty(attempt) else string_field(attempt, "status") or "unknown",
            }
        )
    return records


def final_cleanup_state(state: dict[str, Any]) -> dict[str, Any]:
    cleanup = state.get("cleanup")
    base = dict(cleanup) if isinstance(cleanup, dict) else {"status": "unknown", "resources": []}
    resources = list(base.get("resources")) if isinstance(base.get("resources"), list) else []
    dirty_retry_resources = dirty_retry_checkout_resources(state)
    if not dirty_retry_resources:
        base["resources"] = resources
        return base
    return {
        "status": "dirty_retry_checkouts",
        "resources": resources + dirty_retry_resources,
    }


def dirty_retry_checkout_resources(state: dict[str, Any]) -> list[dict[str, str]]:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list):
        return []
    resources = []
    for attempt in attempts:
        if not isinstance(attempt, dict) or integer_retry_number(attempt) <= 0:
            continue
        if not checkout_attempt_is_dirty(attempt):
            continue
        resources.append(
            {
                "kind": "retry_checkout",
                "path": string_field(attempt, "checkout_path") or "",
                "branch": string_field(attempt, "review_branch") or "",
                "commit": string_field(attempt, "commit") or "",
                "status": "dirty",
            }
        )
    return resources


def integer_retry_number(attempt: dict[str, Any]) -> int:
    value = attempt.get("retry_number")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def pr_body_value(value: Any) -> str:
    return redact_text(str(value))


def selected_work_records(state: dict[str, Any]) -> list[dict[str, str]]:
    records = []
    for item in state["selected_work"]:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "external_id": redact_text(str(item.get("external_id") or "")),
                "title": redact_text(str(item.get("title") or "")),
                "source_id": redact_text(str(item.get("source_id") or "")),
                "source_type": redact_text(str(item.get("source_type") or "")),
                "result": redact_text(selected_work_result(item, state)),
            }
        )
    return records


def selected_work_result(item: dict[str, Any], state: dict[str, Any]) -> str:
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    implementation_status = string_field(implementation, "status") or ""
    implementation_selection = state.get("implementation_selection")
    item_in_implementation = work_item_in_selection(item, implementation_selection)
    current_validations = current_validation_records(state)
    latest_validation = current_validations[-1] if current_validations else None
    current_review = current_review_record(state)
    review = state.get("review") if isinstance(state.get("review"), dict) else {}

    if item_in_implementation:
        if implementation_status and implementation_status != "implemented":
            return "failed"
        if current_review is not None:
            if work_item_in_selection(item, state.get("review_selection")):
                return "passed" if review.get("status") == "passed" and has_current_validated_evidence(state) else "failed"
            return "not_processed"
        if latest_validation is not None and latest_validation["output"].get("status") != "validated":
            return "failed"
        return "blocked"

    if implementation_status or current_validations or current_review is not None:
        return "not_processed"
    return "blocked"


def incomplete_selected_work_ids(state: dict[str, Any]) -> list[str]:
    ids = []
    for item in state.get("selected_work", []):
        if not isinstance(item, dict):
            continue
        if selected_work_result(item, state) != "passed":
            ids.append(redact_text(string_field(item, "external_id") or "selected item"))
    return ids


def work_item_in_selection(item: dict[str, Any], selection: Any) -> bool:
    item_identity = work_item_identity(item)
    if not item_identity:
        return False
    if not isinstance(selection, list):
        return False
    for candidate in selection:
        if not isinstance(candidate, dict):
            continue
        if work_item_identity(candidate) == item_identity:
            return True
    return False


def current_validation_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    implemented_commit = implemented_after_commit(state)
    validations = state.get("validations")
    if not implemented_commit or not isinstance(validations, list):
        return []
    return [
        validation
        for validation in validations
        if isinstance(validation, dict) and validation_checkout_commit(validation) == implemented_commit
    ]


def current_review_record(state: dict[str, Any]) -> dict[str, Any] | None:
    implemented_commit = implemented_after_commit(state)
    review = state.get("review")
    if not implemented_commit or not isinstance(review, dict):
        return None
    if review_checkout_commit(review) != implemented_commit:
        return None
    return review


def selected_work_artifact_lines_for_body(item: dict[str, Any], state: dict[str, Any]) -> list[str]:
    lines = []
    if work_item_in_selection(item, state.get("implementation_selection")):
        implementation_path = string_field(state, "implementation_result_path") or ""
        if implementation_path:
            lines.append(f"implementation: {ledger_relative_path(implementation_path)}")
    validation_paths = selected_work_validation_paths(item, state)
    if validation_paths:
        lines.append(f"validation: {'; '.join(validation_paths)}")
    if work_item_in_selection(item, state.get("review_selection")):
        review_path = string_field(state, "review_result_path") or ""
        if review_path:
            lines.append(f"review: {ledger_relative_path(review_path)}")
    return lines


def selected_work_validation_paths(item: dict[str, Any], state: dict[str, Any]) -> list[str]:
    if not work_item_in_selection(item, state.get("implementation_selection")):
        return []
    refs = []
    for validation in current_validation_records(state):
        step_path = ledger_relative_path(string_field(validation, "step_result_path") or "")
        worker_path = ledger_relative_path(string_field(validation, "worker_result_path") or "")
        if step_path:
            refs.append(step_path)
        if worker_path:
            refs.append(worker_path)
    return refs


def tracker_selected_work_status(
    state: dict[str, Any],
    publication: dict[str, Any],
    tracker: dict[str, Any],
) -> str:
    if tracker.get("close_source_item"):
        return "closed"
    if publication.get("status") == "tracker-close-blocked":
        return "awaiting-review"
    if publication.get("status") == "published":
        return "awaiting-review"
    if publication.get("status") == "validated-unpublished":
        return terminal_selected_work_status(state)
    status = review_status(state)
    if status == "passed":
        return "validated" if has_current_validated_evidence(state) else "implemented"
    return status


def review_status(state: dict[str, Any]) -> str:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    status = review.get("status")
    if status == "passed":
        return "passed"
    if isinstance(status, str) and status:
        return status
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    if implementation.get("status") == "implemented":
        return "implemented"
    return "selected"


def review_passed(state: dict[str, Any]) -> bool:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    return review.get("status") == "passed"


def gated_selected_work_status(state: dict[str, Any]) -> str:
    status = review_status(state)
    if status == "passed":
        return "implemented" if implemented_after_commit(state) else "selected"
    return status


def terminal_selected_work_status(state: dict[str, Any]) -> str:
    if review_passed(state) or has_current_validated_evidence(state):
        return "validated"
    return gated_selected_work_status(state)


def tracker_terminal_decision_present(normalized: dict[str, Any]) -> bool:
    decision = normalized.get("tracker", {}).get("terminal_decision", {})
    return bool(isinstance(decision, dict) and decision.get("status"))


def tracker_terminal_decision_allows_close(normalized: dict[str, Any]) -> bool:
    decision = normalized.get("tracker", {}).get("terminal_decision", {})
    if not isinstance(decision, dict) or not decision.get("status"):
        return False
    if not review_cycles_require_response(normalized.get("review_cycles")):
        return True
    return terminal_review_feedback_status(decision) in TERMINAL_REVIEW_FEEDBACK_STATUSES


def tracker_close_blocked_publication() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "tracker-close-blocked",
        "enabled": False,
        "reason": (
            "terminal tracker decision recorded, but unresolved review feedback still requires an explicit "
            "review_feedback_status of resolved or waived before the source item can close"
        ),
        "next_allowed_command": "none",
        "retry": "",
    }


def tracker_terminal_decision_publication() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "tracker-closed",
        "enabled": False,
        "reason": "terminal tracker decision recorded; PR publication skipped",
        "next_allowed_command": "none",
        "retry": "",
    }


def terminal_review_feedback_status(decision: dict[str, Any]) -> str:
    return string_field(decision, "review_feedback_status") or ""


def merged_close_reason(decision: dict[str, Any]) -> str:
    merge_commit = redact_text(decision["merge_commit"])
    return f"merged via {merge_commit}"


def no_merge_close_reason(decision: dict[str, Any]) -> str:
    return redact_text(decision["reason"])


def terminal_decision_comment(
    decision_status: str,
    review_feedback_status: str,
    review_feedback_requires_response: bool,
) -> str:
    if decision_status == "merged":
        base = "PR merged; close the source Beads item with the recorded merge commit."
    else:
        base = "A terminal no-merge decision was recorded; close the source Beads item with this reason."
    if not review_feedback_requires_response:
        return base
    if review_feedback_status == "resolved":
        return f"{base} Review feedback was explicitly resolved before closure."
    if review_feedback_status == "waived":
        return f"{base} Review feedback was explicitly waived before closure."
    return base


def tracker_record(
    normalized: dict[str, Any],
    state: dict[str, Any],
    publication: dict[str, Any],
) -> dict[str, Any]:
    decision = normalized["tracker"]["terminal_decision"]
    decision_pr_url = redact_text(decision.get("pr_url") or "")
    decision_review_feedback_status = terminal_review_feedback_status(decision)
    review_feedback_requires_response = review_cycles_require_response(normalized.get("review_cycles"))
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "status": tracker_progress_status(state),
        "close_source_item": False,
        "close_reason": "",
        "comment": "",
        "pr_url": "",
        "merge_commit": "",
        "source_item_external_id": current_selected_work_external_id(state),
        "review_status": string_field(review, "status") or "",
        "review_summary": string_field(review, "summary") or "",
        "review_findings": tracker_review_findings(review),
        "review_cycles": redact_review_cycles(normalized.get("review_cycles") or []),
        "retrospective": redact_retrospective(normalized.get("retrospective")),
        "terminal_decision": redact_artifact_value(decision),
    }
    decision_status = decision.get("status")
    if decision_status == "merged":
        if review_feedback_requires_response and not decision_review_feedback_status:
            record["status"] = "review-findings-open"
            record["comment"] = (
                "PR review cycles still require a response; the terminal decision is recorded, but keep the "
                "source Beads item open until review_feedback_status is set to resolved or waived."
            )
            record["pr_url"] = decision_pr_url
            return record
        if publication.get("status") != "tracker-closed":
            record["comment"] = (
                "A terminal merge decision is recorded, but publication did not reach the tracker-closing "
                "terminal state; keep the source Beads item open."
            )
            record["pr_url"] = decision_pr_url
            return record
        record["status"] = "closed"
        record["close_source_item"] = True
        record["close_reason"] = merged_close_reason(decision)
        record["comment"] = terminal_decision_comment(
            "merged",
            decision_review_feedback_status,
            review_feedback_requires_response,
        )
        record["merge_commit"] = redact_text(decision["merge_commit"])
        record["pr_url"] = decision_pr_url
        return record
    if decision_status == "no-merge":
        if review_feedback_requires_response and not decision_review_feedback_status:
            record["status"] = "review-findings-open"
            record["comment"] = (
                "PR review cycles still require a response; the terminal no-merge decision is recorded, but keep "
                "the source Beads item open until review_feedback_status is set to resolved or waived."
            )
            record["pr_url"] = decision_pr_url
            return record
        if publication.get("status") != "tracker-closed":
            record["comment"] = (
                "A terminal no-merge decision is recorded, but publication did not reach the tracker-closing "
                "terminal state; keep the source Beads item open."
            )
            record["pr_url"] = decision_pr_url
            return record
        record["status"] = "closed"
        record["close_source_item"] = True
        record["close_reason"] = no_merge_close_reason(decision)
        record["comment"] = terminal_decision_comment(
            "no-merge",
            decision_review_feedback_status,
            review_feedback_requires_response,
        )
        record["pr_url"] = decision_pr_url
        return record
    if review_feedback_requires_response:
        record["status"] = "review-findings-open"
        record["comment"] = "PR review cycles contain response-required review findings; keep the source Beads item open."
        if publication.get("status") == "published":
            record["pr_url"] = redact_text(str(publication.get("url") or ""))
        return record
    if normalized.get("review_cycles"):
        record["status"] = "review-feedback-addressed"
        record["comment"] = (
            "PR review cycle evidence is present and all response-required findings are addressed; keep the source "
            "Beads item open until merge or an explicit no-merge decision."
        )
        if publication.get("status") == "published":
            record["pr_url"] = redact_text(str(publication.get("url") or ""))
        return record
    if publication.get("status") == "published":
        record["status"] = "awaiting-review"
        record["comment"] = "PR opened; keep the source Beads item open until merge or an explicit no-merge decision."
        record["pr_url"] = redact_text(str(publication.get("url") or ""))
        return record
    if publication.get("status") == "validated-unpublished":
        record["status"] = "validated"
        record["comment"] = "Validated head is ready, but the source Beads item stays open until merge or no-merge."
        return record
    if record["review_findings"]:
        record["comment"] = "Review findings are available; update the source Beads item and keep it open."
    elif review_passed(state):
        record["comment"] = "Final review passed, but keep the source Beads item open until merge or no-merge."
    return record


def tracker_progress_status(state: dict[str, Any]) -> str:
    if has_current_validated_evidence(state):
        return "validated"
    if implemented_after_commit(state):
        return "implemented"
    if state.get("selected_work"):
        return "selected"
    return "idle"


def tracker_review_findings(review: dict[str, Any]) -> list[Any]:
    reviewer_result = review.get("reviewer_result") if isinstance(review.get("reviewer_result"), dict) else {}
    findings = reviewer_result.get("findings")
    if not isinstance(findings, list):
        return []
    return redact_artifact_value(findings)


def pipeline_retrospective_record(
    state: dict[str, Any],
    publication: dict[str, Any],
    tracker: dict[str, Any],
    normalized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signals = (
        _validation_retrospective_signals(state)
        + _publication_retrospective_signals(publication)
        + _blocked_retrospective_signals(publication)
        + _cleanup_retrospective_signals(state)
    )
    follow_up = _retrospective_follow_up_record(signals, normalized)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": workstream_status_from_publication(publication),
        "health": _retrospective_health(signals),
        "publication_status": redact_text(str(publication.get("status") or "")),
        "tracker_status": redact_text(str(tracker.get("status") or "")),
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
) -> dict[str, Any]:
    record = dict(pipeline_retrospective)
    if not judge:
        record["judge"] = _disabled_retrospective_judge_record()
        return record
    signals = list(record.get("signals", [])) if isinstance(record.get("signals"), list) else []
    judge_signals = _retrospective_judge_signals(judge)
    if judge_signals:
        signals.extend(judge_signals)
    record["signals"] = signals
    record["health"] = _retrospective_health(signals)
    follow_up = _retrospective_follow_up_record(signals, normalized)
    record["recommended_follow_up"] = _legacy_recommended_follow_up(follow_up["recommended"])
    record["follow_up"] = follow_up
    record["judge"] = judge
    return record


def _retrospective_judge_signals(judge: dict[str, Any]) -> list[dict[str, Any]]:
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
    skip_classification: str = "auth_preflight_failed",
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
            "status": workstream_status_from_publication(publication),
        },
        "selected_work": _retrospective_judge_prompt_selected_work(selected_work),
        "retrospective": redact_retrospective(normalized.get("retrospective")),
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
            adapter=_retrospective_follow_up_adapter_record(follow_up_config, None, False),
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
            "status": workstream_status_from_publication(publication),
        },
        "retrospective": redact_retrospective(normalized.get("retrospective")),
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


def _validation_retrospective_signals(state: dict[str, Any]) -> list[dict[str, Any]]:
    validations = state.get("validations")
    if not isinstance(validations, list):
        return []
    signals = []
    for validation in validations:
        if not isinstance(validation, dict):
            continue
        output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
        actionable_failures = output.get("actionable_failures")
        if output.get("status") == "validated" or not isinstance(actionable_failures, list):
            continue
        for failure in actionable_failures:
            if not isinstance(failure, dict):
                continue
            summary = _retrospective_missing_tool_or_config_summary(
                string_field(failure, "excerpt") or string_field(failure, "reason") or string_field(output, "summary") or ""
            )
            if not summary:
                continue
            signals.append(
                {
                    "kind": "missing-tool-or-config",
                    "severity": "error",
                    "summary": summary,
                    "evidence_paths": _retrospective_evidence_paths(
                        string_field(failure, "log_path") or "",
                        string_field(validation, "step_result_path") or "",
                        string_field(validation, "worker_result_path") or "",
                    ),
                }
            )
    return signals


def _publication_retrospective_signals(publication: dict[str, Any]) -> list[dict[str, Any]]:
    reason = string_field(publication, "reason") or ""
    if not reason:
        return []
    redacted_reason = redact_text(reason)
    signals = []
    if publication.get("status") == "failed-needs-human":
        if _publisher_auth_failure_reason(reason):
            signals.append(
                {
                    "kind": "publisher-auth",
                    "severity": "error",
                    "summary": redacted_reason,
                    "evidence_paths": [],
                }
            )
        elif _retrospective_missing_tool_or_config_summary(reason):
            signals.append(
                {
                    "kind": "missing-tool-or-config",
                    "severity": "error",
                    "summary": redacted_reason,
                    "evidence_paths": [],
                }
            )
        else:
            signals.append(
                {
                    "kind": "publisher-failure",
                    "severity": "error",
                    "summary": redacted_reason,
                    "evidence_paths": [],
                }
            )
    return signals


def _blocked_retrospective_signals(publication: dict[str, Any]) -> list[dict[str, Any]]:
    reason = string_field(publication, "reason") or ""
    if publication.get("status") != "blocked" or not reason:
        return []
    return [
        {
            "kind": "retry-or-blocked",
            "severity": "error",
            "summary": redact_text(reason),
            "evidence_paths": [],
        }
    ]


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


def _retrospective_follow_up_record(signals: list[dict[str, Any]], normalized: dict[str, Any] | None) -> dict[str, Any]:
    recommended = []
    created = []
    recommended_fingerprints: set[str] = set()
    recommended_identities: set[str] = set()
    created_fingerprints: set[str] = set()
    created_identities: set[str] = set()
    if normalized is not None:
        retrospective = normalized.get("retrospective") if isinstance(normalized, dict) else {}
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
    for signal in signals:
        kind = string_field(signal, "kind") or ""
        follow_up_item = _follow_up_for_signal(kind)
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
    if string_field(merged, "summary"):
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
    if "project:afk-composable-pipeline" not in normalized_labels:
        normalized_labels.append("project:afk-composable-pipeline")
    return normalized_labels


def _retrospective_follow_up_fingerprint(kind: str, summary: str, labels: list[str]) -> str:
    return "retro-follow-up:" + sha256_json(
        {
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


def _follow_up_for_signal(kind: str) -> dict[str, Any] | None:
    if kind == "missing-tool-or-config":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Fix the missing tool or configuration in validation evidence before rerunning the workstream.",
            labels=["afk:follow-up", "area:validation"],
        )
    if kind == "publisher-auth":
        return _retrospective_follow_up_item(
            kind=kind,
            summary="Repair GitHub publisher authentication evidence before rerunning terminal publication.",
            labels=["afk:follow-up", "area:publication"],
        )
    if kind in {"publisher-failure", "retry-or-blocked"}:
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


def _retrospective_health(signals: list[dict[str, Any]]) -> str:
    severities = {string_field(signal, "severity") or "" for signal in signals if isinstance(signal, dict)}
    if "error" in severities:
        return "failing"
    if "warning" in severities:
        return "warning"
    return "healthy"


def _retrospective_missing_tool_or_config_summary(text: str) -> str:
    if not _retrospective_text_has_missing_tool_or_config(text):
        return ""
    return redact_text(text)


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


def _retrospective_evidence_paths(*paths: str) -> list[str]:
    evidence = []
    for path in paths:
        if not path:
            continue
        redacted = redact_text(path)
        if redacted not in evidence:
            evidence.append(redacted)
    return evidence


def redact_review_cycles(review_cycles: Any) -> list[Any]:
    if not isinstance(review_cycles, list):
        return []
    return [redact_review_cycle(cycle) for cycle in review_cycles]


def redact_retrospective(retrospective: Any) -> dict[str, Any]:
    if not isinstance(retrospective, dict):
        return {}
    return redact_artifact_value(retrospective)


def redact_review_cycle(cycle: Any) -> Any:
    if not isinstance(cycle, dict):
        return redact_artifact_value(cycle)
    redacted = redact_artifact_value(cycle)
    reviews = cycle.get("reviews")
    if not isinstance(reviews, list):
        return redacted
    redacted["reviews"] = [
        redact_review_cycle_review(review, redacted_review)
        for review, redacted_review in zip(reviews, redacted.get("reviews", []))
    ]
    return redacted


def redact_review_cycle_review(review: Any, redacted_review: Any) -> Any:
    if not isinstance(review, dict) or not isinstance(redacted_review, dict):
        return redacted_review
    pr_comment_url = review.get("pr_comment_url")
    if isinstance(pr_comment_url, str) and pr_comment_url:
        redacted_review["pr_comment_url"] = redact_review_cycle_pr_comment_url(pr_comment_url)
    return redacted_review


def redact_review_cycle_pr_comment_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return redact_text(value)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    elif not parsed.username and not parsed.password:
        host = parsed.netloc
    fragment = parsed.fragment if review_cycle_fragment_is_safe(parsed.fragment) else ""
    return urlunsplit((parsed.scheme, host, parsed.path, "", fragment))


def review_cycle_fragment_is_safe(fragment: str) -> bool:
    if not fragment:
        return False
    if redact_text(fragment) != fragment:
        return False
    fragment_keys = [key for key, _ in parse_qsl(fragment, keep_blank_values=True)]
    return not any(is_secret_key(key) for key in fragment_keys)


def normalize_review_cycles(review_cycles: Any) -> list[dict[str, Any]]:
    if review_cycles is None:
        return []
    if not isinstance(review_cycles, list):
        raise WorkstreamError("review_cycles must be a list")
    normalized = []
    for cycle_index, cycle in enumerate(review_cycles):
        if not isinstance(cycle, dict):
            raise WorkstreamError(f"review_cycles[{cycle_index}] must be an object")
        cycle_number = cycle.get("cycle", cycle_index + 1)
        if isinstance(cycle_number, bool) or not isinstance(cycle_number, int) or cycle_number <= 0:
            raise WorkstreamError(f"review_cycles[{cycle_index}].cycle must be a positive integer")
        status = normalize_review_cycle_optional_string(
            cycle,
            "status",
            f"review_cycles[{cycle_index}].status must be a string",
        )
        validate_review_cycle_status(
            status,
            f"review_cycles[{cycle_index}].status must be one of: {', '.join(sorted(REVIEW_CYCLE_STATUSES))}",
        )
        reviews = cycle.get("reviews")
        if not isinstance(reviews, list):
            raise WorkstreamError(f"review_cycles[{cycle_index}].reviews must be a list")
        normalized_reviews = []
        for review_index, review in enumerate(reviews):
            if not isinstance(review, dict):
                raise WorkstreamError(f"review_cycles[{cycle_index}].reviews[{review_index}] must be an object")
            normalized_review = {
                "role": normalize_review_cycle_required_string(
                    review,
                    "role",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].role is required",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].role must be a string",
                ),
                "status": normalize_review_cycle_required_string(
                    review,
                    "status",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].status is required",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].status must be a string",
                ),
                "summary": normalize_review_cycle_required_string(
                    review,
                    "summary",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].summary is required",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].summary must be a string",
                ),
                "requires_response": normalize_review_cycle_boolean(
                    review,
                    "requires_response",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].requires_response must be a boolean",
                ),
            }
            validate_review_cycle_status(
                normalized_review["status"],
                "review_cycles"
                f"[{cycle_index}].reviews[{review_index}].status must be one of: "
                f"{', '.join(sorted(REVIEW_CYCLE_STATUSES))}",
            )
            pr_comment_url = normalize_review_cycle_optional_string(
                review,
                "pr_comment_url",
                f"review_cycles[{cycle_index}].reviews[{review_index}].pr_comment_url must be a string",
            )
            if pr_comment_url:
                normalized_review["pr_comment_url"] = pr_comment_url
            if "response" in review:
                response = review["response"]
                if not isinstance(response, (str, dict)):
                    raise WorkstreamError(
                        f"review_cycles[{cycle_index}].reviews[{review_index}].response must be a string or object"
                    )
                normalized_review["response"] = normalize_review_cycle_response(
                    response,
                    cycle_index=cycle_index,
                    review_index=review_index,
                )
            normalized_reviews.append(normalized_review)
        normalized.append({"cycle": cycle_number, "status": status, "reviews": normalized_reviews})
    return normalized


def normalize_retrospective(retrospective: Any) -> dict[str, Any]:
    if retrospective is None:
        return {}
    if not isinstance(retrospective, dict):
        raise WorkstreamError("retrospective must be an object")
    unsupported = [
        key
        for key in retrospective
        if key not in {"summary", "changes", "validation", "review", "unresolved_risks", "process_findings", "follow_up", "notes"}
    ]
    if unsupported:
        raise WorkstreamError(
            "retrospective only supports summary, changes, validation, review, unresolved_risks, "
            "process_findings, follow_up, notes"
        )
    normalized: dict[str, Any] = {}
    summary = normalize_retrospective_optional_string(
        retrospective,
        "summary",
        "retrospective.summary must be a string",
    )
    if summary:
        normalized["summary"] = summary
    for key in ("changes", "validation", "review", "unresolved_risks", "process_findings"):
        values = normalize_retrospective_string_list(
            retrospective,
            key,
            f"retrospective.{key} must be a list",
            f"retrospective.{key} entries must be strings",
        )
        if values:
            normalized[key] = values
    follow_up = normalize_retrospective_follow_up(retrospective.get("follow_up"))
    if follow_up:
        normalized["follow_up"] = follow_up
    notes = normalize_retrospective_notes(retrospective.get("notes"))
    if notes:
        normalized["notes"] = notes
    return normalized


def normalize_retrospective_judge(
    retrospective_judge: Any,
    *,
    checkout_path: Path | None = None,
    checkout_paths: list[Path] | None = None,
) -> dict[str, Any]:
    if retrospective_judge is None:
        return {"enabled": False}
    if not isinstance(retrospective_judge, dict):
        raise WorkstreamError("retrospective_judge must be an object")
    unsupported = [
        key
        for key in retrospective_judge
        if key
        not in {"enabled", "type", "command", "timeout_seconds", "timeoutSeconds", "codex_home", "config_home", "env"}
    ]
    if unsupported:
        raise WorkstreamError(
            "retrospective_judge only supports enabled, type, command, timeout_seconds, codex_home, config_home, env"
        )
    enabled = retrospective_judge.get("enabled", False)
    if not isinstance(enabled, bool):
        raise WorkstreamError("retrospective_judge.enabled must be a boolean")
    if not enabled:
        return {"enabled": False}
    judge_type = string_field(retrospective_judge, "type") or "local-command"
    if judge_type not in {"local-command", "fake-judge-command"}:
        raise WorkstreamError("retrospective_judge.type must be local-command or fake-judge-command")
    for forbidden_key in ("credentials_path", "auth_file", "token", "api_key"):
        if forbidden_key in retrospective_judge:
            raise WorkstreamError(f"retrospective_judge.{forbidden_key} is not supported")
    command = retrospective_judge.get("command")
    if not _is_string_list(command):
        raise WorkstreamError("retrospective_judge.command must be a list of strings")
    if not command:
        raise WorkstreamError("retrospective_judge.command must not be empty")
    command_secret_error = _command_secret_error_message(command, field_name="retrospective_judge.command")
    if command_secret_error:
        raise WorkstreamError(command_secret_error)
    timeout_seconds = retrospective_judge.get("timeout_seconds", retrospective_judge.get("timeoutSeconds", 120))
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise WorkstreamError("retrospective_judge.timeout_seconds must be a positive number")
    normalized = {
        "enabled": True,
        "type": judge_type,
        "command": list(command),
        "timeout_seconds": float(timeout_seconds),
    }
    checkout_mount_boundaries = checkout_paths or ([checkout_path] if checkout_path is not None else [])
    for field_name in ("codex_home", "config_home"):
        raw_value = retrospective_judge.get(field_name)
        if raw_value is None:
            continue
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise WorkstreamError(f"retrospective_judge.{field_name} must be an absolute directory path")
        try:
            normalized[field_name] = validate_retrospective_judge_mount_dir(
                raw_value,
                f"retrospective_judge.{field_name}",
                checkout_path=checkout_path,
                checkout_paths=checkout_mount_boundaries,
            )
        except ValueError as exc:
            raise WorkstreamError(str(exc)) from exc
    raw_env = retrospective_judge.get("env")
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            raise WorkstreamError("retrospective_judge.env must be an object")
        normalized_env: dict[str, str] = {}
        for key, value in raw_env.items():
            if key not in {"PI_CONFIG_HOME", "PI_CODING_AGENT_DIR"}:
                raise WorkstreamError(
                    "retrospective_judge.env only supports PI_CONFIG_HOME and PI_CODING_AGENT_DIR"
                )
            if not isinstance(value, str) or not value.strip():
                raise WorkstreamError(f"retrospective_judge.env.{key} must be an absolute directory path")
            try:
                normalized_env[key] = validate_retrospective_judge_mount_dir(
                    value,
                    f"retrospective_judge.env.{key}",
                    checkout_path=checkout_path,
                    checkout_paths=checkout_mount_boundaries,
                )
            except ValueError as exc:
                raise WorkstreamError(str(exc)) from exc
        normalized["env"] = normalized_env
    mount_error = openai_codex_pi_mount_error(
        command=normalized["command"],
        codex_home=normalized.get("codex_home"),
        config_home=normalized.get("config_home"),
        env=normalized.get("env"),
        field_prefix="retrospective_judge",
    )
    if mount_error:
        raise WorkstreamError(mount_error)
    mount_rejection = non_openai_pi_mount_error(
        command=normalized["command"],
        codex_home=normalized.get("codex_home"),
        config_home=normalized.get("config_home"),
        env=normalized.get("env"),
        field_prefix="retrospective_judge",
    )
    if mount_rejection:
        raise WorkstreamError(mount_rejection)
    return normalized


def validate_retrospective_judge_mount_dir(
    value: str,
    field: str,
    *,
    checkout_path: Path | None,
    checkout_paths: list[Path],
) -> str:
    if checkout_path is None:
        path_value = Path(value)
        if not path_value.is_absolute():
            raise ValueError(f"{field} must be absolute")
        if not path_value.is_dir():
            raise ValueError(f"{field} must be an existing directory")
        for checkout_boundary in checkout_paths:
            if path_is_equal_to_or_inside(path_value, checkout_boundary):
                raise ValueError(f"{field} must be outside checkout")
        return value
    normalized = validate_absolute_dir(value, field, checkout_path=checkout_path)
    path_value = Path(normalized)
    for checkout_boundary in checkout_paths:
        if path_is_equal_to_or_inside(path_value, checkout_boundary):
            raise ValueError(f"{field} must be outside checkout")
    return normalized


def normalize_retrospective_follow_up_config(retrospective_follow_up: Any) -> dict[str, Any]:
    if retrospective_follow_up is None:
        return {"enabled": False}
    if not isinstance(retrospective_follow_up, dict):
        raise WorkstreamError("retrospective_follow_up must be an object")
    for forbidden_key in ("credentials_path", "auth_file", "token", "api_key", "env"):
        if forbidden_key in retrospective_follow_up:
            raise WorkstreamError(f"retrospective_follow_up.{forbidden_key} is not supported")
    unsupported = [
        key
        for key in retrospective_follow_up
        if key not in {"enabled", "type", "command", "timeout_seconds", "timeoutSeconds"}
    ]
    if unsupported:
        raise WorkstreamError("retrospective_follow_up only supports enabled, type, command, timeout_seconds")
    enabled = retrospective_follow_up.get("enabled", False)
    if not isinstance(enabled, bool):
        raise WorkstreamError("retrospective_follow_up.enabled must be a boolean")
    if not enabled:
        return {"enabled": False}
    follow_up_type = string_field(retrospective_follow_up, "type") or "local-command"
    if follow_up_type not in {"local-command", "fake-follow-up-command"}:
        raise WorkstreamError("retrospective_follow_up.type must be local-command or fake-follow-up-command")
    command = retrospective_follow_up.get("command")
    if not _is_string_list(command):
        raise WorkstreamError("retrospective_follow_up.command must be a list of strings")
    if not command:
        raise WorkstreamError("retrospective_follow_up.command must not be empty")
    command_secret_error = _command_secret_error_message(command, field_name="retrospective_follow_up.command")
    if command_secret_error:
        raise WorkstreamError(command_secret_error)
    timeout_seconds = retrospective_follow_up.get(
        "timeout_seconds",
        retrospective_follow_up.get("timeoutSeconds", 120),
    )
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise WorkstreamError("retrospective_follow_up.timeout_seconds must be a positive number")
    return {
        "enabled": True,
        "type": follow_up_type,
        "command": list(command),
        "timeout_seconds": float(timeout_seconds),
    }


def validate_retrospective_terminal_decision(retrospective: dict[str, Any], tracker: dict[str, Any]) -> None:
    if not retrospective:
        return
    decision = tracker.get("terminal_decision") if isinstance(tracker, dict) else {}
    if isinstance(decision, dict) and decision.get("status") in {"merged", "no-merge"}:
        return
    raise WorkstreamError("retrospective requires tracker.terminal_decision.status to be merged or no-merge")


def review_cycles_require_response(review_cycles: Any) -> bool:
    if not isinstance(review_cycles, list):
        return False
    for cycle in review_cycles:
        if not isinstance(cycle, dict):
            continue
        cycle_status = string_field(cycle, "status") or ""
        reviews = cycle.get("reviews")
        if not isinstance(reviews, list):
            continue
        for review in reviews:
            if not isinstance(review, dict):
                continue
            review_status = string_field(review, "status") or ""
            requires_response = bool(review.get("requires_response"))
            response = review.get("response")
            response_is_addressed = review_cycle_response_is_addressed(response)
            if requires_response and not response_is_addressed:
                return True
            if review_cycle_status_requires_response(cycle_status) or review_cycle_status_requires_response(
                review_status
            ):
                if not response_is_addressed:
                    return True
    return False


def validate_review_cycle_status(status: str, error_message: str) -> None:
    if status and status not in REVIEW_CYCLE_STATUSES:
        raise WorkstreamError(error_message)


def normalize_review_cycle_response(
    response: str | dict[str, Any],
    *,
    cycle_index: int,
    review_index: int,
) -> str | dict[str, Any]:
    if isinstance(response, str):
        return response.strip()
    normalized = dict(response)
    status = normalize_review_cycle_required_string(
        normalized,
        "status",
        f"review_cycles[{cycle_index}].reviews[{review_index}].response.status is required",
        f"review_cycles[{cycle_index}].reviews[{review_index}].response.status must be a string",
    )
    if status not in REVIEW_CYCLE_RESPONSE_STATUSES:
        allowed = ", ".join(sorted(REVIEW_CYCLE_RESPONSE_STATUSES))
        raise WorkstreamError(
            f"review_cycles[{cycle_index}].reviews[{review_index}].response.status must be one of: {allowed}"
        )
    normalized["status"] = status
    summary = normalized.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise WorkstreamError(
            f"review_cycles[{cycle_index}].reviews[{review_index}].response.summary must be a string"
        )
    if isinstance(summary, str):
        normalized["summary"] = summary.strip()
    return normalized


def review_cycle_status_requires_response(status: str) -> bool:
    return status in REVIEW_CYCLE_OPEN_STATUSES


def review_cycle_response_is_addressed(response: Any) -> bool:
    if isinstance(response, str):
        return bool(response.strip())
    if not isinstance(response, dict):
        return False
    return (string_field(response, "status") or "") in REVIEW_CYCLE_RESPONSE_STATUSES


def blocked_publication(reason: str, normalized: dict[str, Any], run_id: str) -> dict[str, Any]:
    next_allowed_command = rerun_workstream_command(normalized)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "enabled": True,
        "reason": reason,
        "next_allowed_command": next_allowed_command,
        "retry": retry_instructions(normalized, run_id),
    }


def validated_unpublished_publication(reason: str, *, next_allowed_command: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "validated-unpublished",
        "enabled": False,
        "reason": reason,
        "next_allowed_command": next_allowed_command,
        "retry": "",
    }


def workstream_status_from_publication(publication: dict[str, Any]) -> str:
    if publication["status"] == "published":
        return "published"
    if publication["status"] == "validated-unpublished":
        return "validated-unpublished"
    if publication["status"] == "tracker-close-blocked":
        return "review-findings-open"
    if publication["status"] == "tracker-closed":
        return "closed"
    if publication["status"] == "blocked":
        return "blocked"
    return "failed-needs-human"


def workstream_artifacts(ledger: "WorkstreamLedger") -> dict[str, str]:
    artifacts = {
        "workstream_result": "workstream-result.json",
        "command": "command.json",
        "publication": "publication-result.json",
        "tracker": "tracker-result.json",
        "pipeline_retrospective": "pipeline-retrospective.json",
    }
    if (ledger.path / "pi-auth-preflight.json").is_file():
        artifacts["pi_auth_preflight"] = "pi-auth-preflight.json"
    if (ledger.path / "pr-body.md").is_file():
        artifacts["pr_body"] = "pr-body.md"
    if (ledger.path / "retrospective.json").is_file():
        artifacts["retrospective"] = "retrospective.json"
    if (ledger.path / "retrospective-judge-evidence.json").is_file():
        artifacts["retrospective_judge_evidence"] = "retrospective-judge-evidence.json"
    if (ledger.path / "retrospective-judge-request.json").is_file():
        artifacts["retrospective_judge_request"] = "retrospective-judge-request.json"
    if (ledger.path / "retrospective-judge-result.json").is_file():
        artifacts["retrospective_judge_result"] = "retrospective-judge-result.json"
    if (ledger.path / "retrospective-judge-stdout.log").is_file():
        artifacts["retrospective_judge_stdout"] = "retrospective-judge-stdout.log"
    if (ledger.path / "retrospective-judge-stderr.log").is_file():
        artifacts["retrospective_judge_stderr"] = "retrospective-judge-stderr.log"
    if (ledger.path / "retrospective-follow-up-request.json").is_file():
        artifacts["retrospective_follow_up_request"] = "retrospective-follow-up-request.json"
    if (ledger.path / "retrospective-follow-up-result.json").is_file():
        artifacts["retrospective_follow_up_result"] = "retrospective-follow-up-result.json"
    if (ledger.path / "retrospective-follow-up-stdout.log").is_file():
        artifacts["retrospective_follow_up_stdout"] = "retrospective-follow-up-stdout.log"
    if (ledger.path / "retrospective-follow-up-stderr.log").is_file():
        artifacts["retrospective_follow_up_stderr"] = "retrospective-follow-up-stderr.log"
    return artifacts


def failed_publication(
    exc: PublisherError,
    normalized: dict[str, Any],
    *,
    auth: dict[str, Any],
) -> dict[str, Any]:
    next_allowed_command = rerun_workstream_command(normalized)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "failed-needs-human",
        "enabled": True,
        "reason": exc.message,
        "auth": auth,
        "returncode": exc.returncode,
        "command": redact_artifact_value(exc.command),
        "stdout_excerpt": redact_text(exc.stdout[-2000:]),
        "stderr_excerpt": redact_text(exc.stderr[-2000:]),
        "next_allowed_command": next_allowed_command,
        "retry": retry_instructions(normalized, "latest", auth_hint=True),
    }
    if exc.details:
        result.update(redact_artifact_value(exc.details))
    return result


def failed_publication_config(
    reason: str,
    normalized: dict[str, Any],
    *,
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_allowed_command = rerun_workstream_command(normalized)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed-needs-human",
        "enabled": True,
        "reason": reason,
        "auth": auth or {"configured": False, "source": "minimal_env"},
        "returncode": None,
        "command": [],
        "stdout_excerpt": "",
        "stderr_excerpt": "",
        "next_allowed_command": next_allowed_command,
        "retry": retry_instructions(normalized, "latest", auth_hint=True),
    }


def retry_instructions(normalized: dict[str, Any], run_id: str, auth_hint: bool = False) -> str:
    rerun_command = rerun_workstream_command(normalized)
    instructions = (
        "Fix the failed evidence, keep the shared review branch, and rerun "
        f"{rerun_command}; previous workstream run: {run_id}"
    )
    if auth_hint:
        instructions += (
            ". For GitHub publication, mount a GitHub CLI config directory outside the checkout "
            "and set publisher.gh.auth.config_dir in the recipe before rerunning"
        )
    return instructions


def rerun_workstream_command(normalized: dict[str, Any]) -> str:
    return (
        f"afk run-workstream --workstream-id {normalized['workstream_id']} --ledger <ledger> "
        "--input <recipe>"
    )


def checkout_path_from_state(state: dict[str, Any]) -> Path:
    checkout = state.get("checkout") if isinstance(state.get("checkout"), dict) else {}
    path = checkout.get("checkout_path") or checkout.get("path")
    if isinstance(path, str) and path:
        return Path(path)
    return Path.cwd()


def prepared_checkout_path_from_state(state: dict[str, Any]) -> Path | None:
    checkout = state.get("checkout") if isinstance(state.get("checkout"), dict) else {}
    if checkout.get("status") != "prepared":
        return None
    path = checkout.get("checkout_path") or checkout.get("path")
    if isinstance(path, str) and path:
        return Path(path)
    return None


def validation_name(validation: dict[str, Any]) -> str:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    info = output.get("validation") if isinstance(output.get("validation"), dict) else {}
    return string_field(info, "requested_profile") or string_field(info, "worker_profile") or "validation"


def pr_body_validation_line(validation: dict[str, Any], index: int) -> str:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    profile = validation_name_for_body(validation, index)
    status = string_field(output, "status") or "missing"
    evidence = validation_worker_evidence_for_body(output)
    step_ref = ledger_relative_path(string_field(validation, "step_result_path") or "")
    worker_ref = ledger_relative_path(string_field(validation, "worker_result_path") or "")
    path_evidence = "; ".join(item for item in [step_ref, worker_ref] if item)
    parts = [f"- {pr_body_value(profile)}: {pr_body_value(status)}"]
    if evidence:
        parts.append(pr_body_value(evidence))
    if path_evidence:
        parts.append(f"evidence: {pr_body_value(path_evidence)}")
    return " - ".join(parts)


def validation_name_for_body(validation: dict[str, Any], index: int) -> str:
    name = validation_name(validation)
    return name if name != "validation" else f"validation-{index + 1}"


def validation_worker_evidence_for_body(output: dict[str, Any]) -> str:
    worker_result = output.get("worker_result") if isinstance(output.get("worker_result"), dict) else {}
    raw_result = worker_result.get("raw") if isinstance(worker_result.get("raw"), dict) else {}
    normalized = worker_result.get("normalized") if isinstance(worker_result.get("normalized"), dict) else {}
    result = validation_worker_result_summary(raw_result) or "missing"
    command = validation_worker_command_summary(normalized) or "missing"
    summary = string_field(output, "summary") or string_field(normalized, "summary") or "missing"
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
    marker = "/runs/"
    if marker in path:
        return "runs/" + path.split(marker, 1)[1]
    marker = "/workstreams/"
    if marker in path:
        return "workstreams/" + path.split(marker, 1)[1]
    return path


def string_field(input_data: dict[str, Any], key: str) -> str | None:
    value = input_data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _command_secret_error_message(command: list[str], *, field_name: str) -> str | None:
    for part in command:
        if is_secret_command_flag(part):
            flag = part.strip().split("=", 1)[0].lower()
            return f"{field_name} must not include credential flag {flag}"
        if is_secret_value(part) or COMMAND_BEARER_SECRET_PATTERN.search(part):
            return f"{field_name} must not include secret-looking values"
    return None


def normalize_review_cycle_optional_string(input_data: dict[str, Any], key: str, error_message: str) -> str:
    value = input_data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise WorkstreamError(error_message)
    return value.strip()


def normalize_retrospective_optional_string(input_data: dict[str, Any], key: str, error_message: str) -> str:
    value = input_data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise WorkstreamError(error_message)
    return value.strip()


def normalize_retrospective_string_list(
    input_data: dict[str, Any],
    key: str,
    invalid_message: str,
    item_message: str,
) -> list[str]:
    value = input_data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkstreamError(invalid_message)
    normalized = []
    for item in value:
        if not isinstance(item, str):
            raise WorkstreamError(item_message)
        stripped = item.strip()
        if stripped:
            normalized.append(stripped)
    return normalized


def normalize_retrospective_follow_up(follow_up: Any) -> dict[str, list[dict[str, Any]]]:
    if follow_up is None:
        return {}
    if not isinstance(follow_up, dict):
        raise WorkstreamError("retrospective.follow_up must be an object")
    unsupported = [key for key in follow_up if key not in {"recommended", "created"}]
    if unsupported:
        raise WorkstreamError("retrospective.follow_up only supports recommended and created")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for key in ("recommended", "created"):
        items = follow_up.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            raise WorkstreamError(f"retrospective.follow_up.{key} must be a list")
        normalized_items = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise WorkstreamError(f"retrospective.follow_up.{key}[{index}] must be an object")
            unsupported_item_keys = [field for field in item if field not in {"id", "summary", "labels"}]
            if unsupported_item_keys:
                raise WorkstreamError(
                    f"retrospective.follow_up.{key}[{index}] only supports id, summary, labels"
                )
            normalized_item: dict[str, Any] = {}
            item_id = normalize_retrospective_optional_string(
                item,
                "id",
                f"retrospective.follow_up.{key}[{index}].id must be a string",
            )
            if item_id:
                normalized_item["id"] = item_id
            summary = normalize_retrospective_optional_string(
                item,
                "summary",
                f"retrospective.follow_up.{key}[{index}].summary must be a string",
            )
            if summary:
                normalized_item["summary"] = summary
            labels = item.get("labels")
            if labels is not None:
                if not isinstance(labels, list):
                    raise WorkstreamError(f"retrospective.follow_up.{key}[{index}].labels must be a list")
                normalized_labels = []
                for label in labels:
                    if not isinstance(label, str):
                        raise WorkstreamError(
                            f"retrospective.follow_up.{key}[{index}].labels entries must be strings"
                        )
                    stripped_label = label.strip()
                    if stripped_label:
                        normalized_labels.append(stripped_label)
                if normalized_labels:
                    normalized_item["labels"] = normalized_labels
            normalized_items.append(normalized_item)
        normalized[key] = normalized_items
    return normalized


def normalize_retrospective_notes(notes: Any) -> dict[str, list[str]]:
    if notes is None:
        return {}
    if not isinstance(notes, dict):
        raise WorkstreamError("retrospective.notes must be an object")
    unsupported = [key for key in notes if key not in {"personal_work", "spikes"}]
    if unsupported:
        raise WorkstreamError("retrospective.notes only supports personal_work and spikes")
    normalized: dict[str, list[str]] = {}
    for key in ("personal_work", "spikes"):
        values = notes.get(key)
        if values is None:
            continue
        if not isinstance(values, list):
            raise WorkstreamError(f"retrospective.notes.{key} must be a list")
        normalized_values = []
        for value in values:
            if not isinstance(value, str):
                raise WorkstreamError(f"retrospective.notes.{key} entries must be strings")
            stripped = value.strip()
            if stripped:
                normalized_values.append(stripped)
        normalized[key] = normalized_values
    return normalized


def normalize_review_cycle_required_string(
    input_data: dict[str, Any],
    key: str,
    missing_message: str,
    invalid_message: str,
) -> str:
    value = input_data.get(key)
    if value is None:
        raise WorkstreamError(missing_message)
    if not isinstance(value, str):
        raise WorkstreamError(invalid_message)
    stripped = value.strip()
    if not stripped:
        raise WorkstreamError(missing_message)
    return stripped


def normalize_review_cycle_boolean(input_data: dict[str, Any], key: str, error_message: str) -> bool:
    value = input_data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise WorkstreamError(error_message)
    return value


def path_is_equal_to_or_inside(path: Path, parent: Path) -> bool:
    for candidate in (path, path.resolve()):
        try:
            candidate.relative_to(parent.resolve())
            return True
        except ValueError:
            pass
    return False


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class WorkstreamLedger:
    def __init__(self, ledger_dir: Path, run_id: str):
        self.run_id = run_id
        self.path = ledger_dir / "workstreams" / run_id

    def prepare(self) -> None:
        self.path.mkdir(parents=True, exist_ok=False)

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        (self.path / name).write_text(canonical_json(payload) + "\n", encoding="utf-8")

    def write_text(self, name: str, content: str) -> None:
        (self.path / name).write_text(content, encoding="utf-8")
