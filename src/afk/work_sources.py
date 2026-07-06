from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from afk.recipes import review_branch_for_workstream
from afk.selection import deterministic_candidates

SCHEMA_VERSION = 1
OPEN_TRACKER_STATUSES = {
    "awaiting-review",
    "review-findings-open",
    "review-feedback-addressed",
}


class SourceLoadError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def select_work_step(context: Any) -> dict[str, Any]:
    return select_work(context.input_data, project_contract=context.project_contract)


def select_work(input_data: Any, *, project_contract: Any = None) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        return invalid_request_result("request must be an object")

    request = input_data
    request_error = request_payload_error(request)
    if request_error is not None:
        return invalid_request_result(request_error)

    required_labels = (
        list(request["required_labels"])
        if "required_labels" in request
        else default_required_labels(project_contract)
    )
    required_metadata = list(request.get("required_metadata", []))
    allowed_statuses = set(request.get("allowed_statuses", ["open"]))
    target_ids = set(request.get("target_ids", []))
    exclude_ids = set(request.get("exclude_ids", []))
    selection_limit = request.get("selection_limit")

    source_statuses: list[dict[str, Any]] = []
    selected_work: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    selected_keys: set[str] = set()

    for source in request.get("sources") or []:
        if not isinstance(source, dict):
            source_statuses.append(
                source_status(
                    "unknown",
                    "unknown",
                    "failed_invalid_payload",
                    0,
                    0,
                    "source must be an object",
                )
            )
            continue
        source_type = str(source.get("type") or "")
        source_id = str(source.get("id") or source_type or "unknown")

        adapter_status = source_prerequisite_status(source)
        if adapter_status is not None:
            source_statuses.append(adapter_status)
            continue

        source_for_load = dict(source)
        if source_type == "beads" and target_ids:
            source_for_load["target_ids"] = sorted(target_ids)

        try:
            raw_items = load_source_items(source_for_load)
        except SourceLoadError as exc:
            source_statuses.append(
                source_status(source_id, source_type, exc.status, 0, 0, exc.message)
            )
            continue
        except (TypeError, ValueError, AttributeError) as exc:
            source_statuses.append(
                source_status(
                    source_id,
                    source_type,
                    "failed_invalid_payload",
                    0,
                    0,
                    f"{source_type} payload could not be normalized: {exc.__class__.__name__}",
                )
            )
            continue
        if not isinstance(raw_items, list):
            source_statuses.append(
                source_status(
                    source_id,
                    source_type,
                    "failed_invalid_payload",
                    0,
                    0,
                    f"{source_type} items must be a list",
                )
            )
            continue

        selected_count = 0
        for raw_item in raw_items:
            payload_error = candidate_payload_error(raw_item)
            if payload_error is not None:
                skipped_candidates.append(
                    {"candidate": invalid_candidate(source_id, source_type, raw_item), "reason": payload_error}
                )
                continue
            candidate = normalize_candidate(source_id, source_type, raw_item)
            selection_skip_reason = raw_item.get("selection_skip_reason")
            if isinstance(selection_skip_reason, str) and selection_skip_reason:
                skipped_candidates.append({"candidate": candidate, "reason": selection_skip_reason})
                continue
            rejection = rejection_reason(
                candidate,
                required_labels,
                required_metadata,
                allowed_statuses,
                target_ids,
                exclude_ids,
            )
            if rejection is not None:
                skipped_candidates.append({"candidate": candidate, "reason": rejection})
                continue
            key = duplicate_key(candidate)
            if key in selected_keys:
                skipped_candidates.append({"candidate": candidate, "reason": f"duplicate:{key}"})
                continue
            selected_keys.add(key)
            selected_work.append(candidate)
            selected_count += 1

        status = "selected" if selected_count else "skipped_empty"
        source_statuses.append(
            source_status(
                source_id,
                source_type,
                status,
                len(raw_items),
                selected_count,
                selected_message(selected_count),
            )
        )

    if isinstance(selection_limit, int) and not isinstance(selection_limit, bool) and selection_limit >= 0:
        selected_work, overflow = limited_selected_work(selected_work, selection_limit)
        for candidate in overflow:
            skipped_candidates.append({"candidate": candidate, "reason": "selection_limit_exceeded"})

    return {
        "schema_version": SCHEMA_VERSION,
        "source_statuses": source_statuses,
        "selected_work": selected_work,
        "skipped_candidates": skipped_candidates,
    }


def invalid_request_result(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_statuses": [
            source_status(
                "request",
                "request",
                "failed_invalid_payload",
                0,
                0,
                message,
            )
        ],
        "selected_work": [],
        "skipped_candidates": [],
    }


def request_payload_error(request: dict[str, Any]) -> str | None:
    for key in ("required_labels", "required_metadata", "allowed_statuses", "target_ids", "exclude_ids"):
        if key in request and not is_string_list(request[key]):
            return f"{key} must be a list of strings"
    if "selection_limit" in request:
        selection_limit = request["selection_limit"]
        if isinstance(selection_limit, bool) or not isinstance(selection_limit, int) or selection_limit < 0:
            return "selection_limit must be a non-negative integer"
    if "sources" in request and not isinstance(request["sources"], list):
        return "sources must be a list"
    return None


def limited_selected_work(
    selected_work: list[dict[str, Any]],
    selection_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if selection_limit >= len(selected_work):
        return selected_work, []
    limited = deterministic_candidates(selected_work, limit=selection_limit)
    selected_identities = {duplicate_key(candidate) for candidate in limited}
    overflow = [candidate for candidate in selected_work if duplicate_key(candidate) not in selected_identities]
    return limited, overflow


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def source_prerequisite_status(source: dict[str, Any]) -> dict[str, Any] | None:
    source_type = str(source.get("type") or "")
    source_id = str(source.get("id") or source_type or "unknown")
    if source_type == "fixture":
        return None
    if source_type == "github_issues":
        if not source.get("repo") or not (source.get("labels") or source.get("query")):
            return source_status(
                source_id,
                source_type,
                "skipped_unconfigured",
                0,
                0,
                "GitHub repo and labels or query are required",
            )
        if shutil.which("gh") is None:
            return source_status(
                source_id,
                source_type,
                "skipped_unreachable",
                0,
                0,
                "gh command is not available",
            )
        if not github_auth_available():
            return source_status(
                source_id,
                source_type,
                "skipped_no_auth",
                0,
                0,
                "GH_TOKEN or GITHUB_TOKEN is required",
            )
        return None
    if source_type == "beads":
        workspace = source.get("workspace")
        if not workspace:
            return source_status(
                source_id,
                source_type,
                "skipped_unconfigured",
                0,
                0,
                "beads workspace is required",
            )
        if source.get("workspace_kind") not in {"central", "mounted"}:
            return source_status(
                source_id,
                source_type,
                "skipped_unconfigured",
                0,
                0,
                "beads workspace_kind must be central or mounted",
            )
        if source.get("credentials_path"):
            return source_status(
                source_id,
                source_type,
                "skipped_unconfigured",
                0,
                0,
                "credentials_path override is not supported",
            )
        workspace_path = Path(str(workspace))
        if not workspace_path.is_absolute():
            return source_status(
                source_id,
                source_type,
                "skipped_unconfigured",
                0,
                0,
                "beads workspace must be an absolute mounted path",
            )
        try:
            resolved_workspace_path = workspace_path.resolve(strict=True)
        except OSError:
            return source_status(
                source_id,
                source_type,
                "skipped_unreachable",
                0,
                0,
                "beads workspace is not available",
            )
        if ".beads" in resolved_workspace_path.parts:
            return source_status(
                source_id,
                source_type,
                "skipped_unconfigured",
                0,
                0,
                "project-local .beads workspace is not allowed",
            )
        if not resolved_workspace_path.is_dir():
            return source_status(
                source_id,
                source_type,
                "skipped_unreachable",
                0,
                0,
                "beads workspace is not available",
            )
        if shutil.which("bd") is None:
            return source_status(
                source_id,
                source_type,
                "skipped_unreachable",
                0,
                0,
                "bd command is not available",
            )
        credentials_path = resolved_workspace_path / "secrets" / "dolt_beads_password.txt"
        if not credentials_path.is_file():
            return source_status(
                source_id,
                source_type,
                "skipped_no_auth",
                0,
                0,
                "beads credentials are not available",
            )
        return None
    return source_status(
        source_id,
        source_type or "unknown",
        "skipped_unconfigured",
        0,
        0,
        "unsupported source type",
    )


def github_auth_available() -> bool:
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    try:
        completed = subprocess.run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def load_source_items(source: dict[str, Any]) -> Any:
    source_type = str(source.get("type") or "")
    if source_type == "fixture":
        return source.get("items")
    if source_type == "github_issues":
        return load_github_issues(source)
    if source_type == "beads":
        return load_beads_issues(source)
    return []


def load_github_issues(source: dict[str, Any]) -> list[dict[str, Any]]:
    repo = str(source["repo"])
    command = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        str(source.get("state") or "open"),
        "--limit",
        str(source.get("limit") or 30),
        "--json",
        "number,title,state,url,labels,body",
    ]
    for label in source.get("labels") or []:
        command.extend(["--label", str(label)])
    if source.get("query"):
        command.extend(["--search", str(source["query"])])

    issues = run_github_issue_list(command)
    if not isinstance(issues, list):
        raise SourceLoadError("failed_invalid_payload", "gh issue list returned invalid JSON payload")

    normalized = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise SourceLoadError("failed_invalid_payload", "gh issue list returned invalid issue")
        if not valid_github_issue_number(issue.get("number")):
            raise SourceLoadError("failed_invalid_payload", "gh issue list returned issue without numeric number")
        dependencies = load_github_dependencies(repo, issue.get("number"))
        normalized.append(
            normalize_github_issue(str(source.get("id") or "github"), source, issue, dependencies)
        )
    return normalized


def run_github_issue_list(command: list[str]) -> Any:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceLoadError("skipped_unreachable", "gh issue list failed") from exc
    if completed.returncode != 0:
        status, message = github_issue_list_failure_status_and_message(completed.stdout, completed.stderr)
        raise SourceLoadError(status, message)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SourceLoadError("failed_invalid_payload", "gh issue list failed") from exc


def github_issue_list_failure_status_and_message(stdout: str, stderr: str) -> tuple[str, str]:
    normalized = f"{stdout}\n{stderr}".lower()
    disabled_markers = (
        r"repository has disabled issues",
        r"issues are disabled",
        r"issues have been disabled",
    )
    if any(re.search(marker, normalized, flags=re.IGNORECASE) for marker in disabled_markers):
        return "skipped_unconfigured", "GitHub Issues are disabled for this repository"
    return "skipped_unreachable", "gh issue list failed"


def load_github_dependencies(repo: str, issue_number: Any) -> list[dict[str, str]]:
    if not valid_github_issue_number(issue_number):
        return unknown_github_dependency()
    endpoint = f"repos/{repo}/issues/{issue_number}/dependencies/blocked_by"
    try:
        dependencies = run_json_command(
            ["gh", "api", endpoint, "--paginate"],
            status_on_failure="skipped_unreachable",
            message_on_failure="gh issue dependency lookup failed",
        )
    except SourceLoadError:
        return unknown_github_dependency()
    if not isinstance(dependencies, list):
        return unknown_github_dependency()
    return [normalize_github_dependency(dependency) for dependency in dependencies]


def valid_github_issue_number(issue_number: Any) -> bool:
    return isinstance(issue_number, int) and not isinstance(issue_number, bool) and issue_number > 0


def unknown_github_dependency() -> list[dict[str, str]]:
    return [{"id": "github-dependency-lookup", "status": "unknown", "type": "blocked_by"}]


def normalize_github_dependency(dependency: Any) -> dict[str, str]:
    if not isinstance(dependency, dict):
        return {"id": "github-dependency", "status": "unknown", "type": "blocked_by"}
    state = str(dependency.get("state") or "unknown").lower()
    status = "closed" if state == "closed" else "open" if state == "open" else "unknown"
    external_id = dependency.get("html_url") or dependency.get("url") or dependency.get("number")
    return {"id": str(external_id or "github-dependency"), "status": status, "type": "blocked_by"}


def normalize_github_issue(
    source_id: str,
    source: dict[str, Any],
    issue: dict[str, Any],
    dependencies: list[dict[str, str]],
) -> dict[str, Any]:
    repo = str(source["repo"])
    labels = github_label_names(issue.get("labels") or [])
    number = issue.get("number")
    return {
        "external_id": f"{repo}#{number}",
        "url": str(issue.get("url") or ""),
        "title": str(issue.get("title") or ""),
        "status": str(issue.get("state") or "").lower(),
        "labels": labels,
        "parent": label_value(labels, "parent:"),
        "workstream": label_value(labels, "workstream:"),
        "acceptance_criteria": extract_acceptance_criteria(issue.get("body")),
        "dependencies": dependencies,
        "blockers": [],
        "afk": github_afk_metadata(source, labels),
        "raw": {"github": {"repo": repo, "number": number}},
    }


def github_label_names(labels: list[Any]) -> list[str]:
    names = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if name:
            names.append(str(name))
    return names


def github_afk_metadata(source: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"ready": ready_from_labels(source, labels)}
    active_run_id = label_value(labels, "afk:active-run:")
    if active_run_id:
        metadata["active_run_id"] = active_run_id
    return metadata


def load_beads_issues(source: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        workspace = Path(str(source["workspace"])).resolve(strict=True)
    except OSError as exc:
        raise SourceLoadError("skipped_unreachable", "beads workspace is not available") from exc
    credentials_path = workspace / "secrets" / "dolt_beads_password.txt"
    password = read_beads_password(credentials_path)
    env = os.environ.copy()
    env["BEADS_DOLT_PASSWORD"] = password

    target_ids = source.get("target_ids")
    if target_ids:
        normalized = []
        for issue_id in target_ids:
            requested_id = str(issue_id)
            try:
                issue = load_beads_issue(requested_id, workspace=workspace, env=env, missing_ok=True)
            except SourceLoadError as exc:
                if exc.status != "skipped_missing_target":
                    raise
                normalized.append(missing_beads_target(requested_id))
                continue
            normalized.append(normalize_beads_issue(str(source.get("id") or "beads"), source, issue))
        return normalized

    tracker_records_by_external_id = latest_tracker_records_by_source_item(source)
    target_pr_records_by_branch = open_target_pr_records_by_branch(source)
    command = [
        "bd",
        "list",
        "--json",
        "--no-pager",
        "--status",
        str(source.get("status") or "open"),
        "--limit",
        str(source.get("limit") or 30),
    ]
    for label in source.get("labels") or []:
        command.extend(["--label", str(label)])

    issues = run_json_command(
        command,
        cwd=workspace,
        env=env,
        status_on_failure="skipped_unreachable",
        message_on_failure="bd list failed",
    )
    if not isinstance(issues, list):
        raise SourceLoadError("failed_invalid_payload", "bd list returned invalid JSON payload")

    normalized = []
    for issue_summary in issues:
        issue_id = issue_summary.get("id") if isinstance(issue_summary, dict) else None
        if not is_non_blank_string(issue_id):
            raise SourceLoadError("failed_invalid_payload", "bd list returned issue without id")
        issue = load_beads_issue(issue_id.strip(), workspace=workspace, env=env)
        normalized.append(
            normalize_beads_issue(
                str(source.get("id") or "beads"),
                source,
                issue,
                tracker_records_by_external_id=tracker_records_by_external_id,
                target_pr_records_by_branch=target_pr_records_by_branch,
            )
        )
    return normalized


def read_beads_password(credentials_path: Path) -> str:
    try:
        lines = credentials_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SourceLoadError("skipped_no_auth", "beads credentials are not available") from exc
    if not lines or not lines[0]:
        raise SourceLoadError("skipped_no_auth", "beads credentials are not available")
    return lines[0]


def load_beads_issue(
    issue_id: str,
    *,
    workspace: Path,
    env: dict[str, str],
    missing_ok: bool = False,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["bd", "show", issue_id, "--json"],
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceLoadError("skipped_unreachable", "bd show failed") from exc
    if completed.returncode != 0:
        if missing_ok and beads_show_missing_issue(issue_id, completed.stdout, completed.stderr):
            raise SourceLoadError("skipped_missing_target", f"requested Beads issue was not found: {issue_id}")
        raise SourceLoadError("skipped_unreachable", "bd show failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SourceLoadError("failed_invalid_payload", "bd show failed") from exc
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise SourceLoadError("failed_invalid_payload", "bd show returned invalid JSON payload")


def beads_show_missing_issue(issue_id: str, stdout: str, stderr: str) -> bool:
    output = f"{stdout}\n{stderr}".lower()
    normalized_issue_id = issue_id.lower()
    return normalized_issue_id in output and any(
        marker in output
        for marker in (
            "not found",
            "no issue",
            "does not exist",
            "unknown issue",
        )
    )


def missing_beads_target(issue_id: str) -> dict[str, Any]:
    return {
        "external_id": issue_id,
        "url": "",
        "title": "Requested Beads issue was not found",
        "status": "open",
        "labels": [],
        "parent": None,
        "workstream": None,
        "acceptance_criteria": [],
        "dependencies": [],
        "blockers": [],
        "afk": {},
        "raw": {"beads": {"id": issue_id}, "missing": True},
        "selection_skip_reason": "missing_target_id",
    }


def normalize_beads_issue(
    source_id: str,
    source: dict[str, Any],
    issue: dict[str, Any],
    *,
    tracker_records_by_external_id: dict[str, dict[str, str]] | None = None,
    target_pr_records_by_branch: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    issue_id = issue.get("id")
    if not is_non_blank_string(issue_id):
        raise SourceLoadError("failed_invalid_payload", "bd show returned issue without stable id")
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    labels = [str(label) for label in issue.get("labels") or []]
    normalized: dict[str, Any] = {
        "source_id": source_id,
        "source_type": "beads",
        "external_id": issue_id.strip(),
        "url": beads_issue_url(source, issue),
        "title": str(issue.get("title") or ""),
        "status": str(issue.get("status") or ""),
        "labels": labels,
        "parent": issue.get("parent"),
        "workstream": metadata.get("workstream") or label_value(labels, "workstream:"),
        "acceptance_criteria": extract_beads_acceptance_criteria(issue),
        "dependencies": beads_dependencies(issue.get("dependencies") or []),
        "blockers": [],
        "afk": beads_afk_metadata(source, metadata, labels),
        "raw": {"beads": {"id": issue_id.strip()}},
    }
    description = bounded_issue_description(issue)
    if description:
        normalized["description"] = description
    priority = issue.get("priority")
    if priority is not None:
        normalized["priority"] = priority
    issue_type = issue.get("issue_type")
    if issue_type:
        normalized["issue_type"] = str(issue_type)
    if not source.get("target_ids"):
        selection_skip_reason = open_afk_pr_skip_reason(
            normalized["external_id"],
            normalized.get("workstream"),
            tracker_records_by_external_id=tracker_records_by_external_id or {},
            target_pr_records_by_branch=target_pr_records_by_branch or {},
        )
        if selection_skip_reason:
            normalized["selection_skip_reason"] = selection_skip_reason
    return normalized


def beads_issue_url(source: dict[str, Any], issue: dict[str, Any]) -> str:
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    if metadata.get("url"):
        return str(metadata["url"])
    template = source.get("web_url_template")
    if template:
        return str(template).replace("{id}", str(issue.get("id") or ""))
    return ""


def beads_dependencies(dependencies: list[Any]) -> list[dict[str, str]]:
    normalized = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            normalized.append(
                {"id": "beads-dependency", "status": "unknown", "type": "blocks"}
            )
            continue
        dependency_type = dependency.get("dependency_type") or dependency.get("type")
        if dependency_type == "parent-child":
            continue
        dependency_id = dependency.get("id") or dependency.get("depends_on_id")
        normalized.append(
            {
                "id": str(dependency_id or ""),
                "status": normalize_relation_status(dependency.get("status")),
                "type": str(dependency_type or "blocks"),
            }
        )
    return normalized


def beads_afk_metadata(source: dict[str, Any], metadata: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    ready = metadata.get("afk.ready", metadata.get("afk_ready", ready_from_labels(source, labels)))
    afk: dict[str, Any] = {"ready": metadata_bool(ready)}
    active_run_id = metadata.get("active_run_id") or metadata.get("afk_active_run_id")
    if active_run_id:
        afk["active_run_id"] = str(active_run_id)
    return afk


def ready_from_labels(source: dict[str, Any], labels: list[str]) -> bool:
    label_set = set(labels)
    ready_labels = configured_ready_labels(source)
    ready_labels.add("afk:ready")
    return any(label in label_set for label in ready_labels)


def configured_ready_labels(source: dict[str, Any]) -> set[str]:
    configured: set[str] = set()
    ready_label = source.get("ready_label")
    if is_non_blank_string(ready_label):
        configured.add(str(ready_label).strip())
    ready_labels = source.get("ready_labels")
    if isinstance(ready_labels, list):
        for label in ready_labels:
            if is_non_blank_string(label):
                configured.add(str(label).strip())
    if configured:
        return configured
    for label in source.get("labels") or []:
        if not is_non_blank_string(label):
            continue
        normalized = str(label).strip()
        if "ready" in normalized.lower():
            configured.add(normalized)
    return configured


def open_afk_pr_skip_reason(
    external_id: str,
    workstream_id: Any,
    *,
    tracker_records_by_external_id: dict[str, dict[str, str]],
    target_pr_records_by_branch: dict[str, dict[str, str]],
) -> str | None:
    tracker_record = latest_open_tracker_record(
        external_id,
        tracker_records_by_external_id=tracker_records_by_external_id,
    )
    if tracker_record is None:
        tracker_record = latest_open_target_pr_record(
            external_id,
            workstream_id=workstream_id,
            target_pr_records_by_branch=target_pr_records_by_branch,
        )
    if tracker_record is None:
        return None
    details = []
    workstream_id = tracker_record.get("workstream_id")
    if is_non_blank_string(workstream_id):
        details.append(f"workstream={workstream_id.strip()}")
    pr_url = tracker_record.get("pr_url")
    if is_non_blank_string(pr_url):
        details.append(f"pr_url={pr_url.strip()}")
    if not details:
        return "open_afk_pr_exists"
    return f"open_afk_pr_exists:{','.join(details)}"


def latest_open_tracker_record(
    external_id: str,
    *,
    tracker_records_by_external_id: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    record = tracker_records_by_external_id.get(external_id)
    if record is None:
        return None
    if record.get("status") not in OPEN_TRACKER_STATUSES:
        return None
    return record


def latest_open_target_pr_record(
    external_id: str,
    *,
    workstream_id: Any,
    target_pr_records_by_branch: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    workstream = str(workstream_id or external_id).strip()
    if not workstream:
        return None
    record = target_pr_records_by_branch.get(review_branch_for_workstream(workstream))
    if record is None:
        return None
    normalized = dict(record)
    normalized["workstream_id"] = workstream
    return normalized


def open_target_pr_records_by_branch(source: dict[str, Any]) -> dict[str, dict[str, str]]:
    repo = str(source.get("target_repo") or "").strip()
    if not repo or shutil.which("gh") is None or not github_auth_available():
        return {}
    try:
        payload = run_json_command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "url,headRefName",
            ],
            status_on_failure="skipped_unreachable",
            message_on_failure="gh pr list failed",
        )
    except SourceLoadError as exc:
        if exc.status == "failed_invalid_payload":
            raise
        return {}
    if not isinstance(payload, list):
        raise SourceLoadError("failed_invalid_payload", "gh pr list returned invalid JSON payload")
    records: dict[str, dict[str, str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise SourceLoadError("failed_invalid_payload", "gh pr list returned invalid PR")
        head_ref_name = str(item.get("headRefName") or "").strip()
        if not head_ref_name.startswith("afk/"):
            continue
        records[head_ref_name] = {
            "status": "awaiting-review",
            "pr_url": str(item.get("url") or "").strip(),
        }
    return records


def latest_tracker_records_by_source_item(source: dict[str, Any]) -> dict[str, dict[str, str]]:
    records_by_external_id: dict[str, dict[str, str]] = {}
    for root in tracker_artifact_roots(source):
        for tracker_path in sorted(root.glob("ledger*/workstreams/*/tracker-result.json")):
            record = tracker_record_for_source_item(tracker_path)
            if record is None:
                continue
            external_id = record["source_item_external_id"]
            existing = records_by_external_id.get(external_id)
            if existing is None or tracker_record_sort_key(existing) < tracker_record_sort_key(record):
                records_by_external_id[external_id] = record
    return records_by_external_id


def tracker_artifact_roots(source: dict[str, Any]) -> list[Path]:
    value = source.get("tracker_artifact_roots")
    if not isinstance(value, list):
        return []
    roots: list[Path] = []
    seen: set[str] = set()
    for item in value:
        if not is_non_blank_string(item):
            continue
        try:
            root = Path(item.strip()).resolve(strict=True)
        except OSError:
            continue
        if not root.is_dir():
            continue
        root_key = str(root)
        if root_key in seen:
            continue
        seen.add(root_key)
        roots.append(root)
    return roots


def tracker_record_for_source_item(tracker_path: Path) -> dict[str, str] | None:
    try:
        payload = json.loads(tracker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    external_id = str(payload.get("source_item_external_id") or "").strip()
    if not external_id:
        return None
    return {
        "source_item_external_id": external_id,
        "status": str(payload.get("status") or "").strip(),
        "run_id": tracker_path.parent.name,
        "tracker_path": str(tracker_path),
        "workstream_id": tracker_workstream_id(tracker_path.parent),
        "pr_url": str(payload.get("pr_url") or "").strip(),
    }


def tracker_record_sort_key(record: dict[str, str]) -> tuple[str, str]:
    return (record["run_id"], record["tracker_path"])


def tracker_workstream_id(workstream_dir: Path) -> str:
    workstream_path = workstream_dir / "workstream-result.json"
    try:
        payload = json.loads(workstream_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("workstream_id") or "").strip()


def metadata_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "off", ""}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def run_json_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    status_on_failure: str,
    message_on_failure: str,
    timeout_seconds: float = 60,
) -> Any:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceLoadError(status_on_failure, message_on_failure) from exc
    if completed.returncode != 0:
        raise SourceLoadError(status_on_failure, message_on_failure)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SourceLoadError("failed_invalid_payload", message_on_failure) from exc


def extract_acceptance_criteria(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    if not isinstance(value, str):
        return []
    section_criteria = extract_acceptance_criteria_from_markdown_section(value)
    if section_criteria is not None:
        return section_criteria
    criteria = []
    for line in value.splitlines():
        stripped = line.strip()
        criterion = strip_acceptance_marker(stripped)
        if criterion:
            criteria.append(criterion)
    return criteria


def extract_acceptance_criteria_from_markdown_section(value: str) -> list[str] | None:
    lines = value.splitlines()
    in_section = False
    criteria: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_section:
            if is_acceptance_criteria_heading(stripped):
                in_section = True
            continue
        if is_markdown_heading(stripped) and not is_acceptance_criteria_heading(stripped):
            break
        if not stripped:
            continue
        criterion = strip_acceptance_marker(stripped) or stripped
        criteria.append(criterion)
    return criteria if in_section else None


def strip_acceptance_marker(value: str) -> str:
    lowered = value.lower()
    for prefix in ("- [ ] ", "- [x] ", "* [ ] ", "* [x] ", "- ", "* "):
        if lowered.startswith(prefix):
            return value[len(prefix) :].strip()
    numbered = re.match(r"^\d+[.)]\s+(?P<criterion>.+)$", value)
    if numbered:
        return numbered.group("criterion").strip()
    return ""


def is_acceptance_criteria_heading(value: str) -> bool:
    normalized = value.lower().lstrip("#").strip()
    return normalized in {"acceptance criteria", "acceptance criteria:"}


def is_markdown_heading(value: str) -> bool:
    return value.startswith("#")


def bounded_issue_description(issue: dict[str, Any], *, max_chars: int = 240) -> str:
    raw_value = issue.get("description")
    if not isinstance(raw_value, str) or not raw_value.strip():
        raw_value = issue.get("body")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return ""
    text = raw_value.strip()
    section_index = find_acceptance_criteria_section_index(text)
    if section_index is not None:
        text = text[:section_index].rstrip()
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    if not paragraphs:
        return ""
    excerpt = "\n\n".join(paragraphs[:2]).strip()
    if len(excerpt) <= max_chars:
        return excerpt
    return excerpt[: max_chars - 1].rstrip() + "…"


def find_acceptance_criteria_section_index(value: str) -> int | None:
    offset = 0
    for line in value.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#") and is_acceptance_criteria_heading(stripped):
            return offset
        offset += len(line)
    return None


def extract_beads_acceptance_criteria(issue: dict[str, Any]) -> list[str]:
    criteria = extract_acceptance_criteria(issue.get("acceptance_criteria"))
    if criteria:
        return criteria
    criteria = extract_plain_acceptance_criteria_field(issue.get("acceptance_criteria"))
    if criteria:
        return criteria
    return extract_acceptance_criteria(issue.get("description") or issue.get("body"))


def extract_plain_acceptance_criteria_field(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    normalized = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def label_value(labels: list[str], prefix: str) -> str | None:
    for label in labels:
        if label.startswith(prefix):
            return label[len(prefix) :]
    return None


def selected_message(selected_count: int) -> str:
    noun = "candidate" if selected_count == 1 else "candidates"
    return f"selected {selected_count} {noun}"


def default_required_labels(project_contract: Any) -> list[str]:
    if project_contract is None:
        return []
    return list(project_contract.beads_labels)


def candidate_payload_error(raw_item: Any) -> str | None:
    if not isinstance(raw_item, dict):
        return "invalid_candidate_payload"
    for identity_key in ("external_id", "id", "url"):
        if identity_key in raw_item and raw_item[identity_key] is not None and not isinstance(raw_item[identity_key], str):
            return "invalid_candidate_payload"
    if "labels" in raw_item and not isinstance(raw_item["labels"], list):
        return "invalid_candidate_payload"
    if "labels" in raw_item and any(not isinstance(label, str) for label in raw_item["labels"]):
        return "invalid_candidate_payload"
    if "dependencies" in raw_item and not isinstance(raw_item["dependencies"], list):
        return "invalid_candidate_payload"
    if "blockers" in raw_item and not isinstance(raw_item["blockers"], list):
        return "invalid_candidate_payload"
    if "acceptance_criteria" in raw_item and not isinstance(
        raw_item["acceptance_criteria"],
        (list, str),
    ):
        return "invalid_candidate_payload"
    if "afk" in raw_item and not isinstance(raw_item["afk"], dict):
        return "invalid_candidate_payload"
    if "raw" in raw_item and not isinstance(raw_item["raw"], dict):
        return "invalid_candidate_payload"
    return None


def invalid_candidate(source_id: str, source_type: str, raw_item: Any) -> dict[str, Any]:
    item = raw_item if isinstance(raw_item, dict) else {}
    return {
        "source_id": source_id,
        "source_type": source_type,
        "external_id": identity_value(item, "external_id") or identity_value(item, "id") or "",
        "url": identity_value(item, "url") or "",
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or "").lower(),
        "labels": [],
        "parent": item.get("parent"),
        "workstream": item.get("workstream"),
        "acceptance_criteria": [],
        "dependencies": [],
        "blockers": [],
        "dependency_status": "unknown",
        "afk": {},
        "raw": {},
    }


def normalize_candidate(source_id: str, source_type: str, raw_item: Any) -> dict[str, Any]:
    item = raw_item if isinstance(raw_item, dict) else {}
    dependencies = normalize_relations(item.get("dependencies") or [])
    blockers = normalize_relations(item.get("blockers") or [])
    candidate: dict[str, Any] = {
        "source_id": source_id,
        "source_type": source_type,
        "external_id": identity_value(item, "external_id") or identity_value(item, "id") or "",
        "url": identity_value(item, "url") or "",
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or "").lower(),
        "labels": list(item.get("labels") or []),
        "parent": item.get("parent"),
        "workstream": item.get("workstream"),
        "acceptance_criteria": extract_acceptance_criteria(item.get("acceptance_criteria")),
        "dependencies": dependencies,
        "blockers": blockers,
        "dependency_status": dependency_status(dependencies, blockers),
        "afk": dict(item.get("afk") or {}),
        "raw": dict(item.get("raw") or {}),
    }
    if "description" in item and isinstance(item["description"], str) and item["description"].strip():
        candidate["description"] = item["description"].strip()
    if "priority" in item and item["priority"] is not None:
        candidate["priority"] = item["priority"]
    if "issue_type" in item and item["issue_type"]:
        candidate["issue_type"] = str(item["issue_type"])
    return candidate


def identity_value(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if not is_non_blank_string(value):
        return None
    return value.strip()


def is_non_blank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def dependency_status(dependencies: list[Any], blockers: list[Any]) -> str:
    for relation in [*dependencies, *blockers]:
        if not isinstance(relation, dict):
            return "unknown"
        status = normalize_relation_status(relation.get("status"))
        if status == "unknown":
            return "unknown"
        if status != "closed":
            return "blocked"
    return "clear"


def normalize_relations(relations: Any) -> list[Any]:
    normalized = []
    for relation in relations:
        if not isinstance(relation, dict):
            normalized.append(relation)
            continue
        relation_copy = dict(relation)
        relation_copy["status"] = normalize_relation_status(relation_copy.get("status"))
        normalized.append(relation_copy)
    return normalized


def normalize_relation_status(value: Any) -> str:
    status = str(value or "unknown").strip().lower()
    if status in {"closed", "resolved", "done"}:
        return "closed"
    if status in {"open", "blocked", "in_progress", "in-progress"}:
        return "open"
    return "unknown"


def rejection_reason(
    candidate: dict[str, Any],
    required_labels: list[str],
    required_metadata: list[str],
    allowed_statuses: set[str],
    target_ids: set[str],
    exclude_ids: set[str],
) -> str | None:
    if candidate["status"] not in allowed_statuses:
        return "status_not_allowed"
    if not candidate["external_id"] and not candidate["url"]:
        return "missing_identity"
    if target_ids and candidate["external_id"] not in target_ids:
        return "target_id_mismatch"
    if candidate["external_id"] in exclude_ids or duplicate_key(candidate) in exclude_ids:
        return "attempted_in_run"
    missing_labels = sorted(set(required_labels) - set(candidate["labels"]))
    if missing_labels:
        return f"missing_labels:{','.join(missing_labels)}"
    if candidate["dependency_status"] == "blocked":
        return "blocked"
    if candidate["dependency_status"] == "unknown":
        return "dependency_status_unknown"
    if candidate["afk"].get("active_run_id"):
        return "active_run_exists"
    missing_metadata = [
        metadata_key
        for metadata_key in required_metadata
        if not has_metadata(candidate, metadata_key)
    ]
    if missing_metadata:
        return f"missing_metadata:{','.join(missing_metadata)}"
    return None


def has_metadata(candidate: dict[str, Any], metadata_key: str) -> bool:
    if metadata_key == "acceptance_criteria":
        return bool(candidate["acceptance_criteria"])
    if metadata_key.startswith("afk."):
        value = candidate["afk"]
        for part in metadata_key.split(".")[1:]:
            if not isinstance(value, dict) or part not in value:
                return False
            value = value[part]
        return bool(value)
    return bool(candidate.get(metadata_key))


def duplicate_key(candidate: dict[str, Any]) -> str:
    if candidate["url"]:
        return candidate["url"]
    return f"{candidate['source_type']}:{candidate['external_id']}"


def source_status(
    source_id: str,
    source_type: str,
    status: str,
    candidate_count: int,
    selected_count: int,
    message: str,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_type": source_type,
        "status": status,
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "message": message,
    }
