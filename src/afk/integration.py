from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from afk.jsonutil import canonical_json
from afk.publication import PublisherError, publisher_auth_artifact, run_publisher_command, validate_publisher_auth_config
from afk.redaction import redact_artifact_value, redact_text
from afk.schema_helpers import string_field
from afk.tracking import terminal_review_feedback_status, tracker_close_failure_artifact
from afk.workstream import close_selected_source_item, publisher_pr_merge_commit, publisher_pr_url


SCHEMA_VERSION = 1
DEFAULT_POLL_SECONDS = 300
PENDING_CHECK_STATES = {"PENDING", "PENDING_DEPLOYMENT", "IN_PROGRESS", "QUEUED", "REQUESTED", "WAITING"}
FAILED_CHECK_STATES = {"FAILURE", "FAILED", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
INCONCLUSIVE_CHECK_STATES = {"EXPECTED", "STALE", "NEUTRAL", "SKIPPED", "STARTUP_FAILURE"}
PASSED_CHECK_STATES = {"SUCCESS", "PASS", "PASSED"}
BLOCKED_MERGE_STATES = {"BLOCKED", "DIRTY", "UNKNOWN", "UNSTABLE", "BEHIND", "DRAFT"}


def _path_is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_step_result_path(step: dict[str, Any], workstream_dir: Path) -> Path | None:
    ledger_root = workstream_dir.parent.parent.resolve(strict=False)
    relative_path = string_field(step, "result_path")
    if relative_path:
        candidate = (ledger_root / relative_path).resolve(strict=False)
        if _path_is_within_root(candidate, ledger_root) and candidate.is_file():
            return candidate
    return None


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
    retry_context = prior_tracker_close_failure(request["output_dir"])
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
                check_snapshots = merge_check_snapshots(check_snapshots, checks_snapshots)
    if retry_context:
        decision, next_poll_seconds, remediation = (
            "merge_ready",
            0,
            "Retry the recorded tracker close for the already-merged PR without attempting another merge.",
        )
    else:
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
        "retry": "",
        "auth": auth_artifact,
        "commands": {
            "gh_view": redact_artifact_value(view_command),
            "gh_checks": redact_artifact_value(checks_command),
        },
    }
    if decision == "merge_ready":
        result = integrate_terminal_merge(
            request=request,
            result=result,
            view_payload=view_payload,
            view_command=view_command,
            retry_context=retry_context,
        )
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
    blocked_reason = blocked_workstream_artifact_reason(publication, workstream)
    if blocked_reason:
        raise ValueError(f"cannot integrate blocked workstream artifact: {blocked_reason}")

    repo = publication_repo(publication, workstream)
    pr_number = publication_pr_number(publication, workstream)
    pr_url = publication_pr_url(publication, workstream)
    expected_head_sha = publication_expected_head(publication, workstream, workstream_dir=workstream_dir)
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
        "workstream": workstream,
        "repo": repo,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "expected_head_sha": expected_head_sha,
        "required_checks": required_checks,
        "auth": auth,
        "gh_path": string_field(gh, "path") or "gh",
        "poll_seconds": poll_seconds,
    }


def blocked_workstream_artifact_reason(publication: dict[str, Any], workstream: dict[str, Any]) -> str:
    publication_status = string_field(publication, "status")
    workstream_status = string_field(workstream, "status")
    if publication_status != "blocked" and workstream_status != "blocked":
        return ""
    for candidate in (
        string_field(publication, "reason"),
        string_field(workstream, "terminal_reason"),
        string_field(workstream, "retry"),
    ):
        if candidate:
            return candidate
    nested_publication = workstream.get("publication")
    if isinstance(nested_publication, dict):
        reason = string_field(nested_publication, "reason")
        if reason:
            return reason
    return "workstream publication is blocked"


def integrate_terminal_merge(
    *,
    request: dict[str, Any],
    result: dict[str, Any],
    view_payload: dict[str, Any],
    view_command: list[str],
    retry_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if retry_context is None:
        retry_context = prior_tracker_close_failure(request["output_dir"])
    if retry_context:
        return retry_tracker_close(request=request, result=result, retry_context=retry_context, view_command=view_command)

    merge_command = [
        request["gh_path"],
        "pr",
        "merge",
        str(request["pr_number"]),
        "--repo",
        request["repo"],
        "--merge",
        "--match-head-commit",
        request["expected_head_sha"],
    ]
    try:
        run_publisher_command(
            merge_command,
            cwd=request["command_cwd"],
            tool="gh",
            auth=request["auth"],
            message_on_failure="gh pr merge failed",
        )
    except PublisherError as exc:
        return {
            **result,
            "status": "merge_blocked",
            "remediation": f"Terminal merge failed: {exc.message}",
            "merge": {
                "status": "blocked",
                "method": "merge",
                "matched_head_sha": request["expected_head_sha"],
                "reason": exc.message,
                "returncode": exc.returncode,
                "stdout_excerpt": redact_text(exc.stdout[-2000:]),
                "stderr_excerpt": redact_text(exc.stderr[-2000:]),
            },
            "commands": {
                **result["commands"],
                "gh_merge": redact_artifact_value(merge_command),
            },
        }

    merged_view_command = [
        request["gh_path"],
        "pr",
        "view",
        str(request["pr_number"]),
        "--repo",
        request["repo"],
        "--json",
        "url,mergeCommit,mergedAt",
    ]
    merged_payload = load_json_command(
        merged_view_command,
        cwd=request["command_cwd"],
        auth=request["auth"],
        failure_message="gh pr view returned invalid merged JSON payload",
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
        "pr_url": publisher_pr_url(merged_payload, fallback=string_field(view_payload, "url") or request["pr_url"]),
        "review_feedback_status": integration_review_feedback_status(request["workstream"]),
    }
    return close_tracker_after_merge(
        request=request,
        result=result,
        terminal_decision=terminal_decision,
        merge_status="merged",
        merge_command=merge_command,
        merged_view_command=merged_view_command,
    )


def retry_tracker_close(
    *,
    request: dict[str, Any],
    result: dict[str, Any],
    retry_context: dict[str, Any],
    view_command: list[str],
) -> dict[str, Any]:
    terminal_decision = retry_context.get("terminal_decision")
    if not isinstance(terminal_decision, dict):
        terminal_decision = {}
    commands = retry_context.get("commands")
    merge_command = commands.get("gh_merge") if isinstance(commands, dict) else []
    merged_view_command = commands.get("gh_view_merged") if isinstance(commands, dict) else []
    return close_tracker_after_merge(
        request=request,
        result={
            **result,
            "commands": {
                **result["commands"],
                "gh_view": redact_artifact_value(view_command),
            },
        },
        terminal_decision={
            "status": "merged",
            "merge_commit": string_field(terminal_decision, "merge_commit") or string_field(retry_context.get("merge"), "merge_commit"),
            "reason": "",
            "pr_url": string_field(terminal_decision, "pr_url") or request["pr_url"],
            "review_feedback_status": string_field(terminal_decision, "review_feedback_status")
            or integration_review_feedback_status(request["workstream"]),
        },
        merge_status="already_merged",
        merge_command=merge_command if isinstance(merge_command, list) else [],
        merged_view_command=merged_view_command if isinstance(merged_view_command, list) else [],
    )


def close_tracker_after_merge(
    *,
    request: dict[str, Any],
    result: dict[str, Any],
    terminal_decision: dict[str, Any],
    merge_status: str,
    merge_command: list[str],
    merged_view_command: list[str],
) -> dict[str, Any]:
    merge_commit = string_field(terminal_decision, "merge_commit")
    selected_work = request["workstream"].get("selected_work")
    if not isinstance(selected_work, list) or not selected_work:
        return {
            **result,
            "status": "merged",
            "merge": {
                "status": merge_status,
                "method": "merge",
                "matched_head_sha": request["expected_head_sha"],
                "merge_commit": merge_commit,
            },
            "terminal_decision": terminal_decision,
            "tracker_close": {"status": "not_attempted", "reason": "no selected work item recorded for tracker closure"},
            "commands": terminal_commands(result["commands"], merge_command, merged_view_command),
        }
    try:
        tracker_close = close_selected_source_item(
            normalized=request["workstream"],
            state=request["workstream"],
            config={"gh_path": request["gh_path"], "repo": request["repo"], "pr": str(request["pr_number"])},
            checkout_path=request["command_cwd"],
            auth=request["auth"],
            close_reason=f"merged via {merge_commit}",
        )
    except PublisherError as exc:
        return {
            **result,
            "status": "tracker_close_failed",
            "retry": (
                "PR is already merged. Remediate the recorded source-item closure failure, then retry only the "
                "tracker close without attempting another merge."
            ),
            "merge": {
                "status": merge_status,
                "method": "merge",
                "matched_head_sha": request["expected_head_sha"],
                "merge_commit": merge_commit,
            },
            "terminal_decision": terminal_decision,
            "tracker_close": tracker_close_failure_artifact(exc),
            "commands": terminal_commands(result["commands"], merge_command, merged_view_command),
        }

    return {
        **result,
        "status": "tracker-closed",
        "merge": {
            "status": merge_status,
            "method": "merge",
            "matched_head_sha": request["expected_head_sha"],
            "merge_commit": merge_commit,
        },
        "terminal_decision": terminal_decision,
        "tracker_close": tracker_close,
        "commands": terminal_commands(result["commands"], merge_command, merged_view_command),
    }


def terminal_commands(
    commands: dict[str, Any],
    merge_command: list[str],
    merged_view_command: list[str],
) -> dict[str, Any]:
    merged = dict(commands)
    if merge_command:
        merged["gh_merge"] = redact_artifact_value(merge_command)
    if merged_view_command:
        merged["gh_view_merged"] = redact_artifact_value(merged_view_command)
    return merged


def prior_tracker_close_failure(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir / "integration-result.json"
    if not path.is_file():
        return None
    payload = read_json_file(path)
    if not isinstance(payload, dict) or string_field(payload, "status") != "tracker_close_failed":
        return None
    merge = payload.get("merge")
    if not isinstance(merge, dict) or not string_field(merge, "merge_commit"):
        return None
    return payload


def integration_review_feedback_status(workstream: dict[str, Any]) -> str:
    tracker = workstream.get("tracker")
    if not isinstance(tracker, dict):
        return ""
    decision = tracker.get("terminal_decision")
    if not isinstance(decision, dict):
        return ""
    return terminal_review_feedback_status(decision)


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


def publication_expected_head(
    publication: dict[str, Any],
    workstream: dict[str, Any],
    *,
    workstream_dir: Path | None = None,
) -> str:
    for source in (
        publication,
        workstream.get("publication", {}) if isinstance(workstream.get("publication"), dict) else {},
    ):
        head = string_field(source, "expected_head_sha")
        if head:
            return head
    steps = workstream.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            step_name = string_field(step, "name") or string_field(step, "step")
            if step_name != "implement":
                continue
            output = step.get("output")
            if isinstance(output, dict):
                git_info = output.get("git")
                if isinstance(git_info, dict):
                    head = string_field(git_info, "after_commit")
                    if head:
                        return head
            if workstream_dir is not None:
                path = _resolve_step_result_path(step, workstream_dir)
                if path is not None:
                    payload = read_json_file(path)
                    if isinstance(payload, dict):
                        output = payload.get("output")
                        if isinstance(output, dict):
                            git_info = output.get("git")
                            if isinstance(git_info, dict):
                                head = string_field(git_info, "after_commit")
                                if head:
                                    return head
            break
    tracker = workstream.get("tracker")
    if isinstance(tracker, dict):
        head = string_field(tracker, "implementation_commit")
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


def merge_check_snapshots(existing: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [item.copy() for item in existing]
    names = {item["name"]: index for index, item in enumerate(merged) if item.get("name")}
    for item in fresh:
        name = item.get("name") or ""
        if name and name in names:
            merged[names[name]] = item
            continue
        merged.append(item)
    return merged


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


def classify_terminal_integration(
    published_result_path: str | Path,
    *,
    policy: Any,
    github: dict[str, Any],
    ledger_dir: str | Path,
) -> dict[str, Any]:
    published_path = Path(published_result_path).resolve(strict=True)
    workstream = load_workstream_payload(published_path)
    publication = load_publication_payload(published_path)
    request = {
        "published_result_path": published_path,
        "workstream_dir": published_path.parent,
        "command_cwd": published_path.parent,
        "output_dir": Path(ledger_dir) / "output",
        "workstream": workstream,
        "repo": publication_repo(publication, workstream),
        "pr_number": publication_pr_number(publication, workstream),
        "pr_url": publication_pr_url(publication, workstream),
        "expected_head_sha": publication_expected_head(publication, workstream, workstream_dir=published_path.parent),
        "required_checks": normalize_required_checks(policy if isinstance(policy, dict) else {}),
        "auth": {
            "configured": True,
            "source": "gh_config_dir",
            "config_dir": string_field(github.get("auth") if isinstance(github, dict) else {}, "config_dir"),
        },
        "gh_path": string_field(github if isinstance(github, dict) else {}, "path") or "gh",
        "poll_seconds": (
            policy.get("poll_seconds", policy.get("poll_interval_seconds", DEFAULT_POLL_SECONDS))
            if isinstance(policy, dict)
            else DEFAULT_POLL_SECONDS
        ),
    }
    output_dir = Path(ledger_dir) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    request["output_dir"] = output_dir

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
    decision, next_poll_seconds, remediation = classify_integration(
        expected_head=request["expected_head_sha"],
        observed_head=observed_head,
        pr_state=(string_field(view_payload, "state") or "").upper(),
        is_draft=bool(view_payload.get("isDraft")),
        merge_state_status=(string_field(view_payload, "mergeStateStatus") or "").upper(),
        check_snapshots=normalize_status_check_rollup(view_payload.get("statusCheckRollup")),
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
        "pr_state": (string_field(view_payload, "state") or "").upper(),
        "is_draft": bool(view_payload.get("isDraft")),
        "merge_state_status": (string_field(view_payload, "mergeStateStatus") or "").upper(),
        "check_snapshots": normalize_status_check_rollup(view_payload.get("statusCheckRollup")),
        "decision": decision,
        "next_poll_seconds": next_poll_seconds,
        "remediation": "" if decision == "merge_ready" else remediation,
        "auth": auth_artifact,
        "commands": {"gh_view": redact_artifact_value(view_command)},
    }
    write_json(output_dir / "integration-result.json", result)
    write_events(
        output_dir / "integration-events.jsonl",
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
                "remediation": result["remediation"],
            },
            {
                "schema_version": SCHEMA_VERSION,
                "event": "integration.compatibility-classified",
                "repo": request["repo"],
                "pr_number": request["pr_number"],
                "decision": decision,
            },
        ],
    )
    return result
