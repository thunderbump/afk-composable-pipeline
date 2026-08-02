from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from afk.checkouts import is_exact_clean_commit, run_trusted_read_git
from afk.jsonutil import canonical_json
from afk.process_supervision import SupervisedCommandError, run_supervised_command


SCHEMA_VERSION = 1
CANDIDATE_TIMEOUT_SECONDS = 300
CANDIDATE_OUTPUT_BYTE_LIMIT = 1024 * 1024
CANDIDATE_CLEANUP_SECONDS = 1
MAX_CANDIDATE_TIMEOUT_SECONDS = 3600
MAX_CANDIDATE_OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024


class CandidateBrokerError(ValueError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated Candidate command")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    result_path = Path(args.result)
    try:
        result_path.unlink(missing_ok=True)
        request = _read_request(Path(args.request))
        result = run_candidate(request)
        _publish_result(result_path, result)
    except (CandidateBrokerError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _publish_result(path: Path, result: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(canonical_json(result) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_candidate(request: dict[str, Any]) -> dict[str, Any]:
    candidate = Path(request["candidate_path"])
    _require_exact_candidate(candidate, request["candidate_sha"])
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise CandidateBrokerError("bubblewrap is unavailable")
    with tempfile.TemporaryDirectory(prefix="afk-candidate-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        _materialize_candidate_snapshot(candidate, request["candidate_sha"], snapshot)
        command = [
            bwrap,
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/work",
            "--ro-bind",
            str(snapshot),
            "/candidate",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/work",
            "--chdir",
            "/work",
            "--",
            *request["command"],
        ]
        try:
            completed = run_supervised_command(
                command,
                cwd=Path.cwd(),
                environment=os.environ.copy(),
                timeout_seconds=request.get(
                    "timeout_seconds", CANDIDATE_TIMEOUT_SECONDS
                ),
                output_byte_limit=request.get(
                    "output_byte_limit", CANDIDATE_OUTPUT_BYTE_LIMIT
                ),
                cleanup_seconds=CANDIDATE_CLEANUP_SECONDS,
                input_text=None,
                label="Candidate command",
                decode_errors="replace",
                _precontained_command=True,
            )
        except OSError:
            return _failed_execution_result(
                request["candidate_sha"],
                "launch_failure",
                "Candidate command could not be launched",
            )
        except SupervisedCommandError as exc:
            if exc.classification not in {
                "timeout",
                "output_overflow",
                "abnormal_exit",
            }:
                raise CandidateBrokerError(
                    "Candidate command supervision failed"
                ) from exc
            return _failed_execution_result(
                request["candidate_sha"],
                exc.classification,
                exc.summary,
                exit_code=exc.exit_code,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            )
    if completed.returncode != 0:
        return _failed_execution_result(
            request["candidate_sha"],
            "abnormal_exit",
            f"Candidate command exited with status {completed.returncode}",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": request["candidate_sha"],
        "status": "completed",
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _failed_execution_result(
    candidate_sha: str,
    classification: str,
    summary: str,
    *,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": candidate_sha,
        "status": "failed",
        "failure_classification": classification,
        "summary": summary,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def _read_request(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise CandidateBrokerError("candidate broker request is invalid") from exc
    if (
        not isinstance(value, dict)
        or not {"schema_version", "candidate_sha", "candidate_path", "command"}
        <= set(value)
        <= {
            "schema_version",
            "candidate_sha",
            "candidate_path",
            "command",
            "timeout_seconds",
            "output_byte_limit",
        }
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or not _is_sha(value.get("candidate_sha"))
        or not _is_os_argument(value.get("candidate_path"))
        or not Path(value["candidate_path"]).is_absolute()
        or not isinstance(value.get("command"), list)
        or not value["command"]
        or not all(_is_os_argument(item) for item in value["command"])
        or not _is_timeout_seconds(
            value.get("timeout_seconds", CANDIDATE_TIMEOUT_SECONDS)
        )
        or not _is_output_byte_limit(
            value.get("output_byte_limit", CANDIDATE_OUTPUT_BYTE_LIMIT)
        )
    ):
        raise CandidateBrokerError("candidate broker request is invalid")
    return value


def _require_exact_candidate(candidate: Path, candidate_sha: str) -> None:
    try:
        top_level = run_trusted_read_git(
            ["rev-parse", "--show-toplevel"],
            cwd=candidate,
        )
    except OSError as exc:
        raise CandidateBrokerError("Candidate repository root is unavailable") from exc
    if top_level.returncode != 0 or not top_level.stdout.strip():
        raise CandidateBrokerError("Candidate repository root is unavailable")
    try:
        candidate_root = candidate.resolve(strict=True)
        repository_root = Path(top_level.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise CandidateBrokerError(
            "Candidate does not match its exact clean commit"
        ) from exc
    if candidate_root != repository_root:
        raise CandidateBrokerError("Candidate path is not the repository root")
    if not is_exact_clean_commit(candidate, candidate_sha):
        raise CandidateBrokerError("Candidate does not match its exact clean commit")


def _materialize_candidate_snapshot(
    candidate: Path, candidate_sha: str, destination: Path
) -> None:
    listing = run_trusted_read_git(
        ["ls-tree", "-rz", "--full-tree", candidate_sha],
        cwd=candidate,
        text=False,
    )
    if listing.returncode != 0:
        raise CandidateBrokerError("exact Candidate snapshot is unavailable")
    destination.mkdir(mode=0o700)
    try:
        records = listing.stdout.rstrip(b"\0").split(b"\0") if listing.stdout else []
        for record in records:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split()
            if (mode, object_type) not in {
                (b"100644", b"blob"),
                (b"100755", b"blob"),
                (b"120000", b"blob"),
            }:
                raise CandidateBrokerError(
                    "exact Candidate snapshot contains an unsupported entry"
                )
            relative = PurePosixPath(os.fsdecode(path_bytes))
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise CandidateBrokerError("exact Candidate snapshot path is invalid")
            blob = run_trusted_read_git(
                ["cat-file", "blob", object_id.decode("ascii")],
                cwd=candidate,
                text=False,
            )
            if blob.returncode != 0:
                raise CandidateBrokerError("exact Candidate snapshot is unavailable")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == b"120000":
                os.symlink(os.fsdecode(blob.stdout), target)
            else:
                target.write_bytes(blob.stdout)
                target.chmod(0o755 if mode == b"100755" else 0o644)
    except CandidateBrokerError:
        raise
    except (OSError, ValueError) as exc:
        raise CandidateBrokerError("exact Candidate snapshot is invalid") from exc


def _is_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_os_argument(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        return False
    return True


def _is_timeout_seconds(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 < value <= MAX_CANDIDATE_TIMEOUT_SECONDS
    )


def _is_output_byte_limit(value: Any) -> bool:
    return type(value) is int and 0 < value <= MAX_CANDIDATE_OUTPUT_BYTE_LIMIT


if __name__ == "__main__":
    raise SystemExit(main())
