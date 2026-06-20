from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from afk.checkouts import url_has_secret_material
from afk.contracts import ProjectContract


SCHEMA_VERSION = 1


def generate_workstream_recipe(
    *,
    workstream_id: str,
    project_contract: ProjectContract,
    beads_workspace: Path,
    checkout_root: Path,
    checkout_path: Path,
    validation_profile: str,
) -> dict[str, Any]:
    if url_has_secret_material(project_contract.repo_url):
        raise ValueError("project contract repo_url must not contain embedded credentials or query parameters")

    review_branch = f"afk/{branch_slug(workstream_id)}"
    required_labels = list(project_contract.beads_labels)
    return {
        "schema_version": SCHEMA_VERSION,
        "workstream_id": workstream_id,
        "parent": parent_from_workstream_id(workstream_id),
        "review_branch": review_branch,
        "steps": [
            {
                "name": "select-work",
                "input": {
                    "target_ids": [workstream_id],
                    "required_labels": required_labels,
                    "sources": [
                        {
                            "type": "beads",
                            "id": "central-beads",
                            "workspace": str(beads_workspace),
                            "workspace_kind": "central",
                            "labels": required_labels,
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
                    "validation": {"profile": validation_profile, "commands": []},
                    "agent": {
                        "type": "fake-pi-command",
                        "command": ["python3", "-c", default_agent_code()],
                        "result_path": "agent-result.json",
                    },
                },
            },
            {
                "name": "validate",
                "profile": validation_profile,
                "input": {
                    "validation": {"profile": validation_profile, "dry_run": True, "timeout_seconds": 30},
                    "worker": {
                        "type": "local-command",
                        "command": ["python3", "-c", default_worker_code()],
                        "timeout_seconds": 30,
                    },
                },
            },
            {
                "name": "review",
                "input": {
                    "guardrails": [{"name": "no secrets", "status": "pass"}],
                    "cleanup": {"status": "clean", "resources": []},
                    "reviewer": {
                        "type": "fake-reviewer-command",
                        "command": ["python3", "-c", default_reviewer_code()],
                        "timeout_seconds": 30,
                    },
                },
            },
        ],
        "publisher": {"enabled": False},
    }


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
