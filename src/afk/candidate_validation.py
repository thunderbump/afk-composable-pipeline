from __future__ import annotations

import ctypes
import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.redaction import redact_text
from afk.run_store import GATE_BYTE_LIMIT, RunStore, RunStoreError
from afk.validation_contract import (
    VALIDATION_STATUS_EXIT_CODES,
    ValidationContractError,
    parse_validation_contract,
)


BOOTSTRAP_ADAPTER = "afk.builtin.bootstrap-validation/v1"
BOOTSTRAP_RUNNER = Path(__file__).with_name("bootstrap_adapter.py")
RESULT_BYTE_LIMIT = 1024 * 1024
OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
PROCESS_CLEANUP_SECONDS = 1
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
TRUSTED_SCRIPT_INTERPRETERS = {"python", "python3"}
AFK_EVIDENCE_NAMESPACE = "afk"
CONTRACT_EVIDENCE_NAMESPACE = "contract"
VALIDATION_ENVIRONMENT_ALLOWLIST = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
)


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
    store: RunStore,
    run_id: str,
    *,
    attempt_id: str,
    attempt_evidence: str,
    gate_evidence: str,
) -> dict[str, Any]:
    projection = store.status(run_id)
    try:
        candidate_sha = projection["candidate_sha"]
        worktree = Path(projection["worktree_path"])
        contract_identity = projection["validation_contract"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateValidationError(
            "invalid", "Run lacks exact Candidate validation identity"
        ) from exc
    contract = _load_contract(worktree, contract_identity)
    evidence_relative = gate_evidence

    _require_immutable_candidate(worktree, candidate_sha)
    _require_trusted_harness(
        worktree, candidate_sha, contract_identity, contract["command"]
    )
    with (
        tempfile.TemporaryDirectory(prefix="afk-validation-") as temporary,
        ExitStack() as cleanup,
    ):
        staging = Path(temporary)
        evidence = staging / "evidence"
        evidence.mkdir(mode=0o700)
        evidence_descriptor = os.open(
            evidence,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        cleanup.callback(os.close, evidence_descriptor)
        evidence_view = Path(f"/proc/self/fd/{evidence_descriptor}")
        request_path = staging / "request.json"
        request = {
            "schema_version": 1,
            "run_id": run_id,
            "candidate_sha": candidate_sha,
            "evidence_dir": str(evidence.resolve()),
        }
        request_path.write_text(canonical_json(request) + "\n", encoding="utf-8")
        request_path.chmod(0o400)
        command = list(contract["command"])
        if "bootstrap_harness" in contract:
            harness = staging / "approved-bootstrap-harness"
            _materialize_bootstrap_harness(
                worktree, contract["bootstrap_harness"], harness
            )
            command.extend(["--harness", str(harness)])
        store.write_evidence_text(
            run_id,
            f"{attempt_evidence}/{AFK_EVIDENCE_NAMESPACE}/request.json",
            canonical_json(request) + "\n",
        )
        try:
            completed = _run_contract(
                [*command, "--request", str(request_path.resolve())],
                cwd=worktree,
                environment=_validation_environment(staging),
                timeout_seconds=contract["timeout_seconds"],
            )
        except OSError as exc:
            raise CandidateValidationError(
                "invalid", "validation command is unavailable or not executable"
            ) from exc
        store.write_evidence_text(
            run_id,
            f"{attempt_evidence}/{AFK_EVIDENCE_NAMESPACE}/stdout.log",
            completed.stdout,
        )
        store.write_evidence_text(
            run_id,
            f"{attempt_evidence}/{AFK_EVIDENCE_NAMESPACE}/stderr.log",
            completed.stderr,
        )
        _require_immutable_candidate(worktree, candidate_sha)
        _require_original_evidence_directory(evidence, evidence_descriptor)
        result = _read_result(evidence_view / "result.json", candidate_sha, completed)
        evidence_files, contract_evidence_bytes = _require_evidence_tree(evidence_view)
        _require_regular_logs(evidence_files, result["checks"])
        outcome = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "candidate_sha": candidate_sha,
            "exit_code": completed.returncode,
            "status": result["status"],
            "summary": result["summary"],
        }
        gate_metadata = (
            canonical_json(request) + "\n",
            completed.stdout,
            completed.stderr,
            canonical_json(outcome) + "\n",
        )
        gate_bytes = contract_evidence_bytes + sum(
            len(redact_text(value).encode("utf-8")) for value in gate_metadata
        )
        if gate_bytes > GATE_BYTE_LIMIT:
            raise CandidateValidationError(
                "invalid", "validation Gate evidence exceeds the size limit"
            )
        store.write_evidence_text(
            run_id,
            f"{evidence_relative}/{AFK_EVIDENCE_NAMESPACE}/request.json",
            canonical_json(request) + "\n",
        )
        store.write_evidence_text(
            run_id,
            f"{evidence_relative}/{AFK_EVIDENCE_NAMESPACE}/stdout.log",
            completed.stdout,
        )
        store.write_evidence_text(
            run_id,
            f"{evidence_relative}/{AFK_EVIDENCE_NAMESPACE}/stderr.log",
            completed.stderr,
        )
        store.write_evidence_text(
            run_id,
            f"{evidence_relative}/{AFK_EVIDENCE_NAMESPACE}/outcome.json",
            canonical_json(outcome) + "\n",
        )
        _require_original_evidence_directory(evidence, evidence_descriptor)
        for path in sorted(evidence_view.rglob("*")):
            if path.is_file():
                relative = path.relative_to(evidence_view).as_posix()
                store.ingest_evidence_file(
                    run_id,
                    f"{evidence_relative}/{CONTRACT_EVIDENCE_NAMESPACE}/{relative}",
                    path,
                )
        _require_original_evidence_directory(evidence, evidence_descriptor)
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


def recover_candidate_validation(
    store: RunStore, run_id: str, attempt: dict[str, str]
) -> dict[str, Any] | None:
    attempt_id = attempt.get("attempt_id")
    candidate_sha = attempt.get("candidate_sha")
    if not isinstance(attempt_id, str) or not isinstance(candidate_sha, str):
        return None
    evidence_relative = f"gates/{attempt_id}"
    manifest_path = store.root / "runs" / run_id / evidence_relative / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        store.verify_evidence(run_id, evidence_relative)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        outcome = json.loads(
            (manifest_path.parent / AFK_EVIDENCE_NAMESPACE / "outcome.json").read_text(
                encoding="utf-8"
            )
        )
        contract = store.status(run_id)["validation_contract"]
    except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError, RunStoreError):
        return None
    if (
        not isinstance(outcome, dict)
        or set(outcome)
        != {
            "schema_version",
            "attempt_id",
            "candidate_sha",
            "exit_code",
            "status",
            "summary",
        }
        or type(outcome.get("schema_version")) is not int
        or outcome["schema_version"] != 1
        or outcome.get("attempt_id") != attempt_id
        or outcome.get("candidate_sha") != candidate_sha
        or not isinstance(outcome.get("status"), str)
        or type(outcome.get("exit_code")) is not int
        or outcome.get("exit_code")
        != VALIDATION_STATUS_EXIT_CODES.get(outcome.get("status"))
        or not isinstance(outcome.get("summary"), str)
        or not outcome["summary"].strip()
        or not isinstance(contract, dict)
    ):
        return None
    return {
        "status": outcome["status"],
        "candidate_sha": candidate_sha,
        "exit_code": outcome["exit_code"],
        "summary": outcome["summary"],
        "evidence": evidence_relative,
        "manifest_sha256": _manifest_digest(manifest),
        "contract": contract,
        "next_action": {
            "passed": "advance",
            "rejected": "repair",
            "inconclusive": "attention",
        }[outcome["status"]],
    }


def _load_contract(worktree: Path, identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise CandidateValidationError("invalid", "validation identity is invalid")
    if identity.get("source") == "approved_bootstrap":
        approval = _bootstrap_approval(identity)
        return {
            "command": [
                sys.executable,
                str(BOOTSTRAP_RUNNER),
            ],
            "timeout_seconds": approval["timeout_seconds"],
            "bootstrap_harness": approval["harness"],
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
    if identity.get("source") == "approved_bootstrap":
        approval = _bootstrap_approval(identity)
        if approval["candidate_sha"] != candidate_sha:
            raise CandidateValidationError(
                "invalid", "bootstrap approval is bound to another Candidate"
            )
        harness = approval["harness"]
        observed = tracked_regular_file_identity(
            worktree, candidate_sha, harness["path"]
        )
        if observed != (harness["mode"], harness["blob_sha"]):
            raise CandidateValidationError(
                "invalid", "approved bootstrap harness identity has drifted"
            )
        return
    executable = command[0]
    if executable.startswith("./"):
        harness_start = 0
    elif (
        executable in TRUSTED_SCRIPT_INTERPRETERS
        and len(command) >= 2
        and not command[1].startswith("-")
        and _contained_relative_path(command[1].removeprefix("./"))
        and os.path.lexists(worktree / command[1])
    ):
        harness_start = 1
    else:
        raise CandidateValidationError(
            "invalid",
            "pinned validation command grammar requires a direct ./ executable or "
            "python/python3 followed immediately by a relative repository script",
        )
    for argument in command[harness_start:]:
        relative = argument.removeprefix("./")
        if not argument.startswith("./"):
            path = Path(argument)
            if (
                argument.startswith("-")
                or path.is_absolute()
                or not _contained_relative_path(argument)
                or not os.path.lexists(worktree / path)
            ):
                continue
        if not _contained_relative_path(relative):
            raise CandidateValidationError(
                "invalid", "pinned validation harness path is invalid"
            )
        trusted = tracked_regular_file_identity(
            worktree, identity["base_sha"], relative
        )
        candidate = tracked_regular_file_identity(worktree, candidate_sha, relative)
        if trusted is None or candidate is None:
            raise CandidateValidationError(
                "invalid",
                "pinned validation harness must be a regular tracked file",
            )
        if trusted != candidate:
            raise CandidateValidationError(
                "invalid",
                "Candidate validation harness differs from the trusted pinned base",
            )


def tracked_regular_file_identity(
    worktree: Path, commit_sha: str, relative: str
) -> tuple[str, str] | None:
    observed = subprocess.run(
        ["git", "ls-tree", "-z", commit_sha, "--", relative],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    records = observed.stdout.rstrip(b"\0").split(b"\0")
    if observed.returncode != 0 or len(records) != 1 or b"\t" not in records[0]:
        return None
    metadata, path = records[0].split(b"\t", 1)
    fields = metadata.split()
    if (
        len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
        or path != os.fsencode(relative)
    ):
        return None
    return fields[0].decode("ascii"), fields[2].decode("ascii")


def approve_bootstrap_contract(
    worktree: Path,
    candidate_sha: str,
    identity: Any,
    harness_path: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    if (
        isinstance(identity, dict)
        and set(identity) == {"source", "base_sha", "adapter_id", "approval"}
        and identity.get("source") == "approved_bootstrap"
        and identity.get("adapter_id") == BOOTSTRAP_ADAPTER
    ):
        _bootstrap_approval(identity)
        identity = {
            "source": identity["source"],
            "base_sha": identity["base_sha"],
            "adapter_id": identity["adapter_id"],
        }
    if (
        not isinstance(identity, dict)
        or set(identity) != {"source", "base_sha", "adapter_id"}
        or identity.get("source") != "approved_bootstrap"
        or identity.get("adapter_id") != BOOTSTRAP_ADAPTER
    ):
        raise CandidateValidationError(
            "invalid", "Run does not have an unapproved bootstrap identity"
        )
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
        raise CandidateValidationError(
            "invalid", "bootstrap timeout_seconds must be between 1 and 86400"
        )
    relative = Path(harness_path.removeprefix("./")).as_posix()
    if not _contained_relative_path(relative):
        raise CandidateValidationError("invalid", "bootstrap harness path is invalid")
    _require_immutable_candidate(worktree, candidate_sha)
    observed = tracked_regular_file_identity(worktree, candidate_sha, relative)
    if observed is None or observed[0] != "100755":
        raise CandidateValidationError(
            "invalid", "bootstrap harness must be a tracked executable regular file"
        )
    return {
        **identity,
        "approval": {
            "schema_version": 1,
            "candidate_sha": candidate_sha,
            "command": [f"./{relative}"],
            "timeout_seconds": timeout_seconds,
            "harness": {
                "path": relative,
                "mode": observed[0],
                "blob_sha": observed[1],
            },
        },
    }


def _bootstrap_approval(identity: dict[str, Any]) -> dict[str, Any]:
    if (
        set(identity) != {"source", "base_sha", "adapter_id", "approval"}
        or identity.get("adapter_id") != BOOTSTRAP_ADAPTER
        or not isinstance(identity.get("approval"), dict)
    ):
        raise CandidateValidationError(
            "invalid", "approved bootstrap policy is unavailable"
        )
    approval = identity["approval"]
    harness = approval.get("harness")
    if (
        set(approval)
        != {
            "schema_version",
            "candidate_sha",
            "command",
            "timeout_seconds",
            "harness",
        }
        or type(approval.get("schema_version")) is not int
        or approval["schema_version"] != 1
        or not isinstance(approval.get("candidate_sha"), str)
        or not isinstance(harness, dict)
    ):
        raise CandidateValidationError("invalid", "bootstrap approval is invalid")
    if (
        type(approval.get("timeout_seconds")) is not int
        or not 1 <= approval["timeout_seconds"] <= 86400
        or set(harness) != {"path", "mode", "blob_sha"}
        or harness.get("mode") != "100755"
        or not isinstance(harness.get("blob_sha"), str)
        or len(harness["blob_sha"]) != 40
        or not _contained_relative_path(harness.get("path", ""))
        or approval.get("command") != [f"./{harness['path']}"]
    ):
        raise CandidateValidationError("invalid", "bootstrap approval is invalid")
    return approval


def _materialize_bootstrap_harness(
    worktree: Path, harness: dict[str, str], destination: Path
) -> None:
    content = subprocess.run(
        ["git", "cat-file", "blob", harness["blob_sha"]],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if content.returncode != 0:
        raise CandidateValidationError(
            "invalid", "approved bootstrap harness blob is unavailable"
        )
    destination.write_bytes(content.stdout)
    destination.chmod(0o500)


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
    expected_exit = VALIDATION_STATUS_EXIT_CODES.get(status)
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
        or _checks_resume_after_not_run(checks)
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


def _checks_resume_after_not_run(checks: list[dict[str, Any]]) -> bool:
    skipped = False
    for check in checks:
        if check["status"] == "not_run":
            skipped = True
        elif skipped:
            return True
    return False


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


def _require_evidence_tree(evidence: Path) -> tuple[set[str], int]:
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
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CandidateValidationError(
                "invalid", "validation evidence must be UTF-8 text"
            ) from exc
        total += len(redact_text(value).encode("utf-8"))
        if total > GATE_BYTE_LIMIT:
            raise CandidateValidationError(
                "invalid", "validation Gate evidence exceeds the size limit"
            )
        files.add(path.relative_to(evidence).as_posix())
    return files, total


def _require_original_evidence_directory(path: Path, descriptor: int) -> None:
    try:
        expected = os.fstat(descriptor)
        observed = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise CandidateValidationError(
            "invalid", "validation evidence directory was replaced"
        ) from exc
    if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise CandidateValidationError(
            "invalid", "validation evidence directory was replaced"
        )


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
    environment = {
        name: os.environ[name]
        for name in VALIDATION_ENVIRONMENT_ALLOWLIST
        if name in os.environ
    }
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("LC_ALL", "C.UTF-8")
    if "HOME" not in environment:
        home = temporary / "home"
        home.mkdir(mode=0o700)
        environment["HOME"] = str(home)
    if "TMPDIR" not in environment:
        tmp = temporary / "tmp"
        tmp.mkdir(mode=0o700)
        environment["TMPDIR"] = str(tmp)
    return environment


def _run_contract(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return run_supervised_command(
        command,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        label="validation",
    )


def run_supervised_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    input_text: str | None = None,
    label: str,
) -> subprocess.CompletedProcess[str]:
    subject = label.strip() or "command"
    deadline = time.monotonic() + timeout_seconds
    with _LinuxDescendantSupervisor() as descendants:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            descendants.track(process.pid)
        except CandidateValidationError:
            descendants.terminate_untracked(process)
            raise
        assert process.stdout is not None and process.stderr is not None
        input_bytes = memoryview(input_text.encode("utf-8")) if input_text else None
        input_offset = 0
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = threading.Event()
        readers = [
            threading.Thread(
                target=_capture_output,
                args=(stream, captured[name], overflow),
                daemon=True,
            )
            for name, stream in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            )
        ]
        for reader in readers:
            reader.start()
        while process.poll() is None and not overflow.is_set():
            descendants.discover(process.pid)
            if time.monotonic() >= deadline:
                _close_process_input(process)
                descendants.terminate(process.pid)
                process.poll()
                _join_readers(readers)
                stdout, stderr = _diagnostic_output(captured)
                raise CandidateValidationError(
                    "interrupted",
                    f"{subject} timed out and its process tree was terminated",
                    stdout=stdout,
                    stderr=stderr,
                )
            if process.stdin is not None:
                input_offset = _feed_process_input(process, input_bytes, input_offset)
            overflow.wait(0.01)
        _close_process_input(process)
        descendants.terminate(process.pid)
        _join_readers(readers)
        if overflow.is_set():
            raise CandidateValidationError(
                "invalid",
                f"{subject} output exceeds the size limit",
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
                f"{subject} exited after signal {signal_name}",
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
                f"{subject} output must be UTF-8 text",
                stdout=diagnostic_stdout,
                stderr=diagnostic_stderr,
            ) from exc
        return subprocess.CompletedProcess(
            command, process.returncode, redact_text(stdout), redact_text(stderr)
        )


def _feed_process_input(
    process: subprocess.Popen[bytes],
    input_bytes: memoryview | None,
    input_offset: int,
) -> int:
    assert process.stdin is not None
    if input_bytes is None or input_offset == len(input_bytes):
        process.stdin.close()
        process.stdin = None
        return input_offset
    try:
        written = os.write(process.stdin.fileno(), input_bytes[input_offset:])
    except BlockingIOError:
        return input_offset
    except BrokenPipeError:
        process.stdin.close()
        process.stdin = None
        return input_offset
    input_offset += written
    if input_offset == len(input_bytes):
        process.stdin.close()
        process.stdin = None
    return input_offset


def _close_process_input(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        process.stdin.close()
        process.stdin = None


def _capture_output(
    stream: Any,
    captured: bytearray,
    overflow: threading.Event,
) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            if len(captured) + len(chunk) > OUTPUT_BYTE_LIMIT:
                overflow.set()
            elif not overflow.is_set():
                captured.extend(chunk)
    finally:
        stream.close()


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

    def terminate_untracked(self, process: subprocess.Popen[bytes]) -> None:
        failure: OSError | None = None
        deadline = time.monotonic() + PROCESS_CLEANUP_SECONDS
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                failure = exc
            while time.monotonic() < deadline:
                process.poll()
                children = [
                    pid
                    for pid in _proc_children(os.getpid())
                    if pid not in self._baseline
                ]
                for pid in children:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError as exc:
                        failure = failure or exc
                for pid in children:
                    if pid == process.pid:
                        continue
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                    except OSError as exc:
                        failure = failure or exc
                process.poll()
                remaining = [
                    pid
                    for pid in _proc_children(os.getpid())
                    if pid not in self._baseline
                ]
                if process.returncode is not None and not remaining:
                    if failure is None:
                        return
                    break
                time.sleep(0.01)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise CandidateValidationError(
            "interrupted", "untracked process tree could not be terminated"
        ) from failure

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
