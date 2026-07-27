from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Collection

from afk.jsonutil import sha256_json


ARTIFACT_INVENTORY_LIMIT = 32
TEXT_CHARACTER_LIMIT = 512
TRUNCATION_SUFFIX = "…[TRUNCATED]"
INVENTORY_KEY = "_retrospective_inventory"
INVENTORY_SCHEMA_VERSION = 1
EPISODE_EVENTS = {"run.attention_required", "run.completed"}
EFFECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RetrospectiveContractError(ValueError):
    pass


def is_episode_event(event: str) -> bool:
    return event in EPISODE_EVENTS


def attach_inventory(data: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    if INVENTORY_KEY in data:
        raise RetrospectiveContractError("retrospective inventory is reserved")
    return {**data, INVENTORY_KEY: inventory}


def capture_inventory(
    *,
    through_sequence: int,
    effects: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_effects = sorted(effects, key=lambda record: record["effect_id"])
    ordered_evidence = sorted(evidence, key=lambda record: record["unit"])
    selected_effects = ordered_effects[:ARTIFACT_INVENTORY_LIMIT]
    selected_evidence = ordered_evidence[:ARTIFACT_INVENTORY_LIMIT]
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "through_sequence": through_sequence,
        "effects": [
            {
                "effect_id": record["effect_id"],
                "kind": bounded_text(record["kind"]),
                "status": record["status"],
            }
            for record in selected_effects
        ],
        "evidence": [
            {
                "unit": record["unit"],
                "manifest_sha256": manifest_digest(record["manifest"]),
            }
            for record in selected_evidence
        ],
        "omitted": {
            "effects": len(ordered_effects) - len(selected_effects),
            "evidence_units": len(ordered_evidence) - len(selected_evidence),
        },
    }


def decode_inventory(
    value: Any,
    *,
    sequence: int,
    evidence_roots: Collection[str],
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "through_sequence",
            "effects",
            "evidence",
            "omitted",
        }
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != INVENTORY_SCHEMA_VERSION
        or type(value.get("through_sequence")) is not int
        or value["through_sequence"] != sequence
        or not isinstance(value.get("effects"), list)
        or len(value["effects"]) > ARTIFACT_INVENTORY_LIMIT
        or not isinstance(value.get("evidence"), list)
        or len(value["evidence"]) > ARTIFACT_INVENTORY_LIMIT
        or not isinstance(value.get("omitted"), dict)
        or set(value["omitted"]) != {"effects", "evidence_units"}
        or any(
            type(count) is not int or count < 0 for count in value["omitted"].values()
        )
    ):
        raise RetrospectiveContractError("retrospective inventory is invalid")

    effect_ids = []
    for record in value["effects"]:
        if (
            not isinstance(record, dict)
            or set(record) != {"effect_id", "kind", "status"}
            or not isinstance(record["effect_id"], str)
            or EFFECT_ID_PATTERN.fullmatch(record["effect_id"]) is None
            or not isinstance(record["kind"], str)
            or not record["kind"].strip()
            or not _valid_bounded_text(record["kind"])
            or not isinstance(record["status"], str)
            or record["status"] not in {"prepared", "confirmed"}
        ):
            raise RetrospectiveContractError("retrospective inventory is invalid")
        effect_ids.append(record["effect_id"])

    evidence_units = []
    for record in value["evidence"]:
        unit = record.get("unit") if isinstance(record, dict) else None
        parts = Path(unit).parts if isinstance(unit, str) else ()
        if (
            not isinstance(record, dict)
            or set(record) != {"unit", "manifest_sha256"}
            or len(parts) != 2
            or parts[0] not in evidence_roots
            or not parts[1]
            or not isinstance(record["manifest_sha256"], str)
            or SHA256_PATTERN.fullmatch(record["manifest_sha256"]) is None
        ):
            raise RetrospectiveContractError("retrospective inventory is invalid")
        evidence_units.append(unit)

    if effect_ids != sorted(set(effect_ids)) or evidence_units != sorted(
        set(evidence_units)
    ):
        raise RetrospectiveContractError("retrospective inventory is invalid")
    return value


def manifest_digest(manifest: Any) -> str:
    return sha256_json(manifest)


def bounded_text(value: str) -> str:
    if len(value) <= TEXT_CHARACTER_LIMIT:
        return value
    return f"{value[:TEXT_CHARACTER_LIMIT]}{TRUNCATION_SUFFIX}"


def _valid_bounded_text(value: str) -> bool:
    return len(value) <= TEXT_CHARACTER_LIMIT or (
        len(value) == TEXT_CHARACTER_LIMIT + len(TRUNCATION_SUFFIX)
        and value.endswith(TRUNCATION_SUFFIX)
    )
