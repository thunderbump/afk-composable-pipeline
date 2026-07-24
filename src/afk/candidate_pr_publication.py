from __future__ import annotations

import re
from typing import Any


EVENT = "candidate.pr_published"
FIELDS = {
    "repository",
    "number",
    "url",
    "head_sha",
    "head",
    "base",
    "state",
    "draft",
    "marker",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def marker(run_id: str, candidate_sha: str) -> str:
    return f"<!-- afk-candidate:{run_id}:{candidate_sha} -->"


def event(checkpoint: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": EVENT,
        "state": checkpoint,
        "data": {
            "checkpoint": checkpoint,
            "candidate_pr": value,
            "attention": {},
        },
    }


def valid_event(
    value: dict[str, Any],
    *,
    projection: dict[str, Any],
    checkpoint: str,
) -> bool:
    data = value.get("data")
    publication = data.get("candidate_pr") if isinstance(data, dict) else None
    candidate_sha = projection.get("candidate_sha")
    return (
        value.get("event") == EVENT
        and isinstance(data, dict)
        and set(data) == {"checkpoint", "candidate_pr", "attention"}
        and data.get("checkpoint") == checkpoint
        and value.get("state") == checkpoint
        and data.get("attention") == {}
        and isinstance(publication, dict)
        and set(publication) == FIELDS
        and publication.get("repository") == projection.get("repository")
        and type(publication.get("number")) is int
        and publication["number"] > 0
        and isinstance(publication.get("url"), str)
        and bool(publication["url"])
        and isinstance(candidate_sha, str)
        and bool(SHA_PATTERN.fullmatch(candidate_sha))
        and publication.get("head_sha") == candidate_sha
        and publication.get("head") == projection.get("branch")
        and publication.get("base") == projection.get("base_branch")
        and publication.get("state") == "OPEN"
        and publication.get("draft") is True
        and publication.get("marker")
        == marker(projection.get("run_id", ""), candidate_sha)
    )
