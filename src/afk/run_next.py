from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from afk.contracts import ProjectContract
from afk.recipes import RUNNABLE_REQUIRED_METADATA, generate_workstream_recipe
from afk.selection import deterministic_candidate
from afk.work_sources import select_work


READY_TAG = "ready-for-agent"
WORKSTREAM_RESULT_FIELDS = ("run_id", "workstream_id", "parent", "status", "result_path", "publication_status")
WORKSTREAM_RESULT_SUMMARY_FIELDS = ("publication", "tracker", "artifacts", "pipeline_retrospective")


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
    enable_review_feedback: bool = False,
    expect_generated_smoke_dry_run: bool = False,
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
    chosen = choose_candidate(selection_result.get("selected_work") or [])
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
            expect_generated_smoke_dry_run=expect_generated_smoke_dry_run,
        )
        if execute:
            if workstream_runner is None:
                raise ValueError("workstream_runner is required when --execute is set")
            workstream_result = normalize_workstream_result(
                workstream_runner(recipe, ledger_dir=ledger_dir, project_contract=project_contract),
                ledger_dir=ledger_dir,
            )
    return {
        "command": "run-next",
        "project": project_contract.project_slug,
        "selection_request": selection_request,
        "selection_result": annotate_selection_result(selection_result),
        "chosen_work": selected_work_snapshot(chosen),
        "selector": selector_result(chosen),
        "recipe": recipe,
        "workstream_result": workstream_result,
    }


def validate_beads_workspace(beads_workspace: Path) -> Path:
    try:
        resolved_workspace = beads_workspace.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"beads workspace is not available: {beads_workspace}") from exc
    if ".beads" in resolved_workspace.parts:
        raise ValueError("project-local .beads workspace is not allowed")
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
) -> dict[str, Any] | None:
    if not candidates:
        return None
    return deterministic_candidate(candidates)


def selector_result(chosen: dict[str, Any] | None) -> dict[str, Any]:
    if chosen is None:
        return {"mode": "deterministic", "model": None, "selected": None, "rationale": "no candidates"}
    selected = selected_work_snapshot(chosen)
    return {
        "mode": "deterministic",
        "model": None,
        "selected": {
            "source_id": selected["source_id"],
            "source_type": selected["source_type"],
            "external_id": selected["external_id"],
            "rationale": "deterministic default",
        },
    }


def annotate_selection_result(selection_result: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(selection_result)
    annotated["selected_work"] = scrub_selected_work_value(annotated.get("selected_work"))
    annotated["source_statuses"] = scrub_selected_work_containers(annotated.get("source_statuses"))
    annotated["selected_work_kind"] = "candidate_list"
    return annotated


def selected_work_snapshot(chosen: dict[str, Any] | None) -> dict[str, Any] | None:
    if chosen is None:
        return None
    return {key: value for key, value in chosen.items() if not key.startswith("selector_")}


def scrub_selected_work_containers(value: Any) -> Any:
    if isinstance(value, list):
        return [scrub_selected_work_containers(item) for item in value]
    if not isinstance(value, dict):
        return value
    scrubbed = dict(value)
    if "selected_work" in scrubbed:
        scrubbed["selected_work"] = scrub_selected_work_value(scrubbed["selected_work"])
    return scrubbed


def scrub_selected_work_value(value: Any) -> Any:
    if isinstance(value, list):
        return [scrub_selected_work_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: scrub_selected_work_value(item)
            for key, item in value.items()
            if not key.startswith("selector_")
        }
    return value


def normalize_workstream_result(result: Any, *, ledger_dir: Path | None = None) -> dict[str, Any] | None:
    if result is None:
        return None
    if isinstance(result, dict):
        return dict(result)
    if all(hasattr(result, field) for field in WORKSTREAM_RESULT_FIELDS):
        normalized = {field: getattr(result, field) for field in WORKSTREAM_RESULT_FIELDS}
        normalized.update(load_workstream_result_summary(normalized["result_path"], ledger_dir=ledger_dir))
        return normalized
    return {"result": result}


def load_workstream_result_summary(result_path: Any, *, ledger_dir: Path | None) -> dict[str, Any]:
    if not isinstance(result_path, str) or not result_path or ledger_dir is None:
        return {}
    payload_path = ledger_dir / result_path
    ledger_root = ledger_dir.resolve()
    try:
        payload_path.resolve(strict=False).relative_to(ledger_root)
    except ValueError:
        return {}
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {field: payload[field] for field in WORKSTREAM_RESULT_SUMMARY_FIELDS if field in payload}


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
