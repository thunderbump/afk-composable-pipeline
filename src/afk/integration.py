from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from afk.jsonutil import canonical_json
from afk.publication import PublisherError, publisher_auth_artifact, run_publisher_command, validate_publisher_auth_config
from afk.redaction import redact_artifact_value, redact_text
from afk.schema_helpers import string_field


SCHEMA_VERSION = 1
DEFAULT_POLL_SECONDS = 300
PENDING_CHECK_STATES = {"PENDING", "PENDING_DEPLOYMENT", "IN_PROGRESS", "QUEUED", "REQUESTED", "WAITING"}
FAILED_CHECK_STATES = {"FAILURE", "FAILED", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
INCONCLUSIVE_CHECK_STATES = {"EXPECTED", "STALE", "NEUTRAL", "SKIPPED", "STARTUP_FAILURE"}
PASSED_CHECK_STATES = {"SUCCESS", "PASS", "PASSED"}
BLOCKED_MERGE_STATES = {"BLOCKED", "DIRTY", "UNKNOWN", "UNSTABLE", "BEHIND", "DRAFT"}


def integrate_published_pr(
    published_result_path: str | Path,
    *,
    policy: Any,
    gh_auth_config_dir: str | Path,
) -> dict[str, Any]:
    request = normalize_request(
        published_result_path,
        policy=policy,
        gh_auth_config_dir=gh_auth_config_dir,
    )
    auth_artifact = publisher_auth_artifact(request["auth"])
    view_command = [
        request["gh_path"],
        "pr",
        "view",
        str(request["pr_number"]),
        "--repo",
        request["repo"],
        "--json",
        "number,url,state,isDraft,mergeStateStatus,headRefOid,statusCheckRollup",
    ]
    checks_command = [
        request["gh_path"],
        "pr",
        "checks",
        str(request["pr_number"]),
        "--repo",
        request["repo"],
        "--json",
        "name,state,workflow,link,bucket",
    ]

    run_publisher_command(
        [request["gh_path"], "auth", "status", "--hostname", "github.com"],
        cwd=request["command_cwd"],
        tool="gh",
        auth=request["auth"],
        message_on_failure="gh auth status failed",
    )
    view_payload = load_json_command(
        view_command,
        cwd=request["command_cwd"],
        auth=request["auth"],
        failure_message="gh pr view returned invalid JSON payload",
    )
    observed_head = string_field(view_payload, "headRefOid") or ""
    merge_state_status = (string_field(view_payload, "mergeStateStatus") or "").upper()
    pr_state = (string_field(view_payload, "state") or "").upper()
    is_draft = bool(view_payload.get("isDraft"))
    check_snapshots = normalize_status_check_rollup(view_payload.get("statusCheckRollup"))
    checks_by_name = {item["name"] for item in check_snapshots if item["name"]}
    needs_pr_checks = not check_snapshots or any(name not in checks_by_name for name in request["required_checks"])
    if needs_pr_checks:
        try:
            checks_payload = load_json_command(
                checks_command,
                cwd=request["command_cwd"],
                auth=request["auth"],
                failure_message="gh pr checks returned invalid JSON payload",
            )
        except (PublisherError, ValueError):
            if not check_snapshots:
                raise
        else:
            checks_snapshots = normalize_check_snapshots(checks_payload)
            if checks_snapshots:
                check_snapshots = checks_snapshots
    decision, next_poll_seconds, remediation = classify_integration(
        expected_head=request["expected_head_sha"],
        observed_head=observed_head,
        pr_state=pr_state,
        is_draft=is_draft,
        merge_state_status=merge_state_status,
        check_snapshots=check_snapshots,
        required_checks=request["required_checks"],
        poll_seconds=request["poll_seconds"],
    )

    result = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "integration-result",
        "status": "classified",
        "published_result_path": str(request["published_result_path"]),
        "repo": request["repo"],
        "pr_number": request["pr_number"],
        "pr_url": redact_text(string_field(view_payload, "url") or request["pr_url"]),
        "expected_head_sha": request["expected_head_sha"],
        "observed_head_sha": observed_head,
        "pr_state": pr_state,
        "is_draft": is_draft,
        "merge_state_status": merge_state_status,
        "check_snapshots": check_snapshots,
        "decision": decision,
        "next_poll_seconds": next_poll_seconds,
        "remediation": remediation,
        "auth": auth_artifact,
        "commands": {
            "gh_view": redact_artifact_value(view_command),
            "gh_checks": redact_artifact_value(checks_command),
        },
    }
    write_json(request["output_dir"] / "integration-result.json", result)
    write_events(
        request["output_dir"] / "integration-events.jsonl",
        [
            {
                "schema_version": SCHEMA_VERSION,
                "event": "integration.classified",
                "repo": request["repo"],
                "pr_number": request["pr_number"],
                "expected_head_sha": request["expected_head_sha"],
                "observed_head_sha": observed_head,
                "decision": decision,
                "next_poll_seconds": next_poll_seconds,
                "remediation": remediation,
            }
        ],
    )
    return result


def normalize_request(
    published_result_path: str | Path,
    *,
    policy: Any,
    gh_auth_config_dir: str | Path,
) -> dict[str, Any]:
    published_path = Path(published_result_path).resolve(strict=True)
    if published_path.name not in {"workstream-result.json", "publication-result.json"}:
        raise ValueError("published_result_path must point to workstream-result.json or publication-result.json")
    if not isinstance(policy, dict):
        raise ValueError("policy must be an object")

    workstream_dir = published_path.parent
    publication = load_publication_payload(published_path)
    workstream = load_workstream_payload(published_path)
    output_dir = integration_output_dir(published_path)

    repo = publication_repo(publication, workstream)
    pr_number = publication_pr_number(publication, workstream)
    pr_url = publication_pr_url(publication, workstream)
    expected_head_sha = publication_expected_head(publication, workstream)
    required_checks = normalize_required_checks(policy)
    if not repo:
        raise ValueError("could not determine repo from published artifact")
    if not pr_number:
        raise ValueError("could not determine PR number from published artifact")
    if not expected_head_sha:
        raise ValueError("could not determine expected head SHA from published artifact")

    auth = validate_publisher_auth_config(
        {
            "configured": True,
            "source": "gh_config_dir",
            "config_dir": str(Path(gh_auth_config_dir)),
        },
        checkout_path=workstream_dir,
    )
    gh = policy.get("gh", {})
    if not isinstance(gh, dict):
        raise ValueError("policy.gh must be an object when present")
    poll_seconds = policy.get("poll_seconds", policy.get("poll_interval_seconds", DEFAULT_POLL_SECONDS))
    if not isinstance(poll_seconds, int) or poll_seconds < 0:
        raise ValueError("policy.poll_seconds must be a non-negative integer")

    return {
        "published_result_path": published_path,
        "workstream_dir": workstream_dir,
        "command_cwd": workstream_dir,
        "output_dir": output_dir,
        "repo": repo,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "expected_head_sha": expected_head_sha,
        "required_checks": required_checks,
        "auth": auth,
        "gh_path": string_field(gh, "path") or "gh",
        "poll_seconds": poll_seconds,
    }


def integration_output_dir(published_path: str | Path) -> Path:
    published_path = Path(published_path)
    output_dir = published_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def load_publication_payload(published_path: Path) -> dict[str, Any]:
    if published_path.name == "publication-result.json":
        payload = read_json_file(published_path)
        if not isinstance(payload, dict):
            raise ValueError("publication-result.json must contain an object")
        return payload
    workstream = read_json_file(published_path)
    if not isinstance(workstream, dict):
        raise ValueError("workstream-result.json must contain an object")
    publication = workstream.get("publication")
    if not isinstance(publication, dict):
        raise ValueError("workstream-result.json must contain publication evidence")
    return publication


def load_workstream_payload(published_path: Path) -> dict[str, Any]:
    if published_path.name == "workstream-result.json":
        payload = read_json_file(published_path)
        if not isinstance(payload, dict):
            raise ValueError("workstream-result.json must contain an object")
        return payload
    sibling = published_path.with_name("workstream-result.json")
    if sibling.is_file():
        payload = read_json_file(sibling)
        if isinstance(payload, dict):
            return payload
    return {}


def publication_repo(publication: dict[str, Any], workstream: dict[str, Any]) -> str:
    repo = string_field(publication, "repo")
    if repo:
        return repo
    nested = workstream.get("publication")
    if isinstance(nested, dict):
        repo = string_field(nested, "repo")
        if repo:
            return repo
    parsed = parse_github_pr_url(publication_pr_url(publication, workstream))
    return parsed["repo"]


def publication_pr_number(publication: dict[str, Any], workstream: dict[str, Any]) -> int:
    for source in (
        publication,
        workstream.get("publication", {}) if isinstance(workstream.get("publication"), dict) else {},
    ):
        number = source.get("pr_number")
        if isinstance(number, int) and number > 0:
            return number
        number_text = string_field(source, "pr")
        if number_text and number_text.isdigit():
            return int(number_text)
    parsed = parse_github_pr_url(publication_pr_url(publication, workstream))
    return parsed["pr_number"]


def publication_pr_url(publication: dict[str, Any], workstream: dict[str, Any]) -> str:
    url = string_field(publication, "url")
    if url:
        return url
    nested = workstream.get("publication")
    if isinstance(nested, dict):
        url = string_field(nested, "url")
        if url:
            return url
    return ""


def publication_expected_head(publication: dict[str, Any], workstream: dict[str, Any]) -> str:
    for source in (
        publication,
        workstream.get("publication", {}) if isinstance(workstream.get("publication"), dict) else {},
    ):
        head = string_field(source, "expected_head_sha")
        if head:
            return head
    steps = workstream.get("steps")
    if not isinstance(steps, list):
        return ""
    for step in steps:
        if not isinstance(step, dict) or string_field(step, "name") != "implement":
            continue
        output = step.get("output")
        if not isinstance(output, dict):
            continue
        git_info = output.get("git")
        if isinstance(git_info, dict):
            head = string_field(git_info, "after_commit")
            if head:
                return head
    return ""


def parse_github_pr_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[2] == "pull" and parts[3].isdigit():
        return {"repo": f"{parts[0]}/{parts[1]}", "pr_number": int(parts[3])}
    return {"repo": "", "pr_number": 0}


def normalize_required_checks(policy: dict[str, Any]) -> list[str]:
    value = policy.get("required_checks", policy.get("requiredChecks", []))
    if not isinstance(value, list):
        raise ValueError("policy.required_checks must be a list")
    return [item for item in value if isinstance(item, str) and item]


def load_json_command(
    command: list[str],
    *,
    cwd: Path,
    auth: dict[str, Any],
    failure_message: str,
) -> Any:
    completed = run_publisher_command(command, cwd=cwd, tool="gh", auth=auth)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(failure_message) from exc


def normalize_check_snapshots(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    snapshots = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        snapshots.append(
            {
                "name": string_field(item, "name") or "",
                "workflow": string_field(item, "workflow") or "",
                "state": (string_field(item, "state") or "").upper(),
                "bucket": (string_field(item, "bucket") or "").lower(),
                "status": check_status(item),
                "link": redact_text(string_field(item, "link") or ""),
            }
        )
    return snapshots


def normalize_status_check_rollup(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    snapshots = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_status = (string_field(item, "status") or "").upper()
        conclusion = (string_field(item, "conclusion") or "").upper()
        snapshots.append(
            {
                "name": string_field(item, "name") or "",
                "workflow": "",
                "state": raw_status,
                "bucket": "",
                "status": rollup_status(raw_status, conclusion),
                "link": "",
            }
        )
    return snapshots


def check_status(item: dict[str, Any]) -> str:
    bucket = (string_field(item, "bucket") or "").lower()
    state = (string_field(item, "state") or "").upper()
    if bucket == "pending" or state in PENDING_CHECK_STATES:
        return "pending"
    if bucket == "fail" or state in FAILED_CHECK_STATES:
        return "failed"
    if bucket == "pass" or state in PASSED_CHECK_STATES:
        return "passed"
    if state in INCONCLUSIVE_CHECK_STATES or bucket == "skipping":
        return "inconclusive"
    return "inconclusive"


def rollup_status(status: str, conclusion: str) -> str:
    if status != "COMPLETED":
        return "pending"
    if conclusion in PASSED_CHECK_STATES:
        return "passed"
    if conclusion in FAILED_CHECK_STATES:
        return "failed"
    return "inconclusive"


def classify_integration(
    *,
    expected_head: str,
    observed_head: str,
    pr_state: str,
    is_draft: bool,
    merge_state_status: str,
    check_snapshots: list[dict[str, Any]],
    required_checks: list[str],
    poll_seconds: int,
) -> tuple[str, int, str]:
    if expected_head != observed_head:
        return (
            "merge_blocked",
            0,
            "Exact head mismatch. Do not merge; rerun publication or repair so the PR head matches the validated SHA.",
        )
    if pr_state != "OPEN" or is_draft or merge_state_status in BLOCKED_MERGE_STATES:
        return (
            "merge_blocked",
            0,
            "Merge is blocked by the current PR state. Clear the block before attempting terminal merge.",
        )
    checks_by_name = {item["name"]: item for item in check_snapshots if item["name"]}
    if required_checks:
        missing_checks = [name for name in required_checks if name not in checks_by_name]
        if missing_checks:
            return (
                "checks_pending",
                poll_seconds,
                f"Wait for required checks to appear: {', '.join(missing_checks)}.",
            )
        snapshots = [checks_by_name[name] for name in required_checks]
    else:
        snapshots = check_snapshots
    statuses = {item["status"] for item in snapshots}
    if "pending" in statuses:
        return ("checks_pending", poll_seconds, "Wait for GitHub checks to finish, then rerun terminal integration.")
    if "failed" in statuses:
        return ("checks_failed", 0, "Fix the failing checks on the published head before trying to merge.")
    if "inconclusive" in statuses:
        return (
            "checks_inconclusive",
            0,
            "Investigate the inconclusive checks, then rerun terminal integration once they report a terminal state.",
        )
    return ("merge_ready", 0, "Checks passed on the expected head. Merge/close remains a separate step.")


def read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text("".join(canonical_json(event) + "\n" for event in events), encoding="utf-8")
