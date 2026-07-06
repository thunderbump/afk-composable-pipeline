from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from afk.publication import PublisherError
from afk.review_cycles import review_cycle_response_is_addressed, review_cycle_status_requires_response
from afk.redaction import redact_artifact_value, redact_text
from afk.schema_helpers import first_selected_work_external_id
from afk.workstream_lifecycle import (
    TERMINAL_REVIEW_FEEDBACK_STATUSES,
    has_current_validated_evidence,
    implemented_after_commit,
    repair_stop_record,
    review_passed,
    string_field,
)


@dataclass(frozen=True)
class TrackerContext:
    normalized: dict[str, Any]
    state: dict[str, Any]
    publication: dict[str, Any]
    retrospective: dict[str, Any]
    schema_version: int = 1


def empty_terminal_decision() -> dict[str, str]:
    return {"status": "", "merge_commit": "", "reason": "", "pr_url": "", "review_feedback_status": ""}


def runtime_terminal_decision(decision: Any) -> dict[str, str]:
    if not isinstance(decision, dict):
        return empty_terminal_decision()
    return {
        "status": string_field(decision, "status") or "",
        "merge_commit": string_field(decision, "merge_commit") or "",
        "reason": string_field(decision, "reason") or "",
        "pr_url": string_field(decision, "pr_url") or "",
        "review_feedback_status": string_field(decision, "review_feedback_status") or "",
    }


def tracker_terminal_decision_present(normalized: dict[str, Any]) -> bool:
    decision = normalized.get("tracker", {}).get("terminal_decision", {})
    return bool(isinstance(decision, dict) and decision.get("status"))


def effective_review_cycles(normalized: dict[str, Any], state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    configured = normalized.get("review_cycles")
    runtime = state.get("runtime_review_cycles") if isinstance(state, dict) else []
    cycles = list(configured) if isinstance(configured, list) else []
    if isinstance(runtime, list) and (not cycles or runtime_review_cycles_count_for_tracker(runtime)):
        cycles.extend(cycle for cycle in runtime if isinstance(cycle, dict))
    return cycles


def review_cycles_recorded(review_cycles: Any) -> bool:
    return isinstance(review_cycles, list) and bool(review_cycles)


def tracker_review_cycles(normalized: dict[str, Any], state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    configured = normalized.get("review_cycles")
    runtime = state.get("runtime_review_cycles") if isinstance(state, dict) else []
    cycles = list(configured) if isinstance(configured, list) else []
    if isinstance(runtime, list) and runtime_review_cycles_count_for_tracker(runtime):
        cycles.extend(cycle for cycle in runtime if isinstance(cycle, dict))
    return cycles


def tracker_review_cycles_for_status(
    normalized: dict[str, Any], state: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    configured = normalized.get("review_cycles")
    runtime = state.get("runtime_review_cycles") if isinstance(state, dict) else []
    cycles = (
        [cycle for cycle in configured if isinstance(cycle, dict) and runtime_review_cycle_has_feedback(cycle)]
        if isinstance(configured, list)
        else []
    )
    if isinstance(runtime, list) and runtime_review_cycles_have_feedback(runtime):
        cycles.extend(cycle for cycle in runtime if isinstance(cycle, dict) and runtime_review_cycle_has_feedback(cycle))
    return cycles


def runtime_review_cycles_count_for_tracker(runtime_cycles: list[dict[str, Any]]) -> bool:
    return any(runtime_review_cycle_is_recorded(cycle) for cycle in runtime_cycles if isinstance(cycle, dict))


def runtime_review_cycle_is_recorded(cycle: dict[str, Any]) -> bool:
    reviews = cycle.get("reviews")
    return isinstance(reviews, list) and bool(reviews)


def runtime_review_cycles_have_feedback(runtime_cycles: list[dict[str, Any]]) -> bool:
    return any(runtime_review_cycle_has_feedback(cycle) for cycle in runtime_cycles if isinstance(cycle, dict))


def runtime_review_cycle_has_feedback(cycle: dict[str, Any]) -> bool:
    cycle_status = string_field(cycle, "status") or ""
    reviews = cycle.get("reviews")
    if not isinstance(reviews, list):
        return False
    for review in reviews:
        if not isinstance(review, dict):
            continue
        review_status = string_field(review, "status") or ""
        if bool(review.get("requires_response")):
            return True
        if review_cycle_status_requires_response(cycle_status) or review_cycle_status_requires_response(review_status):
            return True
        if review_cycle_response_is_addressed(review.get("response")):
            return True
    return False


def tracker_terminal_decision_close_block_reason(
    normalized: dict[str, Any], state: dict[str, Any] | None = None
) -> str:
    decision = normalized.get("tracker", {}).get("terminal_decision", {})
    if not isinstance(decision, dict) or not decision.get("status"):
        return "terminal tracker decision is not recorded"
    review_feedback_status = terminal_review_feedback_status(decision)
    review_cycles = tracker_review_cycles(normalized, state)
    if not review_cycles_recorded(review_cycles):
        if review_feedback_status == "waived":
            return ""
        return (
            "terminal tracker decision recorded, but source item closure requires recorded review cycle "
            "evidence or review_feedback_status of waived"
        )
    if not review_cycles_require_response(review_cycles):
        return ""
    if review_feedback_status in TERMINAL_REVIEW_FEEDBACK_STATUSES:
        return ""
    return (
        "terminal tracker decision recorded, but unresolved review feedback still requires an explicit "
        "review_feedback_status of resolved or waived before the source item can close"
    )


def tracker_terminal_decision_allows_close(
    normalized: dict[str, Any], state: dict[str, Any] | None = None
) -> bool:
    return not tracker_terminal_decision_close_block_reason(normalized, state)


def tracker_close_blocked_publication(
    *,
    reason: str | None = None,
    terminal_decision: dict[str, Any] | None = None,
    mode: str = "",
    url: str = "",
    commands: dict[str, list[str]] | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "status": "tracker-close-blocked",
        "enabled": False,
        "reason": reason
        or (
            "terminal tracker decision recorded, but unresolved review feedback still requires an explicit "
            "review_feedback_status of resolved or waived before the source item can close"
        ),
        "next_allowed_command": "none",
        "retry": "",
        "mode": mode,
        "url": redact_text(url),
        "commands": {key: redact_artifact_value(value) for key, value in (commands or {}).items()},
        "terminal_decision": runtime_terminal_decision(terminal_decision),
    }


def tracker_close_failure_artifact(exc: PublisherError) -> dict[str, Any]:
    tool = redact_text(exc.command[0]) if exc.command else ""
    return {
        "status": "failed",
        "tool": tool,
        "reason": exc.message,
        "command": redact_artifact_value(exc.command),
        "returncode": exc.returncode,
        "stdout_excerpt": redact_text(exc.stdout[-2000:]),
        "stderr_excerpt": redact_text(exc.stderr[-2000:]),
        "remediation": (
            "The PR is already merged, but the source item remains open. Remediate the tracker/source close "
            "failure and retry only the source closure, or close it manually with the recorded merge commit."
        ),
    }


def tracker_terminal_decision_publication(schema_version: int = 1) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "status": "tracker-closed",
        "enabled": False,
        "reason": "terminal tracker decision recorded; PR publication skipped",
        "next_allowed_command": "none",
        "retry": "",
    }


def terminal_review_feedback_status(decision: dict[str, Any]) -> str:
    return string_field(decision, "review_feedback_status") or ""


def merged_close_reason(decision: dict[str, Any]) -> str:
    merge_commit = redact_text(decision["merge_commit"])
    return f"merged via {merge_commit}"


def no_merge_close_reason(decision: dict[str, Any]) -> str:
    return redact_text(decision["reason"])


def terminal_decision_comment(
    decision_status: str,
    review_feedback_status: str,
    review_feedback_requires_response: bool,
    review_cycle_evidence_recorded: bool,
) -> str:
    if decision_status == "merged":
        base = "PR merged; close the source Beads item with the recorded merge commit."
    else:
        base = "A terminal no-merge decision was recorded; close the source Beads item with this reason."
    if not review_cycle_evidence_recorded and review_feedback_status == "waived":
        return f"{base} Review cycle evidence was explicitly waived before closure."
    if not review_feedback_requires_response:
        return base
    if review_feedback_status == "resolved":
        return f"{base} Review feedback was explicitly resolved before closure."
    if review_feedback_status == "waived":
        return f"{base} Review feedback was explicitly waived before closure."
    return base


def build_tracker_record(context: TrackerContext) -> dict[str, Any]:
    decision = effective_tracker_terminal_decision(context.normalized, context.publication)
    recorded_terminal_decision = recorded_tracker_terminal_decision(context.normalized, context.publication)
    decision_pr_url = redact_text(decision.get("pr_url") or "")
    decision_review_feedback_status = terminal_review_feedback_status(decision)
    review_cycles = tracker_review_cycles(context.normalized, context.state)
    status_review_cycles = tracker_review_cycles_for_status(context.normalized, context.state)
    review_cycle_evidence_recorded = review_cycles_recorded(review_cycles)
    review_feedback_requires_response = review_cycles_require_response(status_review_cycles)
    review_feedback_recorded = review_cycles_recorded(status_review_cycles)
    review = context.state.get("review") if isinstance(context.state.get("review"), dict) else {}
    record = {
        "schema_version": context.schema_version,
        "status": tracker_progress_status(context.state),
        "repair_stop": redact_artifact_value(repair_stop_record(context.state, context.publication)),
        "close_source_item": False,
        "close_reason": "",
        "comment": "",
        "pr_url": "",
        "merge_commit": "",
        "source_item_external_id": current_selected_work_external_id(context.state),
        "review_status": string_field(review, "status") or "",
        "review_summary": string_field(review, "summary") or "",
        "review_findings": tracker_review_findings(review),
        "review_cycles": redact_review_cycles(review_cycles),
        "retrospective": redact_retrospective(context.retrospective),
        "terminal_decision": redact_artifact_value(recorded_terminal_decision),
    }
    decision_status = decision.get("status")
    if decision_status == "merged":
        record["merge_commit"] = redact_text(decision["merge_commit"])
        record["pr_url"] = decision_pr_url
        if review_feedback_requires_response and not decision_review_feedback_status:
            record["status"] = "review-findings-open"
            record["comment"] = (
                "PR review cycles still require a response; the terminal decision is recorded, but keep the "
                "source Beads item open until review_feedback_status is set to resolved or waived."
            )
            return record
        if context.publication.get("status") != "tracker-closed":
            if publication_tracker_close_failed(context.publication):
                record["comment"] = (
                    "PR merged and the terminal decision is recorded, but source item closure failed; keep the "
                    "source Beads item open until the recorded tracker_close failure is remediated."
                )
            elif not review_cycle_evidence_recorded:
                record["comment"] = (
                    "A terminal merge decision is recorded, but keep the source Beads item open until review "
                    "cycle evidence is recorded or explicitly waived."
                )
            else:
                record["comment"] = (
                    "A terminal merge decision is recorded, but publication did not reach the tracker-closing "
                    "terminal state; keep the source Beads item open."
                )
            return record
        record["status"] = "closed"
        record["close_source_item"] = True
        record["close_reason"] = merged_close_reason(decision)
        record["comment"] = terminal_decision_comment(
            "merged",
            decision_review_feedback_status,
            review_feedback_requires_response,
            review_cycle_evidence_recorded,
        )
        return record
    if decision_status == "blocked":
        blocked_reason = redact_text(str(decision.get("reason") or context.publication.get("reason") or ""))
        if review_feedback_requires_response:
            record["status"] = "review-findings-open"
        elif review_feedback_recorded:
            record["status"] = "review-feedback-addressed"
        if blocked_reason:
            record["comment"] = (
                f"{blocked_reason}; keep the source Beads item open until the recorded blocker is cleared."
            )
        else:
            record["comment"] = (
                "Terminal PR closure is blocked; keep the source Beads item open until the recorded blocker is cleared."
            )
        record["pr_url"] = decision_pr_url or redact_text(str(context.publication.get("url") or ""))
        return record
    if decision_status == "no-merge":
        if review_feedback_requires_response and not decision_review_feedback_status:
            record["status"] = "review-findings-open"
            record["comment"] = (
                "PR review cycles still require a response; the terminal no-merge decision is recorded, but keep "
                "the source Beads item open until review_feedback_status is set to resolved or waived."
            )
            record["pr_url"] = decision_pr_url
            return record
        if context.publication.get("status") != "tracker-closed":
            if not review_cycle_evidence_recorded:
                record["comment"] = (
                    "A terminal no-merge decision is recorded, but keep the source Beads item open until review "
                    "cycle evidence is recorded or explicitly waived."
                )
            else:
                record["comment"] = (
                    "A terminal no-merge decision is recorded, but publication did not reach the tracker-closing "
                    "terminal state; keep the source Beads item open."
                )
            record["pr_url"] = decision_pr_url
            return record
        record["status"] = "closed"
        record["close_source_item"] = True
        record["close_reason"] = no_merge_close_reason(decision)
        record["comment"] = terminal_decision_comment(
            "no-merge",
            decision_review_feedback_status,
            review_feedback_requires_response,
            review_cycle_evidence_recorded,
        )
        record["pr_url"] = decision_pr_url
        return record
    if review_feedback_requires_response:
        record["status"] = "review-findings-open"
        record["comment"] = "PR review cycles contain response-required review findings; keep the source Beads item open."
        if context.publication.get("status") == "published":
            record["pr_url"] = redact_text(str(context.publication.get("url") or ""))
        return record
    if review_feedback_recorded:
        record["status"] = "review-feedback-addressed"
        record["comment"] = (
            "PR review cycle evidence is present and all response-required findings are addressed; keep the source "
            "Beads item open until merge or an explicit no-merge decision."
        )
        if context.publication.get("status") == "published":
            record["pr_url"] = redact_text(str(context.publication.get("url") or ""))
        return record
    if context.publication.get("status") == "published":
        record["status"] = "awaiting-review"
        record["comment"] = "PR opened; keep the source Beads item open until merge or an explicit no-merge decision."
        record["pr_url"] = redact_text(str(context.publication.get("url") or ""))
        return record
    if context.publication.get("status") == "validated-unpublished":
        record["status"] = "validated"
        record["comment"] = "Validated head is ready, but the source Beads item stays open until merge or no-merge."
        return record
    if record["review_findings"]:
        record["comment"] = "Review findings are available; update the source Beads item and keep it open."
    elif review_passed(context.state):
        record["comment"] = "Final review passed, but keep the source Beads item open until merge or no-merge."
    return record


def tracker_record(
    normalized: dict[str, Any],
    state: dict[str, Any],
    publication: dict[str, Any],
    *,
    retrospective: dict[str, Any] | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    return build_tracker_record(
        TrackerContext(
            normalized=normalized,
            state=state,
            publication=publication,
            retrospective=retrospective or {},
            schema_version=schema_version,
        )
    )


def effective_tracker_terminal_decision(normalized: dict[str, Any], publication: dict[str, Any]) -> dict[str, str]:
    return runtime_terminal_decision(publication.get("terminal_decision"))


def recorded_tracker_terminal_decision(normalized: dict[str, Any], publication: dict[str, Any]) -> dict[str, str]:
    publication_decision = runtime_terminal_decision(publication.get("terminal_decision"))
    if publication_decision.get("status"):
        return publication_decision
    return runtime_terminal_decision(normalized.get("tracker", {}).get("terminal_decision"))


def publication_tracker_close_failed(publication: dict[str, Any]) -> bool:
    tracker_close = publication.get("tracker_close")
    return isinstance(tracker_close, dict) and string_field(tracker_close, "status") == "failed"


def tracker_progress_status(state: dict[str, Any]) -> str:
    if has_current_validated_evidence(state):
        return "validated"
    if implemented_after_commit(state):
        return "implemented"
    if state.get("selected_work"):
        return "selected"
    return "idle"


def tracker_review_findings(review: dict[str, Any]) -> list[Any]:
    reviewer_result = review.get("reviewer_result") if isinstance(review.get("reviewer_result"), dict) else {}
    findings = reviewer_result.get("findings")
    if not isinstance(findings, list):
        return []
    return redact_artifact_value(findings)


def current_selected_work_external_id(state: dict[str, Any]) -> str:
    return first_selected_work_external_id(state.get("selected_work"))


def redact_review_cycles(review_cycles: Any) -> list[Any]:
    if not isinstance(review_cycles, list):
        return []
    return [redact_review_cycle(cycle) for cycle in review_cycles]


def redact_retrospective(retrospective: Any) -> dict[str, Any]:
    if not isinstance(retrospective, dict):
        return {}
    return redact_artifact_value(retrospective)


def redact_review_cycle(cycle: Any) -> Any:
    if not isinstance(cycle, dict):
        return redact_artifact_value(cycle)
    redacted = redact_artifact_value(cycle)
    reviews = cycle.get("reviews")
    if not isinstance(reviews, list):
        return redacted
    redacted["reviews"] = [
        redact_review_cycle_review(review, redacted_review)
        for review, redacted_review in zip(reviews, redacted.get("reviews", []))
    ]
    return redacted


def redact_review_cycle_review(review: Any, redacted_review: Any) -> Any:
    if not isinstance(review, dict) or not isinstance(redacted_review, dict):
        return redacted_review
    pr_comment_url = review.get("pr_comment_url")
    if isinstance(pr_comment_url, str) and pr_comment_url:
        redacted_review["pr_comment_url"] = redact_review_cycle_pr_comment_url(pr_comment_url)
    return redacted_review


def redact_review_cycle_pr_comment_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return redact_text(value)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    elif not parsed.username and not parsed.password:
        host = parsed.netloc
    fragment = parsed.fragment if review_cycle_fragment_is_safe(parsed.fragment) else ""
    return urlunsplit((parsed.scheme, host, parsed.path, "", fragment))


def review_cycle_fragment_is_safe(fragment: str) -> bool:
    if not fragment:
        return False
    if redact_text(fragment) != fragment:
        return False
    fragment_keys = [key for key, _ in parse_qsl(fragment, keep_blank_values=True)]
    return all(not key or not key.lower().endswith(("token", "secret", "key")) for key in fragment_keys)


def review_cycles_require_response(review_cycles: Any) -> bool:
    if not isinstance(review_cycles, list):
        return False
    for cycle in review_cycles:
        if not isinstance(cycle, dict):
            continue
        cycle_status = string_field(cycle, "status") or ""
        reviews = cycle.get("reviews")
        if not isinstance(reviews, list):
            continue
        for review in reviews:
            if not isinstance(review, dict):
                continue
            review_status = string_field(review, "status") or ""
            requires_response = bool(review.get("requires_response"))
            response = review.get("response")
            response_is_addressed = review_cycle_response_is_addressed(response)
            if requires_response and not response_is_addressed:
                return True
            if review_cycle_status_requires_response(cycle_status) or review_cycle_status_requires_response(
                review_status
            ):
                if not response_is_addressed:
                    return True
    return False
