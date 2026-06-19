from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class SourceLoadError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def select_work_step(context: Any) -> dict[str, Any]:
    return select_work(context.input_data, project_contract=context.project_contract)


def select_work(input_data: Any, *, project_contract: Any = None) -> dict[str, Any]:
    request = input_data if isinstance(input_data, dict) else {}
    request_error = request_payload_error(request)
    if request_error is not None:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_statuses": [
                source_status(
                    "request",
                    "request",
                    "failed_invalid_payload",
                    0,
                    0,
                    request_error,
                )
            ],
            "selected_work": [],
            "skipped_candidates": [],
        }

    required_labels = (
        list(request["required_labels"])
        if "required_labels" in request
        else default_required_labels(project_contract)
    )
    required_metadata = list(request.get("required_metadata", []))
    allowed_statuses = set(request.get("allowed_statuses", ["open"]))

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

        try:
            raw_items = load_source_items(source)
        except SourceLoadError as exc:
            source_statuses.append(
                source_status(source_id, source_type, exc.status, 0, 0, exc.message)
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
            rejection = rejection_reason(candidate, required_labels, required_metadata, allowed_statuses)
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

    return {
        "schema_version": SCHEMA_VERSION,
        "source_statuses": source_statuses,
        "selected_work": selected_work,
        "skipped_candidates": skipped_candidates,
    }


def request_payload_error(request: dict[str, Any]) -> str | None:
    for key in ("required_labels", "required_metadata", "allowed_statuses"):
        if key in request and not is_string_list(request[key]):
            return f"{key} must be a list of strings"
    if "sources" in request and not isinstance(request["sources"], list):
        return "sources must be a list"
    return None


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

    issues = run_json_command(
        command,
        status_on_failure="skipped_unreachable",
        message_on_failure="gh issue list failed",
    )
    if not isinstance(issues, list):
        raise SourceLoadError("failed_invalid_payload", "gh issue list returned invalid JSON payload")

    normalized = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise SourceLoadError("failed_invalid_payload", "gh issue list returned invalid issue")
        dependencies = load_github_dependencies(repo, issue.get("number"))
        normalized.append(
            normalize_github_issue(str(source.get("id") or "github"), source, issue, dependencies)
        )
    return normalized


def load_github_dependencies(repo: str, issue_number: Any) -> list[dict[str, str]]:
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
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
        "afk": github_afk_metadata(labels),
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


def github_afk_metadata(labels: list[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"ready": "afk:ready" in labels}
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
        if not issue_id:
            raise SourceLoadError("failed_invalid_payload", "bd list returned issue without id")
        issue = load_beads_issue(str(issue_id), workspace=workspace, env=env)
        normalized.append(normalize_beads_issue(str(source.get("id") or "beads"), source, issue))
    return normalized


def read_beads_password(credentials_path: Path) -> str:
    try:
        lines = credentials_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SourceLoadError("skipped_no_auth", "beads credentials are not available") from exc
    if not lines or not lines[0]:
        raise SourceLoadError("skipped_no_auth", "beads credentials are not available")
    return lines[0]


def load_beads_issue(issue_id: str, *, workspace: Path, env: dict[str, str]) -> dict[str, Any]:
    payload = run_json_command(
        ["bd", "show", issue_id, "--json"],
        cwd=workspace,
        env=env,
        status_on_failure="skipped_unreachable",
        message_on_failure="bd show failed",
    )
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise SourceLoadError("failed_invalid_payload", "bd show returned invalid JSON payload")


def normalize_beads_issue(source_id: str, source: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    labels = [str(label) for label in issue.get("labels") or []]
    return {
        "external_id": str(issue.get("id") or ""),
        "url": beads_issue_url(source, issue),
        "title": str(issue.get("title") or ""),
        "status": str(issue.get("status") or ""),
        "labels": labels,
        "parent": issue.get("parent"),
        "workstream": metadata.get("workstream") or label_value(labels, "workstream:"),
        "acceptance_criteria": extract_acceptance_criteria(issue.get("acceptance_criteria")),
        "dependencies": beads_dependencies(issue.get("dependencies") or []),
        "blockers": [],
        "afk": beads_afk_metadata(metadata, labels),
        "raw": {"beads": {"id": issue.get("id")}},
    }


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
                "status": str(dependency.get("status") or "unknown"),
                "type": str(dependency_type or "blocks"),
            }
        )
    return normalized


def beads_afk_metadata(metadata: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    ready = metadata.get("afk.ready", metadata.get("afk_ready", "afk:ready" in labels))
    afk: dict[str, Any] = {"ready": metadata_bool(ready)}
    active_run_id = metadata.get("active_run_id") or metadata.get("afk_active_run_id")
    if active_run_id:
        afk["active_run_id"] = str(active_run_id)
    return afk


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
    criteria = []
    for line in value.splitlines():
        stripped = line.strip()
        for prefix in ("- [ ] ", "- [x] ", "- "):
            if stripped.lower().startswith(prefix):
                criteria.append(stripped[len(prefix) :].strip())
                break
    return criteria


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
        "external_id": str(item.get("external_id") or item.get("id") or ""),
        "url": str(item.get("url") or ""),
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
    dependencies = list(item.get("dependencies") or [])
    blockers = list(item.get("blockers") or [])
    return {
        "source_id": source_id,
        "source_type": source_type,
        "external_id": str(item.get("external_id") or item.get("id") or ""),
        "url": str(item.get("url") or ""),
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


def dependency_status(dependencies: list[Any], blockers: list[Any]) -> str:
    for relation in [*dependencies, *blockers]:
        if not isinstance(relation, dict):
            return "unknown"
        status = relation.get("status")
        if status in {None, "", "unknown"}:
            return "unknown"
        if status != "closed":
            return "blocked"
    return "clear"


def rejection_reason(
    candidate: dict[str, Any],
    required_labels: list[str],
    required_metadata: list[str],
    allowed_statuses: set[str],
) -> str | None:
    if candidate["status"] not in allowed_statuses:
        return "status_not_allowed"
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
