from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from afk.jsonutil import canonical_json
from afk.redaction import redact_artifact_value, redact_text


class RoleAdapterRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        timed_out: bool = False,
        configured_timeout_seconds: float | None = None,
        elapsed_seconds: float | None = None,
        command: list[str] | None = None,
        adapter_details: dict[str, Any] | None = None,
        failure_artifact: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out
        self.configured_timeout_seconds = configured_timeout_seconds
        self.elapsed_seconds = elapsed_seconds
        self.command = list(command) if isinstance(command, list) else None
        self.adapter_details = redact_artifact_value(adapter_details or {})
        self.failure_artifact = redact_artifact_value(failure_artifact or {})


CommandRunner = Callable[[list[str], Path, dict[str, str], float], dict[str, Any]]


def minimal_command_environment(temp_path: Path, *, config_home: str = "") -> dict[str, str]:
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
    home_path.mkdir(exist_ok=True)
    env["HOME"] = str(home_path)
    if config_home:
        env["XDG_CONFIG_HOME"] = config_home
    else:
        xdg_config_home = temp_path / "xdg-config"
        xdg_config_home.mkdir(exist_ok=True)
        env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    return env


def render_command(command: list[str], replacements: dict[str, str]) -> list[str]:
    pattern = re.compile("|".join(sorted((re.escape(token) for token in replacements), key=len, reverse=True)))
    return [pattern.sub(lambda match: replacements[match.group(0)], part) for part in command]


def execute_role_command(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    runtime_failure_message: str,
    timeout_message: str,
    allow_nonzero: bool = False,
    text: bool = True,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    active_runner = runner or _subprocess_runner(text=text)
    try:
        completed = active_runner(command, cwd, env, timeout_seconds)
    except RoleAdapterRuntimeError:
        raise
    except OSError as exc:
        raise RoleAdapterRuntimeError(
            str(exc),
            stderr=str(exc),
            returncode=None,
            command=command,
            configured_timeout_seconds=timeout_seconds,
            elapsed_seconds=time.monotonic() - started_at,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RoleAdapterRuntimeError(
            timeout_message,
            stdout=decode_adapter_output(exc.stdout),
            stderr=decode_adapter_output(exc.stderr) or timeout_message,
            returncode=None,
            timed_out=True,
            command=command,
            configured_timeout_seconds=timeout_seconds,
            elapsed_seconds=time.monotonic() - started_at,
        ) from exc

    result = {
        "command": command,
        "returncode": completed["returncode"],
        "stdout": decode_adapter_output(completed.get("stdout")),
        "stderr": decode_adapter_output(completed.get("stderr")),
        "timed_out": False,
        "configured_timeout_seconds": timeout_seconds,
        "elapsed_seconds": time.monotonic() - started_at,
    }
    if result["returncode"] != 0 and not allow_nonzero:
        raise RoleAdapterRuntimeError(
            runtime_failure_message,
            stdout=result["stdout"],
            stderr=result["stderr"],
            returncode=result["returncode"],
            command=command,
            configured_timeout_seconds=timeout_seconds,
            elapsed_seconds=result["elapsed_seconds"],
        )
    return result


def _subprocess_runner(*, text: bool) -> CommandRunner:
    def run(command: list[str], cwd: Path, env: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=text,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    return run


def decode_adapter_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def read_json_result_file(
    path: Path,
    *,
    missing_message: str,
    invalid_json_message: str,
    invalid_type_message: str,
    exact_secrets: set[str] | None = None,
    cleanup: bool = False,
    fallback_path: Path | None = None,
) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        result_file_present = True
    except FileNotFoundError:
        if fallback_path is not None and fallback_path != path:
            return read_json_result_file(
                fallback_path,
                missing_message=missing_message,
                invalid_json_message=invalid_json_message,
                invalid_type_message=invalid_type_message,
                exact_secrets=exact_secrets,
                cleanup=cleanup,
            )
        return {
            "status": "missing",
            "message": missing_message,
            "result_file_present": False,
        }
    except OSError:
        return {
            "status": "invalid",
            "message": missing_message.replace("was not produced", "could not be read"),
            "result_file_present": path.exists(),
        }
    finally:
        if cleanup:
            try:
                path.unlink()
            except OSError:
                pass

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "status": "invalid",
            "message": invalid_json_message,
            "result_file_present": result_file_present,
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "message": invalid_type_message,
            "result_file_present": result_file_present,
        }
    return {
        "status": "valid",
        "payload": redact_artifact_value(payload, exact_secrets=exact_secrets),
        "result_file_present": result_file_present,
    }


def redact_adapter_streams(
    *,
    stdout: str,
    stderr: str,
    exact_secrets: set[str] | None = None,
) -> tuple[str, str]:
    return (
        redact_text(stdout, exact_secrets=exact_secrets),
        redact_text(stderr, exact_secrets=exact_secrets),
    )


def write_adapter_logs(stdout: str, stderr: str) -> None:
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)


def temp_json_file(temp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = temp_path / name
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    return path
