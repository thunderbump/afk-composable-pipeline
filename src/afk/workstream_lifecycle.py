from __future__ import annotations

import re
import shlex
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from afk import evidence_gate
from afk.contracts import ProjectContract
from afk.implement import safe_git_metadata
from afk.review_cycles import (
    aggregate_runtime_review_cycle_status,
    finalized_runtime_review_cycle_status,
    review_cycle_response_is_addressed,
    runtime_review_cycle_status,
)
from afk.redaction import redact_text
from afk.registry import StepResult


SCHEMA_VERSION = 1
TERMINAL_REVIEW_FEEDBACK_STATUSES = {"resolved", "waived"}
REVIEW_PASS_ROLES = ("correctness", "bug-risk")

StepRunner = Callable[[str, Any, Path, ProjectContract | None], StepResult]


@dataclass(frozen=True)
class LifecycleHooks:
    composed_step_input: Callable[..., dict[str, Any]]
    equivalent_run_step_command: Callable[..., list[str]]
    step_execution_record: Callable[[str, StepResult, list[str], Path], dict[str, Any]]
    update_state_from_step: Callable[[dict[str, Any], str, StepResult, Path], None]
    publish_terminal_pr: Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class LifecycleOutcome:
    state: dict[str, Any]
    steps: list[dict[str, Any]]
    publication: dict[str, Any]


def initial_state() -> dict[str, Any]:
    return {
        "selected_work": [],
        "checkout": None,
        "checkout_attempts": [],
        "implementation": None,
        "implementation_selection": [],
        "implementation_result_path": "",
        "attempted_work_aliases": [],
        "validations": [],
        "pending_repair_context": None,
        "repair_history": [],
        "runtime_review_cycles": [],
        "review": None,
        "review_selection": [],
        "review_result_path": "",
        "cleanup": {"status": "unknown", "resources": []},
        "blocked_reason": "",
        "stop_reason": "",
        "next_allowed_command": "",
    }


def run_lifecycle(
    *,
    normalized: dict[str, Any],
    run_id: str,
    ledger_dir: Path,
    ledger: Any,
    step_runner: StepRunner,
    project_contract: ProjectContract | None,
    hooks: LifecycleHooks,
) -> LifecycleOutcome:
    state = initial_state()
    steps: list[dict[str, Any]] = []
    steps_queue = [dict(step) for step in normalized["steps"]]
    index = 0
    while index < len(steps_queue):
        step_spec = steps_queue[index]
        step_name = step_spec["name"]
        remaining_steps = steps_queue[index + 1 :]
        stop_reason = terminal_stop_reason(step_spec, state)
        if stop_reason:
            state["stop_reason"] = stop_reason
            state["next_allowed_command"] = next_allowed_command_for_terminal_stop(state, normalized)
            break
        blocked_reason = workflow_order_blocking_reason(step_name, state, normalized["repair_policy"])
        if blocked_reason:
            state["blocked_reason"] = blocked_reason
            break
        step_input = hooks.composed_step_input(step_spec, normalized, state, ledger_dir, step_index=index)
        step_profile = step_spec.get("profile")
        equivalent_command = hooks.equivalent_run_step_command(
            step_name,
            step_input,
            ledger_dir,
            profile=step_profile,
            project_contract=project_contract,
        )
        if step_name == "review":
            review_step, aggregate_review, review_cycle = execute_review_cycle(
                step_spec=step_spec,
                normalized=normalized,
                state=state,
                ledger_dir=ledger_dir,
                project_contract=project_contract,
                step_runner=step_runner,
                hooks=hooks,
            )
            steps.append(review_step)
            update_state_from_review_cycle(state, aggregate_review, review_step["result_path"])
            finalize_latest_runtime_review_cycle(state)
            append_runtime_review_cycle(state, review_cycle)
            repair_steps, repair_blocked_reason = review_feedback_follow_up(
                normalized=normalized,
                state=state,
                step_spec=step_spec,
            )
            if repair_blocked_reason:
                state["blocked_reason"] = repair_blocked_reason
                break
            if repair_steps:
                state["pending_repair_context"] = build_review_repair_context(
                    state,
                    step_spec=step_spec,
                    repair_attempt=retry_attempt_count(state) + 1,
                )
                steps_queue[index + 1 : index + 1] = repair_steps
                index += 1
                continue
            blocked_reason = blocking_reason_for_review_output(aggregate_review, remaining_steps)
            if blocked_reason:
                state["blocked_reason"] = blocked_reason
                break
            index += 1
            continue
        result = step_runner(step_name, step_input, ledger_dir, project_contract)
        steps.append(hooks.step_execution_record(step_name, result, equivalent_command, ledger_dir))
        hooks.update_state_from_step(state, step_name, result, ledger_dir)
        if step_name == "implement":
            repair_blocked_reason = no_repair_delta_blocked_reason(state)
            if repair_blocked_reason:
                state["blocked_reason"] = repair_blocked_reason
                break
        if step_name == "validate":
            repair_steps, repair_blocked_reason = validation_feedback_follow_up(
                normalized=normalized,
                state=state,
                step_spec=step_spec,
            )
            if repair_blocked_reason:
                state["blocked_reason"] = repair_blocked_reason
                break
            if repair_steps:
                state["pending_repair_context"] = build_validation_repair_context(
                    state,
                    repair_attempt=retry_attempt_count(state) + 1,
                )
                steps_queue[index + 1 : index + 1] = repair_steps
                index += 1
                continue
        blocked_reason = blocking_reason_for_step(step_name, result, remaining_steps)
        if blocked_reason:
            state["blocked_reason"] = blocked_reason
            break
        index += 1

    state["cleanup"] = final_cleanup_state(state)
    if state["blocked_reason"]:
        publication = blocked_publication(state["blocked_reason"], normalized, run_id)
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
        else:
            publication = hooks.publish_terminal_pr(
                normalized["publisher"],
                normalized=normalized,
                state=state,
                steps=steps,
                selected_work=selected_work_records(state),
                ledger=ledger,
            )
    return LifecycleOutcome(state=state, steps=steps, publication=publication)


def workflow_order_blocking_reason(step_name: str, state: dict[str, Any], repair_policy: dict[str, Any]) -> str:
    if step_name == "prepare-checkout":
        retry_block = retry_prepare_checkout_blocking_reason(state, repair_policy)
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


def blocking_reason_for_step(step_name: str, result: StepResult, remaining_steps: list[dict[str, Any]]) -> str:
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


def validation_feedback_follow_up(
    *, normalized: dict[str, Any], state: dict[str, Any], step_spec: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    if not normalized["validation_feedback"]["enabled"]:
        return [], ""
    validation = latest_validation_record(state)
    if validation is None:
        return [], ""
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    regression_reason = repair_regressed_validation_blocked_reason(state, output)
    if regression_reason:
        return [], regression_reason
    if not validation_feedback_repairable(output):
        return [], ""
    attempted_retries = retry_attempt_count(state)
    hard_cap = normalized["repair_policy"]["hard_cap"]
    if attempted_retries >= hard_cap:
        if normalized["repair_policy"]["source"] == "retry_policy":
            return [], f"retry budget exhausted: {attempted_retries} retries attempted, max_retries={hard_cap}"
        return [], f"repair budget exhausted: {attempted_retries} attempts reached hard_cap={hard_cap}"
    repair_attempt = attempted_retries + 1
    if repair_attempt_already_recorded(state, repair_attempt):
        return [], ""
    repair_context = build_validation_repair_context(state, repair_attempt=repair_attempt)
    record_repair_attempt(state, repair_attempt, repair_context=repair_context)
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


def validation_feedback_repair_steps(normalized: dict[str, Any], validate_step: dict[str, Any]) -> list[dict[str, Any]]:
    prepare_step = recipe_step_template(normalized["steps"], "prepare-checkout")
    implement_step = recipe_step_template(normalized["steps"], "implement")
    if prepare_step is None or implement_step is None:
        return []
    return [prepare_step, implement_step, dict(validate_step)]


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


def review_feedback_follow_up(
    *, normalized: dict[str, Any], state: dict[str, Any], step_spec: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    review_status = string_field(review, "status") or ""
    if review_status == "passed":
        return [], ""
    if not normalized["review_feedback"]["enabled"]:
        return [], ""
    if review_status != "request_revision":
        return [], ""
    repairable_findings = review_feedback_repairable_findings(review)
    if not repairable_findings:
        return [], review_feedback_blocked_reason(review)
    stuck_reason = stuck_same_finding_blocked_reason(state, repairable_findings)
    if stuck_reason:
        return [], stuck_reason
    attempted_retries = retry_attempt_count(state)
    hard_cap = normalized["repair_policy"]["hard_cap"]
    if attempted_retries >= hard_cap:
        if normalized["repair_policy"]["source"] == "retry_policy":
            return [], (
                f"review feedback retry budget exhausted: {attempted_retries} retries attempted, "
                f"max_retries={hard_cap}; {review_feedback_blocked_reason(review)}"
            )
        return [], (
            f"review feedback repair budget exhausted: {attempted_retries} attempts reached "
            f"hard_cap={hard_cap}; {review_feedback_blocked_reason(review)}"
        )
    repair_attempt = attempted_retries + 1
    if repair_attempt_already_recorded(state, repair_attempt):
        return [], ""
    repair_context = build_review_repair_context(state, step_spec=step_spec, repair_attempt=repair_attempt)
    record_repair_attempt(state, repair_attempt, repair_context=repair_context)
    return review_feedback_repair_steps(normalized, step_spec), ""


def review_feedback_repair_steps(normalized: dict[str, Any], review_step: dict[str, Any]) -> list[dict[str, Any]]:
    prepare_step = recipe_step_template(normalized["steps"], "prepare-checkout")
    implement_step = recipe_step_template(normalized["steps"], "implement")
    validate_step = recipe_step_template(normalized["steps"], "validate")
    if prepare_step is None or implement_step is None or validate_step is None:
        return []
    return [prepare_step, implement_step, validate_step, dict(review_step)]


def build_review_repair_context(
    state: dict[str, Any], *, step_spec: dict[str, Any], repair_attempt: int
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
    for path in (string_field(validation, "step_result_path") or "", string_field(validation, "worker_result_path") or ""):
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
    addressed = False
    for review in reviews:
        if not isinstance(review, dict) or review.get("response"):
            continue
        if string_field(review, "status") != "request-changes":
            continue
        review_response = dict(response)
        pipeline_follow_up = review.get("pipeline_follow_up")
        if isinstance(pipeline_follow_up, list) and pipeline_follow_up:
            review_response["pipeline_follow_up"] = pipeline_follow_up
        review["response"] = review_response
        addressed = True
    if addressed:
        latest_cycle["status"] = finalized_runtime_review_cycle_status(reviews)


def review_feedback_role(step_spec: dict[str, Any], review: dict[str, Any]) -> str:
    input_data = step_spec.get("input") if isinstance(step_spec.get("input"), dict) else {}
    return string_field(input_data, "role") or string_field(review, "role") or "reviewer"


def review_feedback_repairable_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    reviewer_result = review.get("reviewer_result") if isinstance(review.get("reviewer_result"), dict) else review
    findings = reviewer_result.get("findings")
    if not isinstance(findings, list):
        return []
    repairable = []
    for finding in findings:
        if not isinstance(finding, dict) or review_finding_is_pipeline_failure(finding):
            continue
        repairable.append(
            {
                "role": string_field(finding, "role") or string_field(review, "role") or "",
                "severity": review_finding_severity(finding),
                "file": review_finding_file(finding),
                "line": review_finding_line(finding),
                "stable_key": review_finding_stable_key(finding),
                "required_fix": review_finding_required_fix(finding),
                "summary": string_field(finding, "summary") or string_field(finding, "title") or "",
            }
        )
    return repairable


def review_feedback_pipeline_follow_up(review: dict[str, Any]) -> list[dict[str, Any]]:
    reviewer_result = review.get("reviewer_result") if isinstance(review.get("reviewer_result"), dict) else review
    findings = reviewer_result.get("findings")
    if not isinstance(findings, list):
        return []
    follow_up = []
    for finding in findings:
        if not isinstance(finding, dict) or not review_finding_is_pipeline_failure(finding):
            continue
        follow_up.append(
            {
                "role": string_field(finding, "role") or string_field(review, "role") or "",
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


def execute_review_cycle(
    *,
    step_spec: dict[str, Any],
    normalized: dict[str, Any],
    state: dict[str, Any],
    ledger_dir: Path,
    project_contract: ProjectContract | None,
    step_runner: StepRunner,
    hooks: LifecycleHooks,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    pass_records = []
    cycle_reviews = []
    pass_outputs = []
    last_record: dict[str, Any] | None = None
    for role in REVIEW_PASS_ROLES:
        role_step_spec = deepcopy(step_spec)
        role_input = role_step_spec.setdefault("input", {})
        role_input["role"] = role
        step_input = hooks.composed_step_input(role_step_spec, normalized, state, ledger_dir, step_index=None)
        equivalent_command = hooks.equivalent_run_step_command(
            "review",
            step_input,
            ledger_dir,
            profile=role_step_spec.get("profile"),
            project_contract=project_contract,
        )
        result = step_runner("review", step_input, ledger_dir, project_contract)
        last_record = hooks.step_execution_record("review", result, equivalent_command, ledger_dir)
        pass_records.append(
            {
                "role": role,
                "run_id": result.run_id,
                "result_path": last_record["result_path"],
                "result_abspath": last_record["result_abspath"],
                "equivalent_command": redact_text(render_review_role_command(equivalent_command)),
            }
        )
        output = result.output if isinstance(result.output, dict) else {}
        pass_outputs.append({"role": role, "output": output, "result_path": last_record["result_path"]})
        cycle_reviews.append(build_runtime_review_record(role, output, last_record["result_path"]))
    cycle = {
        "cycle": next_review_cycle_number(state),
        "status": aggregate_runtime_review_cycle_status(cycle_reviews),
        "reviews": cycle_reviews,
    }
    aggregate_review = aggregate_review_output(pass_outputs, cycle)
    assert last_record is not None
    last_record["output_status"] = string_field(aggregate_review, "status") or last_record.get("output_status", "")
    last_record["review_passes"] = pass_records
    return last_record, aggregate_review, cycle


def update_state_from_review_cycle(state: dict[str, Any], review: dict[str, Any], result_path: str) -> None:
    state["review"] = review
    review_selection = state.get("implementation_selection")
    if isinstance(review_selection, list):
        state["review_selection"] = [dict(item) for item in review_selection if isinstance(item, dict)]
    else:
        state["review_selection"] = []
    state["review_result_path"] = result_path
    cleanup = review.get("cleanup")
    if isinstance(cleanup, dict):
        state["cleanup"] = cleanup


def build_runtime_review_record(role: str, review: dict[str, Any], result_path: str) -> dict[str, Any]:
    role_scoped_review = dict(review)
    role_scoped_review.setdefault("role", role)
    review_record: dict[str, Any] = {
        "role": role,
        "status": runtime_review_cycle_status(string_field(review, "status") or ""),
        "summary": string_field(review, "summary") or role,
        "requires_response": string_field(review, "status") == "request_revision",
        "result_path": result_path,
    }
    pipeline_follow_up = review_feedback_pipeline_follow_up(role_scoped_review)
    if pipeline_follow_up:
        review_record["pipeline_follow_up"] = pipeline_follow_up
    return review_record


def aggregate_review_output(pass_outputs: list[dict[str, Any]], cycle: dict[str, Any]) -> dict[str, Any]:
    failing_reviews = [review for review in cycle["reviews"] if review.get("status") != "passed"]
    if failing_reviews:
        summary = string_field(failing_reviews[0], "summary") or "review requested changes"
        status = "request_revision" if any(review.get("status") == "request-changes" for review in failing_reviews) else "findings-open"
        role = string_field(failing_reviews[0], "role") or ""
    else:
        summary = "Correctness and bug-risk reviews passed with no open findings."
        status = "passed"
        role = "bug-risk"
    last_output = pass_outputs[-1]["output"] if pass_outputs else {}
    aggregate = dict(last_output)
    aggregate["status"] = status
    aggregate["summary"] = summary
    aggregate["role"] = role
    aggregate["reviewer_result"] = {
        "findings": aggregate_review_findings(pass_outputs),
    }
    aggregate["review_passes"] = deepcopy(cycle["reviews"])
    return aggregate


def aggregate_review_findings(pass_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated = []
    for item in pass_outputs:
        role = string_field(item, "role") or ""
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        reviewer_result = output.get("reviewer_result") if isinstance(output.get("reviewer_result"), dict) else output
        findings = reviewer_result.get("findings")
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            enriched = dict(finding)
            enriched.setdefault("role", role)
            aggregated.append(enriched)
    return aggregated


def next_review_cycle_number(state: dict[str, Any]) -> int:
    runtime_cycles = state.get("runtime_review_cycles")
    return len(runtime_cycles) + 1 if isinstance(runtime_cycles, list) else 1


def blocking_reason_for_review_output(review: dict[str, Any], remaining_steps: list[dict[str, Any]]) -> str:
    status = string_field(review, "status") or ""
    if status != "passed" and review_failure_allows_retry_follow_up(remaining_steps):
        return ""
    if status != "passed":
        return f"review did not reach passed: {status or 'missing status'}"
    return ""


def render_review_role_command(command: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def review_finding_is_pipeline_failure(finding: dict[str, Any]) -> bool:
    classification = (string_field(finding, "classification") or string_field(finding, "category") or "").lower()
    return classification in {"pipeline_failure", "tool_failure", "validation_evidence_incomplete", "runtime_failure", "protocol_failure"}


def review_finding_severity(finding: dict[str, Any]) -> str:
    severity = string_field(finding, "severity")
    if severity:
        return severity
    status = string_field(finding, "status") or ""
    return "high" if status in {"request_revision", "fail", "failed"} else "medium"


def review_finding_file(finding: dict[str, Any]) -> str:
    return string_field(finding, "file") or string_field(finding, "path") or string_field(finding, "filename") or ""


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


def validation_gate_entry(validation: dict[str, Any]) -> dict[str, Any]:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    worker_result = output.get("worker_result") if isinstance(output.get("worker_result"), dict) else {}
    worker_normalized = worker_result.get("normalized") if isinstance(worker_result.get("normalized"), dict) else {}
    return {
        "name": validation_name(validation),
        "status": string_field(output, "status") or "missing",
        "classification": string_field(output, "classification") or "",
        "summary": string_field(output, "summary") or "",
        "worker_status": string_field(worker_normalized, "status") or "missing",
        "worker_classification": string_field(worker_normalized, "classification") or "",
        "worker_summary": string_field(worker_normalized, "summary") or "",
        "worker_result": worker_result,
        "evidence_status": "valid",
        "checkout_commit": validation_checkout_commit(validation),
        "step_result_path": string_field(validation, "step_result_path") or "",
        "worker_result_path": string_field(validation, "worker_result_path") or "",
    }


def review_gate_entry(review: Any) -> dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    return {"status": string_field(review, "status") or "", "checkout_commit": review_checkout_commit(review)}


def validation_name(validation: dict[str, Any]) -> str:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    info = output.get("validation") if isinstance(output.get("validation"), dict) else {}
    return string_field(info, "requested_profile") or string_field(info, "worker_profile") or "validation"


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
    if not item_identity or not isinstance(selection, list):
        return False
    return any(isinstance(candidate, dict) and work_item_identity(candidate) == item_identity for candidate in selection)


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


def tracker_selected_work_status(state: dict[str, Any], publication: dict[str, Any], tracker: dict[str, Any]) -> str:
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


def blocked_publication(reason: str, normalized: dict[str, Any], run_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "enabled": True,
        "reason": reason,
        "next_allowed_command": rerun_workstream_command(normalized),
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


def workstream_status_from_publication(publication: dict[str, Any], tracker: dict[str, Any] | None = None) -> str:
    if publication["status"] == "published":
        return "published"
    if publication["status"] == "validated-unpublished":
        return "validated-unpublished"
    if publication["status"] == "tracker-close-blocked":
        tracker_status = string_field(tracker, "status") if isinstance(tracker, dict) else ""
        if tracker_status:
            return tracker_status
        return "review-findings-open"
    if publication["status"] == "tracker-closed":
        return "closed"
    if publication["status"] == "blocked":
        return "blocked"
    return "failed-needs-human"


def rerun_workstream_command(normalized: dict[str, Any]) -> str:
    command = f"afk run-workstream --workstream-id {normalized['workstream_id']}"
    rerun_ledger_arg = string_field(normalized, "rerun_ledger_arg")
    if rerun_ledger_arg:
        command += f" --ledger {shlex.quote(rerun_ledger_arg)}"
    command += " --input <recipe>"
    return command


def retry_instructions(normalized: dict[str, Any], run_id: str) -> str:
    return (
        "Fix the failed evidence, keep the shared review branch, and rerun "
        f"{rerun_workstream_command(normalized)}; previous workstream run: {run_id}"
    )


def retry_prepare_checkout_blocking_reason(state: dict[str, Any], repair_policy: dict[str, Any]) -> str:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list) or not attempts:
        return ""
    attempted_retries = retry_attempt_count(state)
    if attempted_retries >= repair_policy["hard_cap"]:
        if repair_policy["source"] == "retry_policy":
            return (
                f"retry budget exhausted: {attempted_retries} retries attempted, "
                f"max_retries={repair_policy['hard_cap']}"
            )
        return f"repair budget exhausted: {attempted_retries} attempts reached hard_cap={repair_policy['hard_cap']}"
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


def retry_attempt_count(state: dict[str, Any]) -> int:
    attempts = state.get("checkout_attempts")
    if not isinstance(attempts, list):
        return 0
    return sum(1 for attempt in attempts if isinstance(attempt, dict) and integer_retry_number(attempt) > 0)


def retry_budget_record(state: dict[str, Any], repair_policy: dict[str, Any]) -> dict[str, int | bool]:
    attempted_retries = retry_attempt_count(state)
    hard_cap = repair_policy["hard_cap"]
    blocked_reason = string_field(state, "blocked_reason") or ""
    return {
        "max_retries": hard_cap,
        "attempted_retries": attempted_retries,
        "remaining_retries": max(0, hard_cap - attempted_retries),
        "hard_cap_exhausted": "budget exhausted" in blocked_reason,
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
                "retry_number": int(attempt["retry_number"]),
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
    return {"status": "dirty_retry_checkouts", "resources": resources + dirty_retry_resources}


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


def validation_failure_reselects(output: dict[str, Any]) -> bool:
    return output.get("status") == "failed_validation" or output.get("classification") == "worker_failure"


def recipe_step_template(steps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for step in steps:
        if step.get("name") == name:
            return dict(step)
    return None


def repair_attempt_already_recorded(state: dict[str, Any], attempt: int) -> bool:
    history = state.get("repair_history")
    if not isinstance(history, list):
        return False
    for entry in history:
        if entry == attempt:
            return True
        if isinstance(entry, dict) and entry.get("attempt") == attempt:
            return True
    return False


def record_repair_attempt(state: dict[str, Any], attempt: int, *, repair_context: dict[str, Any] | None = None) -> None:
    history = state.get("repair_history")
    values = list(history) if isinstance(history, list) else []
    record = repair_attempt_record_from_context(attempt, repair_context)
    for index, entry in enumerate(values):
        if entry == attempt:
            values[index] = record
            state["repair_history"] = values
            return
        if isinstance(entry, dict) and entry.get("attempt") == attempt:
            merged = dict(entry)
            merged.update(record)
            values[index] = merged
            state["repair_history"] = values
            return
    values.append(record)
    state["repair_history"] = values


def repair_attempt_record_from_context(attempt: int, repair_context: dict[str, Any] | None) -> dict[str, Any]:
    record: dict[str, Any] = {"attempt": attempt}
    if not isinstance(repair_context, dict):
        return record
    trigger = string_field(repair_context, "trigger") or ""
    if trigger:
        record["trigger"] = trigger
    validation = repair_context.get("validation") if isinstance(repair_context.get("validation"), dict) else {}
    record["previous_validation_status"] = string_field(validation, "status") or ""
    record["previous_validation_classification"] = string_field(validation, "classification") or ""
    if trigger == "review_feedback":
        implementation = (
            repair_context.get("current_implementation")
            if isinstance(repair_context.get("current_implementation"), dict)
            else {}
        )
        review = repair_context.get("review") if isinstance(repair_context.get("review"), dict) else {}
        findings = review.get("findings") if isinstance(review.get("findings"), list) else []
        record["previous_implementation_commit"] = string_field(implementation, "commit") or ""
        record["review_fingerprints"] = review_finding_fingerprints(findings)
    elif trigger == "validation_feedback":
        implementation = (
            repair_context.get("previous_implementation")
            if isinstance(repair_context.get("previous_implementation"), dict)
            else {}
        )
        record["previous_implementation_commit"] = string_field(implementation, "commit") or ""
    return record


def latest_repair_attempt_record(state: dict[str, Any]) -> dict[str, Any] | None:
    history = state.get("repair_history")
    if not isinstance(history, list):
        return None
    for entry in reversed(history):
        if isinstance(entry, dict):
            return entry
    return None


def no_repair_delta_blocked_reason(state: dict[str, Any]) -> str:
    repair = latest_repair_attempt_record(state)
    if repair is None:
        return ""
    implementation = state.get("implementation") if isinstance(state.get("implementation"), dict) else {}
    if implementation.get("status") != "implemented":
        return ""
    git_info = implementation.get("git") if isinstance(implementation.get("git"), dict) else {}
    current_commit = string_field(git_info, "after_commit") or ""
    previous_commit = string_field(repair, "previous_implementation_commit") or ""
    changed_files = normalized_changed_files(git_info.get("changed_files"))
    repair["implementation_commit"] = current_commit
    repair["implementation_changed_files"] = changed_files
    if not current_commit or current_commit == previous_commit:
        return "no_repair_delta: repair produced no implementation commit"
    if not changed_files:
        return "no_repair_delta: repair produced no meaningful changed files"
    return ""


def repair_regressed_validation_blocked_reason(state: dict[str, Any], output: dict[str, Any]) -> str:
    repair = latest_repair_attempt_record(state)
    if repair is None:
        return ""
    current_status = string_field(output, "status") or ""
    current_classification = string_field(output, "classification") or ""
    repair["validation_status"] = current_status
    repair["validation_classification"] = current_classification
    if string_field(repair, "previous_validation_status") != "validated":
        return ""
    if current_status == "validated":
        return ""
    detail = string_field(output, "summary") or current_classification or current_status or "validation failed after repair"
    return f"repair_regressed_validation: {detail}"


def stuck_same_finding_blocked_reason(state: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    repair = latest_repair_attempt_record(state)
    if repair is None or string_field(repair, "trigger") != "review_feedback":
        return ""
    previous_fingerprints = repair.get("review_fingerprints")
    if not isinstance(previous_fingerprints, list) or not previous_fingerprints:
        return ""
    current_fingerprints = review_finding_fingerprints(findings)
    repeated = next(
        (
            fingerprint
            for fingerprint in current_fingerprints
            if any(review_finding_repeats(previous, fingerprint) for previous in previous_fingerprints)
        ),
        None,
    )
    if repeated is None:
        return ""
    role = repeated["role"]
    location = repeated["file"]
    if repeated["line"] is not None:
        location = f"{location}:{repeated['line']}" if location else str(repeated["line"])
    detail = repeated["required_fix"] or repeated["summary"] or "review finding"
    context = f"{role} {location}".strip()
    if context:
        return f"stuck_same_finding: {context}: {detail}"
    return f"stuck_same_finding: {detail}"


def review_finding_fingerprints(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fingerprints = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        fingerprints.append(
            {
                "role": normalize_review_finding_text(string_field(finding, "role") or ""),
                "file": normalize_review_finding_text(string_field(finding, "file") or ""),
                "line": review_finding_line(finding),
                "stable_key": normalize_review_finding_text(review_finding_stable_key(finding)),
                "required_fix": normalize_review_finding_text(string_field(finding, "required_fix") or ""),
                "summary": normalize_review_finding_text(string_field(finding, "summary") or ""),
                "key": normalize_review_finding_text(
                    string_field(finding, "required_fix") or string_field(finding, "summary") or ""
                ),
            }
        )
    unique: list[dict[str, Any]] = []
    seen = set()
    for fingerprint in fingerprints:
        signature = (
            fingerprint["role"],
            fingerprint["file"],
            fingerprint["line"],
            fingerprint["stable_key"],
            fingerprint["key"],
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(fingerprint)
    return unique


def review_finding_fingerprint_signature(fingerprint: dict[str, Any]) -> tuple[str, str, int | None, str, str]:
    return (
        str(fingerprint.get("role") or ""),
        str(fingerprint.get("file") or ""),
        fingerprint.get("line") if isinstance(fingerprint.get("line"), int) else None,
        str(fingerprint.get("stable_key") or ""),
        str(fingerprint.get("key") or ""),
    )


def review_finding_repeat_signatures(fingerprint: dict[str, Any]) -> list[tuple[str, str, str, int | None, str]]:
    role = str(fingerprint.get("role") or "")
    file_path = str(fingerprint.get("file") or "")
    line = fingerprint.get("line") if isinstance(fingerprint.get("line"), int) else None
    return [("text", role, file_path, line, str(fingerprint.get("key") or ""))]


def review_finding_repeats(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if review_finding_fingerprint_signature(previous) == review_finding_fingerprint_signature(current):
        return True
    previous_stable_key = str(previous.get("stable_key") or "")
    current_stable_key = str(current.get("stable_key") or "")
    if previous_stable_key and current_stable_key:
        return previous_stable_key == current_stable_key
    if not review_finding_roles_match(previous, current):
        return False
    return review_finding_legacy_repeat(previous, current)


def review_finding_roles_match(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    return str(previous.get("role") or "") == str(current.get("role") or "")


def review_finding_locations_match(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_file = str(previous.get("file") or "")
    current_file = str(current.get("file") or "")
    previous_line = previous.get("line") if isinstance(previous.get("line"), int) else None
    current_line = current.get("line") if isinstance(current.get("line"), int) else None
    return bool(previous_file and current_file and previous_line is not None and previous_file == current_file and previous_line == current_line)


def review_finding_legacy_repeat(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if review_finding_locations_match(previous, current):
        return review_finding_text_key(previous) == review_finding_text_key(current)
    return text_repeat_with_overlap(previous, current)


def review_finding_text_key(fingerprint: dict[str, Any]) -> str:
    return str(fingerprint.get("key") or "")


def text_repeat_with_overlap(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_tokens = meaningful_review_finding_tokens(previous)
    current_tokens = meaningful_review_finding_tokens(current)
    if not previous_tokens or not current_tokens:
        return False
    return token_overlap_ratio(previous_tokens, current_tokens) >= 0.5


def meaningful_review_finding_tokens(fingerprint: dict[str, Any]) -> set[str]:
    text = " ".join(
        part
        for part in (
            str(fingerprint.get("required_fix") or ""),
            str(fingerprint.get("summary") or ""),
        )
        if part
    )
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if len(token) > 2
    }


def token_overlap_ratio(previous_tokens: set[str], current_tokens: set[str]) -> float:
    shared = previous_tokens & current_tokens
    return len(shared) / min(len(previous_tokens), len(current_tokens))


def review_finding_stable_key(finding: dict[str, Any]) -> str:
    return (
        string_field(finding, "issue_key")
        or string_field(finding, "fingerprint")
        or string_field(finding, "stable_key")
        or ""
    )


def normalize_review_finding_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def normalized_changed_files(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [path.strip() for path in value if isinstance(path, str) and path.strip()]


def integer_retry_number(attempt: dict[str, Any]) -> int:
    value = attempt.get("retry_number")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


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


def checkout_attempt_is_dirty(attempt: dict[str, Any]) -> bool:
    return bool(attempt.get("dirty"))


def review_failed(state: dict[str, Any]) -> bool:
    review = state.get("review")
    return isinstance(review, dict) and review.get("status") not in {"", "passed"}


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


def validation_checkout_commit(validation: dict[str, Any]) -> str:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    checkout = output.get("checkout") if isinstance(output.get("checkout"), dict) else {}
    return string_field(checkout, "start_commit") or ""


def review_checkout_commit(review: dict[str, Any]) -> str:
    checkout = review.get("checkout") if isinstance(review.get("checkout"), dict) else {}
    return string_field(checkout, "start_commit") or ""


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


def select_work_proves_different_item(input_data: Any, state: dict[str, Any]) -> bool:
    current_aliases = selected_work_aliases(state.get("selected_work"))
    if not current_aliases or not isinstance(input_data, dict):
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


def string_field(input_data: dict[str, Any] | None, key: str = "") -> str | None:
    if not isinstance(input_data, dict):
        return None
    value = input_data.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _validation_feedback_text_has_infra_or_setup_failure(text: str) -> bool:
    lowered = text.lower()
    return (
        ("permission denied" in lowered and "starting zone harness" in lowered)
        or ("fatal: chdir" in lowered and "no such file or directory" in lowered)
        or ("bash:" in lowered and "no such file or directory" in lowered)
        or ("source directory" in lowered and "does not exist" in lowered)
    )


def _retrospective_text_has_missing_tool_or_config(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("command not found", "executable file not found", "config_dir", "not installed", "missing tool", "missing config")
    )
