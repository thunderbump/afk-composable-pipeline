from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Mapping

from afk.implement import agent_command_secret_error, normalize_wrapper_secret_files, path_is_equal_to_or_inside
from afk.redaction import is_secret_value


PI_PROMPT_PLACEHOLDER = "{prompt}"
PI_RESULT_PATH = "agent-result.json"
PONYTAIL_PACKAGE_NAME = "ponytail"
PONYTAIL_EXTENSION_SOURCE = "git:github.com/DietrichGebert/ponytail"
MAX_MODEL_VERSION = (5, 4)
MODEL_VERSION_PATTERN = re.compile(r"^gpt-(\d+)\.(\d+)(?:$|[-.])")
SHELL_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
PYTHON_OPTIONS_WITH_SEPARATE_VALUES = frozenset({"-W", "-X", "--check-hash-based-pycs"})


def build_pi_real_worker_agent(
    *,
    pi_bin: str,
    provider: str,
    model: str,
    codex_home: str,
    config_home: str,
    pi_config_home: str,
    pi_coding_agent_dir: str | None = None,
    checkout_path: Path,
    prompt_placeholder: str = PI_PROMPT_PLACEHOLDER,
    thinking: str | None = None,
    ponytail_extension: str | None = None,
    ponytail_extension_source: str | None = None,
    wrapper_secret_file: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    provider_name = require_non_empty(provider, "provider")
    require_non_empty(codex_home, "agent.codex_home")
    require_non_empty(config_home, "agent.config_home")
    require_non_empty(pi_config_home, "agent.env.PI_CONFIG_HOME")
    command = build_pi_print_command(
        pi_bin=pi_bin,
        provider=provider_name,
        model=model,
        prompt_placeholder=prompt_placeholder,
        thinking=thinking,
        ponytail_extension=ponytail_extension,
        ponytail_extension_source=ponytail_extension_source,
    )
    agent: dict[str, Any] = {
        "type": "real-agent-command",
        "command": command,
        "result_path": PI_RESULT_PATH,
        **build_pi_mount_config(
            codex_home=codex_home,
            config_home=config_home,
            pi_config_home=pi_config_home,
            pi_coding_agent_dir=pi_coding_agent_dir,
            checkout_path=checkout_path,
            field_prefix="agent",
        ),
    }
    if pi_coding_agent_dir is None and provider_name == "openai-codex":
        raise ValueError("--agent-pi-coding-agent-dir is required when --agent-pi-provider=openai-codex")
    if wrapper_secret_file is not None:
        wrapper_secret_files = normalize_wrapper_secret_files(
            {"primary": wrapper_secret_file},
            checkout_path=checkout_path,
        )
        if wrapper_secret_files["status"] != "valid":
            raise ValueError(wrapper_secret_files["message"])
        agent["wrapper_secret_files"] = wrapper_secret_files["files"]
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise ValueError("agent.timeout_seconds must be positive")
        agent["timeout_seconds"] = timeout_seconds
    return agent


def build_pi_mount_config(
    *,
    codex_home: str | None,
    config_home: str | None,
    pi_config_home: str | None,
    pi_coding_agent_dir: str | None,
    checkout_path: Path,
    field_prefix: str,
) -> dict[str, Any]:
    mount_config: dict[str, Any] = {}
    if codex_home is not None:
        mount_config["codex_home"] = validate_absolute_dir(
            codex_home,
            f"{field_prefix}.codex_home",
            checkout_path=checkout_path,
        )
    if config_home is not None:
        mount_config["config_home"] = validate_absolute_dir(
            config_home,
            f"{field_prefix}.config_home",
            checkout_path=checkout_path,
        )
    env: dict[str, str] = {}
    if pi_config_home is not None:
        env["PI_CONFIG_HOME"] = validate_absolute_dir(
            pi_config_home,
            f"{field_prefix}.env.PI_CONFIG_HOME",
            checkout_path=checkout_path,
        )
    if pi_coding_agent_dir is not None:
        env["PI_CODING_AGENT_DIR"] = validate_absolute_dir(
            pi_coding_agent_dir,
            f"{field_prefix}.env.PI_CODING_AGENT_DIR",
            checkout_path=checkout_path,
        )
    if env:
        mount_config["env"] = env
    return mount_config


def build_provider_pi_mount_config(
    *,
    provider: str,
    codex_home: str | None,
    config_home: str | None,
    pi_config_home: str | None,
    pi_coding_agent_dir: str | None,
    checkout_path: Path,
    field_prefix: str,
) -> dict[str, Any]:
    provider_name = require_non_empty(provider, "provider")
    if provider_name != "openai-codex":
        return {}
    require_non_empty(codex_home, f"{field_prefix}.codex_home")
    require_non_empty(config_home, f"{field_prefix}.config_home")
    require_non_empty(pi_config_home, f"{field_prefix}.env.PI_CONFIG_HOME")
    require_non_empty(pi_coding_agent_dir, f"{field_prefix}.env.PI_CODING_AGENT_DIR")
    return build_pi_mount_config(
        codex_home=codex_home,
        config_home=config_home,
        pi_config_home=pi_config_home,
        pi_coding_agent_dir=pi_coding_agent_dir,
        checkout_path=checkout_path,
        field_prefix=field_prefix,
    )


def build_pi_print_command(
    *,
    pi_bin: str,
    provider: str,
    model: str,
    prompt_placeholder: str = PI_PROMPT_PLACEHOLDER,
    thinking: str | None = None,
    ponytail_extension: str | None = None,
    ponytail_extension_source: str | None = None,
) -> list[str]:
    pi_binary = require_non_empty(pi_bin, "pi_bin")
    provider_name = require_non_empty(provider, "provider")
    prompt = require_non_empty(prompt_placeholder, "prompt_placeholder")
    model_name = validate_model_cap(model)
    command = [pi_binary, "-p", prompt, "--provider", provider_name, "--model", model_name]
    if thinking is not None:
        command.extend(["--thinking", require_non_empty(thinking, "thinking")])
    extension = normalize_ponytail_extension(
        ponytail_extension=ponytail_extension,
        ponytail_extension_source=ponytail_extension_source,
    )
    if extension is not None:
        command.extend(["--extension", extension])
    secret_error = agent_command_secret_error(command)
    if secret_error:
        raise ValueError(secret_error)
    return command


def openai_codex_pi_mount_error(
    *,
    command: list[str],
    codex_home: str | None,
    config_home: str | None,
    env: Mapping[str, str] | None,
    field_prefix: str,
) -> str | None:
    if pi_command_provider(command) != "openai-codex":
        return None
    mounted_env = env or {}
    missing = []
    if not codex_home:
        missing.append(f"{field_prefix}.codex_home")
    if not config_home:
        missing.append(f"{field_prefix}.config_home")
    if not mounted_env.get("PI_CONFIG_HOME"):
        missing.append(f"{field_prefix}.env.PI_CONFIG_HOME")
    if not mounted_env.get("PI_CODING_AGENT_DIR"):
        missing.append(f"{field_prefix}.env.PI_CODING_AGENT_DIR")
    if not missing:
        return None
    verb = "is" if len(missing) == 1 else "are"
    return f"{', '.join(missing)} {verb} required when {field_prefix}.command uses pi --provider openai-codex"


def non_openai_pi_mount_error(
    *,
    command: list[str],
    codex_home: str | None,
    config_home: str | None,
    env: Mapping[str, str] | None,
    field_prefix: str,
) -> str | None:
    mounted_env = env or {}
    mounted = []
    if codex_home:
        mounted.append(f"{field_prefix}.codex_home")
    if config_home:
        mounted.append(f"{field_prefix}.config_home")
    if mounted_env.get("PI_CONFIG_HOME"):
        mounted.append(f"{field_prefix}.env.PI_CONFIG_HOME")
    if mounted_env.get("PI_CODING_AGENT_DIR"):
        mounted.append(f"{field_prefix}.env.PI_CODING_AGENT_DIR")
    if not mounted:
        return None
    provider = pi_command_provider(command)
    if provider == "openai-codex":
        return None
    verb = "is" if len(mounted) == 1 else "are"
    if provider is None:
        return (
            f"{', '.join(mounted)} {verb} only supported when {field_prefix}.command uses pi --provider openai-codex; "
            "provider could not be determined"
        )
    return f"{', '.join(mounted)} {verb} only supported when {field_prefix}.command uses pi --provider openai-codex"


def pi_command_provider(command: list[str]) -> str | None:
    pi_args = pi_command_args(command)
    if pi_args is None:
        return None
    for index, part in enumerate(pi_args):
        if part == "--provider" and index + 1 < len(pi_args):
            provider = pi_args[index + 1].strip()
            return provider or None
        if part.startswith("--provider="):
            provider = part.partition("=")[2].strip()
            return provider or None
    return None


def pi_command_args(command: list[str]) -> list[str] | None:
    if not command:
        return None
    executable = Path(command[0]).name
    if executable == "pi":
        return command[1:]
    if executable == "env":
        return _env_wrapped_pi_args(command[1:])
    if executable in {"bash", "sh", "zsh"}:
        return _shell_wrapped_pi_args(command[1:])
    if executable.startswith("python"):
        python_module = _python_module_pi_command_parts(command[1:])
        if python_module is None:
            return None
        _, pi_args = python_module
        return pi_args
    return None


def pi_command_executable(command: list[str]) -> str | None:
    if not command:
        return None
    executable = Path(command[0]).name
    if executable == "pi":
        return command[0]
    if executable == "env":
        return _env_wrapped_pi_executable(command)
    if executable in {"bash", "sh", "zsh"}:
        return _shell_wrapped_pi_executable(command)
    if executable.startswith("python") and _python_module_pi_args(command[1:]) is not None:
        return command[0]
    return None


def pi_preflight_command(command: list[str], *, prompt: str) -> list[str] | None:
    if not command:
        return None
    executable = Path(command[0]).name
    if executable == "pi":
        return [command[0], *_filtered_pi_args(command[1:]), "--no-session", "--no-tools", "-p", prompt]
    if executable == "env":
        return _env_wrapped_preflight_command(command, prompt=prompt)
    if executable in {"bash", "sh", "zsh"}:
        return _shell_wrapped_preflight_command(command, prompt=prompt)
    if executable.startswith("python"):
        python_module = _python_module_pi_command_parts(command[1:])
        if python_module is None:
            return None
        interpreter_args, pi_args = python_module
        return [
            command[0],
            *interpreter_args,
            "-m",
            "pi",
            *_filtered_pi_args(pi_args),
            "--no-session",
            "--no-tools",
            "-p",
            prompt,
        ]
    return None


def _filtered_pi_args(pi_args: list[str]) -> list[str]:
    filtered_args: list[str] = []
    skip_next = False
    for part in pi_args:
        if skip_next:
            skip_next = False
            continue
        if part == "-p":
            skip_next = True
            continue
        if part in {"--print", "--no-session", "--no-tools"}:
            continue
        filtered_args.append(part)
    return filtered_args


def _env_wrapped_pi_executable(command: list[str]) -> str | None:
    split_string = _env_split_string(command[1:])
    if split_string is not None:
        return _shell_args_pi_executable(split_string)
    remainder = _env_command_remainder(command[1:])
    if remainder is None:
        return None
    return pi_command_executable(remainder)


def _env_wrapped_preflight_command(command: list[str], *, prompt: str) -> list[str] | None:
    leading, remainder = _env_command_parts(command[1:])
    if leading is None:
        return None
    if remainder is None:
        split_string = _env_split_string(command[1:])
        if split_string is None:
            return None
        inner_preflight = _shell_args_preflight_command(split_string, prompt=prompt)
        if inner_preflight is None:
            return None
        return [command[0], *leading, shlex.join(inner_preflight)]
    inner_preflight = pi_preflight_command(remainder, prompt=prompt)
    if inner_preflight is None:
        return None
    return [command[0], *leading, *inner_preflight]


def _env_command_parts(command: list[str]) -> tuple[list[str], list[str] | None]:
    leading: list[str] = []
    index = 0
    while index < len(command):
        part = command[index]
        if part == "--":
            leading.append(part)
            index += 1
            break
        if part in {"-C", "--chdir", "-u", "--unset", "-S", "--split-string"} and index + 1 < len(command):
            leading.extend([part, command[index + 1]])
            if part in {"-S", "--split-string"}:
                return leading, None
            index += 2
            continue
        if part.startswith("--split-string="):
            leading.append(part.partition("=")[0])
            return leading, None
        if part.startswith("-"):
            leading.append(part)
            index += 1
            continue
        if "=" in part:
            leading.append(part)
            index += 1
            continue
        break
    return leading, command[index:]


def _env_split_string(command: list[str]) -> list[str] | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part in {"-S", "--split-string"} and index + 1 < len(command):
            return _parse_shell_command(command[index + 1])
        if part.startswith("--split-string="):
            return _parse_shell_command(part.partition("=")[2])
        if part == "--":
            break
        if part in {"-C", "--chdir", "-u", "--unset"} and index + 1 < len(command):
            index += 2
            continue
        if part.startswith("-") or "=" in part:
            index += 1
            continue
        break
    return None


def _env_command_remainder(command: list[str]) -> list[str] | None:
    _, remainder = _env_command_parts(command)
    return remainder


def _env_wrapped_pi_args(command: list[str]) -> list[str] | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part == "--":
            index += 1
            break
        if part in {"-C", "--chdir"} and index + 1 < len(command):
            index += 2
            continue
        if part in {"-S", "--split-string"} and index + 1 < len(command):
            return _parse_shell_command_args(command[index + 1])
        if part.startswith("--split-string="):
            return _parse_shell_command_args(part.partition("=")[2])
        if part in {"-u", "--unset"} and index + 1 < len(command):
            index += 2
            continue
        if part.startswith("-"):
            index += 1
            continue
        if "=" in part:
            index += 1
            continue
        break
    return pi_command_args(command[index:])


def _python_module_pi_args(command: list[str]) -> list[str] | None:
    python_module = _python_module_pi_command_parts(command)
    if python_module is None:
        return None
    _, pi_args = python_module
    return pi_args


def _python_module_pi_command_parts(command: list[str]) -> tuple[list[str], list[str]] | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part == "-m" and index + 1 < len(command):
            if command[index + 1] != "pi":
                return None
            return command[:index], command[index + 2 :]
        if part in PYTHON_OPTIONS_WITH_SEPARATE_VALUES:
            if index + 1 >= len(command):
                return None
            next_part = command[index + 1]
            if next_part.startswith("-"):
                return None
            index += 2
            continue
        if not part.startswith("-"):
            return None
        index += 1
    return None


def _shell_wrapped_pi_args(command: list[str]) -> list[str] | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part == "-c" and index + 1 < len(command):
            return _parse_shell_command_args(command[index + 1])
        if part.startswith("-") and not part.startswith("--") and "c" in part[1:] and index + 1 < len(command):
            return _parse_shell_command_args(command[index + 1])
        index += 1
    return None


def _shell_wrapped_pi_executable(command: list[str]) -> str | None:
    shell_args = _shell_command_args(command)
    if shell_args is None:
        return None
    return _shell_args_pi_executable(shell_args)


def _shell_wrapped_preflight_command(command: list[str], *, prompt: str) -> list[str] | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part == "-c" and index + 1 < len(command):
            shell_args = _parse_shell_command(command[index + 1])
            if shell_args is None:
                return None
            inner_preflight = _shell_args_preflight_command(shell_args, prompt=prompt)
            if inner_preflight is None:
                return None
            return [*command[: index + 1], shlex.join(inner_preflight), *command[index + 2 :]]
        if part.startswith("-") and not part.startswith("--") and "c" in part[1:] and index + 1 < len(command):
            shell_args = _parse_shell_command(command[index + 1])
            if shell_args is None:
                return None
            inner_preflight = _shell_args_preflight_command(shell_args, prompt=prompt)
            if inner_preflight is None:
                return None
            return [*command[: index + 1], shlex.join(inner_preflight), *command[index + 2 :]]
        index += 1
    return None


def _shell_command_args(command: list[str]) -> list[str] | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part == "-c" and index + 1 < len(command):
            return _parse_shell_command(command[index + 1])
        if part.startswith("-") and not part.startswith("--") and "c" in part[1:] and index + 1 < len(command):
            return _parse_shell_command(command[index + 1])
        index += 1
    return None


def _shell_args_pi_executable(shell_args: list[str]) -> str | None:
    remainder = _shell_args_pi_remainder(shell_args)
    if remainder is None:
        return None
    return pi_command_executable(remainder)


def _shell_args_preflight_command(shell_args: list[str], *, prompt: str) -> list[str] | None:
    prefix, remainder = _shell_command_prefix_and_remainder(shell_args)
    if remainder is None:
        return None
    inner_preflight = pi_preflight_command(remainder, prompt=prompt)
    if inner_preflight is None:
        return None
    return [*prefix, *inner_preflight]


def _shell_args_pi_remainder(shell_args: list[str]) -> list[str] | None:
    _, remainder = _shell_command_prefix_and_remainder(shell_args)
    return remainder


def _shell_command_prefix_and_remainder(shell_args: list[str]) -> tuple[list[str], list[str] | None]:
    remainder = list(shell_args)
    prefix: list[str] = []
    while remainder and SHELL_ASSIGNMENT_PATTERN.match(remainder[0]):
        prefix.append(remainder.pop(0))
    if remainder[:1] == ["exec"]:
        prefix.append(remainder.pop(0))
        if remainder[:1] == ["--"]:
            prefix.append(remainder.pop(0))
        while remainder and SHELL_ASSIGNMENT_PATTERN.match(remainder[0]):
            prefix.append(remainder.pop(0))
    if not remainder:
        return prefix, None
    return prefix, remainder


def _parse_shell_command_args(command: str) -> list[str] | None:
    shell_args = _parse_shell_command(command)
    if shell_args is None:
        return None
    shell_args = _strip_shell_assignment_prefix(shell_args)
    if shell_args[:1] == ["exec"]:
        shell_args = shell_args[1:]
        if shell_args[:1] == ["--"]:
            shell_args = shell_args[1:]
        shell_args = _strip_shell_assignment_prefix(shell_args)
    return pi_command_args(shell_args)


def _parse_shell_command(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _strip_shell_assignment_prefix(command: list[str]) -> list[str]:
    index = 0
    while index < len(command) and SHELL_ASSIGNMENT_PATTERN.match(command[index]):
        index += 1
    return command[index:]


def validate_model_cap(model: str) -> str:
    model_name = require_non_empty(model, "model")
    match = MODEL_VERSION_PATTERN.match(model_name)
    if match is None:
        raise ValueError("Pi worker model must be a gpt-* model at gpt-5.4 or lower")
    version = (int(match.group(1)), int(match.group(2)))
    if version > MAX_MODEL_VERSION:
        raise ValueError("Pi worker model must be gpt-5.4 or lower")
    return model_name


def normalize_ponytail_extension(
    *,
    ponytail_extension: str | None,
    ponytail_extension_source: str | None,
) -> str | None:
    if ponytail_extension is not None and ponytail_extension_source is not None:
        raise ValueError("Specify ponytail_extension or ponytail_extension_source, not both")
    if ponytail_extension_source is not None:
        return require_non_empty(ponytail_extension_source, "ponytail_extension_source")
    if ponytail_extension is not None:
        return require_non_empty(ponytail_extension, "ponytail_extension")
    return None


def validate_absolute_dir(value: str, field: str, *, checkout_path: Path) -> str:
    path_value = require_non_empty(value, field)
    if is_secret_value(path_value):
        raise ValueError(f"{field} must not include a secret-looking value")
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{field} must be absolute")
    if not path.is_dir():
        raise ValueError(f"{field} must be an existing directory")
    if path_is_equal_to_or_inside(path, checkout_path):
        raise ValueError(f"{field} must be outside checkout")
    return str(path)


def require_non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()
