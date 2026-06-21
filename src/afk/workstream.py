from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from afk.contracts import ProjectContract
from afk.jsonutil import canonical_json, sha256_json
from afk.redaction import is_secret_key, redact_artifact_value, redact_text
from afk.registry import StepResult


SCHEMA_VERSION = 1
KNOWN_WORKSTREAM_STEPS = {"select-work", "prepare-checkout", "implement", "validate", "review"}


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
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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
        "implementation": None,
        "validations": [],
        "review": None,
        "cleanup": {"status": "unknown", "resources": []},
        "blocked_reason": "",
        "stop_reason": "",
        "next_allowed_command": "",
    }
    steps = []

    for step_spec in normalized["steps"]:
        step_name = step_spec["name"]
        stop_reason = terminal_stop_reason(step_spec, state)
        if stop_reason:
            state["stop_reason"] = stop_reason
            state["next_allowed_command"] = next_allowed_command_for_terminal_stop(state, normalized)
            break
        blocked_reason = workflow_order_blocking_reason(step_name, state)
        if blocked_reason:
            state["blocked_reason"] = blocked_reason
            break
        step_input = composed_step_input(step_spec, normalized, state, ledger_dir)
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
        blocked_reason = blocking_reason_for_step(step_name, result)
        if blocked_reason:
            state["blocked_reason"] = blocked_reason
            break

    publication: dict[str, Any]
    if state["blocked_reason"]:
        selected_work = selected_work_records(state, review_status(state))
        publication = blocked_publication(state["blocked_reason"], normalized, run_id)
        status = publication["status"]
    else:
        publication_gate = publication_gate_reason(state)
        if publication_gate:
            if state["stop_reason"] and has_current_validated_evidence(state):
                selected_work = selected_work_records(state, terminal_selected_work_status(state))
                publication = validated_unpublished_publication(
                    state["stop_reason"],
                    next_allowed_command=state["next_allowed_command"] or rerun_workstream_command(normalized),
                )
            else:
                selected_work = selected_work_records(state, gated_selected_work_status(state))
                publication = blocked_publication(publication_gate, normalized, run_id)
            status = publication["status"]
        else:
            selected_work = selected_work_records(state, review_status(state))
            publication = publish_terminal_pr(
                normalized["publisher"],
                normalized=normalized,
                state=state,
                steps=steps,
                selected_work=selected_work,
                ledger=ledger,
            )
            status = workstream_status_from_publication(publication)
            if status == "validated-unpublished":
                selected_work = selected_work_records(state, terminal_selected_work_status(state))

    result_payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workstream_id": normalized["workstream_id"],
        "parent": normalized["parent"],
        "review_branch": normalized["review_branch"],
        "status": status,
        "steps": steps,
        "selected_work": selected_work,
        "cleanup": state["cleanup"],
        "retry": publication.get("retry", ""),
        "terminal_reason": publication.get("reason", ""),
        "next_allowed_command": publication.get("next_allowed_command", ""),
        "publication": publication,
        "artifacts": workstream_artifacts(ledger),
    }
    ledger.write_json("publication-result.json", publication)
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
    return {
        "schema_version": SCHEMA_VERSION,
        "workstream_id": resolved_workstream_id,
        "parent": resolved_parent,
        "review_branch": review_branch,
        "steps": normalized_steps,
        "publisher": publisher,
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
    elif step_name == "implement":
        input_data["work_selection"] = {"schema_version": SCHEMA_VERSION, "selected_work": state["selected_work"]}
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
            input_data["work_item"] = state["selected_work"][0]
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
        previous_identity = current_selected_work_identity(state)
        selected = output.get("selected_work")
        state["selected_work"] = list(selected) if isinstance(selected, list) else []
        if previous_identity and current_selected_work_identity(state) != previous_identity:
            reset_cycle_state_for_new_selection(state)
    elif step_name == "prepare-checkout":
        state["checkout"] = output
    elif step_name == "implement":
        state["implementation"] = output
        state["checkout"] = checkout_after_implementation(state.get("checkout"), output)
        if output.get("status") == "implemented":
            state["validations"] = []
            state["review"] = None
    elif step_name == "validate":
        state["validations"].append(
            {
                "run_id": result.run_id,
                "output": output,
                "step_result_path": str((ledger_dir / "runs" / result.run_id / "step-result.json").resolve(strict=False)),
                "worker_result_path": str((ledger_dir / "runs" / result.run_id / "worker-result.json").resolve(strict=False)),
            }
        )
    elif step_name == "review":
        state["review"] = output
        cleanup = output.get("cleanup")
        if isinstance(cleanup, dict):
            state["cleanup"] = cleanup


def workflow_order_blocking_reason(step_name: str, state: dict[str, Any]) -> str:
    if step_name in {"implement", "review"} and selected_work_count(state) > 1:
        return "MVP supports a single selected work item; narrow the selection and rerun"
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


def current_selected_work_identity(state: dict[str, Any]) -> str:
    selected_work = state.get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return ""
    first_item = selected_work[0]
    if not isinstance(first_item, dict):
        return ""
    return work_item_identity(first_item)


def work_item_identity(item: dict[str, Any]) -> str:
    return string_field(item, "url") or string_field(item, "external_id") or ""


def select_work_proves_different_item(input_data: Any, state: dict[str, Any]) -> bool:
    current_identity = current_selected_work_identity(state)
    current_external_id = current_selected_work_external_id(state)
    if not current_identity and not current_external_id:
        return False
    if not isinstance(input_data, dict):
        return False

    target_ids = input_data.get("target_ids")
    if isinstance(target_ids, list) and target_ids and all(isinstance(item, str) and item.strip() for item in target_ids):
        return current_external_id not in {item.strip() for item in target_ids}

    candidate_identities = select_work_candidate_identities(input_data)
    if not candidate_identities:
        return False
    return current_identity not in candidate_identities and current_external_id not in candidate_identities


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
        items = source.get("items")
        if not isinstance(items, list):
            return set()
        for item in items:
            if not isinstance(item, dict):
                return set()
            identity = work_item_identity(item)
            external_id = string_field(item, "external_id")
            if not identity and not external_id:
                return set()
            if identity:
                identities.add(identity)
            if external_id:
                identities.add(external_id)
    return identities


def reset_cycle_state_for_new_selection(state: dict[str, Any]) -> None:
    state["checkout"] = None
    state["implementation"] = None
    state["validations"] = []
    state["review"] = None
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


def blocking_reason_for_step(step_name: str, result: StepResult) -> str:
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
    return ""


def validation_checkout_commit(validation: dict[str, Any]) -> str:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    checkout = output.get("checkout") if isinstance(output.get("checkout"), dict) else {}
    return string_field(checkout, "start_commit") or ""


def review_checkout_commit(review: dict[str, Any]) -> str:
    checkout = review.get("checkout") if isinstance(review.get("checkout"), dict) else {}
    return string_field(checkout, "start_commit") or ""


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
    try:
        run_publisher_command(
            [config["gh_path"], "auth", "status", "--hostname", "github.com"],
            cwd=checkout_path,
            tool="gh",
            auth=auth,
            message_on_failure="gh auth status failed",
        )
        if config["push"]:
            run_publisher_command(
                [config["git_path"], "push", config["remote"], f"HEAD:refs/heads/{config['head']}"],
                cwd=checkout_path,
                tool="git",
                auth=auth,
            )
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
                str(ledger.path / "pr-body.md"),
            ]
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
                str(ledger.path / "pr-body.md"),
            ]
        completed = run_publisher_command(command, cwd=checkout_path, tool="gh", auth=auth)
    except PublisherError as exc:
        return failed_publication(exc, normalized, auth=auth_artifact)
    return {
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
            "git_push": (
                redact_artifact_value([config["git_path"], "push", config["remote"], f"HEAD:refs/heads/{config['head']}"])
                if config["push"]
                else []
            ),
        },
        "body_path": str(ledger.path / "pr-body.md"),
    }


def successful_publisher_url(stdout: str) -> str:
    return redact_text(stdout.strip().splitlines()[-1]) if stdout.strip() else ""


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
    for validation in state["validations"]:
        step_ref = ledger_relative_path(validation["step_result_path"])
        worker_ref = ledger_relative_path(validation["worker_result_path"])
        lines.append(
            f"- {pr_body_value(validation_name(validation))}: "
            f"{pr_body_value(validation['output'].get('status', 'missing'))} "
            f"({pr_body_value(step_ref)}; {pr_body_value(worker_ref)})"
        )
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
            "Retry: not required after successful publication",
            "",
            "## Artifacts",
            "",
            f"- Workstream result: {pr_body_value(f'workstreams/{ledger.run_id}/workstream-result.json')}",
        ]
    )
    for step in steps:
        lines.append(f"- {pr_body_value(step['name'])}: {pr_body_value(step['result_path'])}")
    lines.append("")
    return "\n".join(lines)


def pr_body_value(value: Any) -> str:
    return redact_text(str(value))


def selected_work_records(state: dict[str, Any], result_status: str) -> list[dict[str, str]]:
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
                "result": redact_text(result_status),
            }
        )
    return records


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
    if publication["status"] == "blocked":
        return "blocked"
    return "failed-needs-human"


def workstream_artifacts(ledger: "WorkstreamLedger") -> dict[str, str]:
    artifacts = {
        "workstream_result": "workstream-result.json",
        "command": "command.json",
        "publication": "publication-result.json",
    }
    if (ledger.path / "pr-body.md").is_file():
        artifacts["pr_body"] = "pr-body.md"
    return artifacts


def failed_publication(
    exc: PublisherError,
    normalized: dict[str, Any],
    *,
    auth: dict[str, Any],
) -> dict[str, Any]:
    next_allowed_command = rerun_workstream_command(normalized)
    return {
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


def validation_name(validation: dict[str, Any]) -> str:
    output = validation.get("output") if isinstance(validation.get("output"), dict) else {}
    info = output.get("validation") if isinstance(output.get("validation"), dict) else {}
    return string_field(info, "requested_profile") or string_field(info, "worker_profile") or "validation"


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
