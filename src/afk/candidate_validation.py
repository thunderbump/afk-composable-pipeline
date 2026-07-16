from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.run_store import RunStore
from afk.validation_contract import ValidationContractError, parse_validation_contract


BOOTSTRAP_ADAPTER = "afk.builtin.bootstrap-validation/v1"
EVIDENCE_FILE_BYTE_LIMIT = 16 * 1024 * 1024
EVIDENCE_TOTAL_BYTE_LIMIT = 64 * 1024 * 1024
RESULT_BYTE_LIMIT = 1024 * 1024
OUTPUT_BYTE_LIMIT = 1024 * 1024


class CandidateValidationError(RuntimeError):
    def __init__(self, kind: str, summary: str):
        super().__init__(summary)
        self.kind = kind
        self.summary = summary


def validate_candidate(store: RunStore, run_id: str) -> dict[str, Any]:
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
        try:
            completed = _run_contract(
                [*contract["command"], "--request", str(request_path.resolve())],
                cwd=worktree,
                environment=_validation_environment(staging),
                timeout_seconds=contract["timeout_seconds"],
                output_directory=staging,
            )
        except OSError as exc:
            raise CandidateValidationError(
                "invalid", "validation command is unavailable or not executable"
            ) from exc
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
        try:
            value = (worktree / "afk.toml").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CandidateValidationError(
                "invalid", "afk.toml is missing or invalid"
            ) from exc
        return _parse_contract(value)
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
    if identity["source"] != "pinned_base":
        return
    for argument in command:
        if not argument.startswith("./"):
            continue
        relative = argument.removeprefix("./")
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
        "rejected": {"passed", "rejected"},
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
            and not any(
                check["status"] in {"inconclusive", "not_run"} for check in checks
            )
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
    output_directory: Path,
) -> subprocess.CompletedProcess[str]:
    stdout_path = output_directory / "stdout.raw"
    stderr_path = output_directory / "stderr.raw"
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        try:
            process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise CandidateValidationError(
                "interrupted",
                "validation timed out and its process group was terminated",
            ) from exc
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.returncode < 0:
        signal_number = -process.returncode
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = str(signal_number)
        raise CandidateValidationError(
            "interrupted", f"validation exited after signal {signal_name}"
        )
    if (
        stdout_path.stat().st_size > OUTPUT_BYTE_LIMIT
        or stderr_path.stat().st_size > OUTPUT_BYTE_LIMIT
    ):
        raise CandidateValidationError(
            "invalid", "validation output exceeds the size limit"
        )
    try:
        stdout = stdout_path.read_text(encoding="utf-8")
        stderr = stderr_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateValidationError(
            "invalid", "validation output must be UTF-8 text"
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
