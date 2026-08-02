from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from afk.checkouts import is_exact_clean_commit, run_trusted_read_git
from afk.jsonutil import canonical_json
from afk.process_supervision import (
    SupervisedCommandError,
    _receive_protocol_message,
    _send_protocol_message,
    run_supervised_command,
)


SCHEMA_VERSION = 1
CANDIDATE_TIMEOUT_SECONDS = 300
CANDIDATE_OUTPUT_BYTE_LIMIT = 1024 * 1024
CANDIDATE_CLEANUP_SECONDS = 1
MAX_CANDIDATE_TIMEOUT_SECONDS = 3600
MAX_CANDIDATE_OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
CONTAINER_RUNTIME_PROBE_SECONDS = 5
CONTAINER_WATCHDOG_SECONDS = 5
_CONTAINER_WATCHDOG_PATH = Path(__file__).with_name("container_cleanup_watchdog.py")


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
    execution = request.get("execution")
    container_runtime = None
    if execution is not None:
        container_runtime = _find_container_runtime()
        if container_runtime is None:
            return _failed_execution_result(
                request["candidate_sha"],
                "adapter_unavailable",
                "Container execution adapter is unavailable",
            )
        container_image, inspect_stderr = _inspect_container_image(
            container_runtime, execution["image"]
        )
        if container_image is None:
            return _failed_execution_result(
                request["candidate_sha"],
                "launch_failure",
                "Candidate command could not be launched",
                stderr=inspect_stderr,
            )
    bwrap = shutil.which("bwrap") if execution is None else None
    if execution is None and bwrap is None:
        raise CandidateBrokerError("bubblewrap is unavailable")
    with tempfile.TemporaryDirectory(prefix="afk-candidate-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        _materialize_candidate_snapshot(candidate, request["candidate_sha"], snapshot)
        container_name = None
        container_id_path = None
        if execution is None:
            command = _bubblewrap_command(bwrap, snapshot, request["command"])
        else:
            snapshot.chmod(0o755)
            container_name = f"afk-candidate-{uuid.uuid4().hex}"
            container_id_path = Path(temporary) / "container.cid"
            command = _container_command(
                container_runtime,
                container_name,
                container_id_path,
                snapshot,
                container_image,
                request["command"],
            )
        cleanup_watchdog = None
        if container_name is not None:
            cleanup_watchdog = _start_container_cleanup_watchdog(
                container_runtime, container_name
            )
        interrupted_cleanup = container_name is not None
        container_started = False
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
                _precontained_command=execution is None,
                _trusted_host_command=execution is not None,
            )
            container_started = (
                container_id_path is not None and container_id_path.is_file()
            )
            interrupted_cleanup = False
        except OSError:
            interrupted_cleanup = False
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
        finally:
            if cleanup_watchdog is not None:
                if interrupted_cleanup or container_started:
                    try:
                        _remove_container(container_runtime, container_name)
                    except BaseException:
                        _trigger_container_cleanup_watchdog(cleanup_watchdog)
                        raise
                try:
                    _disarm_container_cleanup_watchdog(cleanup_watchdog)
                except BaseException:
                    _trigger_container_cleanup_watchdog(cleanup_watchdog)
                    raise
    if execution is not None and not container_started:
        return _failed_execution_result(
            request["candidate_sha"],
            "launch_failure",
            "Candidate command could not be launched",
            stdout=completed.stdout,
            stderr=completed.stderr,
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


def _find_container_runtime() -> str | None:
    for name in ("docker", "podman"):
        runtime = shutil.which(name)
        if runtime is None:
            continue
        try:
            completed = run_supervised_command(
                [runtime, "info"],
                cwd=Path.cwd(),
                environment=os.environ.copy(),
                timeout_seconds=CONTAINER_RUNTIME_PROBE_SECONDS,
                output_byte_limit=64 * 1024,
                cleanup_seconds=CANDIDATE_CLEANUP_SECONDS,
                input_text=None,
                label="Candidate container runtime probe",
                decode_errors="replace",
                _trusted_host_command=True,
            )
        except (OSError, SupervisedCommandError):
            continue
        if completed.returncode == 0:
            return runtime
    return None


def _inspect_container_image(runtime: str, image: str) -> tuple[str | None, str]:
    try:
        completed = run_supervised_command(
            [runtime, "image", "inspect", image],
            cwd=Path.cwd(),
            environment=os.environ.copy(),
            timeout_seconds=CONTAINER_RUNTIME_PROBE_SECONDS,
            output_byte_limit=CANDIDATE_OUTPUT_BYTE_LIMIT,
            cleanup_seconds=CANDIDATE_CLEANUP_SECONDS,
            input_text=None,
            label="Candidate container image inspection",
            decode_errors="replace",
            _trusted_host_command=True,
        )
    except (OSError, SupervisedCommandError):
        return None, ""
    if completed.returncode != 0:
        return None, completed.stderr
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(metadata, list) or len(metadata) != 1:
        return None, ""
    record = metadata[0]
    if not isinstance(record, dict) or not isinstance(record.get("Config"), dict):
        return None, ""
    image_id = record.get("Id")
    volumes = record["Config"].get("Volumes")
    if not _is_container_image(image_id) or volumes is not None and volumes != {}:
        return None, ""
    return image_id, ""


def _bubblewrap_command(
    bwrap: str, snapshot: Path, candidate_command: list[str]
) -> list[str]:
    return [
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
        *candidate_command,
    ]


def _container_command(
    runtime: str,
    name: str,
    container_id_path: Path,
    snapshot: Path,
    image: str,
    candidate_command: list[str],
) -> list[str]:
    work_tmpfs = "/work:rw,nosuid,nodev,mode=0700,uid=65534,gid=65534"
    if Path(runtime).name == "podman":
        work_tmpfs = "/work:rw,nosuid,nodev,mode=1777"
    return [
        runtime,
        "run",
        "--pull=never",
        "--cidfile",
        str(container_id_path),
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--user",
        "65534:65534",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,mode=1777",
        "--tmpfs",
        work_tmpfs,
        "--mount",
        f"type=bind,src={snapshot},dst=/candidate,readonly",
        "--workdir",
        "/work",
        "--entrypoint",
        candidate_command[0],
        image,
        *candidate_command[1:],
    ]


def _remove_container(runtime: str, name: str) -> None:
    try:
        completed = run_supervised_command(
            [runtime, "rm", "--force", "--volumes", name],
            cwd=Path.cwd(),
            environment=os.environ.copy(),
            timeout_seconds=10,
            output_byte_limit=64 * 1024,
            cleanup_seconds=CANDIDATE_CLEANUP_SECONDS,
            input_text=None,
            label="Candidate container cleanup",
            decode_errors="replace",
            _trusted_host_command=True,
        )
    except (OSError, SupervisedCommandError) as exc:
        raise CandidateBrokerError("Candidate container cleanup failed") from exc
    if completed.returncode != 0:
        raise CandidateBrokerError("Candidate container cleanup failed")


def _watchdog_environment() -> dict[str, str]:
    allowed = {"HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}
    environment = {name: value for name, value in os.environ.items() if name in allowed}
    environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": os.defpath})
    return environment


def _start_container_cleanup_watchdog(
    runtime: str, name: str
) -> tuple[subprocess.Popen[bytes], socket.socket]:
    parent_channel, child_channel = socket.socketpair()
    watchdog = None
    try:
        try:
            watchdog = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(_CONTAINER_WATCHDOG_PATH),
                    "0",
                    str(os.getpid()),
                ],
                stdin=child_channel,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_watchdog_environment(),
                start_new_session=True,
            )
        except BaseException:
            parent_channel.close()
            raise
    finally:
        child_channel.close()
    try:
        parent_channel.settimeout(CONTAINER_WATCHDOG_SECONDS)
        _send_protocol_message(
            parent_channel,
            {"schema_version": 1, "runtime": runtime, "container_name": name},
        )
        if _receive_protocol_message(parent_channel) != {
            "schema_version": 1,
            "status": "armed",
        }:
            raise ValueError("invalid watchdog response")
        if watchdog is None:
            raise ValueError("watchdog failed to start")
        return watchdog, parent_channel
    except BaseException as exc:
        parent_channel.close()
        if watchdog is not None:
            watchdog.kill()
            watchdog.wait()
        raise CandidateBrokerError(
            "Candidate container cleanup watchdog failed"
        ) from exc


def _disarm_container_cleanup_watchdog(
    watchdog: tuple[subprocess.Popen[bytes], socket.socket],
) -> None:
    process, channel = watchdog
    try:
        _send_protocol_message(channel, {"schema_version": 1, "action": "disarm"})
        if _receive_protocol_message(channel) != {
            "schema_version": 1,
            "status": "disarmed",
        }:
            raise ValueError("invalid watchdog response")
        if process.wait(timeout=CONTAINER_WATCHDOG_SECONDS) != 0:
            raise ValueError("watchdog failed")
    except BaseException as exc:
        raise CandidateBrokerError(
            "Candidate container cleanup watchdog failed"
        ) from exc
    finally:
        channel.close()


def _trigger_container_cleanup_watchdog(
    watchdog: tuple[subprocess.Popen[bytes], socket.socket],
) -> None:
    process, channel = watchdog
    channel.close()
    try:
        process.wait(timeout=4 * CONTAINER_WATCHDOG_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


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
    return validate_candidate_request(value)


def validate_candidate_request(value: object) -> dict[str, Any]:
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
            "execution",
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
        or not _is_execution(value.get("execution"))
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
            parent = target.parent
            while parent != destination:
                if parent.is_symlink():
                    raise CandidateBrokerError("exact Candidate snapshot is invalid")
                parent.chmod(0o755)
                parent = parent.parent
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


def _is_execution(value: Any) -> bool:
    return value is None or (
        isinstance(value, dict)
        and set(value) == {"type", "image"}
        and value.get("type") == "container"
        and _is_container_image(value.get("image"))
    )


def _is_container_image(value: Any) -> bool:
    return _is_os_argument(value) and not value.startswith("-")


if __name__ == "__main__":
    raise SystemExit(main())
