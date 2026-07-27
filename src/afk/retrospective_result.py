from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from afk.durable_id import is_durable_id
from afk.jsonutil import canonical_json
from afk.retrospective_contract import TEXT_CHARACTER_LIMIT
from afk.run_summary import (
    MAX_RUN_SUMMARY_BYTES,
    RUN_SUMMARY_SCHEMA_VERSION,
    citation_manifest,
)


COLLECTION_LIMIT = 32
CATEGORIES = {
    "orchestration",
    "implementation",
    "validation",
    "review",
    "repair",
    "publication",
    "tracker",
    "environment",
    "operator_process",
    "evidence",
}
CONFIDENCE = {"high", "medium", "low"}
SCOPES = {"afk", "target_repository", "environment", "operator_process"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
RESULT_KEYS = {
    "schema_version",
    "run_id",
    "terminal_outcome",
    "summary",
    "process_findings",
    "improvement_proposals",
}
FINDING_KEYS = {"id", "category", "title", "evidence", "impact", "confidence"}
PROPOSAL_KEYS = {
    "id",
    "addresses",
    "scope",
    "priority",
    "title",
    "rationale",
    "suggested_change",
    "requires_human_decision",
}


class RetrospectiveResultError(ValueError):
    def __init__(self, error: str):
        self.errors = (error[:TEXT_CHARACTER_LIMIT],)
        super().__init__(self.errors[0])


def normalize_retrospective_result(
    run_summary: str,
    result: Any,
) -> dict[str, Any]:
    """Validate and normalize one untrusted retrospective result."""
    if not isinstance(run_summary, str):
        raise RetrospectiveResultError("Run Summary is invalid")
    try:
        run_summary_bytes = run_summary.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RetrospectiveResultError("Run Summary is invalid") from exc
    if len(run_summary_bytes) > MAX_RUN_SUMMARY_BYTES:
        raise RetrospectiveResultError("Run Summary is invalid")
    try:
        summary = json.loads(run_summary)
        normalized_summary = canonical_json(summary)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RetrospectiveResultError("Run Summary is invalid") from exc
    if (
        not isinstance(summary, dict)
        or type(summary.get("schema_version")) is not int
        or summary["schema_version"] != RUN_SUMMARY_SCHEMA_VERSION
        or normalized_summary != run_summary
        or not isinstance(summary.get("run"), dict)
        or not isinstance(summary.get("episode"), dict)
        or not isinstance(summary["run"].get("run_id"), str)
        or not isinstance(summary["episode"].get("state"), str)
        or summary["episode"].get("state") not in {"attention_required", "completed"}
        or not isinstance(summary.get("citation_manifest"), dict)
        or summary["citation_manifest"] != citation_manifest(summary)
    ):
        raise RetrospectiveResultError("Run Summary is invalid")
    if (
        not isinstance(result, dict)
        or set(result) != RESULT_KEYS
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
        or result.get("run_id") != summary["run"]["run_id"]
        or result.get("terminal_outcome") != summary["episode"]["state"]
        or not _text(result.get("summary"))
        or not isinstance(result.get("process_findings"), list)
        or not isinstance(result.get("improvement_proposals"), list)
    ):
        raise RetrospectiveResultError("retrospective result is invalid")
    findings = result["process_findings"]
    proposals = result["improvement_proposals"]
    if len(findings) > COLLECTION_LIMIT or len(proposals) > COLLECTION_LIMIT:
        raise RetrospectiveResultError("retrospective result is invalid")
    finding_ids = set()
    for index, finding in enumerate(findings):
        _validate_finding(finding, summary, index)
        if finding["id"] in finding_ids:
            raise RetrospectiveResultError(
                f"process_findings[{index}].id duplicates an existing identity"
            )
        finding_ids.add(finding["id"])
    proposal_ids = set()
    for index, proposal in enumerate(proposals):
        _validate_proposal(proposal, finding_ids, index)
        if proposal["id"] in finding_ids or proposal["id"] in proposal_ids:
            raise RetrospectiveResultError(
                f"improvement_proposals[{index}].id " "duplicates an existing identity"
            )
        proposal_ids.add(proposal["id"])
    return json.loads(canonical_json(result))


def _text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(value.strip()) and len(value) <= TEXT_CHARACTER_LIMIT


def _validate_finding(
    value: Any,
    summary: dict[str, Any],
    index: int,
) -> None:
    if isinstance(value, dict):
        category = value.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise RetrospectiveResultError(
                f"process_findings[{index}].category is invalid"
            )
        confidence = value.get("confidence")
        if not isinstance(confidence, str) or confidence not in CONFIDENCE:
            raise RetrospectiveResultError(
                f"process_findings[{index}].confidence is invalid"
            )
    evidence = value.get("evidence") if isinstance(value, dict) else None
    if not isinstance(evidence, list) or not 0 < len(evidence) <= COLLECTION_LIMIT:
        raise RetrospectiveResultError(f"process_findings[{index}] is invalid")
    for citation_index, citation in enumerate(evidence):
        if not _citation(citation, summary):
            raise RetrospectiveResultError(
                f"process_findings[{index}].evidence"
                f"[{citation_index}] is unresolved"
            )
    if (
        not isinstance(value, dict)
        or set(value) != FINDING_KEYS
        or not is_durable_id(value.get("id"))
        or not _text(value.get("title"))
        or not _text(value.get("impact"))
    ):
        raise RetrospectiveResultError(f"process_findings[{index}] is invalid")


def _validate_proposal(
    value: Any,
    finding_ids: set[str],
    index: int,
) -> None:
    if isinstance(value, dict):
        scope = value.get("scope")
        if not isinstance(scope, str) or scope not in SCOPES:
            raise RetrospectiveResultError(
                f"improvement_proposals[{index}].scope is invalid"
            )
        priority = value.get("priority")
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise RetrospectiveResultError(
                f"improvement_proposals[{index}].priority is invalid"
            )
    addresses = value.get("addresses") if isinstance(value, dict) else None
    if not isinstance(addresses, list) or not 0 < len(addresses) <= COLLECTION_LIMIT:
        raise RetrospectiveResultError(f"improvement_proposals[{index}] is invalid")
    if not all(isinstance(address, str) for address in addresses):
        raise RetrospectiveResultError(f"improvement_proposals[{index}] is invalid")
    if len(set(addresses)) != len(addresses):
        raise RetrospectiveResultError(
            f"improvement_proposals[{index}].addresses contains a duplicate"
        )
    for address_index, finding_id in enumerate(addresses):
        if finding_id not in finding_ids:
            raise RetrospectiveResultError(
                f"improvement_proposals[{index}].addresses"
                f"[{address_index}] is unresolved"
            )
    if (
        not isinstance(value, dict)
        or set(value) != PROPOSAL_KEYS
        or not is_durable_id(value.get("id"))
        or not _text(value.get("title"))
        or not _text(value.get("rationale"))
        or not _text(value.get("suggested_change"))
        or value.get("requires_human_decision") is not True
    ):
        raise RetrospectiveResultError(f"improvement_proposals[{index}] is invalid")


def _citation(value: Any, summary: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    artifact = value.get("artifact")
    if not _artifact(artifact):
        return False
    manifest = summary["citation_manifest"]
    target = manifest.get(artifact)
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "summary_pointer"}
        or target.get("kind") not in {"event", "json", "text"}
        or not isinstance(target.get("summary_pointer"), str)
    ):
        return False
    try:
        source = _resolve_pointer(summary, target["summary_pointer"])
    except (KeyError, ValueError):
        return False
    kind = target["kind"]
    if kind == "event":
        return (
            set(value) == {"artifact", "event_sequence"}
            and type(value["event_sequence"]) is int
            and isinstance(source, list)
            and any(
                isinstance(event, dict)
                and type(event.get("sequence")) is int
                and event["sequence"] == value["event_sequence"]
                for event in source
            )
        )
    if kind == "json":
        if set(value) != {"artifact", "json_pointer"} or not isinstance(
            value["json_pointer"], str
        ):
            return False
        try:
            _resolve_pointer(source, value["json_pointer"])
        except (KeyError, ValueError):
            return False
        return True
    if set(value) not in (
        {"artifact", "line_start"},
        {"artifact", "line_start", "line_end"},
    ):
        return False
    line_start = value.get("line_start")
    line_end = value.get("line_end", line_start)
    line_count = len(source.splitlines()) if isinstance(source, str) else 0
    return (
        type(line_start) is int
        and type(line_end) is int
        and 1 <= line_start <= line_end <= line_count
    )


def _artifact(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > TEXT_CHARACTER_LIMIT:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and str(path) == value


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or len(pointer) > TEXT_CHARACTER_LIMIT:
        raise ValueError("invalid JSON pointer")
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("invalid JSON pointer")
    current = document
    for encoded in pointer[1:].split("/"):
        token = _pointer_token(encoded)
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            if (
                not token.isascii()
                or not token.isdigit()
                or (len(token) > 1 and token.startswith("0"))
            ):
                raise ValueError("invalid array index")
            index = int(token)
            if index >= len(current):
                raise KeyError(token)
            current = current[index]
        else:
            raise KeyError(token)
    return current


def _pointer_token(value: str) -> str:
    decoded = []
    index = 0
    while index < len(value):
        if value[index] != "~":
            decoded.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise ValueError("invalid JSON pointer escape")
        decoded.append("~" if value[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)
