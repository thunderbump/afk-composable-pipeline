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
from typing import Any, Iterator

from afk.jsonutil import canonical_json
from afk.redaction import redact_artifact_value, redact_text


SCHEMA_VERSION = 1
STREAM_BYTE_LIMIT = 64 * 1024 * 1024
ATTEMPT_BYTE_LIMIT = 256 * 1024 * 1024
GATE_BYTE_LIMIT = 512 * 1024 * 1024
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_ROOTS = {"attempts", "gates", "retrospective"}


class RunStoreError(RuntimeError):
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


def default_state_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "afk"
    return Path.home() / ".local" / "state" / "afk"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
            try:
                descriptor = os.open(
                    "afk.lock",
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=root_descriptor,
                )
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
        else:
            _secure_directory(self.root)
            descriptor = os.open(self.root / "afk.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
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
        run_id = (
            run_id
            or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
        )
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
            active = self._active_run_id()
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
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "bead_id": bead_id,
                    "repository": repository,
                    "base_branch": base_branch,
                    "base_sha": base_sha,
                    "created_at": created_at,
                    "start_request": start_request or {},
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

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        selected = run_id or self._active_run_id()
        if selected is None:
            recovered = self._reconcile_completed_active_pointer()
            if recovered is not None:
                return recovered
            selected = self._active_run_id()
            if selected is None:
                raise RunNotFound("no Active Run")
        _validate_run_id(selected)
        identity = self._identity(selected)
        events, _ = self._read_events(selected)
        return _project(identity, events)

    def resume_status(self) -> dict[str, Any]:
        with self.lock(validate_root_permissions=True):
            _require_mode(self.root, 0o700, "Run Store directory")
            active_path = self.root / "active.json"
            if active_path.exists() or active_path.is_symlink():
                _require_mode(active_path, 0o600, "Active Run pointer")
            active_run_id = self._active_pointer_run_id(invalid_is_error=True)
            projection = self.status()
            self._read_events(projection["run_id"], require_complete=True)
            if active_run_id is not None and active_run_id != projection["run_id"]:
                raise EventHistoryCorrupt(
                    "Active Run pointer does not match Event History"
                )
            self._validate_resume_permissions(projection["run_id"])
            self._validate_resume_effects(projection["run_id"])
            self._verify_sealed_evidence(projection["run_id"], projection)
            _validate_open_attempts(projection)
            _atomic_json(self._run_dir(projection["run_id"]) / "state.json", projection)
            if active_run_id is None:
                _atomic_json(
                    self.root / "active.json", {"run_id": projection["run_id"]}
                )
            return projection

    def reconcile_completed_active_pointer(self, run_id: str) -> dict[str, Any]:
        with self.lock():
            projection = self.status(run_id)
            if projection["state"] == "completed":
                self._clear_active_pointer(run_id)
            return projection

    def identity(self, run_id: str) -> dict[str, Any]:
        return self._identity(run_id)

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
        if not path.exists():
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
        with self.lock():
            path = self._evidence_path(run_id, relative_path)
            run_dir = self._run_dir(run_id)
            if path == _evidence_unit_root(path, run_dir) / "manifest.json":
                raise EvidenceError("manifest.json is reserved")
            if _sealed_ancestor(path, run_dir):
                raise EvidenceError("completed evidence is read-only")
            encoded = redact_text(value).encode("utf-8")
            if _is_stream(path) and len(encoded) > STREAM_BYTE_LIMIT:
                raise EvidenceTooLarge(
                    f"evidence stream exceeds {STREAM_BYTE_LIMIT} bytes"
                )
            _secure_directory(path.parent)
            _write_new_bytes(path, encoded, self.root)
            return path

    def write_evidence_value(self, run_id: str, relative_path: str, value: Any) -> Any:
        """Redact and persist one canonical structured evidence value."""
        with self.lock():
            path = self._evidence_path(run_id, relative_path)
            run_dir = self._run_dir(run_id)
            if path == _evidence_unit_root(path, run_dir) / "manifest.json":
                raise EvidenceError("manifest.json is reserved")
            if _sealed_ancestor(path, run_dir):
                raise EvidenceError("completed evidence is read-only")
            redacted = redact_artifact_value(value)
            encoded = (canonical_json(redacted) + "\n").encode("utf-8")
            _secure_directory(path.parent)
            _write_new_bytes(path, encoded, self.root)
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
            directory = self._evidence_path(run_id, relative_directory)
            if not directory.is_dir() or directory.is_symlink():
                raise EvidenceError(
                    f"evidence directory does not exist: {relative_directory}"
                )
            manifest_path = directory / "manifest.json"
            if manifest_path.exists() or manifest_path.is_symlink():
                raise EvidenceError("evidence is already sealed")
            files = _evidence_files(directory)
            limit = _tree_limit(relative_directory)
            _validate_evidence_sizes(files, limit)
            entries = _manifest_entries(directory, files, limit)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "files": entries,
                "total_bytes": sum(entry["bytes"] for entry in entries),
            }
            for path in files:
                path.chmod(0o400)
            for path in sorted(
                [entry for entry in directory.rglob("*") if entry.is_dir()],
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                path.chmod(0o500)
            _write_new_json(manifest_path, manifest, self.root, mode=0o400)
            directory.chmod(0o500)
            _fsync_directory(directory)
            _fsync_directory(directory.parent)
            return manifest

    def verify_evidence(self, run_id: str, relative_directory: str) -> bool:
        directory = self._evidence_path(run_id, relative_directory)
        manifest_path = directory / "manifest.json"
        try:
            if not stat.S_ISREG(manifest_path.lstat().st_mode):
                raise EvidenceTampered("evidence manifest is invalid")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except EvidenceTampered:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceTampered("evidence manifest is missing or invalid") from exc
        expected = _validate_manifest(manifest)
        try:
            files = _evidence_files(directory)
            observed = _manifest_entries(
                directory, files, _tree_limit(relative_directory)
            )
        except EvidenceError as exc:
            raise EvidenceTampered(str(exc)) from exc
        if observed != expected:
            raise EvidenceTampered("evidence does not match its manifest")
        total_bytes = manifest.get("total_bytes")
        if total_bytes != sum(entry["bytes"] for entry in observed):
            raise EvidenceTampered("evidence manifest total is invalid")
        directories = [
            directory,
            *(path for path in directory.rglob("*") if path.is_dir()),
        ]
        for path in [*files, manifest_path, *directories]:
            if stat.S_IMODE(path.stat().st_mode) & 0o222:
                raise EvidenceTampered("sealed evidence is writable")
        return True

    def reconcile_evidence_result(
        self, run_id: str, relative_directory: str, value: Any
    ) -> Any:
        """Recover or verify one evidence result, then seal its evidence unit."""
        with self.lock():
            directory = self._evidence_path(run_id, relative_directory)
            manifest_path = directory / "manifest.json"
            result_path = directory / "result.json"
            expected = redact_artifact_value(value)
            if manifest_path.is_file():
                self._verify_or_finish_seal(run_id, relative_directory)
                stored = _read_evidence_result(result_path)
                if stored != expected:
                    raise EvidenceError("evidence result contradicts expected value")
                return stored

            if directory.exists():
                entries = {path.name for path in directory.iterdir()}
                if entries not in (set(), {"result.json"}):
                    raise EvidenceError("unsealed evidence result is ambiguous")
            if result_path.is_file():
                if _read_evidence_result(result_path) != expected:
                    raise EvidenceError("evidence result contradicts expected value")
            else:
                self.write_evidence_value(
                    run_id, f"{relative_directory}/result.json", expected
                )
            self.seal_evidence(run_id, relative_directory)
            return expected

    def sealed_evidence_result(
        self, run_id: str, relative_directory: str
    ) -> Any | None:
        """Return a verified sealed result, or None when it has not been sealed."""
        with self.lock():
            directory = self._evidence_path(run_id, relative_directory)
            manifest_path = directory / "manifest.json"
            if manifest_path.is_symlink() or (
                manifest_path.exists() and not manifest_path.is_file()
            ):
                raise EvidenceTampered("evidence manifest is invalid")
            if not manifest_path.is_file():
                return None
            self._verify_or_finish_seal(run_id, relative_directory)
            return _read_evidence_result(directory / "result.json")

    def _verify_or_finish_seal(self, run_id: str, relative_directory: str) -> None:
        try:
            self.verify_evidence(run_id, relative_directory)
            return
        except EvidenceTampered as exc:
            if str(exc) != "sealed evidence is writable":
                raise

        directory = self._evidence_path(run_id, relative_directory)
        manifest_path = directory / "manifest.json"
        files = _evidence_files(directory)
        nested_directories = [path for path in directory.rglob("*") if path.is_dir()]
        for path in [*files, manifest_path, *nested_directories]:
            if stat.S_IMODE(path.stat().st_mode) & 0o222:
                raise EvidenceTampered("sealed evidence is writable")
        directory.chmod(0o500)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)
        self.verify_evidence(run_id, relative_directory)

    def unsealed_evidence_result(
        self, run_id: str, relative_directory: str
    ) -> Any | None:
        """Return one complete unsealed result, rejecting partial evidence."""
        with self.lock():
            directory = self._evidence_path(run_id, relative_directory)
            manifest_path = directory / "manifest.json"
            if manifest_path.exists() or manifest_path.is_symlink():
                raise EvidenceError("unsealed evidence contains an invalid manifest")
            if not directory.exists():
                return None
            if not directory.is_dir():
                raise EvidenceError("unsealed evidence result is invalid")
            entries = list(directory.iterdir())
            if not entries:
                return None
            result_path = directory / "result.json"
            if (
                len(entries) != 1
                or entries[0].name != "result.json"
                or result_path.is_symlink()
                or not result_path.is_file()
            ):
                raise EvidenceError("unsealed evidence result is partial or ambiguous")
            return _read_evidence_result(result_path)

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
        record = redact_artifact_value(
            {
                "schema_version": SCHEMA_VERSION,
                "sequence": len(events) + 1,
                "recorded_at": recorded_at or utc_now(),
                "event": event,
                **({"state": state} if state is not None else {}),
                "data": data or {},
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

    def _identity(self, run_id: str) -> dict[str, Any]:
        _validate_run_id(run_id)
        path = self._run_dir(run_id) / "run.json"
        try:
            identity = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RunNotFound(f"Run not found: {run_id}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventHistoryCorrupt(f"Run identity is invalid: {run_id}") from exc
        if (
            not isinstance(identity, dict)
            or set(identity)
            != {
                "schema_version",
                "run_id",
                "bead_id",
                "repository",
                "base_branch",
                "base_sha",
                "created_at",
                "start_request",
            }
            or type(identity.get("schema_version")) is not int
            or identity["schema_version"] != SCHEMA_VERSION
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
            if _project(identity, events)["state"] != "completed":
                active.append(run_dir.name)
        if len(active) > 1:
            raise EventHistoryCorrupt("multiple Active Runs exist")
        return active[0] if active else None

    def _reconcile_completed_active_pointer(self) -> dict[str, Any] | None:
        run_id = self._active_pointer_run_id()
        if run_id is None:
            return None
        identity = self._identity(run_id)
        events, _ = self._read_events(run_id)
        projection = _project(identity, events)
        if projection["state"] != "completed":
            return None
        try:
            with self.lock():
                if self._active_pointer_run_id() != run_id:
                    return None
                identity = self._identity(run_id)
                events, _ = self._read_events(run_id)
                projection = _project(identity, events)
                if projection["state"] != "completed":
                    return None
                self._clear_active_pointer(run_id)
        except RunStoreBusy:
            pass
        return projection

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
                        self.verify_evidence(run_id, relative)
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
            if path.suffix != ".json" or not RUN_ID_PATTERN.fullmatch(path.stem):
                raise ResumePreflightInvalid(f"Effect record is invalid: {path.name}")
            try:
                self.effect(run_id, path.stem)
            except RunStoreError as exc:
                raise ResumePreflightInvalid(str(exc)) from exc

    def _evidence_path(self, run_id: str, relative: str) -> Path:
        parts = Path(relative).parts
        if (
            not parts
            or Path(relative).is_absolute()
            or parts[0] not in EVIDENCE_ROOTS
            or ".." in parts
        ):
            raise EvidenceError(
                "evidence path must stay under attempts, gates, or retrospective"
            )
        run_path = self._run_dir(run_id)
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
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise RunStoreError("run_id contains unsupported characters")


def _require_mode(path: Path, mode: int, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EventHistoryCorrupt(f"{label} is invalid") from exc
    if stat.S_ISLNK(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != mode:
        raise EventHistoryCorrupt(f"{label} permissions are invalid")


def _projected_evidence_units(value: Any) -> set[str]:
    units: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "evidence" and isinstance(nested, str):
                parts = Path(nested).parts
                if len(parts) >= 2 and parts[0] in EVIDENCE_ROOTS:
                    units.add(f"{parts[0]}/{parts[1]}")
            units.update(_projected_evidence_units(nested))
    elif isinstance(value, list):
        for nested in value:
            units.update(_projected_evidence_units(nested))
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

    if "validation" in projection:
        add(projection["validation"], "validation")
    if "bead_spec" in projection:
        add(projection["bead_spec"], "Bead/spec")
    cycles = projection.get("gate_cycles")
    if isinstance(cycles, list):
        for cycle in cycles:
            if isinstance(cycle, dict) and "validation" in cycle:
                add(cycle["validation"], "Gate validation")
    return digests


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
        "candidate_sha",
        "pr_number",
        "pr_url",
        "pr_head_sha",
        "pr_ready",
        "merge",
        "remote_branch_deleted",
        "bead_closure",
        "validation",
        "validation_attempt",
        "previous_candidate_sha",
        "repair_attempts_used",
        "repair_brief",
        "repair_dispositions",
        "gate_cycles",
        "gate_retry",
        "completion",
        "bead_spec",
        "interrupted_repair",
    ):
        if key in details:
            projection[key] = details[key]
    return projection


def _validate_open_attempts(projection: dict[str, Any]) -> None:
    validation = projection.get("validation_attempt")
    if isinstance(validation, dict) and validation.get("status") == "started":
        attempt_id = validation.get("attempt_id")
        if (
            set(validation) != {"attempt_id", "candidate_sha", "status", "evidence"}
            or not isinstance(attempt_id, str)
            or not RUN_ID_PATTERN.fullmatch(attempt_id)
            or not isinstance(validation.get("candidate_sha"), str)
            or not SHA_PATTERN.fullmatch(validation["candidate_sha"])
            or validation.get("candidate_sha") != projection.get("candidate_sha")
            or validation.get("evidence") != f"attempts/{attempt_id}"
        ):
            raise ResumePreflightInvalid("open validation attempt is invalid")

    repair = projection.get("repair_brief")
    repair_attempt = repair.get("repair_attempt") if isinstance(repair, dict) else None
    attention = projection.get("attention")
    repair_is_open = projection.get("last_event") == "repair.started" or (
        isinstance(attention, dict)
        and attention.get("scope") == "repair"
        and repair_attempt == projection.get("repair_attempts_used")
        and isinstance(repair, dict)
        and repair.get("candidate_sha") == projection.get("candidate_sha")
    )
    if repair_is_open and (
        not isinstance(repair, dict)
        or not repair
        or set(repair)
        not in (
            {"candidate_sha", "repair_attempt", "blocking_findings"},
            {
                "schema_version",
                "candidate_sha",
                "repair_attempt",
                "blocking_findings",
            },
        )
        or (
            "schema_version" in repair
            and (
                type(repair["schema_version"]) is not int
                or repair["schema_version"] != SCHEMA_VERSION
            )
        )
        or not isinstance(repair.get("candidate_sha"), str)
        or not SHA_PATTERN.fullmatch(repair["candidate_sha"])
        or repair.get("candidate_sha") != projection.get("candidate_sha")
        or type(repair_attempt) is not int
        or not 1 <= repair_attempt <= 4
        or not isinstance(repair.get("blocking_findings"), list)
    ):
        raise ResumePreflightInvalid("open repair attempt is invalid")


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


def _read_evidence_result(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("evidence result is missing or malformed") from exc


def _write_new_bytes(
    path: Path, value: bytes, staging_root: Path, *, mode: int = 0o600
) -> None:
    staging_directory = staging_root / ".unpublished"
    _secure_directory(staging_directory)
    target_id = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{target_id}.", suffix=".tmp", dir=staging_directory
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        written = os.write(descriptor, value)
        if written != len(value):
            raise RunStoreError(f"write was incomplete: {path.name}")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path)
        _fsync_directory(path.parent)
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


def _evidence_files(directory: Path) -> list[Path]:
    files = []
    root_manifest = directory / "manifest.json"
    for path in directory.rglob("*"):
        if path == root_manifest:
            continue
        if path.is_symlink():
            raise EvidenceError("evidence must not contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise EvidenceError("evidence must contain only regular files")
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(directory).as_posix())


def _manifest_entries(
    directory: Path, files: list[Path], byte_limit: int
) -> list[dict[str, Any]]:
    _validate_evidence_sizes(files, byte_limit)
    entries = []
    for path in files:
        size = path.stat().st_size
        try:
            payload = path.read_bytes()
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError("evidence must be regular UTF-8 text") from exc
        if redact_text(text) != text:
            raise EvidenceError(
                "evidence must cross the redaction boundary before sealing"
            )
        entries.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "bytes": size,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return entries


def _validate_evidence_sizes(files: list[Path], byte_limit: int) -> None:
    total = 0
    for path in files:
        size = path.stat().st_size
        if _is_stream(path) and size > STREAM_BYTE_LIMIT:
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


def _sealed_ancestor(path: Path, run_dir: Path) -> bool:
    manifest = _evidence_unit_root(path, run_dir) / "manifest.json"
    return manifest.exists() or manifest.is_symlink()


def _evidence_unit_root(path: Path, run_dir: Path) -> Path:
    relative = path.relative_to(run_dir)
    return run_dir.joinpath(*relative.parts[:2])


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
