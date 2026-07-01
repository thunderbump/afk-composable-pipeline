from __future__ import annotations

from typing import Any


def deterministic_candidate(candidate_list: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(candidate_list, key=candidate_sort_key)[0]


def deterministic_candidates(candidate_list: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    ordered = sorted(candidate_list, key=candidate_sort_key)
    if limit is None:
        return ordered
    return ordered[:limit]


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, tuple[int, int], str, str]:
    source_type_priority = 0 if candidate.get("source_type") == "beads" else 1
    priority_sort_key = candidate_priority_sort_key(candidate)
    return (
        source_type_priority,
        priority_sort_key,
        str(candidate.get("external_id") or ""),
        str(candidate.get("title") or ""),
    )


def candidate_priority_sort_key(candidate: dict[str, Any]) -> tuple[int, int]:
    if candidate.get("source_type") != "beads":
        return (1, 0)
    priority = candidate.get("priority")
    if isinstance(priority, bool):
        priority = None
    if isinstance(priority, int):
        return (0, priority)
    if isinstance(priority, str):
        stripped = priority.strip()
        if stripped.isdigit():
            return (0, int(stripped))
    return (1, 0)
