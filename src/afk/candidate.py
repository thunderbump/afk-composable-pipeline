from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.run_store import RunStore, RunStoreError


COMMAND_TIMEOUT_SECONDS = 3600
REPORT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "starting_sha",
        "ending_sha",
        "summary",
        "checks",
        "changed_areas",
    ],
    "properties": {
        "status": {"enum": ["completed", "no_change", "blocked"]},
        "starting_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "ending_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "summary": {"type": "string", "minLength": 1},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["command", "outcome"],
                "properties": {
                    "command": {"type": "string"},
                    "outcome": {"type": "string"},
                },
            },
        },
        "changed_areas": {"type": "array", "items": {"type": "string"}},
    },
}


class CandidateError(RuntimeError):
    def __init__(self, summary: str, *, kind: str = "inconclusive"):
        super().__init__(summary)
        self.summary = summary
        self.kind = kind


def produce_candidate(
    store: RunStore,
    run_id: str,
    *,
    bead: dict[str, Any],
) -> dict[str, Any]:
    """Produce and reconcile the Run's one implementation Candidate."""
    identity = store.identity(run_id)
    projection = store.status(run_id)
    worktree = Path(_field(projection, "worktree_path"))
    branch = _field(projection, "branch")
    base_sha = identity["base_sha"]
    attempt = "attempts/implementation-1"
    attempt_path = store.root / "runs" / run_id / attempt

    if (attempt_path / "manifest.json").exists():
        if not store.verify_evidence(run_id, attempt):
            raise CandidateError("implementation evidence could not be verified")
        report = _read_report(attempt_path / "report.json")
    elif attempt_path.exists():
        raise CandidateError("implementation attempt was interrupted before sealing")
    else:
        prompt = _implementation_prompt(identity, bead, worktree, branch)
        store.write_evidence_text(run_id, f"{attempt}/prompt.md", prompt)
        store.write_evidence_text(
            run_id,
            f"{attempt}/schema.json",
            canonical_json(REPORT_SCHEMA) + "\n",
        )
        with tempfile.TemporaryDirectory(prefix="afk-candidate-") as temporary:
            report_path = Path(temporary) / "report.json"
            permission_args = _codex_permission_args(worktree, branch)
            command = [
                "codex",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                *permission_args,
                "--cd",
                str(worktree),
                "--output-schema",
                str(attempt_path / "schema.json"),
                "--output-last-message",
                str(report_path),
                "--json",
                "-",
            ]
            completed = _run(
                command,
                cwd=worktree,
                env=_codex_environment(),
                input_text=prompt,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            if completed.returncode == 0:
                report = _read_report(report_path)
        store.write_evidence_text(run_id, f"{attempt}/events.jsonl", completed.stdout)
        store.write_evidence_text(run_id, f"{attempt}/stderr.txt", completed.stderr)
        if completed.returncode != 0:
            raise CandidateError(
                f"implementation agent exited with status {completed.returncode}"
            )
        store.write_evidence_text(
            run_id, f"{attempt}/report.json", canonical_json(report) + "\n"
        )
        store.seal_evidence(run_id, attempt)

    candidate_sha = _verify_candidate(
        worktree,
        branch=branch,
        base_sha=base_sha,
        report=report,
    )
    store.append_event(
        run_id,
        "candidate.change_committed",
        state="change_committed",
        data={"checkpoint": "change_committed", "candidate_sha": candidate_sha},
    )
    _reconcile_push(store, run_id, worktree, branch, candidate_sha)
    pr = _reconcile_pr(store, identity, run_id, worktree, branch, candidate_sha)
    _verify_published(identity, worktree, branch, candidate_sha, pr)
    return store.append_event(
        run_id,
        "candidate.ready",
        state="candidate_ready",
        data={
            "checkpoint": "candidate_ready",
            "candidate_sha": candidate_sha,
            "pr_number": pr["number"],
            "pr_url": pr["url"],
            "pr_head_sha": pr["headRefOid"],
        },
    )


def _implementation_prompt(
    identity: dict[str, Any],
    bead: dict[str, Any],
    worktree: Path,
    branch: str,
) -> str:
    return f"""# AFK implementation attempt

Run: {identity['run_id']}
Attempt: implementation-1
Repository: {identity['repository']}
Worktree: {worktree}
Branch: {branch}
Starting Candidate: {identity['base_sha']}

## Exact Bead

ID: {_field(bead, 'id')}
Title: {_field(bead, 'title')}
Description: {_field(bead, 'description')}
Acceptance criteria: {_field(bead, 'acceptance_criteria')}

## Contract

Work only in the dedicated worktree. Follow the repository's AGENTS.md files.
Implement the exact Bead, run appropriate local checks, and commit the result.
Do not use the network, GitHub, Beads, AFK state, or credentials. Do not merge,
rewrite the starting commit, push, or create a pull request.

Finish with the schema-constrained report. `completed` means HEAD advanced with
one or more ordinary commits and the worktree is clean. Use `no_change` when no
commit is needed and `blocked` when safe completion is impossible.
"""


def _codex_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "CODEX_HOME")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _codex_permission_args(worktree: Path, branch: str) -> list[str]:
    git_dir = _resolved_git_path(worktree, "--git-dir")
    common_dir = _resolved_git_path(worktree, "--git-common-dir")
    temporary = git_dir / "afk-tmp"
    command_home = temporary / "home"
    temporary.mkdir(mode=0o700, exist_ok=True)
    command_home.mkdir(mode=0o700, exist_ok=True)

    branch_ref_directory = common_dir / "refs" / "heads" / Path(branch).parent
    branch_log_directory = common_dir / "logs" / "refs" / "heads" / Path(branch).parent
    filesystem = {
        ":minimal": "read",
        str(Path.home().resolve()): "deny",
        str(worktree.resolve()): "write",
        str((worktree / ".git").resolve()): "read",
        str(common_dir): "read",
        str(git_dir): "write",
        str(common_dir / "objects"): "write",
        str(branch_ref_directory): "write",
        str(branch_log_directory): "write",
    }
    shell_environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(command_home),
        "TMPDIR": str(temporary),
        "GIT_TERMINAL_PROMPT": "0",
    }
    profile = (
        '{ description = "AFK Candidate implementation", filesystem = '
        f"{_toml_table(filesystem)}, network = {{ enabled = false }} }}"
    )
    shell_policy = (
        '{ inherit = "none", ignore_default_excludes = false, set = '
        f"{_toml_table(shell_environment)} }}"
    )
    return [
        "-c",
        'default_permissions="afk_candidate"',
        "-c",
        f"permissions.afk_candidate={profile}",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        f"shell_environment_policy={shell_policy}",
    ]


def _resolved_git_path(worktree: Path, argument: str) -> Path:
    path = Path(_git(worktree, "rev-parse", argument))
    return path.resolve() if path.is_absolute() else (worktree / path).resolve()


def _toml_table(values: dict[str, str]) -> str:
    fields = ", ".join(
        f"{json.dumps(key)} = {json.dumps(value)}" for key, value in values.items()
    )
    return f"{{ {fields} }}"


def _read_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("implementation report is missing or malformed") from exc
    if not isinstance(value, dict) or set(value) != set(REPORT_SCHEMA["required"]):
        raise CandidateError("implementation report is missing or malformed")
    if value.get("status") not in {"completed", "no_change", "blocked"}:
        raise CandidateError("implementation report is missing or malformed")
    for key in ("starting_sha", "ending_sha", "summary"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise CandidateError("implementation report is missing or malformed")
    for key in ("starting_sha", "ending_sha"):
        if re.fullmatch(r"[0-9a-f]{40}", value[key]) is None:
            raise CandidateError("implementation report is missing or malformed")
    if not isinstance(value.get("checks"), list) or not isinstance(
        value.get("changed_areas"), list
    ):
        raise CandidateError("implementation report is missing or malformed")
    if not all(
        isinstance(check, dict)
        and set(check) == {"command", "outcome"}
        and all(isinstance(check[field], str) for field in check)
        for check in value["checks"]
    ) or not all(isinstance(area, str) for area in value["changed_areas"]):
        raise CandidateError("implementation report is missing or malformed")
    return value


def _verify_candidate(
    worktree: Path,
    *,
    branch: str,
    base_sha: str,
    report: dict[str, Any],
) -> str:
    head = _git(worktree, "rev-parse", "HEAD")
    observed_branch = _git(worktree, "branch", "--show-current")
    dirty = _git(worktree, "status", "--porcelain")
    if report["status"] != "completed":
        raise CandidateError(f"implementation reported {report['status']}")
    if report["starting_sha"] != base_sha or report["ending_sha"] != head:
        raise CandidateError("implementation report contradicts observed Git state")
    if head == base_sha:
        raise CandidateError("implementation did not advance HEAD", kind="invalid")
    if observed_branch != branch:
        raise CandidateError("implementation changed or detached the intended branch")
    if dirty:
        raise CandidateError("implementation left a dirty worktree", kind="invalid")
    ancestor = _run(
        ["git", "merge-base", "--is-ancestor", base_sha, head], cwd=worktree
    )
    if ancestor.returncode != 0:
        raise CandidateError("implementation rewrote or abandoned the starting commit")
    if _git(worktree, "rev-list", "--merges", f"{base_sha}..{head}"):
        raise CandidateError("implementation introduced a merge commit", kind="invalid")
    return head


def _reconcile_push(
    store: RunStore,
    run_id: str,
    worktree: Path,
    branch: str,
    candidate_sha: str,
) -> None:
    effect_id = f"branch-push-{candidate_sha}"
    effect = store.prepare_effect(
        run_id,
        effect_id,
        kind="branch-push",
        intended={"branch": branch, "candidate_sha": candidate_sha, "remote": "origin"},
    )
    remote_sha = _remote_sha(worktree, branch)
    if remote_sha and remote_sha != candidate_sha:
        raise CandidateError("remote Candidate branch has a contradictory head")
    if not remote_sha:
        pushed = _run(
            ["git", "push", "origin", f"{candidate_sha}:refs/heads/{branch}"],
            cwd=worktree,
        )
        if pushed.returncode != 0:
            raise CandidateError("Candidate branch push failed")
        remote_sha = _remote_sha(worktree, branch)
    observed = {"branch": branch, "candidate_sha": remote_sha, "remote": "origin"}
    if remote_sha != candidate_sha:
        raise CandidateError("remote Candidate head could not be confirmed")
    if effect["status"] == "confirmed" and effect.get("observed") != observed:
        raise CandidateError("confirmed Candidate push contradicts the remote")
    store.confirm_effect(run_id, effect_id, observed=observed)


def _reconcile_pr(
    store: RunStore,
    identity: dict[str, Any],
    run_id: str,
    worktree: Path,
    branch: str,
    candidate_sha: str,
) -> dict[str, Any]:
    title = f"{identity['bead_id']}: AFK Candidate"
    body = (
        f"AFK Run `{run_id}` produced Candidate `{candidate_sha}` for "
        f"Bead `{identity['bead_id']}`.\n"
    )
    effect = store.prepare_effect(
        run_id,
        "pr-create",
        kind="pr-create",
        intended={
            "repository": identity["repository"],
            "base": identity["base_branch"],
            "head": branch,
            "candidate_sha": candidate_sha,
            "title": title,
            "body": body,
        },
    )
    prs = _list_prs(worktree, identity["repository"], branch)
    if not prs:
        completed = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                identity["repository"],
                "--base",
                identity["base_branch"],
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
                "--draft",
            ],
            cwd=worktree,
        )
        if completed.returncode != 0:
            raise CandidateError("draft Candidate PR creation failed")
        prs = _list_prs(worktree, identity["repository"], branch)
    if len(prs) != 1:
        raise CandidateError("Candidate branch does not have exactly one stable PR")
    pr = prs[0]
    expected = {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "head_sha": pr.get("headRefOid"),
        "head": pr.get("headRefName"),
        "base": pr.get("baseRefName"),
        "state": pr.get("state"),
        "draft": pr.get("isDraft"),
    }
    if effect["status"] == "confirmed" and effect.get("observed") != expected:
        raise CandidateError("confirmed Candidate PR contradicts GitHub")
    store.confirm_effect(run_id, "pr-create", observed=expected)
    return pr


def _list_prs(worktree: Path, repository: str, branch: str) -> list[dict[str, Any]]:
    completed = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,isDraft,headRefOid,headRefName,baseRefName",
        ],
        cwd=worktree,
    )
    if completed.returncode != 0:
        raise CandidateError("Candidate PR observation failed")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CandidateError("Candidate PR observation was malformed") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CandidateError("Candidate PR observation was malformed")
    return value


def _verify_published(
    identity: dict[str, Any],
    worktree: Path,
    branch: str,
    candidate_sha: str,
    pr: dict[str, Any],
) -> None:
    local = _git(worktree, "rev-parse", "HEAD")
    remote = _remote_sha(worktree, branch)
    target = _remote_sha(worktree, identity["base_branch"])
    if target != identity["base_sha"]:
        raise CandidateError("target branch no longer equals the pinned base")
    if (
        local != candidate_sha
        or remote != candidate_sha
        or pr.get("headRefOid") != candidate_sha
        or pr.get("headRefName") != branch
        or pr.get("baseRefName") != identity["base_branch"]
        or pr.get("state") != "OPEN"
        or pr.get("isDraft") is not True
        or type(pr.get("number")) is not int
        or not isinstance(pr.get("url"), str)
    ):
        raise CandidateError("local, remote, and draft PR Candidate facts disagree")


def _remote_sha(worktree: Path, branch: str) -> str:
    completed = _run(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=worktree
    )
    if completed.returncode != 0:
        raise CandidateError("remote Candidate head observation failed")
    fields = completed.stdout.strip().split()
    if not fields:
        return ""
    if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
        raise CandidateError("remote Candidate head observation was malformed")
    return fields[0]


def _git(worktree: Path, *args: str) -> str:
    completed = _run(["git", *args], cwd=worktree)
    if completed.returncode != 0:
        raise CandidateError(f"Git inspection failed: {' '.join(args)}")
    return completed.stdout.strip()


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CandidateError(f"{Path(command[0]).name} command timed out") from exc
    except OSError as exc:
        raise CandidateError(f"{Path(command[0]).name} command is unavailable") from exc


def _field(value: dict[str, Any], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field:
        raise RunStoreError(f"required Candidate field is missing: {key}")
    return field
