from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import get_ident
from typing import Any, Callable, Iterator, Literal, NamedTuple

from afk.durable_id import is_durable_id
from afk.jsonutil import canonical_json
from afk.redaction import redact_artifact_value, redact_text
from afk.retrospective_contract import (
    INVENTORY_KEY,
    RetrospectiveContractError,
    attach_inventory,
    capture_inventory,
    capture_unavailable_inventory,
    decode_inventory,
    is_episode_event,
    manifest_digest,
    select_inventory_items,
)
from afk.resume_preflight import validate_open_attempts


SCHEMA_VERSION = 1
RUN_IDENTITY_SCHEMA_VERSION = 2
EVIDENCE_RECEIPT_VERSION = 1
STREAM_BYTE_LIMIT = 64 * 1024 * 1024
ATTEMPT_BYTE_LIMIT = 256 * 1024 * 1024
GATE_BYTE_LIMIT = 512 * 1024 * 1024
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_ROOTS = {"attempts", "gates", "retrospective"}


class RunStoreError(RuntimeError):
    pass


class _RetrospectiveInventoryUnavailable(RunStoreError):
    pass


class RunStoreBusy(RunStoreError):
    pass


class ActiveRunExists(RunStoreError):
    pass


class RunNotFound(RunStoreError):
    pass


class EventHistoryCorrupt(RunStoreError):
    pass


class EvidenceError(RunStoreError):
    pass


class EvidenceTooLarge(EvidenceError):
    pass


class EvidenceTampered(EvidenceError):
    pass


class ProjectedEvidenceTampered(EvidenceTampered):
    pass


class ResumePreflightInvalid(EventHistoryCorrupt):
    pass


class _VerifiedEvidence(NamedTuple):
    manifest_digest: str
    payload_bytes: dict[str, bytes]

    @property
    def result_bytes(self) -> bytes | None:
        return self.payload_bytes.get("result.json")


class _OpenEvidenceEntry(NamedTuple):
    path: str
    descriptor: int


class _OpenEvidenceTree(NamedTuple):
    files: tuple[_OpenEvidenceEntry, ...]
    directories: tuple[_OpenEvidenceEntry, ...]


def default_state_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "afk"
    return Path.home() / ".local" / "state" / "afk"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_durable_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = root or default_state_root()
        self._lock_descriptor: int | None = None
        self._lock_owner: int | None = None

    @contextmanager
    def lock(self, *, validate_root_permissions: bool = False) -> Iterator[None]:
        owner = get_ident()
        if self._lock_descriptor is not None:
            if self._lock_owner != owner:
                raise RunStoreBusy("another AFK mutator holds the global lock")
            yield
            return

        root_descriptor = None
        lock_created = False
        if validate_root_permissions:
            try:
                root_descriptor = os.open(
                    self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            except FileNotFoundError:
                _secure_directory(self.root)
                root_descriptor = os.open(
                    self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            except OSError as exc:
                raise EventHistoryCorrupt("Run Store directory is invalid") from exc
            metadata = os.fstat(root_descriptor)
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                os.close(root_descriptor)
                raise EventHistoryCorrupt("Run Store directory permissions are invalid")
            strict_flags = (
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(
                    "afk.lock",
                    strict_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root_descriptor,
                )
                lock_created = True
            except FileExistsError:
                try:
                    descriptor = os.open(
                        "afk.lock", strict_flags, dir_fd=root_descriptor
                    )
                except OSError as exc:
                    os.close(root_descriptor)
                    raise EventHistoryCorrupt("AFK lock file is invalid") from exc
            except OSError as exc:
                os.close(root_descriptor)
                raise EventHistoryCorrupt("AFK lock file is invalid") from exc
            try:
                lock_metadata = os.fstat(descriptor)
            except OSError as exc:
                os.close(descriptor)
                os.close(root_descriptor)
                raise EventHistoryCorrupt("AFK lock file is invalid") from exc
            if not stat.S_ISREG(lock_metadata.st_mode):
                os.close(descriptor)
                os.close(root_descriptor)
                raise EventHistoryCorrupt("AFK lock file is invalid")
            if not lock_created and stat.S_IMODE(lock_metadata.st_mode) != 0o600:
                os.close(descriptor)
                os.close(root_descriptor)
                raise EventHistoryCorrupt("AFK lock file permissions are invalid")
        else:
            _secure_directory(self.root)
            descriptor = os.open(self.root / "afk.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not validate_root_permissions or lock_created:
                os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunStoreBusy("another AFK mutator holds the global lock") from exc
            self._lock_descriptor = descriptor
            self._lock_owner = owner
            yield
        finally:
            if self._lock_descriptor == descriptor:
                self._lock_descriptor = None
                self._lock_owner = None
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            else:
                os.close(descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    def create_run(
        self,
        *,
        bead_id: str,
        repository: str,
        base_branch: str,
        base_sha: str,
        start_request: dict[str, Any] | None = None,
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or new_durable_run_id()
        created_at = created_at or utc_now()
        _validate_run_id(run_id)
        if not bead_id.strip():
            raise RunStoreError("bead_id must not be empty")
        if not repository.strip():
            raise RunStoreError("repository must not be empty")
        if not base_branch.strip():
            raise RunStoreError("base_branch must not be empty")
        if not SHA_PATTERN.fullmatch(base_sha):
            raise RunStoreError("base_sha must be a 40-character lowercase Git SHA")

        with self.lock():
            active = self._active_run_id() or self._active_pointer_run_id(
                invalid_is_error=True
            )
            if active is not None:
                raise ActiveRunExists(f"Active Run already exists: {active}")

            run_dir = self._run_dir(run_id)
            try:
                run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise RunStoreError(f"Run already exists: {run_id}") from exc
            os.chmod(run_dir, 0o700)
            _fsync_directory(run_dir.parent)
            for name in ("attempts", "effects", "gates", "retrospective"):
                _secure_directory(run_dir / name)

            identity = redact_artifact_value(
                {
                    "schema_version": RUN_IDENTITY_SCHEMA_VERSION,
                    "run_id": run_id,
                    "bead_id": bead_id,
                    "repository": repository,
                    "base_branch": base_branch,
                    "base_sha": base_sha,
                    "created_at": created_at,
                    "start_request": start_request or {},
                    "evidence_receipt_version": EVIDENCE_RECEIPT_VERSION,
                }
            )
            _write_new_json(run_dir / "run.json", identity, self.root)
            events_path = run_dir / "events.jsonl"
            _write_new_bytes(events_path, b"", self.root)
            projection = self._append_event_unlocked(
                run_id,
                "run.created",
                state="created",
                data={"bead_id": bead_id},
                recorded_at=created_at,
            )
            _atomic_json(self.root / "active.json", {"run_id": run_id})
            return projection

    def append_event(
        self,
        run_id: str,
        event: str,
        *,
        state: str | None = None,
        data: dict[str, Any] | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        with self.lock():
            projection = self._append_event_unlocked(
                run_id,
                event,
                state=state,
                data=data,
                recorded_at=recorded_at,
            )
            if projection["state"] == "completed" and self._active_run_id() is None:
                self._clear_active_pointer(run_id)
            return projection

    def record_completion_episode(
        self,
        run_id: str,
        *,
        completion: dict[str, Any],
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Record Completed while retaining ownership through its retrospective."""
        with self.lock():
            projection = self.status(run_id)
            if projection["state"] == "completed":
                if (
                    projection.get("completion") == redact_artifact_value(completion)
                    and self._validated_completion_episode(run_id, projection)
                    is not None
                ):
                    return projection
                raise RunStoreError("Run already has a different completion")
            episode_sequence = projection["last_sequence"] + 1
            completion_episode = {
                "schema_version": 1,
                "episode_sequence": episode_sequence,
                "evidence": f"retrospective/completed-{episode_sequence}",
                "effect_id": f"retrospective-analysis-{episode_sequence}",
            }
            return self.append_event(
                run_id,
                "run.completed",
                state="completed",
                data={
                    "checkpoint": "completed",
                    "attention": {},
                    "completion": completion,
                    "completion_episode": completion_episode,
                },
                recorded_at=recorded_at,
            )

    def record_attention_episode(
        self,
        run_id: str,
        *,
        checkpoint: str,
        attention: dict[str, Any],
        details: dict[str, Any] | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Record one distinct transition into Attention Required."""
        with self.lock():
            projection = self.status(run_id)
            payload = redact_artifact_value(
                {
                    "checkpoint": checkpoint,
                    "attention": attention,
                    **(details or {}),
                }
            )
            episode = self._validated_attention_episode(run_id, projection)
            if projection["state"] == "attention_required" and episode is not None:
                events, _ = self._read_events(run_id)
                later = [
                    event
                    for event in events
                    if event["sequence"] > episode["episode_sequence"]
                ]
                continuous = not any("state" in event for event in later)
                uncontradicted = all(
                    not isinstance(event.get("data"), dict)
                    or all(
                        key not in event["data"] or event["data"][key] == value
                        for key, value in payload.items()
                    )
                    for event in later
                )
                event = self.event(run_id, episode["episode_sequence"])
                observed = dict(event["data"])
                observed.pop(INVENTORY_KEY, None)
                if (
                    continuous
                    and uncontradicted
                    and observed
                    == {
                        **payload,
                        "attention_episode": episode,
                    }
                ):
                    return projection

            episode_sequence = projection["last_sequence"] + 1
            attention_episode = {
                "schema_version": 1,
                "episode_sequence": episode_sequence,
                "evidence": f"retrospective/attention-{episode_sequence}",
                "effect_id": f"retrospective-analysis-{episode_sequence}",
            }
            return self.append_event(
                run_id,
                "run.attention_required",
                state="attention_required",
                data={**payload, "attention_episode": attention_episode},
                recorded_at=recorded_at,
            )

    def record_completion_finalization(
        self,
        run_id: str,
        *,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind an exactly reconciled retrospective to its completion episode."""
        with self.lock():
            projection = self.status(run_id)
            episode = self._validated_completion_episode(run_id, projection)
            if projection["state"] != "completed" or episode is None:
                raise RunStoreError("Run has no pending completion episode")
            sealed = self.sealed_evidence_result(run_id, episode["evidence"])
            if sealed != redact_artifact_value(outcome):
                raise RunStoreError(
                    "completion retrospective outcome contradicts sealed evidence"
                )
            expected = self._expected_completion_finalization(
                run_id,
                episode,
                outcome=sealed,
            )
            path = self._run_dir(run_id) / "completion-finalization.json"
            if path.exists() or path.is_symlink():
                observed = self._read_completion_finalization(run_id)
                if observed != expected:
                    raise EventHistoryCorrupt("completion finalization is invalid")
                return observed
            _atomic_json(path, expected)
            return expected

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        selected = run_id or self._active_run_id()
        if selected is None:
            selected = self._active_pointer_run_id(invalid_is_error=True)
            if selected is None:
                raise RunNotFound("no Active Run")
        _validate_run_id(selected)
        identity = self._identity(selected)
        events, _ = self._read_events(selected)
        return _project(identity, events)

    def active_run_id(self) -> str | None:
        return self._active_run_id()

    def validated_attention_episode(
        self,
        run_id: str,
        projection: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return the projected Attention episode after validating its provenance."""
        if projection.get("run_id") != run_id:
            raise EventHistoryCorrupt("attention episode marker is invalid")
        return self._validated_attention_episode(run_id, projection)

    def resume_status(self) -> dict[str, Any]:
        with self.lock(validate_root_permissions=True):
            projection, active_run_id = self._validated_resume_context()
            if active_run_id is not None and active_run_id != projection["run_id"]:
                raise EventHistoryCorrupt(
                    "Active Run pointer does not match Event History"
                )
            if (
                projection["state"] == "completed"
                and active_run_id == projection["run_id"]
            ):
                if projection.get("completion_episode") is None:
                    self._clear_active_pointer(projection["run_id"])
                return projection
            _atomic_json(self._run_dir(projection["run_id"]) / "state.json", projection)
            if active_run_id is None:
                _atomic_json(
                    self.root / "active.json", {"run_id": projection["run_id"]}
                )
            return projection

    def resume_completed_status(self, run_id: str) -> dict[str, Any]:
        """Validate a named Completed Run and reconcile a stale legacy pointer."""
        with self.lock(validate_root_permissions=True):
            projection, active_run_id = self._validated_resume_context(run_id)
            if projection["state"] != "completed":
                raise RunStoreError(
                    "named resume is only available for a completed Run"
                )
            episode = self._validated_completion_episode(run_id, projection)
            finalized = self._completion_episode_finalized(run_id, projection)
            if (
                episode is not None
                and not finalized
                and active_run_id not in {None, run_id}
            ):
                raise EventHistoryCorrupt(
                    "Active Run pointer does not match pending completion"
                )
            if projection.get("completion_episode") is None and active_run_id == run_id:
                self._clear_active_pointer(run_id)
            return projection

    def finalize_completion_episode(self, run_id: str) -> dict[str, Any]:
        with self.lock(validate_root_permissions=True):
            projection, active_run_id = self._validated_resume_context(run_id)
            episode = self._validated_completion_episode(run_id, projection)
            if projection["state"] != "completed" or episode is None:
                raise RunStoreError("Run has no pending completion episode")
            if not self._completion_episode_finalized(run_id, projection):
                raise RunStoreError(
                    "completion retrospective is not sealed and confirmed for the Run"
                )
            if active_run_id == run_id:
                self._clear_active_pointer(run_id)
            return projection

    def _validated_resume_context(
        self,
        run_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        _require_mode(self.root, 0o700, "Run Store directory")
        active_path = self.root / "active.json"
        if active_path.exists() or active_path.is_symlink():
            _require_mode(active_path, 0o600, "Active Run pointer")
        active_run_id = self._active_pointer_run_id(invalid_is_error=True)
        projection = self.status(run_id)
        self.validated_attention_episode(projection["run_id"], projection)
        events = self._validate_resume_projection(projection)
        invalid = validate_open_attempts(projection, events)
        if invalid is not None:
            raise ResumePreflightInvalid(invalid)
        return projection, active_run_id

    def identity(self, run_id: str) -> dict[str, Any]:
        return self._identity(run_id)

    def event(self, run_id: str, sequence: int) -> dict[str, Any]:
        """Read one validated Event History record by its durable sequence."""
        return self._read_events_through(
            run_id,
            sequence,
            parameter_name="sequence",
        )[-1]

    def event_history(self, run_id: str) -> list[dict[str, Any]]:
        """Return the complete validated Event History without mutating the Run."""
        with self._open_observation_run(run_id) as run_descriptor:
            self._identity_at(run_descriptor, run_id)
            events, _ = self._read_events_at(run_descriptor, run_id)
            return events

    def read_run_snapshot(
        self, run_id: str, *, through_sequence: int
    ) -> dict[str, Any]:
        """Read validated durable facts available for one Event History position."""
        selected_events = self._read_events_through(
            run_id,
            through_sequence,
            parameter_name="through_sequence",
        )
        identity = self._identity(run_id)
        effects, evidence, artifact_omitted = self._read_retrospective_inventory(
            run_id,
            selected_events[-1],
        )
        return {
            "identity": identity,
            "events": selected_events,
            "projection": _project(identity, selected_events),
            "effects": effects,
            "evidence": evidence,
            "artifact_omitted": artifact_omitted,
        }

    def prepare_effect(
        self,
        run_id: str,
        effect_id: str,
        *,
        kind: str,
        intended: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_run_id(effect_id)
        record = redact_artifact_value(
            {
                "schema_version": SCHEMA_VERSION,
                "effect_id": effect_id,
                "kind": kind,
                "status": "prepared",
                "intended": intended,
            }
        )
        with self.lock():
            path = self._run_dir(run_id) / "effects" / f"{effect_id}.json"
            if path.exists():
                existing = self.effect(run_id, effect_id)
                if (
                    existing["kind"] != kind
                    or existing["intended"] != record["intended"]
                ):
                    raise RunStoreError(f"Effect identity conflict: {effect_id}")
                return existing
            _write_new_json(path, record, self.root)
            return record

    def effect(self, run_id: str, effect_id: str) -> dict[str, Any]:
        _validate_run_id(effect_id)
        path = self._run_dir(run_id) / "effects" / f"{effect_id}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunStoreError(f"Effect is missing or invalid: {effect_id}") from exc
        if not isinstance(record, dict):
            raise RunStoreError(f"Effect is invalid: {effect_id}")
        status = record.get("status")
        expected_keys = {
            "schema_version",
            "effect_id",
            "kind",
            "status",
            "intended",
        }
        if status == "confirmed":
            expected_keys.add("observed")
        if (
            set(record) != expected_keys
            or type(record.get("schema_version")) is not int
            or record["schema_version"] != SCHEMA_VERSION
            or record.get("effect_id") != effect_id
            or not isinstance(record.get("kind"), str)
            or not record["kind"].strip()
            or status not in {"prepared", "confirmed"}
            or not isinstance(record.get("intended"), dict)
            or (status == "confirmed" and not isinstance(record.get("observed"), dict))
        ):
            raise RunStoreError(f"Effect is invalid: {effect_id}")
        return record

    def effect_if_present(self, run_id: str, effect_id: str) -> dict[str, Any] | None:
        _validate_run_id(effect_id)
        path = self._run_dir(run_id) / "effects" / f"{effect_id}.json"
        if not path.exists() and not path.is_symlink():
            return None
        return self.effect(run_id, effect_id)

    def confirm_effect(
        self, run_id: str, effect_id: str, *, observed: dict[str, Any]
    ) -> dict[str, Any]:
        with self.lock():
            record = self.effect(run_id, effect_id)
            if record["status"] == "confirmed":
                if record.get("observed") != redact_artifact_value(observed):
                    raise RunStoreError(f"Effect observation conflict: {effect_id}")
                return record
            confirmed = {
                **record,
                "status": "confirmed",
                "observed": redact_artifact_value(observed),
            }
            _atomic_json(
                self._run_dir(run_id) / "effects" / f"{effect_id}.json",
                confirmed,
            )
            return confirmed

    def write_evidence_text(self, run_id: str, relative_path: str, value: str) -> Path:
        durable_path = self._run_dir(run_id) / _canonical_evidence_relative(
            relative_path
        )
        with self.lock():
            with self._open_evidence_file(
                run_id,
                relative_path,
                missing_ok=False,
                create_missing=True,
            ) as evidence_file:
                if evidence_file is None:
                    raise EvidenceError("evidence file path is invalid")
                relative_path, unit_descriptor, parent_descriptor, name = evidence_file
                if len(Path(relative_path).parts) == 3 and name == "manifest.json":
                    raise EvidenceError("manifest.json is reserved")
                if _entry_exists_at(unit_descriptor, "manifest.json"):
                    raise EvidenceError("completed evidence is read-only")
                encoded = redact_text(value).encode("utf-8")
                if _is_stream(Path(relative_path)) and len(encoded) > STREAM_BYTE_LIMIT:
                    raise EvidenceTooLarge(
                        f"evidence stream exceeds {STREAM_BYTE_LIMIT} bytes"
                    )
                _write_new_bytes_at(
                    parent_descriptor,
                    name,
                    encoded,
                    self.root,
                )
                return durable_path

    def write_evidence_value(self, run_id: str, relative_path: str, value: Any) -> Any:
        """Redact and persist one canonical structured evidence value."""
        with self.lock():
            with self._open_evidence_file(
                run_id,
                relative_path,
                missing_ok=False,
                create_missing=True,
            ) as evidence_file:
                if evidence_file is None:
                    raise EvidenceError("evidence file path is invalid")
                relative_path, unit_descriptor, parent_descriptor, name = evidence_file
                if len(Path(relative_path).parts) == 3 and name == "manifest.json":
                    raise EvidenceError("manifest.json is reserved")
                if _entry_exists_at(unit_descriptor, "manifest.json"):
                    raise EvidenceError("completed evidence is read-only")
                redacted = redact_artifact_value(value)
                encoded = (canonical_json(redacted) + "\n").encode("utf-8")
                _write_new_bytes_at(
                    parent_descriptor,
                    name,
                    encoded,
                    self.root,
                )
                return redacted

    def ingest_evidence_file(
        self, run_id: str, relative_path: str, source_path: Path
    ) -> Path:
        if source_path.is_symlink() or not source_path.is_file():
            raise EvidenceError("evidence source must be a regular file")
        source_size = source_path.stat().st_size
        target = Path(relative_path)
        if _is_stream(target) and source_size > STREAM_BYTE_LIMIT:
            raise EvidenceTooLarge(f"evidence stream exceeds {STREAM_BYTE_LIMIT} bytes")
        if source_size > _tree_limit(relative_path):
            raise EvidenceTooLarge(
                f"evidence tree exceeds {_tree_limit(relative_path)} bytes"
            )
        try:
            value = source_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError("evidence must be regular UTF-8 text") from exc
        return self.write_evidence_text(run_id, relative_path, value)

    def seal_evidence(self, run_id: str, relative_directory: str) -> dict[str, Any]:
        with self.lock():
            relative_directory = _canonical_evidence_relative(relative_directory)
            with self._open_evidence_directory(
                run_id,
                relative_directory,
                missing_ok=True,
            ) as evidence_directory:
                if evidence_directory is None:
                    raise EvidenceError(
                        f"evidence directory does not exist: {relative_directory}"
                    )
                (
                    relative_directory,
                    run_descriptor,
                    evidence_descriptor,
                    parent_descriptor,
                ) = evidence_directory
                directory = Path(f"/proc/self/fd/{evidence_descriptor}")
                manifest_path = directory / "manifest.json"
                if manifest_path.exists() or manifest_path.is_symlink():
                    raise EvidenceError("evidence is already sealed")
                with _open_evidence_tree(directory) as tree:
                    limit = _tree_limit(relative_directory)
                    _validate_evidence_sizes(tree.files, limit)
                    entries = _manifest_entries(tree.files, limit)
                    manifest = {
                        "schema_version": SCHEMA_VERSION,
                        "files": entries,
                        "total_bytes": sum(entry["bytes"] for entry in entries),
                    }
                    for entry in tree.files:
                        os.fchmod(entry.descriptor, 0o400)
                        os.fsync(entry.descriptor)
                    for entry in reversed(tree.directories):
                        os.fchmod(entry.descriptor, 0o500)
                        os.fsync(entry.descriptor)
                    _write_new_json(
                        manifest_path,
                        manifest,
                        self.root,
                        mode=0o400,
                    )
                os.fchmod(evidence_descriptor, 0o500)
                os.fsync(evidence_descriptor)
                os.fsync(parent_descriptor)
                verified = self._verify_evidence_directory(
                    directory,
                    relative_directory,
                    payload_capture="none",
                )
                self._publish_evidence_receipt_at(
                    run_descriptor,
                    relative_directory,
                    verified.manifest_digest,
                )
                return manifest

    def verify_evidence(self, run_id: str, relative_directory: str) -> bool:
        relative_directory = _canonical_evidence_relative(relative_directory)
        with self._open_evidence_directory(
            run_id,
            relative_directory,
            missing_ok=False,
        ) as evidence_directory:
            if evidence_directory is None:
                raise EvidenceTampered(
                    f"evidence directory does not exist: {relative_directory}"
                )
            relative_directory, _, evidence_descriptor, _ = evidence_directory
            directory = Path(f"/proc/self/fd/{evidence_descriptor}")
            self._verify_evidence_directory(
                directory,
                relative_directory,
                payload_capture="none",
            )
        return True

    def _verify_evidence_directory(
        self,
        directory: Path,
        relative_directory: str,
        *,
        payload_capture: Literal["none", "result", "all"],
    ) -> _VerifiedEvidence:
        manifest_path = directory / "manifest.json"
        manifest_descriptor = -1
        manifest_mode = 0
        try:
            manifest_descriptor = os.open(
                manifest_path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            manifest_metadata = os.fstat(manifest_descriptor)
            manifest_mode = stat.S_IMODE(manifest_metadata.st_mode)
            if not stat.S_ISREG(manifest_metadata.st_mode):
                raise EvidenceTampered("evidence manifest is invalid")
            with os.fdopen(os.dup(manifest_descriptor), encoding="utf-8") as stream:
                manifest = json.load(stream)
        except EvidenceTampered:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceTampered("evidence manifest is missing or invalid") from exc
        finally:
            if manifest_descriptor >= 0:
                os.close(manifest_descriptor)
        expected = _validate_manifest(manifest)
        try:
            with _open_evidence_tree(directory) as tree:
                observed, payload_bytes = _manifest_snapshot(
                    tree.files,
                    _tree_limit(relative_directory),
                    payload_capture=payload_capture,
                )
                file_modes = [
                    stat.S_IMODE(os.fstat(entry.descriptor).st_mode)
                    for entry in tree.files
                ]
                directory_modes = [
                    stat.S_IMODE(os.fstat(entry.descriptor).st_mode)
                    for entry in tree.directories
                ]
        except EvidenceError as exc:
            raise EvidenceTampered(str(exc)) from exc
        if observed != expected:
            raise EvidenceTampered("evidence does not match its manifest")
        total_bytes = manifest.get("total_bytes")
        if total_bytes != sum(entry["bytes"] for entry in observed):
            raise EvidenceTampered("evidence manifest total is invalid")
        modes = [
            *file_modes,
            manifest_mode,
            stat.S_IMODE(directory.stat().st_mode),
            *directory_modes,
        ]
        if any(mode & 0o222 for mode in modes):
            raise EvidenceTampered("sealed evidence is writable")
        return _VerifiedEvidence(_canonical_sha256(manifest), payload_bytes)

    def reconcile_evidence_result(
        self, run_id: str, relative_directory: str, value: Any
    ) -> Any:
        """Recover or verify one evidence result, then seal its evidence unit."""
        with self.lock():
            relative_directory = _canonical_evidence_relative(relative_directory)
            expected = redact_artifact_value(value)
            with self._open_observation_run(run_id) as run_descriptor:
                self._identity_at(run_descriptor, run_id)
                with self._open_observation_evidence(
                    run_descriptor,
                    relative_directory,
                    missing_ok=False,
                    create_missing=True,
                ) as evidence_descriptors:
                    if evidence_descriptors is None:
                        raise EvidenceError(
                            f"evidence directory does not exist: {relative_directory}"
                        )
                    evidence_descriptor, parent_descriptor = evidence_descriptors
                    try:
                        manifest_metadata = os.stat(
                            "manifest.json",
                            dir_fd=evidence_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        manifest_metadata = None
                    if manifest_metadata is not None:
                        if not stat.S_ISREG(manifest_metadata.st_mode):
                            raise EvidenceTampered("evidence manifest is invalid")
                        verified = self._verify_or_finish_seal_at(
                            run_descriptor,
                            evidence_descriptor,
                            parent_descriptor,
                            relative_directory,
                            payload_capture="result",
                        )
                        stored = _read_evidence_result_bytes(verified.result_bytes)
                        if stored != expected:
                            raise EvidenceError(
                                "evidence result contradicts expected value"
                            )
                        return stored

                    entries = set(os.listdir(evidence_descriptor))
                    if entries not in (set(), {"result.json"}):
                        raise EvidenceError("unsealed evidence result is ambiguous")
                    if "result.json" in entries:
                        stored = _read_evidence_result_at(
                            evidence_descriptor, "result.json"
                        )
                        if stored != expected:
                            raise EvidenceError(
                                "evidence result contradicts expected value"
                            )
                    else:
                        _write_new_json_at(
                            evidence_descriptor,
                            "result.json",
                            expected,
                            self.root,
                        )
                    verified = self._seal_reconciled_result_at(
                        run_descriptor,
                        evidence_descriptor,
                        parent_descriptor,
                        relative_directory,
                    )
                    stored = _read_evidence_result_bytes(verified.result_bytes)
                    if stored != expected:
                        raise EvidenceError(
                            "evidence result contradicts expected value"
                        )
                    return stored

    def _seal_reconciled_result_at(
        self,
        run_descriptor: int,
        evidence_descriptor: int,
        parent_descriptor: int,
        relative_directory: str,
    ) -> _VerifiedEvidence:
        directory = Path(f"/proc/self/fd/{evidence_descriptor}")
        with _open_evidence_tree(directory) as tree:
            if [entry.path for entry in tree.files] != ["result.json"]:
                raise EvidenceError("unsealed evidence result is ambiguous")
            limit = _tree_limit(relative_directory)
            _validate_evidence_sizes(tree.files, limit)
            entries = _manifest_entries(tree.files, limit)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "files": entries,
            "total_bytes": sum(entry["bytes"] for entry in entries),
        }
        try:
            result_descriptor = os.open(
                "result.json",
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=evidence_descriptor,
            )
        except OSError as exc:
            raise EvidenceError("evidence result is missing or malformed") from exc
        try:
            metadata = os.fstat(result_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise EvidenceError("evidence result is missing or malformed")
            os.fchmod(result_descriptor, 0o400)
            os.fsync(result_descriptor)
        finally:
            os.close(result_descriptor)
        _write_new_json_at(
            evidence_descriptor,
            "manifest.json",
            manifest,
            self.root,
            mode=0o400,
        )
        os.fchmod(evidence_descriptor, 0o500)
        os.fsync(evidence_descriptor)
        os.fsync(parent_descriptor)
        verified = self._verify_evidence_directory(
            directory,
            relative_directory,
            payload_capture="result",
        )
        self._publish_evidence_receipt_at(
            run_descriptor,
            relative_directory,
            verified.manifest_digest,
        )
        return verified

    def sealed_evidence_result(
        self, run_id: str, relative_directory: str
    ) -> Any | None:
        """Return a verified sealed result, or None when it has not been sealed."""
        with self.lock():
            relative_directory = _canonical_evidence_relative(relative_directory)
            with self._open_observation_run(run_id) as run_descriptor:
                self._identity_at(run_descriptor, run_id)
                with self._open_observation_evidence(
                    run_descriptor,
                    relative_directory,
                    missing_ok=True,
                ) as evidence_descriptors:
                    if evidence_descriptors is None:
                        return None
                    evidence_descriptor, parent_descriptor = evidence_descriptors
                    directory = Path(f"/proc/self/fd/{evidence_descriptor}")
                    manifest_path = directory / "manifest.json"
                    if manifest_path.is_symlink() or (
                        manifest_path.exists() and not manifest_path.is_file()
                    ):
                        raise EvidenceTampered("evidence manifest is invalid")
                    if not manifest_path.is_file():
                        return None
                    verified = self._verify_or_finish_seal_at(
                        run_descriptor,
                        evidence_descriptor,
                        parent_descriptor,
                        relative_directory,
                        payload_capture="result",
                    )
                    return _read_evidence_result_bytes(verified.result_bytes)

    def observe_sealed_evidence_result(
        self, run_id: str, relative_directory: str
    ) -> Any | None:
        """Read a fully sealed result without completing or repairing its seal."""
        relative_directory = _canonical_evidence_relative(relative_directory)
        with self._open_observation_run(run_id) as run_descriptor:
            identity = self._identity_at(run_descriptor, run_id)
            return self._observe_sealed_evidence_result_at(
                run_descriptor,
                identity,
                relative_directory,
            )

    @contextmanager
    def retrospective_observation(
        self, run_id: str
    ) -> Iterator[tuple[list[dict[str, Any]], Callable[[str], Any | None]]]:
        """Hold one validated Run while observing its retrospective episodes."""
        with self._open_observation_run(run_id) as run_descriptor:
            identity = self._identity_at(run_descriptor, run_id)
            events, _ = self._read_events_at(run_descriptor, run_id)

            def observe(relative_directory: str) -> Any | None:
                return self._observe_sealed_evidence_result_at(
                    run_descriptor,
                    identity,
                    _canonical_evidence_relative(relative_directory),
                )

            yield events, observe

    def _observe_sealed_evidence_result_at(
        self,
        run_descriptor: int,
        identity: dict[str, Any],
        relative_directory: str,
    ) -> Any | None:
        receipt = self._read_evidence_receipt_at(run_descriptor, relative_directory)
        if (
            receipt is None
            and identity.get("evidence_receipt_version") == EVIDENCE_RECEIPT_VERSION
        ):
            return None
        with self._open_observation_evidence(
            run_descriptor,
            relative_directory,
            missing_ok=receipt is None,
        ) as evidence_descriptors:
            if evidence_descriptors is None:
                return None
            evidence_descriptor, _ = evidence_descriptors
            directory = Path(f"/proc/self/fd/{evidence_descriptor}")
            manifest_path = directory / "manifest.json"
            if manifest_path.is_symlink() or (
                manifest_path.exists() and not manifest_path.is_file()
            ):
                raise EvidenceTampered("evidence manifest is invalid")
            if not manifest_path.is_file():
                if receipt is None:
                    return None
                raise EvidenceTampered("evidence manifest is missing or invalid")
            verified = self._verify_evidence_directory(
                directory,
                relative_directory,
                payload_capture="result",
            )
            if (
                receipt is not None
                and receipt["manifest_sha256"] != verified.manifest_digest
            ):
                raise EvidenceTampered("evidence receipt does not match its manifest")
            return _read_evidence_result_bytes(verified.result_bytes)

    def sealed_evidence_payloads(
        self, run_id: str, relative_directory: str
    ) -> dict[str, str]:
        """Return every verified UTF-8 payload from one sealed evidence unit."""
        with self.lock():
            verified = self._verify_or_finish_seal(
                run_id,
                relative_directory,
                payload_capture="all",
            )
            payloads = {}
            for relative, payload in verified.payload_bytes.items():
                try:
                    payloads[relative] = payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise EvidenceTampered(
                        "sealed evidence payload is invalid"
                    ) from exc
            return payloads

    def _verify_or_finish_seal(
        self,
        run_id: str,
        relative_directory: str,
        *,
        payload_capture: Literal["none", "result", "all"],
    ) -> _VerifiedEvidence:
        relative_directory = _canonical_evidence_relative(relative_directory)
        with self._open_observation_run(run_id) as run_descriptor:
            self._identity_at(run_descriptor, run_id)
            with self._open_observation_evidence(
                run_descriptor,
                relative_directory,
                missing_ok=False,
            ) as evidence_descriptors:
                if evidence_descriptors is None:
                    raise EvidenceTampered(
                        f"evidence directory does not exist: {relative_directory}"
                    )
                evidence_descriptor, parent_descriptor = evidence_descriptors
                return self._verify_or_finish_seal_at(
                    run_descriptor,
                    evidence_descriptor,
                    parent_descriptor,
                    relative_directory,
                    payload_capture=payload_capture,
                )

    def _verify_or_finish_seal_at(
        self,
        run_descriptor: int,
        evidence_descriptor: int,
        parent_descriptor: int,
        relative_directory: str,
        *,
        payload_capture: Literal["none", "result", "all"],
    ) -> _VerifiedEvidence:
        directory = Path(f"/proc/self/fd/{evidence_descriptor}")
        receipt = self._read_evidence_receipt_at(run_descriptor, relative_directory)
        try:
            verified = self._verify_evidence_directory(
                directory,
                relative_directory,
                payload_capture=payload_capture,
            )
            self._publish_evidence_receipt_at(
                run_descriptor,
                relative_directory,
                verified.manifest_digest,
            )
            return verified
        except EvidenceTampered as exc:
            if str(exc) != "sealed evidence is writable" or receipt is not None:
                raise

        with _open_evidence_tree(directory) as tree:
            modes = [
                *(
                    stat.S_IMODE(os.fstat(entry.descriptor).st_mode)
                    for entry in tree.files
                ),
                *(
                    stat.S_IMODE(os.fstat(entry.descriptor).st_mode)
                    for entry in tree.directories
                ),
                stat.S_IMODE(
                    os.stat(
                        "manifest.json",
                        dir_fd=evidence_descriptor,
                        follow_symlinks=False,
                    ).st_mode
                ),
            ]
            if any(mode & 0o222 for mode in modes):
                raise EvidenceTampered("sealed evidence is writable")
        os.fchmod(evidence_descriptor, 0o500)
        os.fsync(evidence_descriptor)
        os.fsync(parent_descriptor)
        verified = self._verify_evidence_directory(
            directory,
            relative_directory,
            payload_capture=payload_capture,
        )
        self._publish_evidence_receipt_at(
            run_descriptor,
            relative_directory,
            verified.manifest_digest,
        )
        return verified

    def _read_evidence_receipt_at(
        self, run_descriptor: int, relative_directory: str
    ) -> dict[str, Any] | None:
        receipt_name = (
            hashlib.sha256(relative_directory.encode("utf-8")).hexdigest() + ".json"
        )
        try:
            directory_descriptor = os.open(
                ".evidence-receipts",
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=run_descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EvidenceTampered("evidence receipt is invalid") from exc
        try:
            return self._read_evidence_receipt_from_directory(
                directory_descriptor, receipt_name, relative_directory
            )
        finally:
            os.close(directory_descriptor)

    def _read_evidence_receipt_from_directory(
        self,
        directory_descriptor: int,
        receipt_name: str,
        relative_directory: str,
    ) -> dict[str, Any] | None:
        directory_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise EvidenceTampered("evidence receipt is invalid")
        try:
            descriptor = os.open(
                receipt_name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EvidenceTampered("evidence receipt is invalid") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o400
            ):
                raise EvidenceTampered("evidence receipt is invalid")
            with os.fdopen(os.dup(descriptor), encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceTampered("evidence receipt is invalid") from exc
        finally:
            os.close(descriptor)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "evidence",
                "manifest_sha256",
            }
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != SCHEMA_VERSION
            or value.get("evidence") != relative_directory
            or not isinstance(value.get("manifest_sha256"), str)
            or SHA256_PATTERN.fullmatch(value["manifest_sha256"]) is None
        ):
            raise EvidenceTampered("evidence receipt is invalid")
        return value

    def _publish_evidence_receipt_at(
        self,
        run_descriptor: int,
        relative_directory: str,
        manifest_digest: str,
    ) -> None:
        expected = {
            "schema_version": SCHEMA_VERSION,
            "evidence": relative_directory,
            "manifest_sha256": manifest_digest,
        }
        observed = self._read_evidence_receipt_at(run_descriptor, relative_directory)
        if observed is not None:
            if observed != expected:
                raise EvidenceTampered("evidence receipt does not match its manifest")
            return
        try:
            directory_descriptor = self._open_evidence_receipt_directory_at(
                run_descriptor
            )
        except OSError as exc:
            raise EvidenceTampered("evidence receipt is invalid") from exc
        receipt_name = (
            hashlib.sha256(relative_directory.encode("utf-8")).hexdigest() + ".json"
        )
        try:
            _write_new_json_at(
                directory_descriptor,
                receipt_name,
                expected,
                self.root,
                mode=0o400,
            )
        except OSError as exc:
            raise EvidenceTampered("evidence receipt is invalid") from exc
        finally:
            os.close(directory_descriptor)

    def _open_evidence_receipt_directory_at(self, run_descriptor: int) -> int:
        directory_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            os.mkdir(".evidence-receipts", mode=0o700, dir_fd=run_descriptor)
            created = True
        except FileExistsError:
            created = False
        receipt_descriptor = os.open(
            ".evidence-receipts",
            directory_flags,
            dir_fd=run_descriptor,
        )
        try:
            metadata = os.fstat(receipt_descriptor)
            if created:
                os.fchmod(receipt_descriptor, 0o700)
                os.fsync(receipt_descriptor)
                os.fsync(run_descriptor)
            elif stat.S_IMODE(metadata.st_mode) != 0o700:
                raise EvidenceTampered("evidence receipt is invalid")
            result = receipt_descriptor
            receipt_descriptor = -1
            return result
        finally:
            if receipt_descriptor >= 0:
                os.close(receipt_descriptor)

    @contextmanager
    def _open_observation_run(self, run_id: str) -> Iterator[int]:
        _validate_run_id(run_id)
        directory_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors = []
        try:
            try:
                descriptor = os.open(self.root, directory_flags)
                descriptors.append(descriptor)
                descriptor = os.open("runs", directory_flags, dir_fd=descriptor)
                descriptors.append(descriptor)
                descriptor = os.open(run_id, directory_flags, dir_fd=descriptor)
                descriptors.append(descriptor)
            except FileNotFoundError as exc:
                raise RunNotFound(f"Run not found: {run_id}") from exc
            except OSError as exc:
                raise EvidenceError("evidence path is invalid") from exc
            yield descriptor
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextmanager
    def _open_observation_evidence(
        self,
        run_descriptor: int,
        relative_directory: str,
        *,
        missing_ok: bool,
        create_missing: bool = False,
    ) -> Iterator[tuple[int, int] | None]:
        directory_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors = []
        descriptor = run_descriptor
        try:
            try:
                for part in Path(relative_directory).parts:
                    parent_descriptor = descriptor
                    try:
                        descriptor = os.open(
                            part, directory_flags, dir_fd=parent_descriptor
                        )
                    except FileNotFoundError:
                        if not create_missing:
                            raise
                        os.mkdir(part, mode=0o700, dir_fd=parent_descriptor)
                        descriptor = os.open(
                            part, directory_flags, dir_fd=parent_descriptor
                        )
                        os.fchmod(descriptor, 0o700)
                        os.fsync(descriptor)
                        os.fsync(parent_descriptor)
                    descriptors.append(descriptor)
            except FileNotFoundError as exc:
                if missing_ok:
                    yield None
                    return
                raise EvidenceTampered(
                    f"evidence directory does not exist: {relative_directory}"
                ) from exc
            except OSError as exc:
                raise EvidenceError("evidence path must not contain symlinks") from exc
            parent_descriptor = (
                descriptors[-2] if len(descriptors) > 1 else run_descriptor
            )
            yield descriptor, parent_descriptor
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextmanager
    def _open_evidence_directory(
        self,
        run_id: str,
        relative_directory: str,
        *,
        missing_ok: bool,
        create_missing: bool = False,
    ) -> Iterator[tuple[str, int, int, int] | None]:
        relative_directory = _canonical_evidence_relative(relative_directory)
        with self._open_observation_run(run_id) as run_descriptor:
            self._identity_at(run_descriptor, run_id)
            with self._open_observation_evidence(
                run_descriptor,
                relative_directory,
                missing_ok=missing_ok,
                create_missing=create_missing,
            ) as evidence_descriptors:
                if evidence_descriptors is None:
                    yield None
                    return
                evidence_descriptor, parent_descriptor = evidence_descriptors
                yield (
                    relative_directory,
                    run_descriptor,
                    evidence_descriptor,
                    parent_descriptor,
                )

    @contextmanager
    def _open_evidence_file(
        self,
        run_id: str,
        relative_path: str,
        *,
        missing_ok: bool,
        create_missing: bool = False,
    ) -> Iterator[tuple[str, int, int, str] | None]:
        relative_path = _canonical_evidence_relative(relative_path)
        parts = Path(relative_path).parts
        if len(parts) < 3:
            raise EvidenceError("evidence file path is invalid")
        unit = Path(*parts[:2]).as_posix()
        with self._open_observation_run(run_id) as run_descriptor:
            self._identity_at(run_descriptor, run_id)
            with self._open_observation_evidence(
                run_descriptor,
                unit,
                missing_ok=missing_ok,
                create_missing=create_missing,
            ) as unit_descriptors:
                if unit_descriptors is None:
                    yield None
                    return
                unit_descriptor, _ = unit_descriptors
                nested_parent = Path(*parts[2:-1]).as_posix()
                if nested_parent == ".":
                    yield relative_path, unit_descriptor, unit_descriptor, parts[-1]
                    return
                with self._open_observation_evidence(
                    unit_descriptor,
                    nested_parent,
                    missing_ok=missing_ok,
                    create_missing=create_missing,
                ) as parent_descriptors:
                    if parent_descriptors is None:
                        yield None
                        return
                    parent_descriptor, _ = parent_descriptors
                    yield relative_path, unit_descriptor, parent_descriptor, parts[-1]

    def unsealed_evidence_result(
        self, run_id: str, relative_directory: str
    ) -> Any | None:
        """Return one complete unsealed result, rejecting partial evidence."""
        with self.lock():
            with self._open_evidence_directory(
                run_id,
                relative_directory,
                missing_ok=True,
            ) as evidence_directory:
                if evidence_directory is None:
                    return None
                _, _, evidence_descriptor, _ = evidence_directory
                if _entry_exists_at(evidence_descriptor, "manifest.json"):
                    raise EvidenceError(
                        "unsealed evidence contains an invalid manifest"
                    )
                entries = os.listdir(evidence_descriptor)
                if not entries:
                    return None
                if len(entries) != 1 or entries[0] != "result.json":
                    raise EvidenceError(
                        "unsealed evidence result is partial or ambiguous"
                    )
                return _read_evidence_result_at(evidence_descriptor, "result.json")

    def partial_evidence_result(
        self, run_id: str, relative_directory: str
    ) -> Any | None:
        """Return an unsealed result marker while other evidence may be partial."""
        with self.lock():
            with self._open_evidence_directory(
                run_id,
                relative_directory,
                missing_ok=True,
            ) as evidence_directory:
                if evidence_directory is None:
                    return None
                _, _, evidence_descriptor, _ = evidence_directory
                if _entry_exists_at(evidence_descriptor, "manifest.json"):
                    raise EvidenceError("partial evidence contains an invalid manifest")
                if not _entry_exists_at(evidence_descriptor, "result.json"):
                    return None
                return _read_evidence_result_at(evidence_descriptor, "result.json")

    def partial_evidence_files(
        self, run_id: str, relative_directory: str
    ) -> tuple[str, ...]:
        """Return validated relative file names from one unsealed evidence unit."""
        with self.lock():
            with self._open_evidence_directory(
                run_id,
                relative_directory,
                missing_ok=True,
            ) as evidence_directory:
                if evidence_directory is None:
                    return ()
                (
                    relative_directory,
                    _,
                    evidence_descriptor,
                    _,
                ) = evidence_directory
                if _entry_exists_at(evidence_descriptor, "manifest.json"):
                    raise EvidenceError("partial evidence contains an invalid manifest")
                directory = Path(f"/proc/self/fd/{evidence_descriptor}")
                with _open_evidence_tree(directory) as tree:
                    _validate_evidence_sizes(
                        tree.files,
                        _tree_limit(relative_directory),
                    )
                    return tuple(entry.path for entry in tree.files)

    def partial_evidence_value(self, run_id: str, relative_path: str) -> Any:
        """Read one structured value from unsealed evidence."""
        with self.lock():
            with self._open_evidence_file(
                run_id,
                relative_path,
                missing_ok=True,
            ) as evidence_file:
                if evidence_file is None:
                    raise EvidenceError("partial evidence value is invalid")
                _, unit_descriptor, parent_descriptor, name = evidence_file
                if _entry_exists_at(unit_descriptor, "manifest.json"):
                    raise EvidenceError("partial evidence is already sealed")
                if not _entry_exists_at(parent_descriptor, name):
                    raise EvidenceError("partial evidence value is invalid")
                return _read_evidence_result_at(parent_descriptor, name)

    def reconcile_evidence_value(
        self, run_id: str, relative_path: str, value: Any
    ) -> Any:
        """Write an unsealed value once or verify its exact canonical bytes."""
        with self.lock():
            with self._open_evidence_file(
                run_id,
                relative_path,
                missing_ok=False,
                create_missing=True,
            ) as evidence_file:
                if evidence_file is None:
                    raise EvidenceError("evidence file path is invalid")
                relative_path, unit_descriptor, parent_descriptor, name = evidence_file
                if len(Path(relative_path).parts) == 3 and name == "manifest.json":
                    raise EvidenceError("manifest.json is reserved")
                if _entry_exists_at(unit_descriptor, "manifest.json"):
                    raise EvidenceError("completed evidence is read-only")
                expected = redact_artifact_value(value)
                encoded = (canonical_json(expected) + "\n").encode("utf-8")
                if _entry_exists_at(parent_descriptor, name):
                    try:
                        observed = _read_bytes_at(parent_descriptor, name)
                    except EvidenceError as exc:
                        raise EvidenceError(
                            "partial evidence value is invalid"
                        ) from exc
                    if observed != encoded:
                        raise EvidenceError(
                            "partial evidence contradicts its expected value"
                        )
                    return expected
                _write_new_bytes_at(
                    parent_descriptor,
                    name,
                    encoded,
                    self.root,
                )
                return expected

    def _append_event_unlocked(
        self,
        run_id: str,
        event: str,
        *,
        state: str | None,
        data: dict[str, Any] | None,
        recorded_at: str | None,
    ) -> dict[str, Any]:
        if not event.strip():
            raise RunStoreError("event must not be empty")
        identity = self._identity(run_id)
        events, valid_bytes = self._read_events(run_id)
        event_data = dict(data or {})
        if is_episode_event(event):
            try:
                effects, omitted_effects = self._read_effect_inventory(run_id)
                evidence, omitted_evidence_units = self._read_evidence_inventory(run_id)
                inventory = capture_inventory(
                    through_sequence=len(events) + 1,
                    effects=effects,
                    evidence=evidence,
                    omitted_effects=omitted_effects,
                    omitted_evidence_units=omitted_evidence_units,
                )
            except _RetrospectiveInventoryUnavailable:
                inventory = capture_unavailable_inventory(
                    through_sequence=len(events) + 1,
                )
            try:
                event_data = attach_inventory(event_data, inventory)
            except RetrospectiveContractError as exc:
                raise RunStoreError(str(exc)) from exc
        record = redact_artifact_value(
            {
                "schema_version": SCHEMA_VERSION,
                "sequence": len(events) + 1,
                "recorded_at": recorded_at or utc_now(),
                "event": event,
                **({"state": state} if state is not None else {}),
                "data": event_data,
            }
        )
        encoded = f"{canonical_json(record)}\n".encode("utf-8")
        events_path = self._run_dir(run_id) / "events.jsonl"
        descriptor = os.open(events_path, os.O_WRONLY)
        try:
            os.ftruncate(descriptor, valid_bytes)
            os.lseek(descriptor, 0, os.SEEK_END)
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise RunStoreError("event append was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        projection = _project(identity, [*events, record])
        _atomic_json(self._run_dir(run_id) / "state.json", projection)
        return projection

    def _read_events(
        self, run_id: str, *, require_complete: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        path = self._run_dir(run_id) / "events.jsonl"
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise RunNotFound(f"Run not found: {run_id}") from exc
        return self._parse_event_history(payload, require_complete=require_complete)

    def _read_events_at(
        self,
        run_descriptor: int,
        run_id: str,
        *,
        require_complete: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open("events.jsonl", flags, dir_fd=run_descriptor)
        except FileNotFoundError as exc:
            raise RunNotFound(f"Run not found: {run_id}") from exc
        except OSError as exc:
            raise EventHistoryCorrupt("Event History is invalid") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise EventHistoryCorrupt("Event History is invalid")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                payload = stream.read()
        except OSError as exc:
            raise EventHistoryCorrupt("Event History is invalid") from exc
        finally:
            os.close(descriptor)
        return self._parse_event_history(payload, require_complete=require_complete)

    def _parse_event_history(
        self, payload: bytes, *, require_complete: bool
    ) -> tuple[list[dict[str, Any]], int]:
        complete_bytes = payload.rfind(b"\n") + 1
        if require_complete and complete_bytes != len(payload):
            raise EventHistoryCorrupt("Event History has an incomplete trailing record")
        complete = payload[:complete_bytes]
        events = []
        offset = 0
        for line in complete.splitlines(keepends=True):
            offset += len(line)
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EventHistoryCorrupt(
                    f"invalid Event History at byte {offset - len(line)}"
                ) from exc
            expected_sequence = len(events) + 1
            if (
                not isinstance(record, dict)
                or type(record.get("sequence")) is not int
                or record.get("sequence") != expected_sequence
            ):
                raise EventHistoryCorrupt(
                    f"Event History sequence must be {expected_sequence}"
                )
            expected_keys = {
                "schema_version",
                "sequence",
                "recorded_at",
                "event",
                "data",
            }
            if "state" in record:
                expected_keys.add("state")
            if (
                set(record) != expected_keys
                or type(record.get("schema_version")) is not int
                or record["schema_version"] != SCHEMA_VERSION
                or not isinstance(record.get("recorded_at"), str)
                or not record["recorded_at"].strip()
                or not isinstance(record.get("event"), str)
                or not record["event"].strip()
                or not isinstance(record.get("data"), dict)
                or (
                    "state" in record
                    and (
                        not isinstance(record["state"], str)
                        or not record["state"].strip()
                    )
                )
            ):
                raise EventHistoryCorrupt(
                    f"Event History record {expected_sequence} is invalid"
                )
            events.append(record)
        return events, complete_bytes

    def _read_events_through(
        self,
        run_id: str,
        sequence: int,
        *,
        parameter_name: str,
    ) -> list[dict[str, Any]]:
        if type(sequence) is not int or sequence < 1:
            raise RunStoreError(f"{parameter_name} must be a positive integer")
        events, _ = self._read_events(run_id)
        if sequence > len(events):
            raise RunStoreError(f"Event History has no sequence {sequence}: {run_id}")
        return events[:sequence]

    def _read_effect_inventory(self, run_id: str) -> tuple[list[dict[str, Any]], int]:
        effects_directory = self._run_dir(run_id) / "effects"
        if effects_directory.is_symlink() or not effects_directory.is_dir():
            raise EventHistoryCorrupt("Effect directory is invalid")
        paths = []
        for path in sorted(effects_directory.iterdir(), key=lambda item: item.name):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or path.stem == ""
                or not is_durable_id(path.stem)
            ):
                raise RunStoreError("Effects directory contains an invalid record")
            paths.append(path)
        selected, omitted = select_inventory_items(
            paths,
            identity=lambda path: path.stem,
        )
        try:
            effects = [self.effect(run_id, path.stem) for path in selected]
        except RunStoreError as exc:
            raise _RetrospectiveInventoryUnavailable from exc
        return effects, omitted

    def _read_retrospective_inventory(
        self,
        run_id: str,
        event: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        inventory = event["data"].get(INVENTORY_KEY)
        if inventory is None:
            if is_episode_event(event["event"]):
                raise RunStoreError(
                    "retrospective inventory is unavailable "
                    f"for episode {event['sequence']}"
                )
            return [], [], {"effects": 0, "evidence_units": 0}
        try:
            inventory = decode_inventory(
                inventory,
                sequence=event["sequence"],
                evidence_roots=EVIDENCE_ROOTS,
            )
        except RetrospectiveContractError as exc:
            raise EventHistoryCorrupt(str(exc)) from exc
        if inventory.get("status") == "unavailable":
            raise RunStoreError(
                "retrospective inventory is unavailable "
                f"for episode {event['sequence']}"
            )
        evidence = []
        for reference in inventory["evidence"]:
            unit = reference["unit"]
            self.verify_evidence(run_id, unit)
            manifest_path = self._evidence_path(run_id, unit) / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EvidenceTampered(
                    "evidence manifest is missing or invalid"
                ) from exc
            digest = manifest_digest(manifest)
            if digest != reference["manifest_sha256"]:
                raise EvidenceTampered(
                    "retrospective evidence manifest digest does not match"
                )
            evidence.append({"unit": unit, "manifest": manifest})
        return inventory["effects"], evidence, inventory["omitted"]

    def _read_evidence_inventory(self, run_id: str) -> tuple[list[dict[str, Any]], int]:
        run_directory = self._run_dir(run_id)
        units = []
        for root_name in sorted(EVIDENCE_ROOTS):
            root = run_directory / root_name
            if root.is_symlink() or not root.is_dir():
                raise EvidenceTampered(f"{root_name} evidence root is invalid")
            for unit in sorted(root.iterdir(), key=lambda item: item.name):
                if unit.is_symlink() or not unit.is_dir():
                    raise EvidenceTampered(
                        f"{root_name} contains an invalid evidence unit"
                    )
                manifest_path = unit / "manifest.json"
                if manifest_path.is_symlink() or (
                    manifest_path.exists() and not manifest_path.is_file()
                ):
                    raise EvidenceTampered("evidence manifest is invalid")
                if not manifest_path.exists():
                    continue
                units.append(f"{root_name}/{unit.name}")
        selected, omitted = select_inventory_items(
            units,
            identity=lambda unit: unit,
        )
        records = []
        for relative in selected:
            try:
                self.verify_evidence(run_id, relative)
                manifest_path = self._evidence_path(run_id, relative) / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (
                EvidenceError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                raise _RetrospectiveInventoryUnavailable from exc
            records.append({"unit": relative, "manifest": manifest})
        return records, omitted

    def _identity(self, run_id: str) -> dict[str, Any]:
        _validate_run_id(run_id)
        path = self._run_dir(run_id) / "run.json"
        try:
            identity = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RunNotFound(f"Run not found: {run_id}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventHistoryCorrupt(f"Run identity is invalid: {run_id}") from exc
        return self._validate_identity(identity, run_id)

    def _identity_at(self, run_descriptor: int, run_id: str) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open("run.json", flags, dir_fd=run_descriptor)
        except FileNotFoundError as exc:
            raise RunNotFound(f"Run not found: {run_id}") from exc
        except OSError as exc:
            raise EventHistoryCorrupt(f"Run identity is invalid: {run_id}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise EventHistoryCorrupt(f"Run identity is invalid: {run_id}")
            with os.fdopen(os.dup(descriptor), encoding="utf-8") as stream:
                identity = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventHistoryCorrupt(f"Run identity is invalid: {run_id}") from exc
        finally:
            os.close(descriptor)
        return self._validate_identity(identity, run_id)

    def _validate_identity(self, identity: Any, run_id: str) -> dict[str, Any]:
        legacy_keys = {
            "schema_version",
            "run_id",
            "bead_id",
            "repository",
            "base_branch",
            "base_sha",
            "created_at",
            "start_request",
        }
        receipt_keys = legacy_keys | {"evidence_receipt_version"}
        schema_version = (
            identity.get("schema_version") if isinstance(identity, dict) else None
        )
        valid_format = type(schema_version) is int and (
            (schema_version == SCHEMA_VERSION and set(identity) == legacy_keys)
            or (
                schema_version in (SCHEMA_VERSION, RUN_IDENTITY_SCHEMA_VERSION)
                and set(identity) == receipt_keys
                and type(identity.get("evidence_receipt_version")) is int
                and identity["evidence_receipt_version"] == EVIDENCE_RECEIPT_VERSION
            )
        )
        if (
            not valid_format
            or identity.get("run_id") != run_id
            or any(
                not isinstance(identity.get(field), str) or not identity[field].strip()
                for field in (
                    "bead_id",
                    "repository",
                    "base_branch",
                    "created_at",
                )
            )
            or not isinstance(identity.get("base_sha"), str)
            or not SHA_PATTERN.fullmatch(identity["base_sha"])
            or not isinstance(identity.get("start_request"), dict)
        ):
            raise EventHistoryCorrupt(f"Run identity is invalid: {run_id}")
        return identity

    def _active_run_id(self) -> str | None:
        runs_dir = self.root / "runs"
        try:
            run_directories = sorted(
                path for path in runs_dir.iterdir() if path.is_dir()
            )
        except FileNotFoundError:
            return None
        active = []
        for run_dir in run_directories:
            identity = self._identity(run_dir.name)
            events, _ = self._read_events(run_dir.name)
            projection = _project(identity, events)
            if projection[
                "state"
            ] != "completed" or not self._completion_episode_finalized(
                run_dir.name, projection
            ):
                active.append(run_dir.name)
        if len(active) > 1:
            raise EventHistoryCorrupt("multiple Active Runs exist")
        return active[0] if active else None

    def _completion_episode_finalized(
        self,
        run_id: str,
        projection: dict[str, Any],
    ) -> bool:
        episode = self._validated_completion_episode(run_id, projection)
        finalization = self._read_completion_finalization(run_id, missing_ok=True)
        if episode is None:
            if finalization is not None:
                raise EventHistoryCorrupt("completion finalization is invalid")
            return True
        if finalization is None:
            return False
        outcome = self.sealed_evidence_result(run_id, episode["evidence"])
        expected = self._expected_completion_finalization(
            run_id,
            episode,
            outcome=outcome,
        )
        if finalization != expected:
            raise EventHistoryCorrupt("completion finalization is invalid")
        return True

    def _expected_completion_finalization(
        self,
        run_id: str,
        episode: dict[str, Any],
        *,
        outcome: Any,
    ) -> dict[str, Any]:
        if not isinstance(outcome, dict):
            raise EventHistoryCorrupt("completion retrospective outcome is invalid")
        effect = self.effect(run_id, episode["effect_id"])
        if (
            effect["kind"] != "retrospective-analysis"
            or effect["status"] != "confirmed"
            or effect["intended"].get("episode_sequence") != episode["episode_sequence"]
            or effect["intended"].get("evidence") != episode["evidence"]
            or effect.get("observed")
            != {"evidence": episode["evidence"], "status": outcome.get("status")}
        ):
            raise EventHistoryCorrupt("completion retrospective Effect is invalid")
        self.verify_evidence(run_id, episode["evidence"])
        manifest_path = (
            self._evidence_path(run_id, episode["evidence"]) / "manifest.json"
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceTampered(
                "completion retrospective manifest is invalid"
            ) from exc
        bindings = {
            "run_id": run_id,
            "episode_sequence": episode["episode_sequence"],
            "completion_episode_sha256": _canonical_sha256(episode),
            "evidence": episode["evidence"],
            "evidence_manifest_sha256": manifest_digest(manifest),
            "outcome_sha256": _canonical_sha256(outcome),
            "effect_id": episode["effect_id"],
            "effect_sha256": _canonical_sha256(effect),
        }
        return {
            "schema_version": 1,
            **bindings,
            "binding_sha256": _canonical_sha256(bindings),
        }

    def _read_completion_finalization(
        self,
        run_id: str,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        path = self._run_dir(run_id) / "completion-finalization.json"
        if not path.exists() and not path.is_symlink():
            if missing_ok:
                return None
            raise EventHistoryCorrupt("completion finalization is missing")
        try:
            if path.is_symlink() or not path.is_file():
                raise EventHistoryCorrupt("completion finalization is invalid")
            _require_mode(path, 0o600, "Completion finalization")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventHistoryCorrupt("completion finalization is invalid") from exc
        if not isinstance(value, dict):
            raise EventHistoryCorrupt("completion finalization is invalid")
        return value

    def _validated_completion_episode(
        self,
        run_id: str,
        projection: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self._validated_episode(
            run_id,
            projection,
            name="completion",
            evidence_name="completed",
            event_name="run.completed",
            event_state="completed",
            projection_state="completed",
        )

    def _validated_attention_episode(
        self,
        run_id: str,
        projection: dict[str, Any],
    ) -> dict[str, Any] | None:
        episode = self._validated_episode(
            run_id,
            projection,
            name="attention",
            evidence_name="attention",
            event_name="run.attention_required",
            event_state="attention_required",
        )
        if episode is None:
            return None
        events, _ = self._read_events(run_id)
        latest = next(
            event["data"]["attention_episode"]
            for event in reversed(events)
            if event["event"] == "run.attention_required"
            and isinstance(event.get("data"), dict)
            and "attention_episode" in event["data"]
        )
        if latest != episode:
            raise EventHistoryCorrupt("attention episode marker is invalid")
        return episode

    def _validated_episode(
        self,
        run_id: str,
        projection: dict[str, Any],
        *,
        name: str,
        evidence_name: str,
        event_name: str,
        event_state: str,
        projection_state: str | None = None,
    ) -> dict[str, Any] | None:
        episode = _episode_marker(
            projection,
            name=name,
            evidence_name=evidence_name,
            projection_state=projection_state,
        )
        if episode is None:
            return None
        event = self.event(run_id, episode["episode_sequence"])
        if (
            event.get("event") != event_name
            or event.get("state") != event_state
            or event.get("data", {}).get(f"{name}_episode") != episode
        ):
            raise EventHistoryCorrupt(f"{name} episode marker is invalid")
        return episode

    def _clear_active_pointer(self, run_id: str) -> None:
        if self._active_pointer_run_id() != run_id:
            return
        (self.root / "active.json").unlink(missing_ok=True)
        _fsync_directory(self.root)

    def _active_pointer_run_id(self, *, invalid_is_error: bool = False) -> str | None:
        path = self.root / "active.json"
        try:
            active = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if invalid_is_error:
                raise EventHistoryCorrupt("Active Run pointer is invalid") from exc
            return None
        if (
            not isinstance(active, dict)
            or set(active) != {"run_id"}
            or not isinstance(active["run_id"], str)
        ):
            if invalid_is_error:
                raise EventHistoryCorrupt("Active Run pointer is invalid")
            return None
        try:
            _validate_run_id(active["run_id"])
        except RunStoreError as exc:
            if invalid_is_error:
                raise EventHistoryCorrupt("Active Run pointer is invalid") from exc
            return None
        return active["run_id"]

    def _run_dir(self, run_id: str) -> Path:
        _validate_run_id(run_id)
        return self.root / "runs" / run_id

    def _verify_sealed_evidence(self, run_id: str, projection: dict[str, Any]) -> None:
        run_dir = self._run_dir(run_id)
        projected_units = _projected_evidence_units(projection)
        projected_digests = _projected_manifest_digests(projection)
        for root_name in sorted(EVIDENCE_ROOTS):
            root = run_dir / root_name
            for unit in root.iterdir():
                if unit.is_symlink() or not unit.is_dir():
                    raise EvidenceTampered("evidence unit is invalid")
                relative = f"{root_name}/{unit.name}"
                manifest = unit / "manifest.json"
                if manifest.exists() or manifest.is_symlink():
                    try:
                        self._verify_or_finish_seal(
                            run_id,
                            relative,
                            payload_capture="none",
                        )
                    except EvidenceTampered as exc:
                        if relative in projected_units:
                            raise ProjectedEvidenceTampered(str(exc)) from exc
                        raise
                    expected = projected_digests.get(relative)
                    if expected is not None:
                        observed = hashlib.sha256(
                            canonical_json(
                                json.loads(manifest.read_text(encoding="utf-8"))
                            ).encode("utf-8")
                        ).hexdigest()
                        if expected != {observed}:
                            raise ProjectedEvidenceTampered(
                                "projected evidence manifest digest does not match"
                            )

    def _validate_resume_permissions(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        _require_mode(run_dir, 0o700, "Run directory")
        for name in ("run.json", "events.jsonl"):
            _require_mode(run_dir / name, 0o600, f"Run {name}")
        effects = run_dir / "effects"
        _require_mode(effects, 0o700, "Effect directory")
        for effect in effects.iterdir():
            _require_mode(effect, 0o600, "Effect record")
        for root_name in EVIDENCE_ROOTS:
            _require_mode(run_dir / root_name, 0o700, "evidence root")

    def _validate_resume_effects(self, run_id: str) -> None:
        effects = self._run_dir(run_id) / "effects"
        for path in sorted(effects.iterdir()):
            if path.suffix != ".json" or not is_durable_id(path.stem):
                raise ResumePreflightInvalid(f"Effect record is invalid: {path.name}")
            try:
                self.effect(run_id, path.stem)
            except RunStoreError as exc:
                raise ResumePreflightInvalid(str(exc)) from exc

    def _validate_resume_projection(
        self, projection: dict[str, Any]
    ) -> list[dict[str, Any]]:
        run_id = projection["run_id"]
        events, _ = self._read_events(run_id, require_complete=True)
        self._validate_resume_permissions(run_id)
        self._validate_resume_effects(run_id)
        self._verify_sealed_evidence(run_id, projection)
        return events

    def _evidence_path(self, run_id: str, relative: str) -> Path:
        return self._evidence_path_from_run(self._run_dir(run_id), relative)

    def _evidence_path_from_run(self, run_path: Path, relative: str) -> Path:
        relative = _canonical_evidence_relative(relative)
        parts = Path(relative).parts
        path = run_path
        for part in parts:
            path /= part
            if path.is_symlink():
                raise EvidenceError("evidence path must not contain symlinks")
        run_dir = run_path.resolve()
        if not path.resolve(strict=False).is_relative_to(run_dir):
            raise EvidenceError("evidence path escapes the Run directory")
        return path


def _validate_run_id(run_id: str) -> None:
    if not is_durable_id(run_id):
        raise RunStoreError("run_id contains unsupported characters")


def _canonical_evidence_relative(relative: str) -> str:
    if not isinstance(relative, str) or redact_text(relative) != relative:
        raise EvidenceError("evidence path must not contain secret-shaped text")
    path = Path(relative)
    parts = path.parts
    if (
        not parts
        or path.is_absolute()
        or parts[0] not in EVIDENCE_ROOTS
        or ".." in parts
    ):
        raise EvidenceError(
            "evidence path must stay under attempts, gates, or retrospective"
        )
    return Path(*parts).as_posix()


def _require_mode(path: Path, mode: int, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EventHistoryCorrupt(f"{label} is invalid") from exc
    if stat.S_ISLNK(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != mode:
        raise EventHistoryCorrupt(f"{label} permissions are invalid")


def _projected_evidence_units(value: Any) -> set[str]:
    units: set[str] = set()
    for _, record in _walk_projected_values(value):
        if not isinstance(record, dict):
            continue
        evidence = record.get("evidence")
        if isinstance(evidence, str):
            parts = Path(evidence).parts
            if len(parts) >= 2 and parts[0] in EVIDENCE_ROOTS:
                units.add(f"{parts[0]}/{parts[1]}")
    attempt = value.get("validation_attempt") if isinstance(value, dict) else None
    if (
        isinstance(attempt, dict)
        and attempt.get("status") in {"started", "passed", "rejected", "inconclusive"}
        and isinstance(attempt.get("attempt_id"), str)
    ):
        units.add(f"gates/{attempt['attempt_id']}")
    return units


def _projected_manifest_digests(projection: dict[str, Any]) -> dict[str, set[str]]:
    digests: dict[str, set[str]] = {}

    def add(record: Any, label: str) -> None:
        if not isinstance(record, dict):
            raise ProjectedEvidenceTampered(
                f"projected {label} manifest digest is invalid"
            )
        evidence = record.get("evidence")
        digest = record.get("manifest_sha256")
        parts = Path(evidence).parts if isinstance(evidence, str) else ()
        if (
            len(parts) != 2
            or parts[0] not in EVIDENCE_ROOTS
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
        ):
            raise ProjectedEvidenceTampered(
                f"projected {label} manifest digest is invalid"
            )
        digests.setdefault(evidence, set()).add(digest)

    for path, record in _walk_projected_values(projection):
        label = None
        if path == ("validation",):
            label = "validation"
        elif path == ("bead_spec",):
            label = "Bead/spec"
        elif (
            len(path) == 3
            and path[0] == "gate_cycles"
            and isinstance(path[1], int)
            and path[2] == "validation"
        ):
            label = "Gate validation"
        if label is not None:
            add(record, label)
    return digests


def _walk_projected_values(
    value: Any, path: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _walk_projected_values(nested, (*path, key))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_projected_values(nested, (*path, index))


def _validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files", "total_bytes"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != SCHEMA_VERSION
        or type(manifest["total_bytes"]) is not int
        or manifest["total_bytes"] < 0
        or not isinstance(manifest["files"], list)
    ):
        raise EvidenceTampered("evidence manifest schema is invalid")
    for entry in manifest["files"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "bytes", "sha256"}
            or not isinstance(entry["path"], str)
            or type(entry["bytes"]) is not int
            or entry["bytes"] < 0
            or not isinstance(entry["sha256"], str)
            or not SHA256_PATTERN.fullmatch(entry["sha256"])
        ):
            raise EvidenceTampered("evidence manifest files are invalid")
    return manifest["files"]


def _project(identity: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        raise EventHistoryCorrupt("Event History has no durable facts")
    state = None
    for event in events:
        if "state" in event:
            state = event["state"]
    if not isinstance(state, str) or not state:
        raise EventHistoryCorrupt("Event History has no Run State")
    details: dict[str, Any] = {}
    checkpoint = "created"
    for event in events:
        data = event.get("data")
        if isinstance(data, dict):
            details.update(data)
        event_state = event.get("state")
        if isinstance(event_state, str) and event_state != "attention_required":
            checkpoint = event_state
    last = events[-1]
    projection = {
        "schema_version": SCHEMA_VERSION,
        "run_id": identity["run_id"],
        "bead_id": identity["bead_id"],
        "repository": identity["repository"],
        "base_branch": identity["base_branch"],
        "base_sha": identity["base_sha"],
        "created_at": identity["created_at"],
        "state": state,
        "last_sequence": last["sequence"],
        "last_event": last["event"],
        "updated_at": last["recorded_at"],
        "checkpoint": details.get("checkpoint", checkpoint),
    }
    for key in (
        "unit",
        "worktree_path",
        "branch",
        "attention",
        "lingering",
        "validation_contract",
        "worker_exit_code",
        "worker_result",
        "implementation_attempt",
        "candidate_sha",
        "candidate_publication",
        "candidate_pr",
        "pr_number",
        "pr_url",
        "pr_head_sha",
        "pr_ready",
        "merge",
        "remote_branch_deleted",
        "bead_closure",
        "bead_claim",
        "validation",
        "validation_attempt",
        "previous_candidate_sha",
        "repair_attempts_used",
        "repair_brief",
        "repair_dispositions",
        "gate_cycles",
        "gate_retry",
        "completion",
        "completion_episode",
        "attention_episode",
        "bead_spec",
        "interrupted_repair",
        "lifecycle_interruption",
    ):
        if key in details:
            projection[key] = details[key]
    return projection


def _episode_marker(
    projection: dict[str, Any],
    *,
    name: str,
    evidence_name: str,
    projection_state: str | None = None,
) -> dict[str, Any] | None:
    marker_name = f"{name}_episode"
    if marker_name not in projection:
        return None
    episode = projection[marker_name]
    sequence = episode.get("episode_sequence") if isinstance(episode, dict) else None
    expected = {
        "schema_version": 1,
        "episode_sequence": sequence,
        "evidence": f"retrospective/{evidence_name}-{sequence}",
        "effect_id": f"retrospective-analysis-{sequence}",
    }
    if (
        type(sequence) is not int
        or sequence < 1
        or episode != expected
        or (
            projection_state is not None and projection.get("state") != projection_state
        )
        or type(projection.get("last_sequence")) is not int
        or projection["last_sequence"] < sequence
    ):
        raise EventHistoryCorrupt(f"{name} episode marker is invalid")
    return episode


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _secure_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    _fsync_directory(path)
    if not existed:
        _fsync_directory(path.parent)


def _write_new_json(
    path: Path, value: Any, staging_directory: Path, *, mode: int = 0o600
) -> None:
    _write_new_bytes(
        path,
        f"{canonical_json(redact_artifact_value(value))}\n".encode("utf-8"),
        staging_directory,
        mode=mode,
    )


def _write_new_json_at(
    directory_descriptor: int,
    name: str,
    value: Any,
    staging_root: Path,
    *,
    mode: int = 0o600,
) -> None:
    payload = (f"{canonical_json(redact_artifact_value(value))}\n").encode("utf-8")
    _write_new_bytes_at(
        directory_descriptor,
        name,
        payload,
        staging_root,
        mode=mode,
    )


def _write_new_bytes_at(
    directory_descriptor: int,
    name: str,
    payload: bytes,
    staging_root: Path,
    *,
    mode: int = 0o600,
) -> None:
    with _staged_new_bytes(
        payload,
        staging_root,
        target_id=name,
        target_name=name,
        mode=mode,
    ) as temporary:
        os.link(
            temporary,
            name,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        os.fsync(directory_descriptor)


def _read_bytes_at(directory_descriptor: int, name: str) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise EvidenceError("evidence file is invalid") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EvidenceError("evidence file is invalid")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read()
    except OSError as exc:
        raise EvidenceError("evidence file is invalid") from exc
    finally:
        os.close(descriptor)


def _entry_exists_at(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _read_evidence_result(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("evidence result is missing or malformed") from exc


def _read_evidence_result_at(directory_descriptor: int, name: str) -> Any:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise EvidenceError("evidence result is missing or malformed") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceError("evidence result is missing or malformed")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            value = stream.read()
    except OSError as exc:
        raise EvidenceError("evidence result is missing or malformed") from exc
    finally:
        os.close(descriptor)
    return _read_evidence_result_bytes(value)


def _read_evidence_result_bytes(value: bytes | None) -> Any:
    try:
        if value is None:
            raise ValueError
        return json.loads(value.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("evidence result is missing or malformed") from exc


def _write_new_bytes(
    path: Path, value: bytes, staging_root: Path, *, mode: int = 0o600
) -> None:
    with _staged_new_bytes(
        value,
        staging_root,
        target_id=str(path),
        target_name=path.name,
        mode=mode,
    ) as temporary:
        os.link(temporary, path)
        _fsync_directory(path.parent)


@contextmanager
def _staged_new_bytes(
    value: bytes,
    staging_root: Path,
    *,
    target_id: str,
    target_name: str,
    mode: int,
) -> Iterator[Path]:
    staging_directory = staging_root / ".unpublished"
    _secure_directory(staging_directory)
    digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{digest}.", suffix=".tmp", dir=staging_directory
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        written = os.write(descriptor, value)
        if written != len(value):
            raise RunStoreError(f"write was incomplete: {target_name}")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        yield temporary
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    payload = f"{canonical_json(redact_artifact_value(value))}\n".encode("utf-8")
    _atomic_bytes(path, payload)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _secure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise RunStoreError(f"write was incomplete: {path.name}")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@contextmanager
def _open_evidence_tree(directory: Path) -> Iterator[_OpenEvidenceTree]:
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    root_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    descriptors = [root_descriptor]
    files = []
    directories = []

    def walk(parent_descriptor: int, prefix: str) -> None:
        for name in sorted(os.listdir(parent_descriptor)):
            relative = f"{prefix}/{name}" if prefix else name
            if relative == "manifest.json":
                continue
            try:
                metadata = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(metadata.st_mode):
                    descriptor = os.open(
                        name,
                        directory_flags,
                        dir_fd=parent_descriptor,
                    )
                    descriptors.append(descriptor)
                    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                        raise EvidenceError("evidence must contain only regular files")
                    directories.append(_OpenEvidenceEntry(relative, descriptor))
                    walk(descriptor, relative)
                elif stat.S_ISREG(metadata.st_mode):
                    descriptor = os.open(
                        name,
                        file_flags,
                        dir_fd=parent_descriptor,
                    )
                    descriptors.append(descriptor)
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise EvidenceError("evidence must contain only regular files")
                    files.append(_OpenEvidenceEntry(relative, descriptor))
                else:
                    raise EvidenceError("evidence must contain only regular files")
            except OSError as exc:
                raise EvidenceError("evidence must not contain symlinks") from exc

    try:
        walk(root_descriptor, "")
        yield _OpenEvidenceTree(
            tuple(sorted(files, key=lambda entry: entry.path)),
            tuple(sorted(directories, key=lambda entry: entry.path)),
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _manifest_entries(
    files: tuple[_OpenEvidenceEntry, ...], byte_limit: int
) -> list[dict[str, Any]]:
    entries, _ = _manifest_snapshot(
        files,
        byte_limit,
        payload_capture="none",
    )
    return entries


def _manifest_snapshot(
    files: tuple[_OpenEvidenceEntry, ...],
    byte_limit: int,
    *,
    payload_capture: Literal["none", "result", "all"],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    _validate_evidence_sizes(files, byte_limit)
    entries = []
    payload_bytes = {}
    for entry in files:
        size = os.fstat(entry.descriptor).st_size
        try:
            with os.fdopen(os.dup(entry.descriptor), "rb") as stream:
                payload = stream.read()
            text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise EvidenceError("evidence must be regular UTF-8 text") from exc
        if redact_text(text) != text:
            raise EvidenceError(
                "evidence must cross the redaction boundary before sealing"
            )
        relative = entry.path
        if payload_capture == "all" or (
            payload_capture == "result" and relative == "result.json"
        ):
            payload_bytes[relative] = payload
        entries.append(
            {
                "path": relative,
                "bytes": size,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return entries, payload_bytes


def _validate_evidence_sizes(
    files: tuple[_OpenEvidenceEntry, ...], byte_limit: int
) -> None:
    total = 0
    for entry in files:
        size = os.fstat(entry.descriptor).st_size
        if _is_stream(Path(entry.path)) and size > STREAM_BYTE_LIMIT:
            raise EvidenceTooLarge(f"evidence stream exceeds {STREAM_BYTE_LIMIT} bytes")
        total += size
        if total > byte_limit:
            raise EvidenceTooLarge(f"evidence tree exceeds {byte_limit} bytes")


def _is_stream(path: Path) -> bool:
    return path.name in {"stdout", "stderr", "stdout.txt", "stderr.txt"}


def _tree_limit(relative_directory: str) -> int:
    if Path(relative_directory).parts[0] == "gates":
        return GATE_BYTE_LIMIT
    return ATTEMPT_BYTE_LIMIT


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
