from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from afk.implement import agent_command_secret_error, normalize_wrapper_secret_files, path_is_equal_to_or_inside
from afk.redaction import is_secret_value


PI_PROMPT_PLACEHOLDER = "{prompt}"
PI_RESULT_PATH = "agent-result.json"
PONYTAIL_PACKAGE_NAME = "ponytail"
PONYTAIL_EXTENSION_SOURCE = "git:github.com/DietrichGebert/ponytail"
MAX_MODEL_VERSION = (5, 4)
MODEL_VERSION_PATTERN = re.compile(r"^gpt-(\d+)\.(\d+)(?:$|[-.])")


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
