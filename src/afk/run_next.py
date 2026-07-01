from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from afk.contracts import ProjectContract
from afk.recipes import RUNNABLE_REQUIRED_METADATA, generate_workstream_recipe
from afk.selection import deterministic_candidate
from afk.work_sources import select_work


READY_TAG = "ready-for-agent"
ALLOWED_SELECTOR_MODELS = {"gpt-5.3-codex-spark", "gpt-5.4-mini"}
SELECTOR_TIMEOUT_SECONDS = 60


def run_next(
    *,
    project_contract: ProjectContract,
    beads_workspace: Path,
    checkout_root: Path,
    checkout_path: Path,
    validation_profile: str,
    validation_input: dict[str, Any] | None = None,
    agent: dict[str, Any] | None = None,
    reviewer: dict[str, Any] | None = None,
    retrospective_judge: dict[str, Any] | None = None,
    retrospective_follow_up: dict[str, Any] | None = None,
    publisher_factory: Callable[[str], dict[str, Any] | None] | None = None,
    ready_tag: str = READY_TAG,
    selector_mode: str = "deterministic",
    selector_model: str | None = None,
    selector_choice_json: str | None = None,
    enable_review_feedback: bool = False,
    execute: bool = False,
    ledger_dir: Path | None = None,
    workstream_runner: Callable[..., Any] | None = None,
    tracker_artifact_root: Path | None = None,
) -> dict[str, Any]:
    workspace = validate_beads_workspace(beads_workspace)
    selection_request = build_selection_request(
        project_contract,
        beads_workspace=workspace,
        ready_tag=ready_tag,
        tracker_artifact_root=tracker_artifact_root,
    )
    selection_result = select_work(selection_request, project_contract=project_contract)
    chosen = choose_candidate(
        selection_result.get("selected_work") or [],
        selector_mode=selector_mode,
        selector_model=selector_model,
        selector_choice_json=selector_choice_json,
    )
    recipe = None
    workstream_result = None
    if chosen is not None:
        recipe = generate_workstream_recipe(
            workstream_id=chosen["external_id"],
            project_contract=project_contract,
            beads_workspace=workspace,
            checkout_root=checkout_root,
            checkout_path=checkout_path,
            validation_profile=validation_profile,
            validation_input=validation_input,
            agent=agent,
            reviewer=reviewer,
            retrospective_judge=retrospective_judge,
            retrospective_follow_up=retrospective_follow_up,
            publisher=publisher_factory(chosen["external_id"]) if publisher_factory is not None else None,
            sources=selection_request["sources"],
            required_labels=selection_request["required_labels"],
            required_metadata=selection_request["required_metadata"],
            enable_review_feedback=enable_review_feedback,
        )
        if execute:
            if ledger_dir is None:
                raise ValueError("--ledger is required when --execute is set")
            if workstream_runner is None:
                raise ValueError("workstream_runner is required when --execute is set")
            workstream_result = normalize_workstream_result(
                workstream_runner(recipe, ledger_dir=ledger_dir, project_contract=project_contract)
            )
    elif execute and ledger_dir is None:
        raise ValueError("--ledger is required when --execute is set")
    return {
        "command": "run-next",
        "project": project_contract.project_slug,
        "selection_request": selection_request,
        "selection_result": selection_result,
        "selector": selector_result(chosen, selector_mode=selector_mode, selector_model=selector_model),
        "recipe": recipe,
        "workstream_result": workstream_result,
    }


def validate_beads_workspace(beads_workspace: Path) -> Path:
    try:
        resolved_workspace = beads_workspace.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"beads workspace is not available: {beads_workspace}") from exc
    if not resolved_workspace.is_dir():
        raise ValueError(f"beads workspace is not available: {beads_workspace}")
    if not os.access(resolved_workspace, os.R_OK | os.X_OK):
        raise ValueError(f"beads workspace is not readable: {beads_workspace}")
    return resolved_workspace


def build_selection_request(
    project_contract: ProjectContract,
    *,
    beads_workspace: Path,
    ready_tag: str,
    tracker_artifact_root: Path | None = None,
) -> dict[str, Any]:
    required_labels = list(project_contract.beads_labels) + [ready_tag]
    tracker_root = (tracker_artifact_root or Path.cwd()).resolve(strict=False)
    sources = [
        {
            "type": "beads",
            "id": "central-beads",
            "workspace": str(beads_workspace),
            "workspace_kind": "central",
            "ready_label": ready_tag,
            "labels": required_labels,
            "status": "open",
            "tracker_artifact_roots": [str(tracker_root)],
        }
    ]
    github_repo = github_repo_from_repo_url(project_contract.repo_url)
    if github_repo:
        sources.append(
            {
                "type": "github_issues",
                "id": "github",
                "repo": github_repo,
                "ready_label": ready_tag,
                "labels": required_labels,
                "query": f"label:{ready_tag} is:open",
            }
        )
    return {
        "required_labels": required_labels,
        "required_metadata": list(RUNNABLE_REQUIRED_METADATA),
        "sources": sources,
    }


def choose_candidate(
    candidates: list[dict[str, Any]],
    *,
    selector_mode: str,
    selector_model: str | None,
    selector_choice_json: str | None,
) -> dict[str, Any] | None:
    if selector_mode not in {"deterministic", "model"}:
        raise ValueError("selector mode must be deterministic or model")
    if selector_mode == "model":
        validate_selector_model(selector_model)
    if not candidates:
        return None
    if selector_mode == "model":
        model_choice = parse_selector_choice(selector_model, selector_choice_json, candidates)
        if model_choice is None and selector_choice_json is None:
            model_choice = invoke_codex_selector(selector_model, candidates)
        if model_choice is not None:
            return model_choice
    return deterministic_candidate(candidates)


def validate_selector_model(selector_model: str | None) -> None:
    if selector_model is None:
        raise ValueError("selector model is required for model mode")
    if selector_model not in ALLOWED_SELECTOR_MODELS:
        raise ValueError(
            "selector model must be one of: gpt-5.3-codex-spark, gpt-5.4-mini"
        )


def selector_result(
    chosen: dict[str, Any] | None,
    *,
    selector_mode: str,
    selector_model: str | None,
) -> dict[str, Any]:
    if chosen is None:
        return {"mode": selector_mode, "model": selector_model, "selected": None, "rationale": "no candidates"}
    return {
        "mode": selector_mode,
        "model": selector_model,
        "selected": {
            "source_id": chosen["source_id"],
            "source_type": chosen["source_type"],
            "external_id": chosen["external_id"],
            "rationale": chosen.get("selector_rationale", "deterministic default"),
        },
    }

def parse_selector_choice(
    selector_model: str | None,
    selector_choice_json: str | None,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    validate_selector_model(selector_model)
    if not selector_choice_json:
        return None
    try:
        choice = json.loads(selector_choice_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(choice, dict):
        return None
    chosen_id = choice.get("external_id")
    if not isinstance(chosen_id, str) or not chosen_id:
        return None
    for candidate in candidates:
        if candidate.get("external_id") == chosen_id:
            selected = dict(candidate)
            rationale = choice.get("rationale")
            if isinstance(rationale, str) and rationale.strip():
                selected["selector_rationale"] = rationale.strip()
            else:
                selected["selector_rationale"] = f"model {selector_model}"
            return selected
    return None


def invoke_codex_selector(selector_model: str | None, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    validate_selector_model(selector_model)
    if shutil.which("codex") is None:
        return None
    prompt = selector_prompt(candidates)
    try:
        with tempfile.TemporaryDirectory(prefix="afk-selector-") as temp_dir:
            output_path = Path(temp_dir) / "selector-result.json"
            completed = subprocess.run(
                [
                    "codex",
                    "exec",
                    "--model",
                    str(selector_model),
                    "--sandbox",
                    "read-only",
                    "--ephemeral",
                    "--ignore-rules",
                    "--output-last-message",
                    str(output_path),
                    prompt,
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=SELECTOR_TIMEOUT_SECONDS,
            )
            if completed.returncode != 0 or not output_path.is_file():
                return None
            return parse_selector_choice(selector_model, output_path.read_text(encoding="utf-8"), candidates)
    except (OSError, subprocess.TimeoutExpired):
        return None


def selector_prompt(candidates: list[dict[str, Any]]) -> str:
    selector_candidates = [
        {
            "source_id": candidate.get("source_id"),
            "source_type": candidate.get("source_type"),
            "external_id": candidate.get("external_id"),
            "title": candidate.get("title"),
            "priority": candidate.get("priority"),
            "issue_type": candidate.get("issue_type"),
            "labels": candidate.get("labels", []),
            "workstream": candidate.get("workstream"),
            "description": candidate.get("description"),
            "acceptance_criteria": candidate.get("acceptance_criteria", []),
        }
        for candidate in candidates
    ]
    return (
        "Choose one ready, unblocked work item from this JSON array. "
        "Return only JSON with keys external_id and rationale. "
        "Do not use tools.\n\n"
        + json.dumps({"candidates": selector_candidates}, sort_keys=True)
    )


def normalize_workstream_result(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    if isinstance(result, dict):
        return dict(result)
    fields = ("run_id", "workstream_id", "parent", "status", "result_path", "publication_status")
    if all(hasattr(result, field) for field in fields):
        return {field: getattr(result, field) for field in fields}
    if hasattr(result, "__dict__"):
        return dict(result.__dict__)
    return {"result": result}


def github_repo_from_repo_url(repo_url: str) -> str | None:
    parsed = urlsplit(repo_url)
    path = ""
    if parsed.scheme in {"http", "https", "ssh"}:
        if parsed.hostname != "github.com":
            return None
        path = parsed.path
    elif repo_url.startswith("git@github.com:"):
        path = repo_url.split("git@github.com:", 1)[1]
    elif repo_url.startswith("github.com/"):
        path = repo_url.split("github.com/", 1)[1]
    if not path:
        return None
    cleaned = path.strip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if cleaned.count("/") != 1:
        return None
    return cleaned
