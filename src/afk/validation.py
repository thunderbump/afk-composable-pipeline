from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.redaction import is_secret_command_flag, redact_artifact_value, redact_text, redact_url


SCHEMA_VERSION = 1


class WorkerRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        timed_out: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out


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

    worker_request = build_worker_request(request, run_dir)
    request_path = run_dir / "worker-request.json"
    evidence_dir = Path(worker_request["evidence_dir"])
    result_path = evidence_dir / "result.json"
    worker_result_path = run_dir / "worker-result.json"

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
        normalized = normalized_worker_result(
            status="failed_timeout" if exc.timed_out else "failed_runtime",
            classification="timeout" if exc.timed_out else "runtime_failure",
            summary=exc.message,
            raw_result=None,
            adapter=adapter_record(request["worker"], exc.returncode, exc.timed_out),
            stdout=stdout,
            stderr=stderr,
        )
        worker_result = {"raw": None, "normalized": normalized}
        write_worker_result(worker_result_path, run_id, worker_result)
        return validate_output(request, worker_request, worker_result)

    stdout = redact_text(adapter_result["stdout"])
    stderr = redact_text(adapter_result["stderr"])
    write_adapter_logs(stdout, stderr)

    raw_payload = read_worker_payload(result_path)
    if raw_payload["status"] != "valid":
        if raw_payload["status"] == "missing" and adapter_result["returncode"] != 0:
            normalized = normalized_worker_result(
                status="failed_runtime",
                classification="runtime_failure",
                summary="worker command failed",
                raw_result=None,
                adapter=adapter_record(request["worker"], adapter_result["returncode"], False),
                stdout=stdout,
                stderr=stderr,
            )
            worker_result = {"raw": None, "normalized": normalized}
            write_worker_result(worker_result_path, run_id, worker_result)
            return validate_output(request, worker_request, worker_result)
        if raw_payload["status"] == "missing":
            status = "failed_missing_result"
            classification = "missing_worker_result"
        else:
            status = "failed_protocol"
            classification = "protocol_failure"
        normalized = normalized_worker_result(
            status=status,
            classification=classification,
            summary=raw_payload["message"],
            raw_result=None,
            adapter=adapter_record(request["worker"], adapter_result["returncode"], False),
            stdout=stdout,
            stderr=stderr,
        )
        worker_result = {"raw": None, "normalized": normalized}
        write_worker_result(worker_result_path, run_id, worker_result)
        return validate_output(request, worker_request, worker_result)

    raw_result = raw_payload["payload"]
    normalized = normalize_worker_payload(
        raw_result,
        adapter=adapter_record(request["worker"], adapter_result["returncode"], False),
        stdout=stdout,
        stderr=stderr,
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

    validation = normalize_validation(input_data.get("validation", {}), project_contract)
    if validation["status"] != "valid":
        return invalid_request(validation["message"])

    worker = normalize_worker(input_data.get("worker"), checkout["checkout"], project_contract)
    if worker["status"] != "valid":
        return invalid_request(worker["message"])

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "run_id": run_id,
        "checkout": checkout["checkout"],
        "validation": validation["validation"],
        "worker": worker["worker"],
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


def normalize_validation(validation: Any, project_contract: Any) -> dict[str, Any]:
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
    return {
        "status": "valid",
        "validation": {
            "requested_profile": profile,
            "worker_profile": worker_profile,
            "worker_request": profile_request,
            "available_profiles": available_profiles,
            "dry_run": dry_run,
            "timeout_seconds": float(timeout_seconds),
        },
    }


def normalize_worker(worker: Any, checkout: dict[str, Any], project_contract: Any) -> dict[str, Any]:
    if worker is None and getattr(project_contract, "project_slug", None) == "bump-eqemu":
        worker = {
            "type": "local-command",
            "command": [
                str(Path(checkout["path"]) / "scripts" / "validation-worker.sh"),
                "run",
                "--request",
                "{request_path}",
            ],
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
        },
    }


def build_worker_request(request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    checkout = request["checkout"]
    validation = request["validation"]
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
    env = minimal_worker_environment(evidence_dir)
    env["AFK_WORKER_REQUEST"] = str(request_path)
    env["AFK_WORKER_RESULT"] = str(result_path)
    env["AFK_WORKER_EVIDENCE_DIR"] = str(evidence_dir)
    env["AFK_VALIDATION_PROFILE"] = profile
    if worker["type"] == "remote-command":
        env["AFK_WORKER_REMOTE_HOST"] = worker["host"]
    command = render_command(
        worker["command"],
        request_path=request_path,
        result_path=result_path,
        evidence_dir=evidence_dir,
        profile=profile,
    )
    try:
        completed = subprocess.run(
            command,
            cwd=checkout_path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=worker["timeout_seconds"],
        )
    except OSError as exc:
        raise WorkerRuntimeError(str(exc), stderr=str(exc), returncode=None) from exc
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        raise WorkerRuntimeError(
            "worker command timed out",
            stdout=stdout,
            stderr=stderr or "worker command timed out",
            returncode=None,
            timed_out=True,
        ) from exc
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def render_command(
    command: list[str],
    *,
    request_path: Path,
    result_path: Path,
    evidence_dir: Path,
    profile: str,
) -> list[str]:
    replacements = {
        "{request_path}": str(request_path),
        "{result_path}": str(result_path),
        "{evidence_dir}": str(evidence_dir),
        "{profile}": profile,
    }
    rendered = []
    for part in command:
        item = part
        for marker, value in replacements.items():
            item = item.replace(marker, value)
        rendered.append(item)
    return rendered


def minimal_worker_environment(temp_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "PATH",
        "LANG",
        "LC_ALL",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    home_path = temp_path / "home"
    xdg_config_home = temp_path / "xdg-config"
    home_path.mkdir(exist_ok=True)
    xdg_config_home.mkdir(exist_ok=True)
    env["HOME"] = str(home_path)
    env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    return env


def read_worker_payload(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"status": "missing", "message": "worker result file was not produced"}
    except OSError:
        return {"status": "invalid", "message": "worker result file could not be read"}
    try:
        import json

        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "invalid", "message": "worker result file is not valid JSON"}
    if not isinstance(payload, dict):
        return {"status": "invalid", "message": "worker result file must contain an object"}
    return {"status": "valid", "payload": redact_artifact_value(payload)}


def normalize_worker_payload(
    payload: dict[str, Any],
    *,
    adapter: dict[str, Any],
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    raw_status = string_field(payload, "status") or ""
    returncode = adapter.get("returncode")
    if isinstance(returncode, int) and returncode != 0 and raw_status not in {"fail", "failed"}:
        status = "failed_runtime"
        classification = "runtime_failure"
        summary = f"worker command exited {returncode} after reporting {raw_status or 'no status'}"
        return normalized_worker_result(
            status=status,
            classification=classification,
            summary=summary,
            raw_result=payload,
            adapter=adapter,
            stdout=stdout,
            stderr=stderr,
        )
    if raw_status in {"pass", "passed", "success", "succeeded", "completed"}:
        status = "validated"
        classification = "success"
    elif raw_status in {"skip", "skipped"}:
        status = "skipped_profile"
        classification = "profile_skipped"
    elif raw_status in {"fail", "failed"}:
        status = "failed_validation"
        classification = "worker_failure"
    else:
        status = "failed_protocol"
        classification = "protocol_failure"
    summary = string_field(payload, "summary") or status
    return normalized_worker_result(
        status=status,
        classification=classification,
        summary=summary,
        raw_result=payload,
        adapter=adapter,
        stdout=stdout,
        stderr=stderr,
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
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "classification": classification,
        "summary": summary,
        "failures": failure_records(raw_result),
        "adapter": adapter,
        "evidence": {
            "stdout_path": "stdout.log",
            "stderr_path": "stderr.log",
            "stdout_excerpt": stdout[-2000:],
            "stderr_excerpt": stderr[-2000:],
        },
    }


def adapter_record(worker: dict[str, Any], returncode: int | None, timed_out: bool) -> dict[str, Any]:
    adapter = {
        "type": worker["type"],
        "returncode": returncode,
        "timed_out": timed_out,
    }
    if worker.get("host"):
        adapter["host"] = worker["host"]
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
        "summary": normalized["summary"],
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


def write_adapter_logs(stdout: str, stderr: str) -> None:
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)


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
