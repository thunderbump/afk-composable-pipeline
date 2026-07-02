from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from afk.checkouts import url_has_secret_material
from afk.contracts import ProjectContract
from afk.implement import agent_command_secret_error


SCHEMA_VERSION = 1
RUNNABLE_REQUIRED_METADATA = ["afk.ready"]


def generate_workstream_recipe(
    *,
    workstream_id: str,
    project_contract: ProjectContract,
    beads_workspace: Path,
    checkout_root: Path,
    checkout_path: Path,
    validation_profile: str,
    validation_input: dict[str, Any] | None = None,
    agent: dict[str, Any] | None = None,
    reviewer: dict[str, Any] | None = None,
    retrospective_judge: dict[str, Any] | None = None,
    retrospective_follow_up: dict[str, Any] | None = None,
    publisher: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    required_labels: list[str] | None = None,
    required_metadata: list[str] | None = None,
    enable_review_feedback: bool = False,
    expect_generated_smoke_dry_run: bool = False,
) -> dict[str, Any]:
    if url_has_secret_material(project_contract.repo_url):
        raise ValueError("project contract repo_url must not contain embedded credentials or query parameters")

    review_branch = review_branch_for_workstream(workstream_id)
    recipe_required_labels = required_labels if required_labels is not None else list(project_contract.beads_labels)
    recipe_required_metadata = (
        list(required_metadata) if required_metadata is not None else list(RUNNABLE_REQUIRED_METADATA)
    )
    implement_agent = agent if agent is not None else default_recipe_agent()
    validate_step_input = validation_input if validation_input is not None else default_validation_input(validation_profile)
    recipe_publisher = publisher if publisher is not None else {"enabled": False}
    recipe_reviewer = reviewer if reviewer is not None else default_reviewer_config()

    recipe: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "workstream_id": workstream_id,
        "parent": parent_from_workstream_id(workstream_id),
        "review_branch": review_branch,
        "validation_feedback": {"enabled": True},
        "review_feedback": {"enabled": enable_review_feedback},
        "retry_policy": {"max_retries": 1},
        "steps": [
            {
                "name": "select-work",
                "input": {
                    "target_ids": [workstream_id],
                    "required_labels": recipe_required_labels,
                    "required_metadata": recipe_required_metadata,
                    "allowed_statuses": ["open", "in_progress"],
                    "sources": sources
                    if sources is not None
                    else [
                        {
                            "type": "beads",
                            "id": "central-beads",
                            "workspace": str(beads_workspace),
                            "workspace_kind": "central",
                            "labels": recipe_required_labels,
                            "status": "open",
                        }
                    ],
                },
            },
            {
                "name": "prepare-checkout",
                "input": {
                    "repo_url": project_contract.repo_url,
                    "base_ref": project_contract.base_branch,
                    "checkout_root": str(checkout_root),
                    "checkout_path": str(checkout_path),
                },
            },
            {
                "name": "implement",
                "input": {
                    "guardrails": ["stay within the prepared checkout", "do not write secrets"],
                    "validation": implement_validation_input(
                        validation_profile=validation_profile,
                        validation_input=validate_step_input,
                    ),
                    "agent": implement_agent,
                },
            },
            {
                "name": "validate",
                "profile": validation_profile,
                "input": validate_step_input,
            },
            {
                "name": "review",
                "input": {
                    "guardrails": [{"name": "no secrets", "status": "pass"}],
                    "cleanup": {"status": "clean", "resources": []},
                    "reviewer": recipe_reviewer,
                },
            },
        ],
        "publisher": recipe_publisher,
    }

    if retrospective_judge is not None:
        recipe["retrospective_judge"] = retrospective_judge
    if retrospective_follow_up is not None:
        recipe["retrospective_follow_up"] = retrospective_follow_up
    if expect_generated_smoke_dry_run:
        recipe["validation_expectations"] = {"generated_smoke_dry_run_expected": True}

    return recipe


def write_recipe(path: Path, recipe: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recipe, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parent_from_workstream_id(workstream_id: str) -> str:
    if "." in workstream_id:
        return workstream_id.rsplit(".", 1)[0]
    if "-" in workstream_id:
        return workstream_id.rsplit("-", 1)[0]
    return ""


def branch_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or "workstream"


def review_branch_for_workstream(workstream_id: str) -> str:
    return f"afk/{branch_slug(workstream_id)}"


def default_agent_code() -> str:
    return (
        "import json, subprocess\n"
        "from pathlib import Path\n"
        "Path('afk-generated-workstream.txt').write_text('generated recipe executed\\n', encoding='utf-8')\n"
        "subprocess.run(['git','config','user.name','AFK Generated Recipe'], check=True)\n"
        "subprocess.run(['git','config','user.email','afk-generated@example.test'], check=True)\n"
        "subprocess.run(['git','add','afk-generated-workstream.txt'], check=True)\n"
        "subprocess.run(['git','commit','-m','afk generated recipe evidence'], check=True)\n"
        "Path('agent-result.json').write_text(json.dumps({'status':'completed','summary':'generated recipe stub implementation'}), encoding='utf-8')\n"
    )


def default_recipe_agent() -> dict[str, Any]:
    return {
        "type": "fake-pi-command",
        "command": ["python3", "-c", default_agent_code()],
        "result_path": "agent-result.json",
    }


def default_validation_input(validation_profile: str) -> dict[str, Any]:
    return {
        "validation": {"profile": validation_profile, "dry_run": True, "timeout_seconds": 30},
        "worker": {
            "type": "local-command",
            "command": ["python3", "-c", default_worker_code()],
            "timeout_seconds": 30,
        },
    }


def validate_recipe_agent_command(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("agent.command must be a non-empty JSON array of strings")
    command: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("agent.command must be a non-empty JSON array of strings")
        command.append(item)
    secret_error = agent_command_secret_error(command)
    if secret_error:
        raise ValueError(secret_error)
    return command


def validate_recipe_absolute_dir(
    value: str | None,
    field: str,
    *,
    checkout_path: Path,
) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{field} is required")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field} must be absolute")
    if not path.is_dir():
        raise ValueError(f"{field} must be an existing directory")
    if path_is_equal_to_or_inside(path, checkout_path):
        raise ValueError(f"{field} must be outside checkout")
    return str(path)


def real_local_recipe_agent(
    *,
    command: list[str],
    codex_home: str,
    config_home: str,
    pi_config_home: str,
    checkout_path: Path,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    agent: dict[str, Any] = {
        "type": "real-agent-command",
        "command": validate_recipe_agent_command(command),
        "result_path": "agent-result.json",
        "codex_home": validate_recipe_absolute_dir(codex_home, "agent.codex_home", checkout_path=checkout_path),
        "config_home": validate_recipe_absolute_dir(config_home, "agent.config_home", checkout_path=checkout_path),
        "env": {
            "PI_CONFIG_HOME": validate_recipe_absolute_dir(
                pi_config_home,
                "agent.env.PI_CONFIG_HOME",
                checkout_path=checkout_path,
            )
        },
    }
    if timeout_seconds is not None:
        agent["timeout_seconds"] = timeout_seconds
    return agent


def implement_validation_input(
    *,
    validation_profile: str,
    validation_input: dict[str, Any],
) -> dict[str, Any]:
    implement_validation: dict[str, Any] = {"profile": validation_profile, "commands": []}
    validation = validation_input.get("validation")
    if not isinstance(validation, dict):
        return implement_validation
    commands = validation.get("commands")
    if isinstance(commands, list) and all(isinstance(command, list) and all(isinstance(part, str) for part in command) for command in commands):
        implement_validation["commands"] = [list(command) for command in commands]
    if not implement_validation["commands"]:
        implement_validation["run_commands_during_implementation"] = False
    worker_home = validation.get("worker_home")
    if not isinstance(worker_home, str) or not worker_home.strip():
        worker_home = validation.get("workerHome")
    if isinstance(worker_home, str) and worker_home.strip():
        implement_validation["worker_home"] = worker_home
    stack = validation.get("stack")
    if isinstance(stack, dict):
        role = stack.get("role")
        path = stack.get("path")
        normalized_role = role if isinstance(role, str) and role.strip() else "validation"
        if isinstance(path, str) and path.strip():
            implement_validation["stack"] = {"role": normalized_role, "path": path}
    return implement_validation


def create_recipe_publisher(
    *,
    review_branch: str,
    repo: str,
    base: str,
    gh_config_dir: str,
    checkout_path: Path,
) -> dict[str, Any]:
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("publisher.repo is required")
    if not isinstance(base, str) or not base.strip():
        raise ValueError("publisher.base is required for create")
    config_dir = validate_recipe_absolute_dir(
        gh_config_dir,
        "publisher.gh.auth.config_dir",
        checkout_path=checkout_path,
    )
    return {
        "enabled": True,
        "mode": "create",
        "repo": repo.strip(),
        "base": base.strip(),
        "head": review_branch,
        "git": {"push": True, "remote": "origin"},
        "gh": {"auth": {"config_dir": config_dir}},
    }


def default_reviewer_config() -> dict[str, Any]:
    return {
        "type": "fake-reviewer-command",
        "command": ["python3", "-c", default_reviewer_code()],
        "timeout_seconds": 30,
    }


def path_is_equal_to_or_inside(path: Path, parent: Path) -> bool:
    path_resolved = path.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    return path_resolved == parent_resolved or parent_resolved in path_resolved.parents


def default_worker_code() -> str:
    return (
        "import json, os\n"
        "from pathlib import Path\n"
        "request=json.loads(Path(os.environ['AFK_WORKER_REQUEST']).read_text(encoding='utf-8'))\n"
        "Path(os.environ['AFK_WORKER_RESULT']).write_text(json.dumps({'profile':request['profile'],'status':'pass','failureCount':0,'steps':[{'name':'generated-recipe-smoke','status':'pass'}]}), encoding='utf-8')\n"
    )


def default_reviewer_code() -> str:
    return (
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['AFK_REVIEWER_RESULT']).write_text(json.dumps({'status':'pass','summary':'generated recipe review passed','findings':[]}), encoding='utf-8')\n"
    )
