from __future__ import annotations

import hashlib
import json
from typing import Any

from afk.jsonutil import canonical_json
from afk.redaction import redact_artifact_value
from afk.run_store import RunStore, RunStoreError


BEAD_SPEC_EVIDENCE = "attempts/start-bead-spec"
BEAD_SPEC_ARTIFACT = f"{BEAD_SPEC_EVIDENCE}/bead.json"


def persist_bead_spec(
    store: RunStore, run_id: str, bead: dict[str, Any]
) -> dict[str, Any]:
    stored = store.write_evidence_value(run_id, BEAD_SPEC_ARTIFACT, bead)
    manifest = store.seal_evidence(run_id, BEAD_SPEC_EVIDENCE)
    record = {
        "schema_version": 1,
        "evidence": BEAD_SPEC_EVIDENCE,
        "manifest_sha256": _manifest_digest(manifest),
    }
    store.append_event(
        run_id,
        "bead.spec_recorded",
        data={"bead_spec": record},
    )
    return stored


def load_bead_spec(
    store: RunStore,
    run_id: str,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = store.status(run_id)
    record = projection.get("bead_spec")
    root = store.root / "runs" / run_id / BEAD_SPEC_EVIDENCE
    if record is None:
        if root.exists():
            if not store.verify_evidence(run_id, BEAD_SPEC_EVIDENCE):
                raise RunStoreError(
                    "canonical Bead/spec evidence could not be verified"
                )
            try:
                recovered_manifest = json.loads(
                    (root / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RunStoreError(
                    "canonical Bead/spec evidence is malformed"
                ) from exc
            record = {
                "schema_version": 1,
                "evidence": BEAD_SPEC_EVIDENCE,
                "manifest_sha256": _manifest_digest(recovered_manifest),
            }
        elif fallback is not None:
            return redact_artifact_value(fallback)
    if not isinstance(record, dict) or set(record) != {
        "schema_version",
        "evidence",
        "manifest_sha256",
    }:
        raise RunStoreError("Run lacks canonical Bead/spec identity")
    if record["schema_version"] != 1 or record["evidence"] != BEAD_SPEC_EVIDENCE:
        raise RunStoreError("canonical Bead/spec identity is invalid")
    if not store.verify_evidence(run_id, BEAD_SPEC_EVIDENCE):
        raise RunStoreError("canonical Bead/spec evidence could not be verified")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        bead = json.loads((root / "bead.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunStoreError("canonical Bead/spec evidence is malformed") from exc
    if record["manifest_sha256"] != _manifest_digest(manifest):
        raise RunStoreError("canonical Bead/spec manifest identity is invalid")
    if (
        not isinstance(bead, dict)
        or bead.get("id") != store.identity(run_id)["bead_id"]
    ):
        raise RunStoreError("canonical Bead/spec does not match the Run")
    return bead


def _manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
