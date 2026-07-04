from __future__ import annotations

import codecs
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.redaction import is_secret_command_flag, redact_artifact_value, redact_text, redact_url
from afk.role_adapters import (
    RoleAdapterRuntimeError,
    execute_role_command,
    minimal_command_environment,
    read_json_result_file,
    render_command,
    write_adapter_logs,
)


SCHEMA_VERSION = 1


WorkerRuntimeError = RoleAdapterRuntimeError


def validate_step(context: Any) -> dict[str, Any]:
    return validate(
        context.input_data,
        project_contract=context.project_contract,
        run_id=context.run_id,
        run_dir=context.run_dir,
    )


def validate(
    input_data: Any,
    *,
    project_contract: Any = None,
    run_id: str,
    run_dir: Path | None,
) -> dict[str, Any]:
    request = normalize_request(input_data, project_contract=project_contract, run_id=run_id)
    if request["status"] != "valid":
        return request
    if run_dir is None:
        return invalid_request("run_dir is required")
    run_dir = run_dir.resolve(strict=False)

    worker_request = build_worker_request(request, run_dir)
    request_path = run_dir / "worker-request.json"
    evidence_dir = Path(worker_request["evidence_dir"])
    evidence_result_path = evidence_dir / "result.json"
    result_path = evidence_dir / "worker-output.json"
    worker_result_path = run_dir / "worker-result.json"
    stdout_log_path = run_dir / "stdout.log"
    stderr_log_path = run_dir / "stderr.log"

    write_json(request_path, redact_artifact_value(worker_request))

    adapter_result: dict[str, Any] | None = None
    try:
        adapter_result = run_command_adapter(
            request["worker"],
            checkout_path=Path(request["checkout"]["path"]),
            request_path=request_path,
            result_path=result_path,
            evidence_dir=evidence_dir,
            profile=worker_request["profile"],
        )
    except WorkerRuntimeError as exc:
        stdout = redact_text(exc.stdout)
        stderr = redact_text(exc.stderr or exc.message)
        write_adapter_logs(stdout, stderr)
        read_worker_payload(result_path, fallback_path=evidence_result_path)
        sync_worker_failure_artifacts(
            result_path,
            evidence_result_path,
            "failed_timeout" if exc.timed_out else "failed_runtime",
            "timeout" if exc.timed_out else "runtime_failure",
            exc.message,
            details=exc.failure_artifact,
        )
        normalized = normalized_worker_result(
            status="failed_timeout" if exc.timed_out else "failed_runtime",
            classification="timeout" if exc.timed_out else "runtime_failure",
            summary=exc.message,
            raw_result=None,
            adapter=adapter_record(
                request["worker"],
                exc.returncode,
                exc.timed_out,
                command=exc.command,
                details={**exc.adapter_details, **exc.failure_artifact},
            ),
            stdout=stdout,
            stderr=stderr,
            evidence_dir=evidence_dir,
            stdout_path=stdout_log_path,
            stderr_path=stderr_log_path,
        )
        worker_result = {"raw": None, "normalized": normalized}
        write_worker_result(worker_result_path, run_id, worker_result)
        return validate_output(request, worker_request, worker_result)

    stdout = redact_text(adapter_result["stdout"])
    stderr = redact_text(adapter_result["stderr"])
    write_adapter_logs(stdout, stderr)

    raw_payload = read_worker_payload(result_path, fallback_path=evidence_result_path)
    if raw_payload["status"] == "valid":
        sync_worker_payload_artifacts(result_path, evidence_result_path, raw_payload["payload"])
    if raw_payload["status"] != "valid":
        if raw_payload["status"] == "missing":
            status = "failed_missing_result"
            classification = "missing_worker_result"
        else:
            status = "failed_protocol"
            classification = "protocol_failure"
        sync_worker_failure_artifacts(result_path, evidence_result_path, status, classification, raw_payload["message"])
        normalized = normalized_worker_result(
            status=status,
            classification=classification,
            summary=raw_payload["message"],
            raw_result=None,
            adapter=adapter_record(
                request["worker"],
                adapter_result["returncode"],
                False,
                command=adapter_result.get("command"),
            ),
            stdout=stdout,
            stderr=stderr,
            evidence_dir=evidence_dir,
            stdout_path=stdout_log_path,
            stderr_path=stderr_log_path,
        )
        worker_result = {"raw": None, "normalized": normalized}
        write_worker_result(worker_result_path, run_id, worker_result)
        return validate_output(request, worker_request, worker_result)

    raw_result = raw_payload["payload"]
    normalized = normalize_worker_payload(
        raw_result,
        adapter=adapter_record(
            request["worker"],
            adapter_result["returncode"],
            False,
            command=adapter_result.get("command"),
        ),
        stdout=stdout,
        stderr=stderr,
        evidence_dir=evidence_dir,
        stdout_path=stdout_log_path,
        stderr_path=stderr_log_path,
    )
    worker_result = {"raw": raw_result, "normalized": normalized}
    write_worker_result(worker_result_path, run_id, worker_result)
    return validate_output(request, worker_request, worker_result)


def normalize_request(input_data: Any, *, project_contract: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        return invalid_request("request must be an object")

    checkout = normalize_checkout(input_data.get("checkout"))
    if checkout["status"] != "valid":
        return invalid_request(checkout["message"])
    default_project_worker_requested = (
        input_data.get("worker") is None and getattr(project_contract, "project_slug", None) == "bump-eqemu"
    )

    validation_without_external_config = normalize_validation(
        input_data.get("validation", {}),
        project_contract,
        checkout=checkout["checkout"],
        allow_external_worker_config=False,
        defer_external_worker_config=True,
    )
    if validation_without_external_config["status"] != "valid":
        return invalid_request(validation_without_external_config["message"])

    worker = normalize_worker(
        input_data.get("worker"),
        checkout["checkout"],
        project_contract,
        default_timeout_seconds=validation_without_external_config["validation"]["timeout_seconds"],
    )
    if worker["status"] != "valid":
        return invalid_request(worker["message"])
    explicit_local_command_worker_requested = (
        not worker["worker"].get("default_project_worker") and worker["worker"]["type"] == "local-command"
    )

    validation = normalize_validation(
        input_data.get("validation", {}),
        project_contract,
        checkout=checkout["checkout"],
        allow_external_worker_config=(
            default_project_worker_requested or explicit_local_command_worker_requested
        ),
    )
    if validation["status"] != "valid":
        return invalid_request(validation["message"])

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "run_id": run_id,
        "project_slug": getattr(project_contract, "project_slug", ""),
        "project_repo_url": getattr(project_contract, "repo_url", ""),
        "checkout": checkout["checkout"],
        "validation": validation["validation"],
        "worker": {
            **worker["worker"],
            "env": dict(validation["validation"].get("adapter_env", {})),
        },
    }


def invalid_request(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed_invalid_payload",
        "message": message,
    }


def normalize_checkout(checkout: Any) -> dict[str, Any]:
    if not isinstance(checkout, dict):
        return {"status": "invalid", "message": "checkout must be an object"}
    if checkout.get("status") != "prepared":
        return {"status": "invalid", "message": "checkout.status must be prepared"}
    path = string_field(checkout, "checkout_path")
    start_commit = string_field(checkout, "start_commit")
    if not path:
        return {"status": "invalid", "message": "checkout.checkout_path is required"}
    if not start_commit:
        return {"status": "invalid", "message": "checkout.start_commit is required"}
    checkout_path = Path(path)
    if not checkout_path.is_absolute():
        return {"status": "invalid", "message": "checkout.checkout_path must be absolute"}
    if not (checkout_path / ".git").is_dir():
        return {"status": "invalid", "message": "checkout.checkout_path must be a git checkout"}
    return {
        "status": "valid",
        "checkout": {
            "path": str(checkout_path),
            "repo_url": redact_url(string_field(checkout, "repo_url") or ""),
            "review_branch": string_field(checkout, "review_branch") or "",
            "requested_ref": string_field(checkout, "requested_ref") or "",
            "start_commit": start_commit,
        },
    }


def normalize_validation(
    validation: Any,
    project_contract: Any,
    *,
    checkout: dict[str, Any],
    allow_external_worker_config: bool = False,
    defer_external_worker_config: bool = False,
) -> dict[str, Any]:
    if not isinstance(validation, dict):
        return {"status": "invalid", "message": "validation must be an object"}
    profile = string_field(validation, "profile")
    if not profile:
        return {"status": "invalid", "message": "validation.profile is required"}
    available_profiles = []
    profile_request = {}
    if project_contract is not None:
        available_profiles = list(project_contract.validation_profiles)
        if profile not in project_contract.validation_profiles:
            return {
                "status": "invalid",
                "message": f"validation.profile {profile!r} is not declared by the project contract",
            }
        profile_request = dict(getattr(project_contract, "validation_profile_requests", {}).get(profile, {}))
    worker_profile = string_field(profile_request, "profile") or profile
    dry_run = validation.get("dryRun", validation.get("dry_run", False))
    if not isinstance(dry_run, bool):
        return {"status": "invalid", "message": "validation.dry_run must be a boolean"}
    timeout_seconds = validation.get("timeoutSeconds", validation.get("timeout_seconds", 120))
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return {"status": "invalid", "message": "validation.timeout_seconds must be a positive number"}
    external_worker = normalize_external_worker_config(
        validation,
        checkout=checkout,
        allow_external_worker_config=allow_external_worker_config,
        defer_external_worker_config=defer_external_worker_config,
    )
    if external_worker["status"] != "valid":
        return {"status": "invalid", "message": external_worker["message"]}
    profile_request.update(external_worker["worker_request"])
    return {
        "status": "valid",
        "validation": {
            "requested_profile": profile,
            "worker_profile": worker_profile,
            "worker_request": profile_request,
            "adapter_env": external_worker["adapter_env"],
            "available_profiles": available_profiles,
            "dry_run": dry_run,
            "timeout_seconds": float(timeout_seconds),
        },
    }


def normalize_external_worker_config(
    validation: dict[str, Any],
    *,
    checkout: dict[str, Any],
    allow_external_worker_config: bool,
    defer_external_worker_config: bool = False,
) -> dict[str, Any]:
    checkout_path = Path(checkout["path"])
    worker_request: dict[str, Any] = {}
    adapter_env: dict[str, str] = {}

    worker_home = string_field(validation, "worker_home") or string_field(validation, "workerHome")
    stack = validation.get("stack")
    if (worker_home or stack is not None) and not allow_external_worker_config:
        if defer_external_worker_config:
            return {"status": "valid", "worker_request": {}, "adapter_env": {}}
        return {
            "status": "invalid",
            "message": (
                "validation.worker_home and validation.stack are only supported for the default project worker "
                "or explicit local-command validation workers"
            ),
        }
    if worker_home:
        validated = validate_external_path(
            worker_home,
            field="validation.worker_home",
            checkout_path=checkout_path,
        )
        if validated["status"] != "valid":
            return validated
        worker_request["worker_home"] = validated["path"]
        adapter_env["VALIDATION_WORKER_HOME"] = validated["path"]

    if stack is not None:
        if not isinstance(stack, dict):
            return {"status": "invalid", "message": "validation.stack must be an object"}
        stack_path = string_field(stack, "path")
        if not stack_path:
            return {"status": "invalid", "message": "validation.stack.path is required"}
        validated = validate_external_path(
            stack_path,
            field="validation.stack.path",
            checkout_path=checkout_path,
        )
        if validated["status"] != "valid":
            return validated
        role = string_field(stack, "role") or "validation"
        worker_request["stack"] = {"role": role, "path": validated["path"]}
        adapter_env["AKKSTACK_DIR"] = validated["path"]

    return {
        "status": "valid",
        "worker_request": worker_request,
        "adapter_env": adapter_env,
    }


def validate_external_path(value: str, *, field: str, checkout_path: Path) -> dict[str, Any]:
    path = Path(value)
    if not path.is_absolute():
        return {"status": "invalid", "message": f"{field} must be absolute"}
    if path_is_equal_to_or_inside(path, checkout_path):
        return {"status": "invalid", "message": f"{field} must be outside checkout"}
    return {"status": "valid", "path": str(path)}


def normalize_worker(
    worker: Any,
    checkout: dict[str, Any],
    project_contract: Any,
    *,
    default_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    default_project_worker = False
    if worker is None and getattr(project_contract, "project_slug", None) == "bump-eqemu":
        default_project_worker = True
        worker = {
            "type": "local-command",
            "command": [
                str(Path(checkout["path"]) / "scripts" / "validation-worker.sh"),
                "run",
                "--request",
                "{request_path}",
            ],
            "timeout_seconds": default_timeout_seconds if default_timeout_seconds is not None else 120,
        }
    if not isinstance(worker, dict):
        return {"status": "invalid", "message": "worker must be an object"}
    worker_type = worker.get("type")
    if worker_type not in {"local-command", "remote-command"}:
        return {"status": "invalid", "message": "worker.type must be local-command or remote-command"}
    host = string_field(worker, "host") or ""
    if worker_type == "remote-command":
        if not host:
            return {"status": "invalid", "message": "worker.host is required for remote-command"}
        if not checkout["repo_url"]:
            return {"status": "invalid", "message": "checkout.repo_url is required for remote-command"}
    for forbidden_key in ("credentials_path", "auth_file", "token", "api_key", "env"):
        if forbidden_key in worker:
            return {"status": "invalid", "message": f"worker.{forbidden_key} is not supported"}
    command = worker.get("command")
    if not is_string_list(command):
        return {"status": "invalid", "message": "worker.command must be a list of strings"}
    command_secret_error = command_secret_error_message(command)
    if command_secret_error:
        return {"status": "invalid", "message": command_secret_error}
    timeout_seconds = worker.get("timeout_seconds", worker.get("timeoutSeconds", 120))
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return {"status": "invalid", "message": "worker.timeout_seconds must be a positive number"}
    return {
        "status": "valid",
        "worker": {
            "type": worker_type,
            "host": host,
            "command": list(command),
            "timeout_seconds": float(timeout_seconds),
            "default_project_worker": default_project_worker,
        },
    }


def build_worker_request(request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    checkout = request["checkout"]
    validation = request["validation"]
    if request["worker"].get("default_project_worker") and request.get("project_slug") == "bump-eqemu":
        repo = checkout["path"]
        ref = checkout["requested_ref"] or checkout["start_commit"]
        worker_request = {
            "project": request["project_slug"],
            "repo": repo,
            "ref": ref,
            "commit": checkout["start_commit"],
            "profile": validation["worker_profile"],
            "run_id": request["run_id"],
            "evidence_dir": str(run_dir / "validation-evidence"),
            "timeout_seconds": int(validation["timeout_seconds"]),
            "lock_wait_seconds": 30,
        }
        if validation["dry_run"]:
            worker_request["dryRun"] = True
        for key, value in validation["worker_request"].items():
            if key not in {
                "project",
                "repo",
                "ref",
                "commit",
                "profile",
                "run_id",
                "evidence_dir",
                "evidenceDir",
                "timeout_seconds",
                "timeoutSeconds",
                "lock_wait_seconds",
                "lockWaitSeconds",
                "dryRun",
            }:
                worker_request[key] = value
        return worker_request
    if request["worker"]["type"] == "remote-command":
        repo = {
            "url": checkout["repo_url"],
            "commit": checkout["start_commit"],
        }
        if checkout["requested_ref"]:
            repo["ref"] = checkout["requested_ref"]
    else:
        repo = {
            "path": checkout["path"],
            "commit": checkout["start_commit"],
        }
    worker_request = {
        "profile": validation["worker_profile"],
        "repo": repo,
        "evidence_dir": str(run_dir / "validation-evidence"),
        "dryRun": validation["dry_run"],
        "timeoutSeconds": int(validation["timeout_seconds"]),
    }
    for key, value in validation["worker_request"].items():
        if key not in {"profile", "repo", "evidence_dir", "evidenceDir", "dryRun", "timeoutSeconds"}:
            worker_request[key] = value
    return worker_request


def run_command_adapter(
    worker: dict[str, Any],
    *,
    checkout_path: Path,
    request_path: Path,
    result_path: Path,
    evidence_dir: Path,
    profile: str,
) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    env = minimal_command_environment(evidence_dir)
    env["AFK_WORKER_REQUEST"] = str(request_path)
    env["AFK_WORKER_RESULT"] = str(result_path)
    env["AFK_WORKER_EVIDENCE_DIR"] = str(evidence_dir)
    env["AFK_VALIDATION_PROFILE"] = profile
    env.update(worker.get("env", {}))
    if worker["type"] == "remote-command":
        env["AFK_WORKER_REMOTE_HOST"] = worker["host"]
    command = render_command(
        worker["command"],
        {
            "{request_path}": str(request_path),
            "{result_path}": str(result_path),
            "{evidence_dir}": str(evidence_dir),
            "{profile}": profile,
        },
    )
    runner = None
    if worker["type"] == "local-command":
        runner = lambda command, cwd, env, timeout_seconds: run_local_command_adapter(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
    return execute_role_command(
        command=command,
        cwd=checkout_path,
        env=env,
        timeout_seconds=worker["timeout_seconds"],
        runtime_failure_message="worker command failed",
        timeout_message="worker command timed out",
        allow_nonzero=True,
        text=False,
        runner=runner,
    )


STOPPED_PROCESS_REMEDIATION = (
    "Send SIGCONT to the worker process group to resume it for debugging, then fix PTY/job-control "
    "behavior or the worker script before retrying validation. AFK only inspects and cleans up the "
    "worker process group it started; descendants that detach with setsid/setpgid are outside AFK's "
    "cleanup guarantee."
)


def run_local_command_adapter(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    process_groups_available = supports_process_groups()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=process_groups_available,
        )
    except OSError as exc:
        raise WorkerRuntimeError(str(exc), stderr=str(exc), returncode=None, command=command) from exc

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = stream_reader(process.stdout, stdout_chunks)
    stderr_thread = stream_reader(process.stderr, stderr_chunks)
    deadline = time.monotonic() + timeout_seconds

    while True:
        if process.poll() is not None:
            break
        stopped_processes = stopped_worker_processes(process.pid, process_groups_available=process_groups_available)
        if stopped_processes:
            raise stopped_process_error(
                process,
                command=command,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                stdout_chunks=stdout_chunks,
                stderr_chunks=stderr_chunks,
                process_groups_available=process_groups_available,
                stopped_processes=stopped_processes,
            )
        if time.monotonic() >= deadline:
            raise timeout_runtime_error(
                process,
                command=command,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                stdout_chunks=stdout_chunks,
                stderr_chunks=stderr_chunks,
                process_groups_available=process_groups_available,
                post_exit=False,
            )
        time.sleep(min(0.05, max(deadline - time.monotonic(), 0.0)))

    stdout, stderr, drained = drain_post_exit_output(
        process=process,
        command=command,
        stdout_thread=stdout_thread,
        stderr_thread=stderr_thread,
        stdout_chunks=stdout_chunks,
        stderr_chunks=stderr_chunks,
        process_groups_available=process_groups_available,
        deadline=deadline,
    )
    if not drained:
        raise timeout_runtime_error(
            process,
            command=command,
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
            stdout_chunks=stdout_chunks,
            stderr_chunks=stderr_chunks,
            process_groups_available=process_groups_available,
            post_exit=True,
        )
    return {
        "command": command,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def stream_reader(stream: Any, chunks: list[str]) -> threading.Thread:
    def read_stream() -> None:
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    text = decoder.decode(chunk)
                else:
                    text = decode_adapter_output(chunk)
                if text:
                    chunks.append(text)
            tail = decoder.decode(b"", final=True)
            if tail:
                chunks.append(tail)
        finally:
            stream.close()

    thread = threading.Thread(target=read_stream, daemon=True)
    thread.start()
    return thread


def finalize_process_output(
    process: subprocess.Popen[str],
    *,
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    stdout_chunks: list[str],
    stderr_chunks: list[str],
    output_timeout: float | None,
    process_groups_available: bool,
) -> tuple[str, str, bool]:
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process, process_groups_available=process_groups_available)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    stdout_done, stderr_done = join_reader_threads(stdout_thread, stderr_thread, timeout=output_timeout)
    return ("".join(stdout_chunks), "".join(stderr_chunks), stdout_done and stderr_done)


def join_reader_threads(
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    *,
    timeout: float | None,
) -> tuple[bool, bool]:
    if timeout is None:
        stdout_thread.join()
        stderr_thread.join()
        return (not stdout_thread.is_alive(), not stderr_thread.is_alive())
    if timeout <= 0:
        return (not stdout_thread.is_alive(), not stderr_thread.is_alive())
    started_at = time.monotonic()
    stdout_thread.join(timeout=timeout)
    elapsed = time.monotonic() - started_at
    remaining = max(timeout - elapsed, 0.0)
    stderr_thread.join(timeout=remaining)
    return (not stdout_thread.is_alive(), not stderr_thread.is_alive())


def supports_process_groups() -> bool:
    return os.name == "posix" and hasattr(os, "killpg")


def decode_adapter_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def terminate_process_tree(process: subprocess.Popen[str], *, process_groups_available: bool) -> None:
    if process_groups_available:
        terminate_process_group(process)
        return
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=0.2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        return


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    process_group = process.pid
    for sig in available_group_termination_signals():
        try:
            os.killpg(process_group, sig)
        except ProcessLookupError:
            return
        except OSError:
            return
    try:
        process.wait(timeout=0.2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process_group, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return


def available_group_termination_signals() -> list[int]:
    signals: list[int] = []
    sigcont = getattr(signal, "SIGCONT", None)
    if sigcont is not None:
        signals.append(sigcont)
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None:
        signals.append(sigterm)
    return signals


def stopped_process_tree(root_pid: int) -> list[dict[str, Any]]:
    if sys.platform != "linux":
        return []
    pending = [root_pid]
    seen: set[int] = set()
    stopped: list[dict[str, Any]] = []
    while pending:
        pid = pending.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        status = read_proc_status(pid)
        if status is None:
            continue
        pending.extend(proc_children(pid))
        state_code = string_field(status, "state_code") or ""
        if state_code in {"T", "t"}:
            stopped.append(status)
    return sorted(stopped, key=lambda item: int(item["pid"]))


def stopped_process_group(process_group: int) -> list[dict[str, Any]]:
    if sys.platform != "linux":
        return []
    stopped: list[dict[str, Any]] = []
    for pid in process_group_members(process_group):
        status = read_proc_status(pid)
        if status is None:
            continue
        state_code = string_field(status, "state_code") or ""
        if state_code in {"T", "t"}:
            stopped.append(status)
    return sorted(stopped, key=lambda item: int(item["pid"]))


def stopped_worker_processes(root_pid: int, *, process_groups_available: bool) -> list[dict[str, Any]]:
    stopped = {int(item["pid"]): item for item in stopped_process_tree(root_pid)}
    if process_groups_available:
        for item in stopped_process_group(root_pid):
            stopped[int(item["pid"])] = item
    return [stopped[pid] for pid in sorted(stopped)]


def process_group_members(process_group: int) -> list[int]:
    proc_root = Path("/proc")
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return []
    members: list[int] = []
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if proc_process_group(pid) == process_group:
            members.append(pid)
    return members


def proc_process_group(pid: int) -> int | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    closing = raw.rfind(")")
    if closing == -1:
        return None
    fields = raw[closing + 2 :].split()
    if len(fields) < 3:
        return None
    try:
        return int(fields[2])
    except ValueError:
        return None


def proc_children(pid: int) -> list[int]:
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        text = children_path.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    children: list[int] = []
    for item in text.split():
        try:
            children.append(int(item))
        except ValueError:
            continue
    return children


def read_proc_status(pid: int) -> dict[str, Any] | None:
    status_path = Path(f"/proc/{pid}/status")
    try:
        lines = status_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key] = value.strip()
    state = values.get("State", "")
    state_code = state[:1]
    result: dict[str, Any] = {
        "pid": pid,
        "name": values.get("Name", ""),
        "state": state,
        "state_code": state_code,
    }
    ppid_text = values.get("PPid", "")
    if ppid_text.isdigit():
        result["ppid"] = int(ppid_text)
    return result


def format_stopped_process_message(root_pid: int, stopped_processes: list[dict[str, Any]]) -> str:
    entries = [
        f"pid {item['pid']} {item.get('name') or 'process'} State: {item.get('state') or '?'}"
        for item in stopped_processes[:3]
    ]
    if len(stopped_processes) > 3:
        entries.append(f"+{len(stopped_processes) - 3} more")
    details = "; ".join(entries)
    return (
        f"worker process group stopped under pid {root_pid}: {details}. "
        f"Remediation: {STOPPED_PROCESS_REMEDIATION}"
    )


def descendant_stdio_failure_artifact(*, process_groups_available: bool) -> dict[str, Any]:
    remediation = (
        "Ensure descendants in the worker process group exit or close inherited stdout/stderr before "
        "the worker process exits. Descendants that detach with setsid/setpgid are outside AFK's "
        "cleanup guarantee."
    )
    artifact: dict[str, Any] = {
        "process_state": "stdout_stderr_open_after_exit",
        "remediation": remediation,
    }
    if not process_groups_available:
        artifact["cleanup_scope"] = "best_effort"
        artifact["remediation"] = (
            remediation + " Process groups are unavailable, so descendant cleanup is best-effort only."
        )
    return artifact


def stopped_process_error(
    process: subprocess.Popen[Any],
    *,
    command: list[str],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    stdout_chunks: list[str],
    stderr_chunks: list[str],
    process_groups_available: bool,
    stopped_processes: list[dict[str, Any]],
) -> WorkerRuntimeError:
    message = format_stopped_process_message(process.pid, stopped_processes)
    terminate_process_tree(process, process_groups_available=process_groups_available)
    stdout, stderr, _ = finalize_process_output(
        process,
        stdout_thread=stdout_thread,
        stderr_thread=stderr_thread,
        stdout_chunks=stdout_chunks,
        stderr_chunks=stderr_chunks,
        output_timeout=1.0,
        process_groups_available=process_groups_available,
    )
    return WorkerRuntimeError(
        message,
        stdout=stdout,
        stderr=append_error_message(stderr, message),
        returncode=process.returncode,
        command=command,
        adapter_details={"stopped_processes": stopped_processes},
        failure_artifact={
            "process_state": "stopped",
            "stopped_processes": stopped_processes,
            "remediation": STOPPED_PROCESS_REMEDIATION,
        },
    )


def timeout_runtime_error(
    process: subprocess.Popen[Any],
    *,
    command: list[str],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    stdout_chunks: list[str],
    stderr_chunks: list[str],
    process_groups_available: bool,
    post_exit: bool,
) -> WorkerRuntimeError:
    terminate_process_tree(process, process_groups_available=process_groups_available)
    stdout, stderr, _ = finalize_process_output(
        process,
        stdout_thread=stdout_thread,
        stderr_thread=stderr_thread,
        stdout_chunks=stdout_chunks,
        stderr_chunks=stderr_chunks,
        output_timeout=1.0,
        process_groups_available=process_groups_available,
    )
    failure_artifact = (
        descendant_stdio_failure_artifact(process_groups_available=process_groups_available) if post_exit else None
    )
    return WorkerRuntimeError(
        "worker command timed out",
        stdout=stdout,
        stderr=stderr or "worker command timed out",
        returncode=process.returncode,
        timed_out=True,
        command=command,
        adapter_details=(
            {
                "process_state": failure_artifact["process_state"],
                **({"cleanup_scope": failure_artifact["cleanup_scope"]} if "cleanup_scope" in failure_artifact else {}),
            }
            if failure_artifact
            else None
        ),
        failure_artifact=failure_artifact,
    )


def drain_post_exit_output(
    *,
    process: subprocess.Popen[Any],
    command: list[str],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    stdout_chunks: list[str],
    stderr_chunks: list[str],
    process_groups_available: bool,
    deadline: float,
) -> tuple[str, str, bool]:
    while True:
        remaining = max(deadline - time.monotonic(), 0.0)
        stdout_done, stderr_done = join_reader_threads(
            stdout_thread,
            stderr_thread,
            timeout=min(0.05, remaining) if remaining > 0 else 0.0,
        )
        stopped_processes = stopped_worker_processes(process.pid, process_groups_available=process_groups_available)
        if stopped_processes:
            raise stopped_process_error(
                process,
                command=command,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                stdout_chunks=stdout_chunks,
                stderr_chunks=stderr_chunks,
                process_groups_available=process_groups_available,
                stopped_processes=stopped_processes,
            )
        if stdout_done and stderr_done:
            return ("".join(stdout_chunks), "".join(stderr_chunks), True)
        if remaining <= 0:
            return ("".join(stdout_chunks), "".join(stderr_chunks), False)


def append_error_message(stderr: str, message: str) -> str:
    if not stderr:
        return message
    if stderr.endswith("\n"):
        return stderr + message
    return stderr + "\n" + message


def path_is_equal_to_or_inside(path: Path, parent: Path) -> bool:
    path_resolved = path.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    return path_resolved == parent_resolved or parent_resolved in path_resolved.parents


def read_worker_payload(path: Path, *, fallback_path: Path | None = None) -> dict[str, Any]:
    result = read_json_result_file(
        path,
        missing_message="worker result file was not produced",
        invalid_json_message="worker result file is not valid JSON",
        invalid_type_message="worker result file must contain an object",
        fallback_path=fallback_path,
    )
    if result["status"] == "missing":
        return result
    if result["status"] != "valid":
        replace_worker_result_evidence(path, protocol_error_evidence(result["message"]))
        return {"status": "invalid", "message": result["message"]}
    redacted_payload = result["payload"]
    if not replace_worker_result_evidence(path, redacted_payload):
        message = "worker result file could not be sanitized"
        replace_worker_result_evidence(path, protocol_error_evidence(message))
        return {"status": "invalid", "message": message}
    return {"status": "valid", "payload": redacted_payload}


def protocol_error_evidence(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed_protocol",
        "classification": "protocol_failure",
        "summary": message,
    }


def replace_worker_result_evidence(path: Path, payload: dict[str, Any]) -> bool:
    content = canonical_json(payload) + "\n"
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
        os.replace(temp_path, path)
        return True
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
    try:
        if path.exists() and not path.is_dir() and not path.is_symlink():
            path.chmod(0o600)
            path.write_text(content, encoding="utf-8")
            return True
    except OSError:
        pass
    try:
        if path.is_symlink():
            path.unlink()
        elif path.exists() and path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        path.write_text(content, encoding="utf-8")
    except OSError:
        return False
    return True


def sync_worker_payload_artifacts(worker_output_path: Path, evidence_result_path: Path, payload: dict[str, Any]) -> None:
    replace_worker_result_evidence(worker_output_path, payload)
    replace_worker_result_evidence(evidence_result_path, payload)


def sync_worker_protocol_artifacts(worker_output_path: Path, evidence_result_path: Path, message: str) -> None:
    payload = protocol_error_evidence(message)
    replace_worker_result_evidence(worker_output_path, payload)
    replace_worker_result_evidence(evidence_result_path, payload)


def sync_worker_failure_artifacts(
    worker_output_path: Path,
    evidence_result_path: Path,
    status: str,
    classification: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "classification": classification,
        "summary": message,
    }
    if details:
        payload.update(redact_artifact_value(details))
    replace_worker_result_evidence(worker_output_path, payload)
    replace_worker_result_evidence(evidence_result_path, payload)


def normalize_worker_payload(
    payload: dict[str, Any],
    *,
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
    evidence_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    raw_status = string_field(payload, "status") or ""
    returncode = adapter.get("returncode")
    success_statuses = {"pass", "passed", "success", "succeeded", "completed"}
    skip_statuses = {"skip", "skipped"}
    failure_statuses = {"fail", "failed"}
    if raw_status in success_statuses:
        status = "validated"
        classification = "success"
    elif raw_status in skip_statuses:
        status = "skipped_profile"
        classification = "profile_skipped"
    elif raw_status in failure_statuses:
        status = "failed_validation"
        classification = "worker_failure"
    else:
        status = "failed_protocol"
        classification = "protocol_failure"
    if isinstance(returncode, int) and returncode != 0 and raw_status in success_statuses | skip_statuses:
        status = "failed_runtime"
        classification = "runtime_failure"
        summary = f"worker command exited {returncode} after reporting {raw_status}"
        return normalized_worker_result(
            status=status,
            classification=classification,
            summary=summary,
            raw_result=payload,
            adapter=adapter,
            stdout=stdout,
            stderr=stderr,
            evidence_dir=evidence_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    summary = string_field(payload, "summary") or status
    return normalized_worker_result(
        status=status,
        classification=classification,
        summary=summary,
        raw_result=payload,
        adapter=adapter,
        stdout=stdout,
        stderr=stderr,
        evidence_dir=evidence_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def normalized_worker_result(
    *,
    status: str,
    classification: str,
    summary: str,
    raw_result: dict[str, Any] | None,
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
    evidence_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    failures = failure_records(raw_result)
    evidence = {
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_excerpt": stdout[-2000:],
        "stderr_excerpt": stderr[-2000:],
    }
    actionable_failures = actionable_failure_records(
        status=status,
        classification=classification,
        summary=summary,
        failures=failures,
        evidence_dir=evidence_dir,
        adapter=adapter,
        evidence=evidence,
        evidence_dir_for_logs=evidence_dir,
        adapter_stdout=stdout,
        adapter_stderr=stderr,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "classification": classification,
        "summary": summary,
        "actionable_summary": actionable_summary(status, summary, actionable_failures),
        "actionable_failures": actionable_failures,
        "failures": failures,
        "adapter": adapter,
        "evidence": evidence,
    }


def adapter_record(
    worker: dict[str, Any],
    returncode: int | None,
    timed_out: bool,
    *,
    command: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adapter = {
        "type": worker["type"],
        "returncode": returncode,
        "timed_out": timed_out,
    }
    if command is not None:
        adapter["command"] = redact_artifact_value(command)
    if worker.get("host"):
        adapter["host"] = worker["host"]
    if details:
        adapter.update(redact_artifact_value(details))
    return adapter


def failure_records(raw_result: dict[str, Any] | None) -> list[Any]:
    if not isinstance(raw_result, dict):
        return []
    failures = raw_result.get("failures")
    if isinstance(failures, list):
        return redact_artifact_value(failures)
    steps = raw_result.get("steps")
    if not isinstance(steps, list):
        return []
    return [
        redact_artifact_value(step)
        for step in steps
        if isinstance(step, dict) and step.get("status") in {"fail", "failed", "skip", "skipped"}
    ]


def actionable_failure_records(
    *,
    status: str,
    classification: str,
    summary: str,
    failures: list[Any],
    evidence_dir: Path,
    adapter: dict[str, Any],
    evidence: dict[str, Any],
    evidence_dir_for_logs: Path,
    adapter_stdout: str,
    adapter_stderr: str,
) -> list[dict[str, Any]]:
    records = [
        summarize_failure_record(failure, evidence_dir=evidence_dir)
        for failure in failures
        if isinstance(failure, dict)
    ]
    if records:
        return prioritized_actionable_failures(records)
    if status == "validated":
        return []
    return [
        summarize_adapter_failure(
            status,
            classification,
            summary,
            adapter,
            evidence,
            evidence_dir_for_logs=evidence_dir_for_logs,
            adapter_stdout=adapter_stdout,
            adapter_stderr=adapter_stderr,
        )
    ]


def summarize_failure_record(failure: dict[str, Any], *, evidence_dir: Path) -> dict[str, Any]:
    log_path, log_path_status = resolve_worker_failure_log_path(failure, evidence_dir=evidence_dir)
    reason = string_field(failure, "reason") or ""
    excerpt = log_failure_excerpt(log_path, default_excerpt=reason)
    if not log_path and is_generic_failure_reason(reason):
        fallback = best_evidence_excerpt(evidence_dir, summary=reason)
        if fallback is not None:
            log_path, excerpt = fallback
            log_path_status = "fallback"
    return {
        "name": string_field(failure, "name") or "",
        "status": string_field(failure, "status") or "",
        "category": classify_failure_record(failure, excerpt),
        "reason": reason,
        "command": string_field(failure, "command") or "",
        "exit_code": integer_field(failure.get("exitCode")),
        "log_path": log_path,
        "log_path_status": log_path_status,
        "excerpt": excerpt,
    }


def resolve_worker_failure_log_path(failure: dict[str, Any], *, evidence_dir: Path) -> tuple[str | None, str]:
    log_path = string_field(failure, "log")
    if log_path:
        path = Path(log_path)
        if not path.is_absolute():
            path = evidence_dir / path
        return str(path.resolve()), "exact"
    return None, "unavailable"


def summarize_adapter_failure(
    status: str,
    classification: str,
    summary: str,
    adapter: dict[str, Any],
    evidence: dict[str, Any],
    *,
    evidence_dir_for_logs: Path,
    adapter_stdout: str,
    adapter_stderr: str,
) -> dict[str, Any]:
    log_path, excerpt = adapter_failure_excerpt(
        summary,
        evidence,
        evidence_dir=evidence_dir_for_logs,
        adapter_stdout=adapter_stdout,
        adapter_stderr=adapter_stderr,
    )
    return {
        "name": "worker",
        "status": status,
        "category": classify_adapter_failure(status, classification, excerpt),
        "reason": summary,
        "command": display_command(adapter.get("command")),
        "exit_code": integer_field(adapter.get("returncode")),
        "log_path": log_path,
        "excerpt": excerpt,
    }


def actionable_summary(status: str, summary: str, failures: list[dict[str, Any]]) -> str:
    if not failures:
        return summary
    first = failures[0]
    if not is_generic_failure_summary(status, summary):
        details = compact_failure_summary(first)
        if details and details not in summary:
            return f"{summary}; {details}"
        return summary
    return "; ".join(summary_line(item) for item in failures[:2])


def compact_failure_summary(item: dict[str, Any]) -> str:
    command = compact_command(string_field(item, "command") or "")
    exit_code = integer_field(item.get("exit_code"))
    log_path = string_field(item, "log_path") or ""
    excerpt = compact_excerpt(string_field(item, "excerpt") or string_field(item, "reason") or "")
    parts = []
    if command:
        parts.append(f"cmd: {command}")
    if exit_code is not None:
        parts.append(f"exit: {exit_code}")
    if log_path:
        parts.append(log_path)
    if excerpt:
        parts.append(excerpt)
    return ", ".join(parts)


def summary_line(item: dict[str, Any]) -> str:
    name = string_field(item, "name") or "worker"
    category = string_field(item, "category") or "failure"
    excerpt = string_field(item, "excerpt") or string_field(item, "reason") or ""
    command = compact_command(string_field(item, "command") or "")
    log_path = string_field(item, "log_path") or ""
    exit_code = integer_field(item.get("exit_code"))
    parts = [f"{name} [{category}]"]
    if command:
        parts.append(command)
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    if log_path:
        parts.append(log_path)
    if excerpt:
        parts.append(excerpt.replace("\n", " "))
    return ": ".join(parts)


def prioritized_actionable_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(failures, key=actionable_failure_priority)


def actionable_failure_priority(item: dict[str, Any]) -> tuple[int, int]:
    category = string_field(item, "category") or ""
    status = string_field(item, "status") or ""
    is_skip = category == "prerequisite_skip" or status in {"skip", "skipped"}
    exit_code = integer_field(item.get("exit_code"))
    no_exit_code = exit_code is None or exit_code == 0
    return (1 if is_skip else 0, 1 if no_exit_code else 0)


def compact_excerpt(text: str, *, limit: int = 160) -> str:
    excerpt = " ".join(text.replace("\n", " ").split())
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 3].rstrip() + "..."


def is_generic_failure_summary(status: str, summary: str) -> bool:
    generic = {
        "",
        "failed_validation",
        "failed_timeout",
        "failed_runtime",
        "failed_protocol",
        "failed_missing_result",
        "skipped_profile",
        "worker command timed out",
        "worker result file was not produced",
        "worker result file could not be read",
        "worker result file is not valid JSON",
        "worker result file must contain an object",
        "worker result file could not be sanitized",
    }
    return summary.strip() in generic or summary.strip() == status


def is_generic_failure_reason(reason: str) -> bool:
    stripped = reason.strip()
    if is_generic_failure_summary("", stripped):
        return True
    lowered = stripped.lower()
    return bool(
        re.fullmatch(r"command exited with status \d+", lowered)
        or re.fullmatch(r"exit status \d+", lowered)
        or re.fullmatch(r"exited with status \d+", lowered)
    )


def classify_failure_record(failure: dict[str, Any], excerpt: str) -> str:
    category = string_field(failure, "category") or ""
    status = string_field(failure, "status") or ""
    if category == "prerequisite_failed" or status in {"skip", "skipped"}:
        return "prerequisite_skip"
    kind = excerpt_kind(excerpt)
    if kind in {"compiler", "test"}:
        return kind
    return category or "validation"


def classify_adapter_failure(status: str, classification: str, excerpt: str) -> str:
    if status == "failed_timeout" or classification == "timeout":
        return "timeout"
    if status == "failed_missing_result" or classification == "missing_worker_result":
        return "missing_result"
    excerpt_type = excerpt_kind(excerpt)
    if excerpt_type in {"compiler", "test"}:
        return excerpt_type
    if classification == "runtime_failure":
        return "runtime"
    if classification == "protocol_failure":
        return "protocol"
    return classification or status


def adapter_failure_excerpt(
    summary: str,
    evidence: dict[str, Any],
    *,
    evidence_dir: Path,
    adapter_stdout: str,
    adapter_stderr: str,
) -> tuple[str, str]:
    candidates = [
        (
            string_field(evidence, "stdout_path") or "stdout.log",
            adapter_stdout or string_field(evidence, "stdout_excerpt") or "",
        ),
        (
            string_field(evidence, "stderr_path") or "stderr.log",
            adapter_stderr or string_field(evidence, "stderr_excerpt") or "",
        ),
    ]
    candidate_excerpts: list[tuple[str, str, int]] = []
    for path, text in candidates:
        excerpt = text_excerpt(text, default_excerpt="")
        if excerpt:
            candidate_excerpts.append((path, excerpt, 1))
    evidence_excerpt = best_evidence_excerpt(evidence_dir, summary=summary)
    if evidence_excerpt is not None:
        source_rank = 2 if Path(evidence_excerpt[0]).suffix == ".json" else 0
        candidate_excerpts.append((evidence_excerpt[0], evidence_excerpt[1], source_rank))
    if candidate_excerpts:
        return min(candidate_excerpts, key=lambda item: evidence_excerpt_priority(item[1], summary, item[2]))[:2]
    fallback_path = string_field(evidence, "stderr_path") or string_field(evidence, "stdout_path") or "stderr.log"
    return fallback_path, redact_text(summary)


def best_evidence_excerpt(evidence_dir: Path, *, summary: str) -> tuple[str, str] | None:
    candidates: list[tuple[str, str, int]] = []
    for relative_path, source_rank in (
        ("logs/validation.log", 0),
        ("logs/stack.log", 1),
        ("logs/submodule.log", 1),
        ("logs/fetch.log", 1),
        ("logs/lock.log", 1),
        ("logs/request.log", 1),
        ("result.json", 2),
        ("worker-output.json", 2),
    ):
        log_path = evidence_dir / relative_path
        try:
            excerpt = evidence_file_excerpt(log_path)
        except OSError:
            continue
        if excerpt:
            candidates.append((str(log_path), excerpt, source_rank))
    if not candidates:
        return None
    best = min(candidates, key=lambda item: evidence_excerpt_priority(item[1], summary, item[2]))
    return best[0], best[1]


def evidence_file_excerpt(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            import json

            payload = json.loads(text)
        except json.JSONDecodeError:
            return text_excerpt(text, default_excerpt="")
        if isinstance(payload, dict):
            for key in ("summary", "message", "status"):
                value = string_field(payload, key)
                if value:
                    return redact_text(value)
    return text_excerpt(text, default_excerpt="")


def evidence_excerpt_priority(excerpt: str, summary: str, source_rank: int) -> tuple[int, int]:
    kind = excerpt_kind(excerpt)
    if kind in {"compiler", "test"}:
        quality = 0
    elif is_generic_failure_summary("", excerpt) or excerpt.strip() == summary.strip():
        quality = 2
    else:
        quality = 1
    return (quality, source_rank)


def log_failure_excerpt(log_path: str | None, *, default_excerpt: str) -> str:
    if not log_path:
        return redact_text(default_excerpt)
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return redact_text(default_excerpt)
    return text_excerpt(text, default_excerpt=default_excerpt)


def text_excerpt(text: str, *, default_excerpt: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        kind = actionable_line_kind(line)
        if kind is None:
            continue
        excerpt = excerpt_from_lines(lines, index, kind)
        if excerpt:
            return redact_text(excerpt)
    return redact_text(default_excerpt or first_informative_line(lines))


def excerpt_from_lines(lines: list[str], start: int, kind: str) -> str:
    line = lines[start].strip()
    if not line:
        return ""
    if line.lower().startswith("reason:"):
        return line.split(":", 1)[1].strip()
    excerpt_lines = [line]
    for candidate in lines[start + 1 : start + 4]:
        stripped = candidate.strip()
        if not stripped:
            break
        lowered = stripped.lower()
        if lowered.startswith(("step:", "status:", "category:", "command:", "exitcode:", "startedat:", "endedat:")):
            break
        if lowered.startswith("preset cmake variables"):
            break
        if is_warning_only_line(lowered):
            break
        if actionable_line_kind(stripped) in {kind, "generic"}:
            excerpt_lines.append(stripped)
            continue
        break
    return "\n".join(excerpt_lines)


def actionable_line_kind(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if is_warning_only_line(lowered):
        return None
    if lowered.startswith("reason:"):
        return "generic"
    if any(token in lowered for token in ("cmake error", "fatal error", "undefined reference", "ld: error")):
        return "compiler"
    if re.search(r"(^|[^a-z])error:", lowered):
        return "compiler"
    if any(token in lowered for token in ("traceback (most recent call last):", "assertionerror", "failed:", "fail:")):
        return "test"
    if (
        "timed out" in lowered
        or "permission denied" in lowered
        or "no such file or directory" in lowered
        or re.search(r"(^|[\s\[(])(?:[a-z_][a-z0-9_:]*exception)([\s:.)\]]|$)", lowered)
    ):
        return "generic"
    if " failed" in lowered or lowered.startswith("failed "):
        return "test"
    return None


def excerpt_kind(excerpt: str) -> str:
    lowered = excerpt.lower()
    if any(token in lowered for token in ("cmake error", "fatal error", "undefined reference", "ld: error")):
        return "compiler"
    if re.search(r"(^|[^a-z])error:", lowered):
        return "compiler"
    if any(token in lowered for token in ("traceback (most recent call last):", "assertionerror", "failed:", "fail:")):
        return "test"
    return "generic"


def is_warning_only_line(line: str) -> bool:
    return "warning" in line and not any(token in line for token in ("error", "fail", "exception", "traceback"))


def first_informative_line(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped and not is_warning_only_line(stripped.lower()):
            return stripped
    for line in lines:
        if line.strip():
            return line.strip()
    return ""


def display_command(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(item) for item in command)
    if isinstance(command, str):
        return command
    return ""


def compact_command(command: str, *, max_length: int = 120) -> str:
    command = command.strip()
    if not command:
        return ""
    if len(command) <= max_length:
        return command
    return command[: max_length - 3].rstrip() + "..."


def integer_field(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def validate_output(
    request: dict[str, Any],
    worker_request: dict[str, Any],
    worker_result: dict[str, Any],
) -> dict[str, Any]:
    normalized = worker_result["normalized"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": normalized["status"],
        "classification": normalized["classification"],
        "summary": normalized["actionable_summary"],
        "actionable_failures": normalized["actionable_failures"],
        "validation": request["validation"],
        "checkout": request["checkout"],
        "worker_request": redact_artifact_value(worker_request),
        "worker_result": worker_result,
        "artifacts": {
            "worker_request": "worker-request.json",
            "worker_result": "worker-result.json",
        },
    }


def write_worker_result(path: Path, run_id: str, worker_result: dict[str, Any]) -> None:
    write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "step": "validate",
            "artifact_type": "worker-result",
            "result": redact_artifact_value(worker_result),
        },
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def command_secret_error_message(command: list[str]) -> str | None:
    for part in command:
        if is_secret_command_flag(part):
            flag = part.strip().split("=", 1)[0].lower()
            return f"worker.command must not include credential flag {flag}"
    return None


def string_field(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if isinstance(item, str) and item.strip():
        return item.strip()
    return None


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
