from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def codex_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "CODEX_HOME")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def codex_package_beneath_home() -> Path | None:
    executable = shutil.which("codex")
    if executable is None:
        return None
    resolved = Path(executable).resolve()
    home = Path.home().resolve()
    if not resolved.is_relative_to(home):
        return None
    if resolved.name != "codex.js" or resolved.parent.name != "bin":
        return None
    package = resolved.parent.parent
    if (
        package.name != "codex"
        or package.parent.name != "@openai"
        or package.parent.parent.name != "node_modules"
    ):
        return None
    return package


def codex_permission_args(
    *,
    profile_name: str,
    description: str,
    filesystem: dict[str, str],
    shell_environment: dict[str, str],
) -> list[str]:
    profile = (
        f"{{ description = {json.dumps(description)}, filesystem = "
        f"{_toml_table(filesystem)}, network = {{ enabled = false }} }}"
    )
    shell_policy = (
        '{ inherit = "none", ignore_default_excludes = false, set = '
        f"{_toml_table(shell_environment)} }}"
    )
    return [
        "-c",
        f'default_permissions="{profile_name}"',
        "-c",
        f"permissions.{profile_name}={profile}",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        f"shell_environment_policy={shell_policy}",
    ]


def _toml_table(values: dict[str, str]) -> str:
    fields = ", ".join(
        f"{json.dumps(key)} = {json.dumps(value)}" for key, value in values.items()
    )
    return f"{{ {fields} }}"
