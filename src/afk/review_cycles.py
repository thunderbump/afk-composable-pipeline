from __future__ import annotations

from typing import Any


REVIEW_CYCLE_STATUSES = {"passed", "findings-open", "findings-addressed", "request-changes"}
REVIEW_CYCLE_OPEN_STATUSES = {"findings-open", "request-changes"}
REVIEW_CYCLE_RESPONSE_STATUSES = {"addressed", "findings-addressed"}


def normalize_review_cycles(review_cycles: Any) -> list[dict[str, Any]]:
    if review_cycles is None:
        return []
    if not isinstance(review_cycles, list):
        raise ValueError("review_cycles must be a list")
    normalized = []
    for cycle_index, cycle in enumerate(review_cycles):
        if not isinstance(cycle, dict):
            raise ValueError(f"review_cycles[{cycle_index}] must be an object")
        cycle_number = cycle.get("cycle", cycle_index + 1)
        if isinstance(cycle_number, bool) or not isinstance(cycle_number, int) or cycle_number <= 0:
            raise ValueError(f"review_cycles[{cycle_index}].cycle must be a positive integer")
        status = normalize_review_cycle_optional_string(
            cycle,
            "status",
            f"review_cycles[{cycle_index}].status must be a string",
        )
        validate_review_cycle_status(
            status,
            f"review_cycles[{cycle_index}].status must be one of: {', '.join(sorted(REVIEW_CYCLE_STATUSES))}",
        )
        reviews = cycle.get("reviews")
        if not isinstance(reviews, list):
            raise ValueError(f"review_cycles[{cycle_index}].reviews must be a list")
        normalized_reviews = []
        for review_index, review in enumerate(reviews):
            if not isinstance(review, dict):
                raise ValueError(f"review_cycles[{cycle_index}].reviews[{review_index}] must be an object")
            normalized_review = {
                "role": normalize_review_cycle_required_string(
                    review,
                    "role",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].role is required",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].role must be a string",
                ),
                "status": normalize_review_cycle_required_string(
                    review,
                    "status",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].status is required",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].status must be a string",
                ),
                "summary": normalize_review_cycle_required_string(
                    review,
                    "summary",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].summary is required",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].summary must be a string",
                ),
                "requires_response": normalize_review_cycle_boolean(
                    review,
                    "requires_response",
                    f"review_cycles[{cycle_index}].reviews[{review_index}].requires_response must be a boolean",
                ),
            }
            validate_review_cycle_status(
                normalized_review["status"],
                "review_cycles"
                f"[{cycle_index}].reviews[{review_index}].status must be one of: "
                f"{', '.join(sorted(REVIEW_CYCLE_STATUSES))}",
            )
            pr_comment_url = normalize_review_cycle_optional_string(
                review,
                "pr_comment_url",
                f"review_cycles[{cycle_index}].reviews[{review_index}].pr_comment_url must be a string",
            )
            if pr_comment_url:
                normalized_review["pr_comment_url"] = pr_comment_url
            if "response" in review:
                response = review["response"]
                if not isinstance(response, (str, dict)):
                    raise ValueError(
                        f"review_cycles[{cycle_index}].reviews[{review_index}].response must be a string or object"
                    )
                normalized_review["response"] = normalize_review_cycle_response(
                    response,
                    cycle_index=cycle_index,
                    review_index=review_index,
                )
            normalized_reviews.append(normalized_review)
        normalized.append({"cycle": cycle_number, "status": status, "reviews": normalized_reviews})
    return normalized


def validate_review_cycle_status(status: str, error_message: str) -> None:
    if status and status not in REVIEW_CYCLE_STATUSES:
        raise ValueError(error_message)


def normalize_review_cycle_response(
    response: str | dict[str, Any],
    *,
    cycle_index: int,
    review_index: int,
) -> str | dict[str, Any]:
    if isinstance(response, str):
        return response.strip()
    normalized = dict(response)
    status = normalize_review_cycle_required_string(
        normalized,
        "status",
        f"review_cycles[{cycle_index}].reviews[{review_index}].response.status is required",
        f"review_cycles[{cycle_index}].reviews[{review_index}].response.status must be a string",
    )
    if status not in REVIEW_CYCLE_RESPONSE_STATUSES:
        allowed = ", ".join(sorted(REVIEW_CYCLE_RESPONSE_STATUSES))
        raise ValueError(
            f"review_cycles[{cycle_index}].reviews[{review_index}].response.status must be one of: {allowed}"
        )
    normalized["status"] = status
    summary = normalized.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError(
            f"review_cycles[{cycle_index}].reviews[{review_index}].response.summary must be a string"
        )
    if isinstance(summary, str):
        normalized["summary"] = summary.strip()
    return normalized


def review_cycle_status_requires_response(status: str) -> bool:
    return status in REVIEW_CYCLE_OPEN_STATUSES


def review_cycle_response_is_addressed(response: Any) -> bool:
    if isinstance(response, str):
        return bool(response.strip())
    if not isinstance(response, dict):
        return False
    return (string_field(response, "status") or "") in REVIEW_CYCLE_RESPONSE_STATUSES


def runtime_review_cycle_status(review_status: str) -> str:
    if review_status == "request_revision":
        return "request-changes"
    if review_status == "passed":
        return "passed"
    return "findings-open"


def aggregate_runtime_review_cycle_status(reviews: list[dict[str, Any]]) -> str:
    statuses = [string_field(review, "status") or "" for review in reviews if isinstance(review, dict)]
    if any(status == "request-changes" for status in statuses):
        return "request-changes"
    if all(status == "passed" for status in statuses) and statuses:
        return "passed"
    return "findings-open"


def finalized_runtime_review_cycle_status(reviews: list[dict[str, Any]]) -> str:
    saw_addressed_request_changes = False
    saw_reviews = False
    saw_only_passed = True
    for review in reviews:
        if not isinstance(review, dict):
            continue
        saw_reviews = True
        status = string_field(review, "status") or ""
        if status == "findings-open":
            return "findings-open"
        if status == "request-changes":
            if review_cycle_response_is_addressed(review.get("response")):
                saw_addressed_request_changes = True
                saw_only_passed = False
                continue
            return "request-changes"
        if status != "passed":
            return "findings-open"
    if saw_only_passed and saw_reviews:
        return "passed"
    if saw_addressed_request_changes:
        return "findings-addressed"
    return "findings-open"


def normalize_review_cycle_optional_string(input_data: dict[str, Any], key: str, error_message: str) -> str:
    value = input_data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(error_message)
    return value.strip()


def normalize_review_cycle_required_string(
    input_data: dict[str, Any],
    key: str,
    missing_error_message: str,
    type_error_message: str,
) -> str:
    value = input_data.get(key)
    if value is None:
        raise ValueError(missing_error_message)
    if not isinstance(value, str):
        raise ValueError(type_error_message)
    normalized = value.strip()
    if not normalized:
        raise ValueError(missing_error_message)
    return normalized


def normalize_review_cycle_boolean(input_data: dict[str, Any], key: str, error_message: str) -> bool:
    value = input_data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(error_message)
    return value


def string_field(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if isinstance(item, str) and item.strip():
        return item.strip()
    return None
