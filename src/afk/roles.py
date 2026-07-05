from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from afk.role_adapters import (
    CommandRunner,
    RoleAdapterRuntimeError,
    execute_role_command,
    minimal_command_environment,
    redact_adapter_streams,
    render_command,
    write_adapter_logs,
)


def execute_role_adapter(
    adapter: dict[str, Any],
    *,
    cwd: Path,
    env_root: Path,
    env_vars: dict[str, str],
    replacements: dict[str, str] | None,
    runtime_failure_message: str,
    timeout_message: str,
    configure_env: Callable[[dict[str, str]], None] | None = None,
    allow_nonzero: bool = False,
    text: bool = True,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    env = minimal_command_environment(env_root, config_home=adapter.get("config_home") or "")
    env.update(adapter.get("env") or {})
    if adapter.get("codex_home"):
        env["CODEX_HOME"] = adapter["codex_home"]
    env.update(env_vars)
    if configure_env is not None:
        configure_env(env)
    command = render_command(adapter["command"], replacements or {})
    return execute_role_command(
        command=command,
        cwd=cwd,
        env=env,
        timeout_seconds=adapter["timeout_seconds"],
        runtime_failure_message=runtime_failure_message,
        timeout_message=timeout_message,
        allow_nonzero=allow_nonzero,
        text=text,
        runner=runner,
    )


def log_role_runtime_error(
    exc: RoleAdapterRuntimeError,
    *,
    exact_secrets: set[str] | None = None,
) -> tuple[str, str]:
    stdout, stderr = redact_adapter_streams(
        stdout=exc.stdout,
        stderr=exc.stderr or exc.message,
        exact_secrets=exact_secrets,
    )
    write_adapter_logs(stdout, stderr)
    return stdout, stderr


def log_role_adapter_result(
    adapter_result: dict[str, Any],
    *,
    exact_secrets: set[str] | None = None,
) -> tuple[str, str]:
    stdout, stderr = redact_adapter_streams(
        stdout=adapter_result["stdout"],
        stderr=adapter_result["stderr"],
        exact_secrets=exact_secrets,
    )
    write_adapter_logs(stdout, stderr)
    return stdout, stderr
