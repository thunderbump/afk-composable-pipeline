from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from afk.contracts import ProjectContract
from afk import evidence_gate
from afk.implement import runtime_failure_excerpt, safe_git_metadata
from afk.jsonutil import canonical_json, sha256_json
from afk.pi_workers import (
    non_openai_pi_mount_error,
    openai_codex_pi_mount_error,
    validate_absolute_dir,
)
from afk.publication import (
    PublicationRequest,
    PublisherError,
    publish_terminal_pr as run_publication,
    publisher_auth_artifact,
    run_publisher_command,
)
from afk.redaction import (
    bearer_secret_present,
    is_bearer_secret_value,
    is_secret_command_flag,
    is_secret_value,
    normalize_bearer_secret_value,
    redact_artifact_value,
    redact_text,
)
from afk.recipes import review_branch_for_workstream
from afk.registry import StepResult
from afk.retrospective import (
    RetrospectiveContext,
    _apply_retrospective_judge,
    _retrospective_follow_up_bead_description,
    _retrospective_follow_up_bead_labels,
    _retrospective_follow_up_fingerprint,
    _retrospective_text_has_missing_tool_or_config,
    _validation_feedback_text_has_infra_or_setup_failure,
    build_pipeline_retrospective,
    effective_retrospective,
    pipeline_retrospective_record,
    redacted_terminal_retrospective,
)
from afk.tracking import (
    TrackerContext,
    build_tracker_record,
    effective_review_cycles,
    effective_tracker_terminal_decision,
    empty_terminal_decision,
    redact_retrospective,
    redact_review_cycles,
    review_cycles_recorded,
    review_cycles_require_response,
    runtime_terminal_decision,
    terminal_review_feedback_status,
    tracker_close_blocked_publication,
    tracker_close_failure_artifact,
    tracker_record,
    tracker_review_cycles,
    tracker_terminal_decision_allows_close,
    tracker_terminal_decision_close_block_reason,
    tracker_terminal_decision_present,
    tracker_terminal_decision_publication,
)
from afk.workstream_lifecycle import (
    LifecycleHooks,
    current_review_record,
    current_validation_records,
    has_current_validated_evidence,
    implemented_after_commit,
    retry_attempt_count,
    retry_attempt_records,
    retry_budget_record,
    review_passed,
    review_status,
    run_lifecycle,
    selected_work_records,
    terminal_selected_work_status,
    tracker_selected_work_status,
    validation_gate_entry,
    review_gate_entry,
    workstream_status_from_publication,
)


SCHEMA_VERSION = 1
KNOWN_WORKSTREAM_STEPS = {"select-work", "prepare-checkout", "implement", "validate", "review"}
REVIEW_CYCLE_STATUSES = {"passed", "findings-open", "findings-addressed", "request-changes"}
REVIEW_CYCLE_OPEN_STATUSES = {"findings-open", "request-changes"}
REVIEW_CYCLE_RESPONSE_STATUSES = {"addressed", "findings-addressed"}
TERMINAL_REVIEW_FEEDBACK_STATUSES = {"resolved", "waived"}


@dataclass(frozen=True)
class WorkstreamResult:
    run_id: str
    workstream_id: str
    parent: str
    status: str
    result_path: str
    publication_status: str


StepRunner = Callable[[str, Any, Path, ProjectContract | None], StepResult]


@dataclass(frozen=True)
class PipelinePlan:
    normalized: dict[str, Any]
    run_id: str


@dataclass(frozen=True)
class PipelineOutcome:
    state: dict[str, Any]
    steps: list[dict[str, Any]]
    publication: dict[str, Any]


class WorkstreamError(ValueError):
    pass


class PipelineEngine:
    def run(
        self,
        plan: PipelinePlan,
        *,
        ledger_dir: Path,
        ledger: Any,
        step_runner: StepRunner,
        project_contract: ProjectContract | None = None,
    ) -> PipelineOutcome:
        lifecycle = run_lifecycle(
            normalized=plan.normalized,
            run_id=plan.run_id,
            ledger_dir=ledger_dir,
            ledger=ledger,
            step_runner=step_runner,
            project_contract=project_contract,
            hooks=LifecycleHooks(
                composed_step_input=composed_step_input,
                equivalent_run_step_command=equivalent_run_step_command,
                step_execution_record=step_execution_record,
                update_state_from_step=update_state_from_step,
                publish_terminal_pr=publish_terminal_pr,
            ),
        )
        return PipelineOutcome(
            state=lifecycle.state,
            steps=lifecycle.steps,
            publication=lifecycle.publication,
        )


PIPELINE_ENGINE = PipelineEngine()


def run_workstream(
    recipe: Any,
    *,
    ledger_dir: Path,
    rerun_ledger_arg: str | None = None,
    step_runner: StepRunner,
    parent: str | None = None,
    workstream_id: str | None = None,
    project_contract: ProjectContract | None = None,
) -> WorkstreamResult:
    normalization_input = recipe
    if isinstance(recipe, dict):
        normalization_input = dict(recipe)
        normalization_input.pop("retrospective_judge", None)
        normalization_input.pop("retrospective_follow_up", None)
    normalized = normalize_recipe(normalization_input, parent=parent, workstream_id=workstream_id)
    normalized["rerun_ledger_arg"] = rerun_ledger_arg
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

    lifecycle = PIPELINE_ENGINE.run(
        PipelinePlan(normalized=normalized, run_id=run_id),
        ledger_dir=ledger_dir,
        ledger=ledger,
        step_runner=step_runner,
        project_contract=project_contract,
    )
    state = lifecycle.state
    steps = lifecycle.steps
    publication = lifecycle.publication
    tracker = build_tracker_record(
        TrackerContext(
            normalized=normalized,
            state=state,
            publication=publication,
            retrospective=effective_retrospective(normalized, publication),
            schema_version=SCHEMA_VERSION,
        )
    )
    status = workstream_status_from_publication(publication, tracker)
    selected_work = selected_work_records(state)
    pipeline_retrospective = build_pipeline_retrospective(
        RetrospectiveContext(
            state=state,
            publication=publication,
            tracker=tracker,
            normalized=normalized,
        )
    )

    ledger.write_json("publication-result.json", publication)
    ledger.write_json("tracker-result.json", tracker)
    terminal_retrospective = effective_retrospective(normalized, publication)
    redacted_retrospective = redacted_terminal_retrospective(normalized, publication)
    if terminal_retrospective:
        ledger.write_json("retrospective.json", redacted_retrospective)
    ledger.write_json("pipeline-retrospective.json", pipeline_retrospective)
    result_payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workstream_id": normalized["workstream_id"],
        "parent": normalized["parent"],
        "review_branch": normalized["review_branch"],
        "status": status,
        "review_cycles": redact_review_cycles(effective_review_cycles(normalized, state)),
        "retrospective": redacted_retrospective,
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
        "outcome": {
            "functional": {
                "status": status,
                "publication_status": publication.get("status", ""),
                "reason": publication.get("reason", ""),
            },
            "process_retrospective": {
                "status": "action-needed" if pipeline_retrospective.get("health") in {"warning", "failing"} else "clear",
                "health": pipeline_retrospective.get("health", "healthy"),
            },
        },
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
    review_branch = string_field(recipe, "review_branch") or review_branch_for_workstream(resolved_workstream_id)
    publisher = recipe.get("publisher", {"enabled": False})
    retry_policy = normalize_retry_policy(recipe.get("retry_policy"))
    validation_feedback = normalize_validation_feedback(recipe.get("validation_feedback"))
    validation_expectations = normalize_validation_expectations(recipe.get("validation_expectations"))
    review_feedback = normalize_review_feedback(recipe.get("review_feedback"))
    tracker = normalize_tracker_config(recipe.get("tracker"))
    review_cycles = normalize_review_cycles(recipe.get("review_cycles"))
    retrospective = normalize_retrospective(recipe.get("retrospective"))
    retrospective_follow_up = normalize_retrospective_follow_up_config(recipe.get("retrospective_follow_up"))
    retrospective_judge = normalize_retrospective_judge(
        recipe.get("retrospective_judge"),
        checkout_path=recipe_checkout_path(normalized_steps),
        checkout_paths=recipe_checkout_paths(normalized_steps),
    )
    validate_retrospective_terminal_decision(retrospective, tracker, publisher=recipe.get("publisher"))
    return {
        "schema_version": SCHEMA_VERSION,
        "workstream_id": resolved_workstream_id,
        "parent": resolved_parent,
        "review_branch": review_branch,
        "steps": normalized_steps,
        "publisher": publisher,
        "retry_policy": retry_policy,
        "validation_feedback": validation_feedback,
        "validation_expectations": validation_expectations,
        "review_feedback": review_feedback,
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
    *,
    step_index: int | None = None,
) -> dict[str, Any]:
    step_name = step_spec["name"]
    input_data = dict(step_spec["input"])
    if step_name == "select-work":
        exclude_ids = attempted_work_aliases(state)
        target_ids = input_data.get("target_ids")
        if exclude_ids and isinstance(target_ids, list):
            explicit_target_suffixes: set[str] = set()
            for item in target_ids:
                if not isinstance(item, str):
                    continue
                target_id = item.strip()
                if not target_id:
                    continue
                explicit_target_suffixes.add(target_id)
                explicit_target_suffixes.add(target_id.rsplit(":", 1)[-1])
            exclude_ids = {
                item
                for item in exclude_ids
                if item not in explicit_target_suffixes
                and not any(item.endswith(f":{suffix}") for suffix in explicit_target_suffixes)
            }
        if exclude_ids:
            request_exclude_ids = input_data.get("exclude_ids")
            if isinstance(request_exclude_ids, list):
                exclude_ids.update(
                    item.strip() for item in request_exclude_ids if isinstance(item, str) and item.strip()
                )
            input_data["exclude_ids"] = sorted(exclude_ids)
    elif step_name == "prepare-checkout":
        input_data["review_branch"] = normalized["review_branch"]
        checkout = state.get("checkout")
        if isinstance(checkout, dict):
            current_checkout_path = string_field(checkout, "checkout_path")
            next_checkout_path = string_field(input_data, "checkout_path")
            requested_ref = string_field(checkout, "start_commit") or string_field(checkout, "requested_ref")
            base_commit = string_field(checkout, "base_commit") or string_field(checkout, "start_commit")
            has_explicit_requested_ref = bool(string_field(input_data, "requested_ref") or string_field(input_data, "ref"))
            if (
                requested_ref
                and current_checkout_path
                and current_checkout_path == next_checkout_path
                and not has_explicit_requested_ref
            ):
                input_data["requested_ref"] = requested_ref
                input_data["base_commit"] = base_commit
    elif step_name == "implement":
        input_data["work_selection"] = {"schema_version": SCHEMA_VERSION, "selected_work": state["selected_work"]}
        if selected_work_count(state) > 1 and "work_index" not in input_data and "work_scope" not in input_data:
            input_data["work_scope"] = "selection"
        if state.get("checkout") is not None:
            input_data["checkout"] = state["checkout"]
        else:
            input_data.pop("checkout", None)
        repair_context = pending_repair_context(state)
        if repair_context is not None:
            input_data["repair_context"] = repair_context
        else:
            input_data.pop("repair_context", None)
        input_data["validation"] = merged_implement_validation_input(
            input_data.get("validation"),
            normalized["steps"],
            step_index=step_index,
        )
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
            input_data["implementation"] = review_implementation_input(state)
        validation = input_data.get("validation", {})
        if not isinstance(validation, dict):
            validation = {}
        input_data["validation"] = {
            **validation,
            "required_artifacts": validation_artifact_refs(state, ledger_dir),
        }
        input_data.setdefault("cleanup", {"status": "clean", "resources": []})
    return input_data


def merged_implement_validation_input(
    implement_validation: Any,
    steps: list[dict[str, Any]],
    *,
    step_index: int | None = None,
) -> dict[str, Any]:
    if isinstance(implement_validation, dict):
        merged = dict(implement_validation)
    else:
        merged = {}
    validate_context = validate_step_implement_context(
        steps,
        implement_step_index=step_index,
        preferred_profile=string_field(merged, "profile"),
    )
    if "profile" not in merged and isinstance(validate_context.get("profile"), str):
        merged["profile"] = validate_context["profile"]
    validate_commands = validate_context.get("commands", [])
    explicitly_suppresses_commands = merged.get("run_commands_during_implementation") is False
    should_backfill_commands = (
        "commands" not in merged and not explicitly_suppresses_commands
    ) or (
        merged.get("commands") == [] and bool(validate_commands) and not explicitly_suppresses_commands
    )
    if should_backfill_commands:
        merged["commands"] = validate_commands
    for field in ("worker_home", "stack"):
        if field not in validate_context:
            continue
        if field == "worker_home" and ("worker_home" in merged or "workerHome" in merged):
            continue
        if field in merged:
            continue
        merged[field] = validate_context[field]
    return merged


def validate_step_implement_context(
    steps: list[dict[str, Any]],
    *,
    implement_step_index: int | None = None,
    preferred_profile: str | None = None,
) -> dict[str, Any]:
    if implement_step_index is None:
        candidate_steps = steps
    else:
        candidate_steps = steps[implement_step_index + 1 :]
    fallback_context: dict[str, Any] | None = None
    for step in candidate_steps:
        if step.get("name") != "validate":
            continue
        context = validate_step_context(step)
        if not context:
            continue
        if preferred_profile is None:
            return context
        if context.get("profile") == preferred_profile:
            return context
        if fallback_context is None:
            fallback_context = context
    if preferred_profile is not None:
        return {}
    return fallback_context or {}


def validate_step_context(step: dict[str, Any]) -> dict[str, Any]:
    input_data = step.get("input")
    if not isinstance(input_data, dict):
        return {}
    validation = input_data.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    context: dict[str, Any] = {}
    profile = string_field(validation, "profile") or string_field(step, "profile")
    if profile:
        context["profile"] = profile
    commands = validation.get("commands")
    if isinstance(commands, list) and all(_is_string_list(command) for command in commands):
        context["commands"] = [list(command) for command in commands]
    worker_home = string_field(validation, "worker_home") or string_field(validation, "workerHome")
    if worker_home:
        context["worker_home"] = worker_home
    stack = validation.get("stack")
    if isinstance(stack, dict):
        role = string_field(stack, "role") or "validation"
        path = string_field(stack, "path")
        if path:
            context["stack"] = {"role": role, "path": path}
    return context


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
        state["pending_repair_context"] = None
        state["checkout"] = checkout_after_implementation(state.get("checkout"), output)
        update_checkout_attempt_after_implementation(state, output)
        if output.get("status") != "implemented":
            record_attempted_selected_work(state, state.get("implementation_selection"))
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
        if validation_failure_reselects(output):
            record_attempted_selected_work(state, state.get("implementation_selection"))
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


def review_implementation_input(state: dict[str, Any]) -> dict[str, Any]:
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    if implementation.get("status") != "implemented":
        return implementation
    checkout = state.get("checkout") if isinstance(state.get("checkout"), dict) else {}
    checkout_path = Path(string_field(checkout, "checkout_path") or "")
    base_commit = string_field(checkout, "base_commit")
    if not base_commit or not checkout_path:
        return implementation
    cumulative_git = safe_git_metadata(checkout_path, base_commit)
    if cumulative_git.get("metadata_status") == "failed":
        return implementation
    latest_git = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    merged = dict(implementation)
    merged["git"] = cumulative_git
    if latest_git and latest_git != cumulative_git:
        merged["latest_repair"] = latest_git
    else:
        merged.pop("latest_repair", None)
    return merged


def pending_repair_context(state: dict[str, Any]) -> dict[str, Any] | None:
    repair_context = state.get("pending_repair_context")
    return dict(repair_context) if isinstance(repair_context, dict) else None


def build_validation_repair_context(state: dict[str, Any], *, repair_attempt: int) -> dict[str, Any] | None:
    if repair_attempt <= 0:
        return None
    validation = latest_validation_record(state)
    if validation is None:
        return None
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    if not validation_feedback_repairable(output):
        return None
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    work_item = implementation.get("work_item") if isinstance(implementation.get("work_item"), dict) else {}
    failure = first_validation_failure(output) or {}
    evidence_paths = []
    for path in (
        string_field(failure, "log_path") or "",
        string_field(validation, "step_result_path") or "",
        string_field(validation, "worker_result_path") or "",
    ):
        if path and path not in evidence_paths:
            evidence_paths.append(path)
    return {
        "attempt": repair_attempt,
        "trigger": "validation_feedback",
        "validation": {
            "status": string_field(output, "status") or "",
            "classification": string_field(failure, "category") or string_field(output, "classification") or "",
            "summary": string_field(output, "summary") or "",
            "root_excerpt": string_field(failure, "excerpt") or string_field(failure, "reason") or string_field(output, "summary") or "",
            "evidence_paths": evidence_paths,
        },
        "previous_implementation": {
            "commit": string_field(git_info, "after_commit") or "",
            "changed_files": list(git_info.get("changed_files")) if isinstance(git_info.get("changed_files"), list) else [],
            "step_result_path": state.get("implementation_result_path") or "",
        },
        "acceptance_criteria": list(work_item.get("acceptance_criteria")) if isinstance(work_item.get("acceptance_criteria"), list) else [],
    }


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


def attempted_work_aliases(state: dict[str, Any]) -> set[str]:
    value = state.get("attempted_work_aliases")
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def record_attempted_selected_work(state: dict[str, Any], selected_work: Any | None = None) -> None:
    attempted = attempted_work_aliases(state)
    attempted.update(selected_work_aliases(state.get("selected_work") if selected_work is None else selected_work))
    state["attempted_work_aliases"] = sorted(attempted)


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
    state["pending_repair_context"] = None
    state["repair_history"] = []
    state["runtime_review_cycles"] = []
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
    updated["base_commit"] = string_field(checkout, "base_commit") or string_field(checkout, "start_commit")
    updated["start_commit"] = after_commit
    updated["requested_ref"] = after_commit
    return updated


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
    if step_name == "implement" and status != expected and implementation_failure_allows_retry_follow_up(remaining_steps):
        return ""
    if (
        step_name == "validate"
        and status != expected
        and validation_failure_reselects(output)
        and validation_failure_allows_retry_follow_up(remaining_steps)
    ):
        return ""
    if step_name == "review" and status != expected and review_failure_allows_retry_follow_up(remaining_steps):
        return ""
    if expected and status != expected:
        return f"{step_name} did not reach {expected}: {status or 'missing status'}"
    return ""


def validation_failure_reselects(output: dict[str, Any]) -> bool:
    return output.get("status") == "failed_validation" or output.get("classification") == "worker_failure"


def validation_feedback_follow_up(
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    step_spec: dict[str, Any],
    ledger_dir: Path,
) -> tuple[list[dict[str, Any]], str]:
    if not normalized["validation_feedback"]["enabled"]:
        return [], ""
    validation = latest_validation_record(state)
    if validation is None:
        return [], ""
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    if not validation_feedback_repairable(output):
        return [], ""
    attempted_retries = retry_attempt_count(state)
    max_retries = normalized["retry_policy"]["max_retries"]
    if attempted_retries >= max_retries:
        return [], (
            "retry budget exhausted: "
            f"{attempted_retries} retries attempted, max_retries={max_retries}"
        )
    repair_attempt = attempted_retries + 1
    if repair_attempt_already_recorded(state, repair_attempt):
        return [], ""
    record_repair_attempt(state, repair_attempt)
    return validation_feedback_repair_steps(normalized, step_spec), ""


def validation_feedback_repairable(output: dict[str, Any]) -> bool:
    if not validation_failure_reselects(output):
        return False
    failure = first_validation_failure(output)
    if failure is None:
        return True
    category = string_field(failure, "category") or ""
    excerpt = string_field(failure, "excerpt") or string_field(failure, "reason") or ""
    if category in {"runtime", "protocol", "timeout", "missing_result", "prerequisite_skip"}:
        return False
    if _retrospective_text_has_missing_tool_or_config(excerpt):
        return False
    if _validation_feedback_text_has_infra_or_setup_failure(excerpt):
        return False
    return True


def validation_feedback_repair_steps(
    normalized: dict[str, Any],
    validate_step: dict[str, Any],
) -> list[dict[str, Any]]:
    prepare_step = recipe_step_template(normalized["steps"], "prepare-checkout")
    implement_step = recipe_step_template(normalized["steps"], "implement")
    if prepare_step is None or implement_step is None:
        return []
    return [prepare_step, implement_step, deepcopy(validate_step)]


def recipe_step_template(steps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for step in steps:
        if step.get("name") == name:
            return deepcopy(step)
    return None


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


def repair_attempt_already_recorded(state: dict[str, Any], attempt: int) -> bool:
    history = state.get("repair_history")
    if not isinstance(history, list):
        return False
    return attempt in history


def record_repair_attempt(state: dict[str, Any], attempt: int) -> None:
    history = state.get("repair_history")
    values = list(history) if isinstance(history, list) else []
    if attempt not in values:
        values.append(attempt)
    state["repair_history"] = values


def review_feedback_follow_up(
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    step_spec: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    if not normalized["review_feedback"]["enabled"]:
        return [], ""
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    review_status = string_field(review, "status") or ""
    if review_status == "passed":
        finalize_latest_runtime_review_cycle(state)
        append_runtime_review_cycle(state, build_runtime_review_cycle(state, step_spec=step_spec))
        return [], ""
    if review_status != "request_revision":
        return [], ""

    append_runtime_review_cycle(state, build_runtime_review_cycle(state, step_spec=step_spec))
    repairable_findings = review_feedback_repairable_findings(review)
    if not repairable_findings:
        return [], review_feedback_blocked_reason(review)
    attempted_retries = retry_attempt_count(state)
    max_retries = normalized["retry_policy"]["max_retries"]
    if attempted_retries >= max_retries:
        return [], (
            "review feedback retry budget exhausted: "
            f"{attempted_retries} retries attempted, max_retries={max_retries}; "
            f"{review_feedback_blocked_reason(review)}"
        )
    repair_attempt = attempted_retries + 1
    if repair_attempt_already_recorded(state, repair_attempt):
        return [], ""
    record_repair_attempt(state, repair_attempt)
    return review_feedback_repair_steps(normalized, step_spec), ""


def review_feedback_repair_steps(
    normalized: dict[str, Any],
    review_step: dict[str, Any],
) -> list[dict[str, Any]]:
    prepare_step = recipe_step_template(normalized["steps"], "prepare-checkout")
    implement_step = recipe_step_template(normalized["steps"], "implement")
    validate_step = recipe_step_template(normalized["steps"], "validate")
    if prepare_step is None or implement_step is None or validate_step is None:
        return []
    return [prepare_step, implement_step, validate_step, deepcopy(review_step)]


def build_review_repair_context(
    state: dict[str, Any],
    *,
    step_spec: dict[str, Any],
    repair_attempt: int,
) -> dict[str, Any] | None:
    if repair_attempt <= 0:
        return None
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    if string_field(review, "status") != "request_revision":
        return None
    repairable_findings = review_feedback_repairable_findings(review)
    if not repairable_findings:
        return None
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    work_item = implementation.get("work_item") if isinstance(implementation.get("work_item"), dict) else {}
    validation = latest_validation_record(state)
    validation_output = validation.get("output") if isinstance(validation, dict) and isinstance(validation.get("output"), dict) else {}
    validation_paths = []
    for path in (
        string_field(validation, "step_result_path") or "",
        string_field(validation, "worker_result_path") or "",
    ):
        if path and path not in validation_paths:
            validation_paths.append(path)
    return {
        "attempt": repair_attempt,
        "trigger": "review_feedback",
        "review": {
            "role": review_feedback_role(step_spec, review),
            "status": string_field(review, "status") or "",
            "summary": string_field(review, "summary") or "",
            "findings": repairable_findings,
        },
        "current_implementation": {
            "summary": string_field(implementation, "summary") or "",
            "commit": string_field(git_info, "after_commit") or "",
            "changed_files": list(git_info.get("changed_files")) if isinstance(git_info.get("changed_files"), list) else [],
            "step_result_path": state.get("implementation_result_path") or "",
        },
        "validation": {
            "status": string_field(validation_output, "status") or "",
            "classification": string_field(validation_output, "classification") or "",
            "summary": string_field(validation_output, "summary") or "",
            "evidence_paths": validation_paths,
        },
        "acceptance_criteria": list(work_item.get("acceptance_criteria")) if isinstance(work_item.get("acceptance_criteria"), list) else [],
    }


def build_runtime_review_cycle(state: dict[str, Any], *, step_spec: dict[str, Any]) -> dict[str, Any]:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    cycle_status = runtime_review_cycle_status(string_field(review, "status") or "")
    review_record: dict[str, Any] = {
        "role": review_feedback_role(step_spec, review),
        "status": cycle_status,
        "summary": string_field(review, "summary") or cycle_status,
        "requires_response": cycle_status == "request-changes",
    }
    pipeline_follow_up = review_feedback_pipeline_follow_up(review)
    if pipeline_follow_up:
        review_record["pipeline_follow_up"] = pipeline_follow_up
    runtime_cycles = state.get("runtime_review_cycles")
    cycle_number = len(runtime_cycles) + 1 if isinstance(runtime_cycles, list) else 1
    return {"cycle": cycle_number, "status": cycle_status, "reviews": [review_record]}


def append_runtime_review_cycle(state: dict[str, Any], cycle: dict[str, Any]) -> None:
    runtime_cycles = state.get("runtime_review_cycles")
    cycles = list(runtime_cycles) if isinstance(runtime_cycles, list) else []
    cycles.append(cycle)
    state["runtime_review_cycles"] = cycles


def finalize_latest_runtime_review_cycle(state: dict[str, Any]) -> None:
    runtime_cycles = state.get("runtime_review_cycles")
    if not isinstance(runtime_cycles, list) or not runtime_cycles:
        return
    latest_cycle = runtime_cycles[-1]
    if not isinstance(latest_cycle, dict):
        return
    reviews = latest_cycle.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        return
    latest_review = reviews[-1]
    if not isinstance(latest_review, dict) or latest_review.get("response"):
        return
    if string_field(latest_review, "status") != "request-changes":
        return
    validation = latest_validation_record(state)
    validation_output = validation.get("output") if isinstance(validation, dict) and isinstance(validation.get("output"), dict) else {}
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    response: dict[str, Any] = {
        "status": "addressed",
        "summary": string_field(implementation, "summary") or "Addressed in follow-up implementation.",
        "implementation_commit": string_field(git_info, "after_commit") or "",
        "implementation_step_result_path": state.get("implementation_result_path") or "",
        "validation_status": string_field(validation_output, "status") or "",
        "validation_summary": string_field(validation_output, "summary") or "",
        "validation_step_result_path": string_field(validation, "step_result_path") or "",
        "validation_worker_result_path": string_field(validation, "worker_result_path") or "",
        "follow_up_review_status": string_field(state.get("review") if isinstance(state.get("review"), dict) else {}, "status") or "",
        "follow_up_review_summary": string_field(state.get("review") if isinstance(state.get("review"), dict) else {}, "summary") or "",
        "follow_up_review_result_path": state.get("review_result_path") or "",
    }
    pipeline_follow_up = latest_review.get("pipeline_follow_up")
    if isinstance(pipeline_follow_up, list) and pipeline_follow_up:
        response["pipeline_follow_up"] = pipeline_follow_up
    latest_review["response"] = response
    latest_cycle["status"] = finalized_runtime_review_cycle_status(reviews)


def runtime_review_cycle_status(review_status: str) -> str:
    if review_status == "request_revision":
        return "request-changes"
    if review_status == "passed":
        return "passed"
    return "findings-open"


def finalized_runtime_review_cycle_status(reviews: list[dict[str, Any]]) -> str:
    saw_addressed_request_changes = False
    saw_reviews = False
    saw_only_passed = True
    for review in reviews:
        if not isinstance(review, dict):
            continue
        saw_reviews = True
        status = string_field(review, "status") or ""
        if status == "findings-open":
            return "findings-open"
        if status == "request-changes":
            if review_cycle_response_is_addressed(review.get("response")):
                saw_addressed_request_changes = True
                saw_only_passed = False
                continue
            return "request-changes"
        if status != "passed":
            return "findings-open"
    if saw_only_passed and saw_reviews:
        return "passed"
    if saw_addressed_request_changes:
        return "findings-addressed"
    return "findings-open"


def review_feedback_role(step_spec: dict[str, Any], review: dict[str, Any]) -> str:
    input_data = step_spec.get("input") if isinstance(step_spec.get("input"), dict) else {}
    return string_field(input_data, "role") or string_field(review, "role") or "reviewer"


def review_feedback_repairable_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    reviewer_result = review.get("reviewer_result") if isinstance(review.get("reviewer_result"), dict) else {}
    findings = reviewer_result.get("findings")
    if not isinstance(findings, list):
        return []
    repairable = []
    for finding in findings:
        if not isinstance(finding, dict) or review_finding_is_pipeline_failure(finding):
            continue
        repairable.append(
            {
                "severity": review_finding_severity(finding),
                "file": review_finding_file(finding),
                "line": review_finding_line(finding),
                "required_fix": review_finding_required_fix(finding),
                "summary": string_field(finding, "summary") or string_field(finding, "title") or "",
            }
        )
    return repairable


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


def review_feedback_blocked_reason(review: dict[str, Any]) -> str:
    repairable = review_feedback_repairable_findings(review)
    if repairable:
        required_fix = string_field(repairable[0], "required_fix") or string_field(repairable[0], "summary") or "review finding"
        return f"review requested changes: {required_fix}"
    pipeline_follow_up = review_feedback_pipeline_follow_up(review)
    if pipeline_follow_up:
        return f"review requested pipeline follow-up: {string_field(pipeline_follow_up[0], 'summary') or 'pipeline issue'}"
    return string_field(review, "summary") or "review requested changes"


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


def review_finding_file(finding: dict[str, Any]) -> str:
    return (
        string_field(finding, "file")
        or string_field(finding, "path")
        or string_field(finding, "filename")
        or ""
    )


def review_finding_line(finding: dict[str, Any]) -> int | None:
    value = finding.get("line")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def review_finding_required_fix(finding: dict[str, Any]) -> str:
    return (
        string_field(finding, "required_fix")
        or string_field(finding, "summary")
        or string_field(finding, "details")
        or string_field(finding, "title")
        or "Address the review finding."
    )


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
    gate = evidence_gate.publication_gate(
        validations=[validation_gate_entry(item) for item in state["validations"] if isinstance(item, dict)],
        review=review_gate_entry(state.get("review")),
        implemented_commit=implemented_after_commit(state),
        incomplete_selected_work=incomplete_selected_work_ids(state),
    )
    if gate["passed"]:
        return ""
    reason = str(gate.get("reason") or "")
    if reason == "required final validation evidence is not validated: required validation":
        return "required final validation evidence is missing"
    return reason


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
    return run_publication(
        PublicationRequest(
            publisher=publisher,
            workstream_id=normalized["workstream_id"],
            review_branch=normalized["review_branch"],
            checkout_path=checkout_path_from_state(state),
            checkout_base_commit=checkout_base_commit(state),
            next_allowed_command=rerun_workstream_command(normalized),
            ledger=ledger,
            build_pr_body=lambda: pr_body_markdown(normalized, state, steps, selected_work, ledger),
        )
    )


def close_published_pr(
    config: dict[str, Any],
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    ledger: "WorkstreamLedger",
    auth: dict[str, Any],
    auth_artifact: dict[str, Any],
) -> dict[str, Any]:
    checkout_path = checkout_path_from_state(state)
    configured_review_feedback_status = terminal_review_feedback_status(
        normalized.get("tracker", {}).get("terminal_decision", {})
    )
    view_payload, view_command = publisher_pr_view(
        config,
        checkout_path=checkout_path,
        auth=auth,
        fields=["url", "state", "isDraft", "mergeStateStatus", "headRefOid"],
    )
    pr_url = publisher_pr_url(view_payload, fallback=config["pr"])
    review_cycles = tracker_review_cycles(normalized, state)
    if not review_cycles_recorded(review_cycles) and configured_review_feedback_status != "waived":
        return tracker_close_blocked_publication(
            reason=(
                "terminal PR closure requires recorded review cycle evidence or review_feedback_status "
                "of waived before merge"
            ),
            terminal_decision={
                "status": "blocked",
                "reason": "review cycle evidence is still required before terminal PR closure",
                "pr_url": pr_url,
                "review_feedback_status": configured_review_feedback_status,
            },
            mode="close",
            url=pr_url,
            commands={"gh_view": view_command},
        )
    if (
        review_cycles_require_response(review_cycles)
        and configured_review_feedback_status not in TERMINAL_REVIEW_FEEDBACK_STATUSES
    ):
        return tracker_close_blocked_publication(
            reason=(
                "terminal PR closure requires addressed review-cycle responses before merge; "
                "record response evidence or set review_feedback_status"
            ),
            terminal_decision={
                "status": "blocked",
                "reason": "review cycle responses are still required before terminal PR closure",
                "pr_url": pr_url,
                "review_feedback_status": configured_review_feedback_status,
            },
            mode="close",
            url=pr_url,
            commands={"gh_view": view_command},
        )
    merge_state_status = string_field(view_payload, "mergeStateStatus") or ""
    if (
        (string_field(view_payload, "state") or "").upper() != "OPEN"
        or bool(view_payload.get("isDraft"))
        or merge_state_status != "CLEAN"
    ):
        blocked_reason = (
            "terminal PR closure is blocked: "
            f"state={(string_field(view_payload, 'state') or 'unknown').upper()}, "
            f"draft={bool(view_payload.get('isDraft'))}, "
            f"mergeStateStatus={merge_state_status or 'unknown'}"
        )
        return blocked_terminal_closure_publication(
            blocked_reason,
            normalized=normalized,
            auth=auth_artifact,
            pr_url=pr_url,
            commands={"gh_view": view_command},
            merge_state=view_payload,
        )
    merge_command = [
        config["gh_path"],
        "pr",
        "merge",
        config["pr"],
        "--repo",
        config["repo"],
        "--merge",
    ]
    implemented_commit = implemented_after_commit(state)
    if implemented_commit:
        merge_command.extend(["--match-head-commit", implemented_commit])
    try:
        run_publisher_command(
            merge_command,
            cwd=checkout_path,
            tool="gh",
            auth=auth,
            message_on_failure="gh pr merge failed",
        )
    except PublisherError as exc:
        return blocked_terminal_closure_publication(
            f"terminal PR closure is blocked: {exc.message}",
            normalized=normalized,
            auth=auth_artifact,
            pr_url=pr_url,
            commands={"gh_view": view_command, "gh_merge": merge_command},
            stdout=exc.stdout,
            stderr=exc.stderr,
            returncode=exc.returncode,
        )
    merged_payload, merged_view_command = publisher_pr_view(
        config,
        checkout_path=checkout_path,
        auth=auth,
        fields=["url", "mergeCommit", "mergedAt"],
    )
    merge_commit = publisher_pr_merge_commit(merged_payload)
    if not merge_commit:
        raise PublisherError(
            "merged PR did not report a merge commit",
            command=merged_view_command,
            returncode=0,
            stdout=json.dumps(merged_payload),
            stderr="",
        )
    terminal_decision = {
        "status": "merged",
        "merge_commit": merge_commit,
        "reason": "",
        "pr_url": publisher_pr_url(merged_payload, fallback=pr_url),
        "review_feedback_status": configured_review_feedback_status,
    }
    try:
        tracker_close = close_selected_source_item(
            normalized=normalized,
            state=state,
            config=config,
            checkout_path=checkout_path,
            auth=auth,
            close_reason=f"merged via {merge_commit}",
        )
    except PublisherError as exc:
        return merged_terminal_tracker_close_failed_publication(
            auth=auth_artifact,
            pr_url=terminal_decision["pr_url"],
            merge_commit=merge_commit,
            terminal_decision=terminal_decision,
            commands={
                "gh_view": view_command,
                "gh_merge": merge_command,
                "gh_view_merged": merged_view_command,
            },
            exc=exc,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "tracker-closed",
        "enabled": True,
        "mode": "close",
        "reason": "terminal PR merged and source item closed",
        "auth": auth_artifact,
        "url": terminal_decision["pr_url"],
        "merge_commit": merge_commit,
        "next_allowed_command": "none",
        "retry": "",
        "commands": {
            "gh_view": redact_artifact_value(view_command),
            "gh_merge": redact_artifact_value(merge_command),
            "gh_view_merged": redact_artifact_value(merged_view_command),
        },
        "tracker_close": tracker_close,
        "terminal_decision": terminal_decision,
    }


def merged_terminal_tracker_close_failed_publication(
    *,
    auth: dict[str, Any],
    pr_url: str,
    merge_commit: str,
    terminal_decision: dict[str, Any],
    commands: dict[str, list[str]],
    exc: PublisherError,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed-needs-human",
        "enabled": True,
        "mode": "close",
        "reason": "terminal PR merged, but source item closure failed",
        "auth": auth,
        "url": redact_text(pr_url),
        "merge_commit": redact_text(merge_commit),
        "next_allowed_command": "none",
        "retry": (
            "PR is already merged. Remediate the recorded source-item closure failure, then close the source item "
            "with the recorded terminal decision evidence instead of attempting another PR merge."
        ),
        "commands": {key: redact_artifact_value(value) for key, value in commands.items()},
        "terminal_decision": runtime_terminal_decision(terminal_decision),
        "tracker_close": tracker_close_failure_artifact(exc),
    }


def publisher_pr_view(
    config: dict[str, Any],
    *,
    checkout_path: Path,
    auth: dict[str, Any],
    fields: list[str],
) -> tuple[dict[str, Any], list[str]]:
    command = [
        config["gh_path"],
        "pr",
        "view",
        config["pr"],
        "--repo",
        config["repo"],
        "--json",
        ",".join(fields),
    ]
    completed = run_publisher_command(command, cwd=checkout_path, tool="gh", auth=auth)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PublisherError(
            "gh pr view returned invalid JSON payload",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        ) from exc
    if not isinstance(payload, dict):
        raise PublisherError(
            "gh pr view must return an object payload",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return payload, command


def publisher_pr_url(payload: dict[str, Any], *, fallback: str = "") -> str:
    return redact_text(string_field(payload, "url") or fallback)


def publisher_pr_merge_commit(payload: dict[str, Any]) -> str:
    merge_commit = payload.get("mergeCommit")
    if isinstance(merge_commit, dict):
        return redact_text(string_field(merge_commit, "oid") or "")
    return ""


def blocked_terminal_closure_publication(
    reason: str,
    *,
    normalized: dict[str, Any],
    auth: dict[str, Any],
    pr_url: str,
    commands: dict[str, list[str]],
    merge_state: dict[str, Any] | None = None,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
) -> dict[str, Any]:
    result = blocked_publication(reason, normalized, "latest")
    result.update(
        {
            "mode": "close",
            "auth": auth,
            "url": redact_text(pr_url),
            "returncode": returncode,
            "stdout_excerpt": redact_text(stdout[-2000:]),
            "stderr_excerpt": redact_text(stderr[-2000:]),
            "commands": {key: redact_artifact_value(value) for key, value in commands.items()},
            "terminal_decision": {
                "status": "blocked",
                "merge_commit": "",
                "reason": redact_text(reason),
                "pr_url": redact_text(pr_url),
                "review_feedback_status": "",
            },
        }
    )
    if merge_state is not None:
        result["merge_state"] = redact_artifact_value(merge_state)
    return result


def close_selected_source_item(
    *,
    normalized: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
    checkout_path: Path,
    auth: dict[str, Any],
    close_reason: str,
) -> dict[str, Any]:
    selected_item = current_selected_work_item(state)
    if selected_item is None:
        raise PublisherError(
            "no selected work item is available for source closure",
            command=[],
            returncode=None,
        )
    source_type = string_field(selected_item, "source_type") or ""
    if source_type == "beads":
        source = selected_work_source_config(normalized, selected_item)
        if source is None:
            raise PublisherError(
                "could not resolve the Beads source configuration for terminal closure",
                command=["bd", "close", string_field(selected_item, "external_id") or ""],
                returncode=None,
            )
        workspace = Path(str(source.get("workspace") or ""))
        password = read_tracker_beads_password(workspace / "secrets" / "dolt_beads_password.txt")
        command = [
            "bd",
            "close",
            string_field(selected_item, "external_id") or "",
            "--reason",
            close_reason,
        ]
        run_local_tracker_command(
            command,
            cwd=workspace,
            extra_env={"BEADS_DOLT_PASSWORD": password},
            exact_secrets={password},
            message_on_failure="bd close failed",
        )
        return {
            "status": "closed",
            "tool": "bd",
            "command": redact_artifact_value(command),
        }
    if source_type == "github_issues":
        repo, issue_number = github_issue_close_target(selected_item)
        command = [
            config["gh_path"],
            "issue",
            "close",
            issue_number,
            "--repo",
            repo,
            "--reason",
            "completed",
            "--comment",
            close_reason,
        ]
        run_publisher_command(
            command,
            cwd=checkout_path,
            tool="gh",
            auth=auth,
            message_on_failure="gh issue close failed",
        )
        return {
            "status": "closed",
            "tool": "gh",
            "command": redact_artifact_value(command),
        }
    raise PublisherError(
        f"source type {source_type or 'unknown'} does not support automatic terminal closure",
        command=[],
        returncode=None,
    )


def current_selected_work_item(state: dict[str, Any]) -> dict[str, Any] | None:
    selected_work = state.get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return None
    first_item = selected_work[0]
    return first_item if isinstance(first_item, dict) else None


def selected_work_source_config(normalized: dict[str, Any], selected_item: dict[str, Any]) -> dict[str, Any] | None:
    source_id = string_field(selected_item, "source_id") or ""
    source_type = string_field(selected_item, "source_type") or ""
    for step in normalized.get("steps", []):
        if not isinstance(step, dict) or step.get("name") != "select-work":
            continue
        input_data = step.get("input") if isinstance(step.get("input"), dict) else {}
        sources = input_data.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            candidate_source_id = str(source.get("id") or source.get("type") or "")
            candidate_source_type = str(source.get("type") or "")
            if candidate_source_id == source_id and candidate_source_type == source_type:
                return source
    return None


def read_tracker_beads_password(credentials_path: Path) -> str:
    try:
        lines = credentials_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PublisherError(
            "Beads credentials are not available for terminal closure",
            command=["bd", "close"],
            returncode=None,
            stderr=str(exc),
        ) from exc
    if not lines or not lines[0]:
        raise PublisherError(
            "Beads credentials are not available for terminal closure",
            command=["bd", "close"],
            returncode=None,
        )
    return lines[0]


def run_local_tracker_command(
    command: list[str],
    *,
    cwd: Path,
    extra_env: dict[str, str],
    exact_secrets: set[str] | None = None,
    message_on_failure: str,
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key in ("PATH", "LANG", "LC_ALL") if (value := os.environ.get(key)) is not None}
    env.update(extra_env)
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
        raise PublisherError(
            str(exc),
            command=command,
            returncode=None,
            stderr=redact_text(str(exc), exact_secrets=exact_secrets),
        ) from exc
    if completed.returncode != 0:
        raise PublisherError(
            message_on_failure,
            command=command,
            returncode=completed.returncode,
            stdout=redact_text(completed.stdout, exact_secrets=exact_secrets),
            stderr=redact_text(completed.stderr, exact_secrets=exact_secrets),
        )
    return completed


def github_issue_close_target(selected_item: dict[str, Any]) -> tuple[str, str]:
    raw = selected_item.get("raw") if isinstance(selected_item.get("raw"), dict) else {}
    github = raw.get("github") if isinstance(raw.get("github"), dict) else {}
    repo = string_field(github, "repo") or ""
    number = github.get("number")
    issue_number = str(number).strip() if isinstance(number, int) and not isinstance(number, bool) else ""
    if repo and issue_number:
        return repo, issue_number
    raise PublisherError(
        "GitHub issue source is missing repo or issue number for terminal closure",
        command=[],
        returncode=None,
    )


def checkout_base_commit(state: dict[str, Any]) -> str:
    checkout = state.get("checkout")
    if not isinstance(checkout, dict):
        return ""
    return string_field(checkout, "base_commit") or string_field(checkout, "start_commit") or ""


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


def normalize_validation_feedback(validation_feedback: Any) -> dict[str, bool]:
    if validation_feedback is None:
        return {"enabled": False}
    if not isinstance(validation_feedback, dict):
        raise WorkstreamError("validation_feedback must be an object")
    unsupported = [key for key in validation_feedback if key != "enabled"]
    if unsupported:
        raise WorkstreamError("validation_feedback only supports enabled")
    enabled = validation_feedback.get("enabled", False)
    if not isinstance(enabled, bool):
        raise WorkstreamError("validation_feedback.enabled must be a boolean")
    return {"enabled": enabled}


def normalize_validation_expectations(validation_expectations: Any) -> dict[str, bool]:
    if validation_expectations is None:
        return {"generated_smoke_dry_run_expected": False}
    if not isinstance(validation_expectations, dict):
        raise WorkstreamError("validation_expectations must be an object")
    unsupported = [key for key in validation_expectations if key != "generated_smoke_dry_run_expected"]
    if unsupported:
        raise WorkstreamError("validation_expectations only supports generated_smoke_dry_run_expected")
    expected = validation_expectations.get("generated_smoke_dry_run_expected", False)
    if not isinstance(expected, bool):
        raise WorkstreamError("validation_expectations.generated_smoke_dry_run_expected must be a boolean")
    return {"generated_smoke_dry_run_expected": expected}


def normalize_review_feedback(review_feedback: Any) -> dict[str, bool]:
    if review_feedback is None:
        return {"enabled": False}
    if not isinstance(review_feedback, dict):
        raise WorkstreamError("review_feedback must be an object")
    unsupported = [key for key in review_feedback if key != "enabled"]
    if unsupported:
        raise WorkstreamError("review_feedback only supports enabled")
    enabled = review_feedback.get("enabled", False)
    if not isinstance(enabled, bool):
        raise WorkstreamError("review_feedback.enabled must be a boolean")
    return {"enabled": enabled}


def normalize_tracker_config(tracker: Any) -> dict[str, Any]:
    if tracker is None:
        return {"terminal_decision": empty_terminal_decision()}
    if not isinstance(tracker, dict):
        raise WorkstreamError("tracker must be an object")
    unsupported = [key for key in tracker if key != "terminal_decision"]
    if unsupported:
        raise WorkstreamError("tracker only supports terminal_decision")
    return {"terminal_decision": normalize_tracker_terminal_decision(tracker.get("terminal_decision"))}


def normalize_tracker_terminal_decision(decision: Any) -> dict[str, str]:
    if decision is None:
        return empty_terminal_decision()
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
        return empty_terminal_decision()
    if review_feedback_status and review_feedback_status not in TERMINAL_REVIEW_FEEDBACK_STATUSES:
        allowed = ", ".join(sorted(TERMINAL_REVIEW_FEEDBACK_STATUSES))
        raise WorkstreamError(f"tracker.terminal_decision.review_feedback_status must be one of: {allowed}")
    if not status:
        if merge_commit or reason or pr_url:
            raise WorkstreamError("tracker.terminal_decision.status must be merged or no-merge")
        return {
            "status": "",
            "merge_commit": "",
            "reason": "",
            "pr_url": "",
            "review_feedback_status": review_feedback_status,
        }
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


def implementation_failure_allows_retry_follow_up(remaining_steps: list[dict[str, Any]]) -> bool:
    for step in remaining_steps:
        if not isinstance(step, dict):
            continue
        name = step.get("name")
        if name in {"prepare-checkout", "select-work"}:
            return True
        if name in {"review", "validate", "implement"}:
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
        not in {
            "enabled",
            "type",
            "command",
            "timeout_seconds",
            "timeoutSeconds",
            "provider",
            "codex_home",
            "config_home",
            "env",
        }
    ]
    if unsupported:
        raise WorkstreamError(
            "retrospective_judge only supports enabled, type, command, timeout_seconds, provider, codex_home, config_home, env"
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
    provider = retrospective_judge.get("provider")
    if provider is not None:
        if not isinstance(provider, str) or not provider.strip():
            raise WorkstreamError("retrospective_judge.provider must be a non-empty string")
        normalized["provider"] = provider.strip()
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
        provider=normalized.get("provider"),
        codex_home=normalized.get("codex_home"),
        config_home=normalized.get("config_home"),
        env=normalized.get("env"),
        field_prefix="retrospective_judge",
    )
    if mount_error:
        raise WorkstreamError(mount_error)
    mount_rejection = non_openai_pi_mount_error(
        provider=normalized.get("provider"),
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
        if key
        not in {
            "enabled",
            "creator",
            "type",
            "command",
            "timeout_seconds",
            "timeoutSeconds",
            "beads_workspace",
            "labels",
        }
    ]
    if unsupported:
        raise WorkstreamError(
            "retrospective_follow_up only supports enabled, creator, type, command, timeout_seconds, "
            "beads_workspace, labels"
        )
    enabled = retrospective_follow_up.get("enabled", False)
    if not isinstance(enabled, bool):
        raise WorkstreamError("retrospective_follow_up.enabled must be a boolean")
    if not enabled:
        return {"enabled": False}
    creator = string_field(retrospective_follow_up, "creator") or "command"
    if creator not in {"command", "beads"}:
        raise WorkstreamError("retrospective_follow_up.creator must be command or beads")
    if creator == "beads":
        beads_workspace = retrospective_follow_up.get("beads_workspace")
        if not isinstance(beads_workspace, str) or not beads_workspace:
            raise WorkstreamError("retrospective_follow_up.beads_workspace must be an absolute directory")
        workspace_path = Path(beads_workspace)
        if not workspace_path.is_absolute():
            raise WorkstreamError("retrospective_follow_up.beads_workspace must be an absolute directory")
        if not workspace_path.is_dir():
            raise WorkstreamError("retrospective_follow_up.beads_workspace must be an existing directory")
        labels = retrospective_follow_up.get("labels", [])
        if not _is_string_list(labels):
            raise WorkstreamError("retrospective_follow_up.labels must be a list of strings")
        return {
            "enabled": True,
            "creator": "beads",
            "beads_workspace": str(workspace_path),
            "labels": list(labels),
        }
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
        "creator": "command",
        "type": follow_up_type,
        "command": list(command),
        "timeout_seconds": float(timeout_seconds),
    }


def validate_retrospective_terminal_decision(
    retrospective: dict[str, Any],
    tracker: dict[str, Any],
    *,
    publisher: Any = None,
) -> None:
    if not retrospective:
        return
    decision = tracker.get("terminal_decision") if isinstance(tracker, dict) else {}
    if isinstance(decision, dict) and decision.get("status") in {"merged", "no-merge"}:
        return
    publisher_config = publisher if isinstance(publisher, dict) else {}
    if (string_field(publisher_config, "mode") or "") == "close":
        return
    raise WorkstreamError("retrospective requires tracker.terminal_decision.status to be merged or no-merge")


def effective_retrospective(normalized: dict[str, Any], publication: dict[str, Any]) -> dict[str, Any]:
    retrospective = normalized.get("retrospective") if isinstance(normalized, dict) else {}
    if not isinstance(retrospective, dict) or not retrospective:
        return {}
    if publisher_mode(normalized) != "close":
        return retrospective
    decision_status = effective_tracker_terminal_decision(normalized, publication).get("status")
    if decision_status not in {"merged", "no-merge"}:
        return {}
    return retrospective


def retrospective_follow_up_allowed(normalized: dict[str, Any], publication: dict[str, Any]) -> bool:
    if publisher_mode(normalized) != "close":
        return True
    decision_status = effective_tracker_terminal_decision(normalized, publication).get("status")
    return decision_status in {"merged", "no-merge"}


def publisher_mode(normalized: dict[str, Any]) -> str:
    publisher = normalized.get("publisher") if isinstance(normalized, dict) else {}
    if not isinstance(publisher, dict):
        publisher = {}
    return string_field(publisher, "mode") or "create"


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


def workstream_artifacts(ledger: "WorkstreamLedger") -> dict[str, str]:
    artifacts = {
        "workstream_result": "workstream-result.json",
        "command": "command.json",
        "publication": "publication-result.json",
        "tracker": "tracker-result.json",
        "pipeline_retrospective": "pipeline-retrospective.json",
    }
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
    command = f"afk run-workstream --workstream-id {normalized['workstream_id']}"
    rerun_ledger_arg = string_field(normalized, "rerun_ledger_arg")
    if rerun_ledger_arg:
        command += f" --ledger {shlex.quote(rerun_ledger_arg)}"
    command += " --input <recipe>"
    return command


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
    gate_entry = validation_gate_entry(validation)
    gate_entry["name"] = validation_name_for_body(validation, index)
    return pr_body_value(evidence_gate.validation_summary_line(gate_entry, index))


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
    for index, part in enumerate(command):
        stripped_part = part.strip()
        if is_secret_command_flag(part):
            flag = part.strip().split("=", 1)[0].lower()
            return f"{field_name} must not include credential flag {flag}"
        if is_secret_value(part) or bearer_secret_present(part):
            return f"{field_name} must not include secret-looking values"
        if (
            re.search(r"(?:^|[\s:])Bearer\s*$", stripped_part, re.IGNORECASE)
            and index + 1 < len(command)
            and is_bearer_secret_value(normalize_bearer_secret_value(command[index + 1]))
        ):
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
