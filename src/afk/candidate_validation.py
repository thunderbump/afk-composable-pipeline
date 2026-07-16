from __future__ import annotations

import ctypes
import hashlib
import json
import os
import select
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.redaction import redact_text
from afk.run_store import RunStore
from afk.validation_contract import ValidationContractError, parse_validation_contract


BOOTSTRAP_ADAPTER = "afk.builtin.bootstrap-validation/v1"
BOOTSTRAP_COMMAND = ["./scripts/validation-worker.sh", "run"]
BOOTSTRAP_TIMEOUT_SECONDS = 2700
EVIDENCE_FILE_BYTE_LIMIT = 16 * 1024 * 1024
EVIDENCE_TOTAL_BYTE_LIMIT = 64 * 1024 * 1024
RESULT_BYTE_LIMIT = 1024 * 1024
OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
PROCESS_CLEANUP_SECONDS = 1
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


class CandidateValidationError(RuntimeError):
    def __init__(
        self,
        kind: str,
        summary: str,
        *,
        stdout: str | None = None,
        stderr: str | None = None,
    ):
        super().__init__(summary)
        self.kind = kind
        self.summary = summary
        self.stdout = stdout
        self.stderr = stderr


def validate_candidate(
    store: RunStore, run_id: str, *, attempt_evidence: str
) -> dict[str, Any]:
    projection = store.status(run_id)
    identity = store.identity(run_id)
    try:
        candidate_sha = projection["candidate_sha"]
        worktree = Path(projection["worktree_path"])
        contract_identity = identity["start_request"]["validation_contract"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateValidationError(
            "invalid", "Run lacks exact Candidate validation identity"
        ) from exc
    contract = _load_contract(worktree, contract_identity)
    evidence_relative = f"gates/validation-{candidate_sha}"

    _require_immutable_candidate(worktree, candidate_sha)
    _require_trusted_harness(
        worktree, candidate_sha, contract_identity, contract["command"]
    )
    with tempfile.TemporaryDirectory(prefix="afk-validation-") as temporary:
        staging = Path(temporary)
        evidence = staging / "evidence"
        evidence.mkdir(mode=0o700)
        request_path = staging / "request.json"
        request = {
            "schema_version": 1,
            "run_id": run_id,
            "candidate_sha": candidate_sha,
            "evidence_dir": str(evidence.resolve()),
        }
        request_path.write_text(canonical_json(request) + "\n", encoding="utf-8")
        request_path.chmod(0o400)
        store.write_evidence_text(
            run_id, f"{attempt_evidence}/request.json", canonical_json(request) + "\n"
        )
        try:
            completed = _run_contract(
                [*contract["command"], "--request", str(request_path.resolve())],
                cwd=worktree,
                environment=_validation_environment(staging),
                timeout_seconds=contract["timeout_seconds"],
            )
        except OSError as exc:
            raise CandidateValidationError(
                "invalid", "validation command is unavailable or not executable"
            ) from exc
        store.write_evidence_text(
            run_id, f"{attempt_evidence}/stdout.log", completed.stdout
        )
        store.write_evidence_text(
            run_id, f"{attempt_evidence}/stderr.log", completed.stderr
        )
        _require_immutable_candidate(worktree, candidate_sha)
        result = _read_result(evidence / "result.json", candidate_sha, completed)
        evidence_files = _require_evidence_tree(evidence)
        _require_regular_logs(evidence_files, result["checks"])
        store.write_evidence_text(
            run_id, f"{evidence_relative}/request.json", canonical_json(request) + "\n"
        )
        store.write_evidence_text(
            run_id, f"{evidence_relative}/stdout.log", completed.stdout
        )
        store.write_evidence_text(
            run_id, f"{evidence_relative}/stderr.log", completed.stderr
        )
        outcome = {
            "schema_version": 1,
            "candidate_sha": candidate_sha,
            "exit_code": completed.returncode,
            "status": result["status"],
            "summary": result["summary"],
        }
        store.write_evidence_text(
            run_id,
            f"{evidence_relative}/outcome.json",
            canonical_json(outcome) + "\n",
        )
        for path in sorted(evidence.rglob("*")):
            if path.is_file():
                relative = path.relative_to(evidence).as_posix()
                store.ingest_evidence_file(
                    run_id, f"{evidence_relative}/{relative}", path
                )
    manifest = store.seal_evidence(run_id, evidence_relative)
    validation = {
        "status": result["status"],
        "candidate_sha": candidate_sha,
        "exit_code": completed.returncode,
        "summary": result["summary"],
        "evidence": evidence_relative,
        "manifest_sha256": _manifest_digest(manifest),
        "contract": contract_identity,
        "next_action": {
            "passed": "advance",
            "rejected": "repair",
            "inconclusive": "attention",
        }[result["status"]],
    }
    return validation


def _load_contract(worktree: Path, identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise CandidateValidationError("invalid", "validation identity is invalid")
    if identity.get("source") == "approved_bootstrap":
        if (
            set(identity) != {"source", "base_sha", "adapter_id"}
            or identity.get("adapter_id") != BOOTSTRAP_ADAPTER
        ):
            raise CandidateValidationError(
                "invalid", "approved bootstrap identity is invalid"
            )
        return {
            "command": list(BOOTSTRAP_COMMAND),
            "timeout_seconds": BOOTSTRAP_TIMEOUT_SECONDS,
        }
    if identity.get("source") != "pinned_base" or set(identity) != {
        "source",
        "base_sha",
        "blob_sha",
    }:
        raise CandidateValidationError(
            "invalid", "validation contract source is not supported"
        )
    observed = subprocess.run(
        ["git", "rev-parse", f"{identity['base_sha']}:afk.toml"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    content = subprocess.run(
        ["git", "cat-file", "blob", identity["blob_sha"]],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if (
        observed.returncode != 0
        or observed.stdout.strip() != identity["blob_sha"]
        or content.returncode != 0
    ):
        raise CandidateValidationError(
            "invalid", "pinned validation contract is unavailable"
        )
    return _parse_contract(content.stdout)


def _parse_contract(value: str) -> dict[str, Any]:
    try:
        return parse_validation_contract(value)
    except ValidationContractError as exc:
        raise CandidateValidationError("invalid", f"afk.toml {exc}") from exc


def _require_trusted_harness(
    worktree: Path,
    candidate_sha: str,
    identity: dict[str, str],
    command: list[str],
) -> None:
    for position, argument in enumerate(command):
        relative = argument.removeprefix("./")
        if not argument.startswith("./"):
            path = Path(argument)
            if (
                argument.startswith("-")
                or path.is_absolute()
                or (position == 0 and "/" not in argument)
                or not _contained_relative_path(argument)
                or not os.path.lexists(worktree / path)
            ):
                continue
        if not _contained_relative_path(relative):
            raise CandidateValidationError(
                "invalid", "pinned validation harness path is invalid"
            )
        trusted = subprocess.run(
            ["git", "rev-parse", f"{identity['base_sha']}:{relative}"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
        )
        candidate = subprocess.run(
            ["git", "rev-parse", f"{candidate_sha}:{relative}"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
        )
        if (
            trusted.returncode != 0
            or candidate.returncode != 0
            or trusted.stdout.strip() != candidate.stdout.strip()
        ):
            raise CandidateValidationError(
                "invalid",
                "Candidate validation harness differs from the trusted pinned base",
            )


def _read_result(
    path: Path, candidate_sha: str, completed: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CandidateValidationError(
            "invalid", "validation result must be a regular file"
        )
    if path.stat().st_size > RESULT_BYTE_LIMIT:
        raise CandidateValidationError(
            "invalid", "validation result exceeds the size limit"
        )
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError(
            "invalid", "validation result is missing or invalid"
        ) from exc
    checks = result.get("checks") if isinstance(result, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    expected_exit = {"passed": 0, "rejected": 1, "inconclusive": 2}.get(status)
    allowed_check_status = {
        "passed": {"passed"},
        "rejected": {"passed", "rejected", "inconclusive", "not_run"},
        "inconclusive": {"passed", "inconclusive", "not_run"},
    }.get(status, set())
    if isinstance(checks, list) and any(
        isinstance(check, dict)
        and isinstance(check.get("log_path"), str)
        and not _contained_relative_path(check["log_path"])
        for check in checks
    ):
        raise CandidateValidationError(
            "invalid", "validation evidence path escapes its directory"
        )
    if (
        completed.returncode != expected_exit
        or not isinstance(result, dict)
        or set(result)
        != {"schema_version", "candidate_sha", "status", "summary", "checks"}
        or type(result.get("schema_version")) is not int
        or result.get("schema_version") != 1
        or result.get("candidate_sha") != candidate_sha
        or status not in {"passed", "rejected", "inconclusive"}
        or not isinstance(result.get("summary"), str)
        or not result["summary"].strip()
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, dict)
            or set(check) != {"name", "status", "log_path"}
            or not isinstance(check.get("name"), str)
            or not check["name"].strip()
            or check.get("status") not in allowed_check_status
            or not isinstance(check.get("log_path"), str)
            or not check["log_path"].strip()
            for check in checks
        )
        or len({check["name"] for check in checks}) != len(checks)
        or (
            status == "rejected"
            and not any(check["status"] == "rejected" for check in checks)
        )
        or (
            status == "inconclusive"
            and not any(check["status"] == "inconclusive" for check in checks)
        )
    ):
        raise CandidateValidationError(
            "invalid", "validation exit, result, or checks disagree"
        )
    return result


def _contained_relative_path(value: str) -> bool:
    path = Path(value)
    return not path.is_absolute() and value not in {"", "."} and ".." not in path.parts


def _require_regular_logs(
    evidence_files: set[str], checks: list[dict[str, Any]]
) -> None:
    for check in checks:
        if Path(check["log_path"]).as_posix() not in evidence_files:
            raise CandidateValidationError(
                "invalid", "validation evidence log must be a regular file"
            )


def _require_evidence_tree(evidence: Path) -> set[str]:
    total = 0
    files: set[str] = set()
    for path in evidence.rglob("*"):
        if path.is_symlink():
            raise CandidateValidationError(
                "invalid", "validation evidence must contain only regular files"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise CandidateValidationError(
                "invalid", "validation evidence must contain only regular files"
            )
        size = path.stat().st_size
        if size > EVIDENCE_FILE_BYTE_LIMIT:
            raise CandidateValidationError(
                "invalid", "validation evidence file exceeds the size limit"
            )
        total += size
        if total > EVIDENCE_TOTAL_BYTE_LIMIT:
            raise CandidateValidationError(
                "invalid", "validation evidence tree exceeds the size limit"
            )
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CandidateValidationError(
                "invalid", "validation evidence must be UTF-8 text"
            ) from exc
        files.add(path.relative_to(evidence).as_posix())
    return files


def _require_immutable_candidate(worktree: Path, candidate_sha: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if (
        head.returncode != 0
        or head.stdout.strip() != candidate_sha
        or dirty.returncode != 0
        or dirty.stdout
    ):
        raise CandidateValidationError(
            "head_mismatch", "Candidate changed during validation"
        )


def _validation_environment(temporary: Path) -> dict[str, str]:
    home = temporary / "home"
    home.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }


def _run_contract(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    with _LinuxDescendantSupervisor() as descendants:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        descendants.track(process.pid)
        assert process.stdout is not None and process.stderr is not None
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        captured_bytes = [0]
        capture_lock = threading.Lock()
        overflow = threading.Event()
        readers = [
            threading.Thread(
                target=_capture_output,
                args=(stream, captured[name], captured_bytes, capture_lock, overflow),
                daemon=True,
            )
            for name, stream in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            )
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None and not overflow.is_set():
            descendants.discover(process.pid)
            if time.monotonic() >= deadline:
                descendants.terminate(process.pid)
                _join_readers(readers)
                stdout, stderr = _diagnostic_output(captured)
                raise CandidateValidationError(
                    "interrupted",
                    "validation timed out and its process tree was terminated",
                    stdout=stdout,
                    stderr=stderr,
                )
            overflow.wait(0.01)
        descendants.terminate(process.pid)
        _join_readers(readers)
        if overflow.is_set():
            raise CandidateValidationError(
                "invalid",
                "validation output exceeds the size limit",
                stdout="",
                stderr="",
            )
        if process.returncode < 0:
            signal_number = -process.returncode
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = str(signal_number)
            stdout, stderr = _diagnostic_output(captured)
            raise CandidateValidationError(
                "interrupted",
                f"validation exited after signal {signal_name}",
                stdout=stdout,
                stderr=stderr,
            )
        try:
            stdout = bytes(captured["stdout"]).decode("utf-8")
            stderr = bytes(captured["stderr"]).decode("utf-8")
        except UnicodeDecodeError as exc:
            diagnostic_stdout, diagnostic_stderr = _diagnostic_output(captured)
            raise CandidateValidationError(
                "invalid",
                "validation output must be UTF-8 text",
                stdout=diagnostic_stdout,
                stderr=diagnostic_stderr,
            ) from exc
        return subprocess.CompletedProcess(
            command, process.returncode, redact_text(stdout), redact_text(stderr)
        )


def _capture_output(
    stream: Any,
    captured: bytearray,
    captured_bytes: list[int],
    lock: threading.Lock,
    overflow: threading.Event,
) -> None:
    while chunk := stream.read(64 * 1024):
        with lock:
            if captured_bytes[0] + len(chunk) > OUTPUT_BYTE_LIMIT:
                overflow.set()
            elif not overflow.is_set():
                captured.extend(chunk)
                captured_bytes[0] += len(chunk)


def _diagnostic_output(captured: dict[str, bytearray]) -> tuple[str, str]:
    return tuple(
        redact_text(bytes(captured[name]).decode("utf-8", errors="replace"))
        for name in ("stdout", "stderr")
    )


def _join_readers(readers: list[threading.Thread]) -> None:
    deadline = time.monotonic() + PROCESS_CLEANUP_SECONDS
    for reader in readers:
        reader.join(timeout=max(deadline - time.monotonic(), 0))
    if any(reader.is_alive() for reader in readers):
        raise CandidateValidationError(
            "interrupted", "validation output streams could not be drained"
        )


class _LinuxDescendantSupervisor:
    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._previous = ctypes.c_int()
        self._baseline: set[int] = set()
        self._pidfds: dict[int, int] = {}
        self._root_pid: int | None = None

    def __enter__(self) -> _LinuxDescendantSupervisor:
        if (
            self._libc.prctl(
                PR_GET_CHILD_SUBREAPER, ctypes.byref(self._previous), 0, 0, 0
            )
            != 0
            or self._libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0
        ):
            raise CandidateValidationError(
                "invalid", "Linux validation descendant supervision is unavailable"
            )
        self._baseline = set(_proc_children(os.getpid()))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if self._root_pid is not None and self._pidfds:
                self.terminate(self._root_pid)
        finally:
            for pidfd in self._pidfds.values():
                os.close(pidfd)
            self._pidfds.clear()
            if (
                self._libc.prctl(PR_SET_CHILD_SUBREAPER, self._previous.value, 0, 0, 0)
                != 0
            ):
                raise CandidateValidationError(
                    "interrupted", "Linux validation descendant supervision was lost"
                )

    def track(self, pid: int) -> None:
        self._root_pid = pid
        self._track(pid)

    def discover(self, root_pid: int) -> None:
        pending = [
            root_pid,
            *(pid for pid in _proc_children(os.getpid()) if pid not in self._baseline),
        ]
        seen: set[int] = set()
        while pending:
            pid = pending.pop()
            if pid in seen or pid == os.getpid():
                continue
            seen.add(pid)
            self._track(pid)
            pending.extend(_proc_children(pid))

    def terminate(self, root_pid: int) -> None:
        if self._wait_for_exit(root_pid, signal.SIGTERM):
            return
        if self._wait_for_exit(root_pid, signal.SIGKILL):
            return
        raise CandidateValidationError(
            "interrupted", "validation process tree could not be terminated"
        )

    def _wait_for_exit(self, root_pid: int, requested_signal: signal.Signals) -> bool:
        deadline = time.monotonic() + PROCESS_CLEANUP_SECONDS
        while time.monotonic() < deadline:
            self.discover(root_pid)
            self._discard_exited()
            if not self._pidfds:
                self.discover(root_pid)
                self._discard_exited()
                if not self._pidfds:
                    return True
            for pidfd in tuple(self._pidfds.values()):
                try:
                    signal.pidfd_send_signal(pidfd, requested_signal)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    raise CandidateValidationError(
                        "interrupted", "validation process tree could not be signalled"
                    ) from exc
            time.sleep(0.01)
        return False

    def _track(self, pid: int) -> None:
        if pid in self._pidfds:
            return
        try:
            self._pidfds[pid] = os.pidfd_open(pid)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise CandidateValidationError(
                "invalid", "Linux validation descendant supervision is unavailable"
            ) from exc

    def _discard_exited(self) -> None:
        if not self._pidfds:
            return
        poller = select.poll()
        for pidfd in self._pidfds.values():
            poller.register(pidfd, select.POLLIN)
        readable = {pidfd for pidfd, _ in poller.poll(0)}
        for pid, pidfd in tuple(self._pidfds.items()):
            if pidfd not in readable:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            os.close(pidfd)
            del self._pidfds[pid]


def _proc_children(pid: int) -> list[int]:
    children: set[int] = set()
    try:
        tasks = list(Path(f"/proc/{pid}/task").iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CandidateValidationError(
            "invalid", "Linux validation descendant supervision is unavailable"
        ) from exc
    for task in tasks:
        try:
            values = (task / "children").read_text(encoding="utf-8").split()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CandidateValidationError(
                "invalid", "Linux validation descendant supervision is unavailable"
            ) from exc
        children.update(int(value) for value in values if value.isdigit())
    return sorted(children)


def _manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
