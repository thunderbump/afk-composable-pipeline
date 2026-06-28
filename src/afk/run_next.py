from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from afk.contracts import ProjectContract
from afk.recipes import generate_workstream_recipe
from afk.work_sources import select_work


READY_TAG = "ready-for-agent"
DEFAULT_BEADS_WORKSPACE = Path("/home/bump/Projects/beads")
ALLOWED_SELECTOR_MODELS = {"gpt-5.3-codex-spark", "gpt-5.4-mini"}


def run_next(
    *,
    project_contract: ProjectContract,
    beads_workspace: Path | None = None,
    checkout_root: Path,
    checkout_path: Path,
    validation_profile: str,
    ready_tag: str = READY_TAG,
    selector_mode: str = "deterministic",
    selector_model: str | None = None,
    selector_choice_json: str | None = None,
) -> dict[str, Any]:
    workspace = beads_workspace or DEFAULT_BEADS_WORKSPACE
    selection_request = build_selection_request(
        project_contract,
        beads_workspace=workspace,
        ready_tag=ready_tag,
    )
    selection_result = select_work(selection_request, project_contract=project_contract)
    chosen = choose_candidate(
        selection_result.get("selected_work") or [],
        selector_mode=selector_mode,
        selector_model=selector_model,
        selector_choice_json=selector_choice_json,
    )
    recipe = None
    if chosen is not None:
        recipe = generate_workstream_recipe(
            workstream_id=chosen["external_id"],
            project_contract=project_contract,
            beads_workspace=workspace,
            checkout_root=checkout_root,
            checkout_path=checkout_path,
            validation_profile=validation_profile,
            sources=selection_request["sources"],
        )
    return {
        "command": "run-next",
        "project": project_contract.project_slug,
        "selection_request": selection_request,
        "selection_result": selection_result,
        "selector": selector_result(chosen, selector_mode=selector_mode, selector_model=selector_model),
        "recipe": recipe,
    }


def build_selection_request(
    project_contract: ProjectContract,
    *,
    beads_workspace: Path,
    ready_tag: str,
) -> dict[str, Any]:
    required_labels = list(project_contract.beads_labels) + [ready_tag]
    sources = [
        {
            "type": "beads",
            "id": "central-beads",
            "workspace": str(beads_workspace),
            "workspace_kind": "central",
            "labels": required_labels,
            "status": "open",
        }
    ]
    github_repo = github_repo_from_repo_url(project_contract.repo_url)
    if github_repo:
        sources.append(
            {
                "type": "github_issues",
                "id": "github",
                "repo": github_repo,
                "labels": required_labels,
                "query": f"label:{ready_tag} is:open",
            }
        )
    return {
        "required_labels": required_labels,
        "sources": sources,
    }


def choose_candidate(
    candidates: list[dict[str, Any]],
    *,
    selector_mode: str,
    selector_model: str | None,
    selector_choice_json: str | None,
) -> dict[str, Any] | None:
    if selector_mode == "model":
        validate_selector_model(selector_model)
    if not candidates:
        return None
    if selector_mode == "model":
        model_choice = parse_selector_choice(selector_model, selector_choice_json, candidates)
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


def deterministic_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(candidates, key=candidate_sort_key)[0]


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, str, str, str]:
    source_type_priority = 0 if candidate.get("source_type") == "beads" else 1
    return (
        source_type_priority,
        str(candidate.get("workstream") or ""),
        str(candidate.get("external_id") or ""),
        str(candidate.get("title") or ""),
    )


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


def github_repo_from_repo_url(repo_url: str) -> str | None:
    parsed = urlsplit(repo_url)
    path = ""
    if parsed.scheme in {"http", "https", "ssh"}:
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
