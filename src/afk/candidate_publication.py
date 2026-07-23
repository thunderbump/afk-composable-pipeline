from __future__ import annotations

import re
from typing import Any


EVENT = "candidate.branch_published"
FIELDS = {"repository", "branch", "candidate_sha", "remote"}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def publication(repository: str, branch: str, candidate_sha: str) -> dict[str, str]:
    return {
        "repository": repository,
        "branch": branch,
        "candidate_sha": candidate_sha,
        "remote": "origin",
    }


def event(checkpoint: str, value: dict[str, str]) -> dict[str, Any]:
    return {
        "event": EVENT,
        "state": checkpoint,
        "data": {
            "checkpoint": checkpoint,
            "candidate_publication": value,
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
    publication = data.get("candidate_publication") if isinstance(data, dict) else None
    return (
        value.get("event") == EVENT
        and isinstance(data, dict)
        and set(data) == {"checkpoint", "candidate_publication", "attention"}
        and data.get("checkpoint") == checkpoint
        and value.get("state") == checkpoint
        and data.get("attention") == {}
        and isinstance(publication, dict)
        and set(publication) == FIELDS
        and publication.get("repository") == projection.get("repository")
        and publication.get("branch") == projection.get("branch")
        and publication.get("remote") == "origin"
        and isinstance(publication.get("candidate_sha"), str)
        and bool(SHA_PATTERN.fullmatch(publication["candidate_sha"]))
    )
