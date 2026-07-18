from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from afk.bead_spec import load_bead_spec
from afk.candidate_validation import (
    CandidateValidationError,
    run_supervised_command,
)
from afk.codex_permissions import (
    codex_environment,
    codex_package_beneath_home,
    codex_permission_args,
)
from afk.jsonutil import canonical_json
from afk.redaction import redact_artifact_value
from afk.run_store import RunStore, RunStoreError


COMMAND_TIMEOUT_SECONDS = 3600
BEAD_COMMENT_CONTEXT_MAX_CHARS = 4_000
BEAD_COMMENT_CONTEXT_MAX_ITEMS = 8
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
REPAIR_REPORT_SCHEMA = {
    **REPORT_SCHEMA,
    "required": [*REPORT_SCHEMA["required"], "dispositions"],
    "properties": {
        **REPORT_SCHEMA["properties"],
        "dispositions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["finding_id", "disposition"],
                "properties": {
                    "finding_id": {"type": "string", "minLength": 1},
                    "disposition": {"enum": ["addressed", "not_addressed", "disputed"]},
                },
            },
        },
    },
}


class CandidateError(RuntimeError):
    def __init__(
        self,
        summary: str,
        *,
        kind: str = "inconclusive",
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(summary)
        self.summary = summary
        self.kind = kind
        self.stdout = stdout
        self.stderr = stderr


def produce_candidate(
    store: RunStore,
    run_id: str,
    *,
    bead: dict[str, Any],
) -> dict[str, Any]:
    """Produce and reconcile the Run's one implementation Candidate."""
    bead = load_bead_spec(store, run_id, fallback=bead)
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
                env=codex_environment(),
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


def produce_repair_candidate(
    store: RunStore,
    run_id: str,
    *,
    bead: dict[str, Any],
    repair_brief: dict[str, Any],
) -> dict[str, Any]:
    """Run one budgeted repair and advance the existing Candidate branch/PR."""
    bead = load_bead_spec(store, run_id, fallback=bead)
    identity = store.identity(run_id)
    projection = store.status(run_id)
    worktree = Path(_field(projection, "worktree_path"))
    branch = _field(projection, "branch")
    previous_sha = _field(projection, "candidate_sha")
    attempt_number = repair_brief.get("repair_attempt")
    if type(attempt_number) is not int or not 1 <= attempt_number <= 4:
        raise CandidateError(
            "repair attempt is outside the four-slot budget", kind="invalid"
        )
    if repair_brief.get("candidate_sha") != previous_sha:
        raise CandidateError(
            "Repair Brief is not bound to the current Candidate", kind="invalid"
        )
    attempt = f"attempts/repair-{attempt_number}"
    attempt_path = store.root / "runs" / run_id / attempt
    if attempt_path.exists():
        if (
            projection.get("repair_attempts_used") != attempt_number
            or projection.get("repair_brief") != repair_brief
        ):
            raise CandidateError("repair attempt is not bound to the current brief")
        if not (attempt_path / "manifest.json").is_file():
            raise CandidateError("repair attempt is incomplete", kind="inconclusive")
        if not store.verify_evidence(run_id, attempt):
            raise CandidateError("repair attempt evidence could not be verified")
        result_paths = [
            path
            for name in ("report.json", "outcome.json")
            if (path := attempt_path / name).is_file()
        ]
        if len(result_paths) != 1 or result_paths[0].name != "report.json":
            raise CandidateError("repair attempt did not produce a completed report")
        report = _read_repair_report(result_paths[0], repair_brief)
        return _finish_repair_candidate(
            store,
            run_id,
            identity=identity,
            worktree=worktree,
            branch=branch,
            previous_sha=previous_sha,
            attempt_number=attempt_number,
            report=report,
        )

    store.append_event(
        run_id,
        "repair.started",
        data={
            "checkpoint": projection["checkpoint"],
            "repair_attempts_used": attempt_number,
            "repair_brief": repair_brief,
            "interrupted_repair": {},
        },
    )
    prompt = _repair_prompt(identity, bead, repair_brief, worktree, branch)
    store.write_evidence_text(run_id, f"{attempt}/prompt.md", prompt)
    store.write_evidence_text(
        run_id,
        f"{attempt}/schema.json",
        canonical_json(REPAIR_REPORT_SCHEMA) + "\n",
    )
    with tempfile.TemporaryDirectory(prefix="afk-repair-") as temporary:
        report_path = Path(temporary) / "report.json"
        report_error: CandidateError | None = None
        execution_error: CandidateError | None = None
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            *_codex_permission_args(worktree, branch),
            "--cd",
            str(worktree),
            "--output-schema",
            str(attempt_path / "schema.json"),
            "--output-last-message",
            str(report_path),
            "--json",
            "-",
        ]
        try:
            completed = _run_codex(command, cwd=worktree, input_text=prompt)
        except CandidateError as exc:
            execution_error = exc
        if execution_error is None and completed.returncode == 0:
            try:
                report = _read_repair_report(report_path, repair_brief)
            except CandidateError as exc:
                report_error = exc
                try:
                    raw_report = report_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    raw_report = ""
    if execution_error is not None:
        store.write_evidence_text(
            run_id, f"{attempt}/events.jsonl", execution_error.stdout
        )
        store.write_evidence_text(
            run_id, f"{attempt}/stderr.txt", execution_error.stderr
        )
        store.write_evidence_text(
            run_id,
            f"{attempt}/outcome.json",
            canonical_json(
                {"status": execution_error.kind, "summary": execution_error.summary}
            )
            + "\n",
        )
        store.seal_evidence(run_id, attempt)
        raise execution_error
    store.write_evidence_text(run_id, f"{attempt}/events.jsonl", completed.stdout)
    store.write_evidence_text(run_id, f"{attempt}/stderr.txt", completed.stderr)
    if completed.returncode != 0 or report_error is not None:
        error = report_error or CandidateError(
            f"repair agent exited with status {completed.returncode}"
        )
        if report_error is not None:
            store.write_evidence_text(run_id, f"{attempt}/raw-report.txt", raw_report)
        store.write_evidence_text(
            run_id,
            f"{attempt}/outcome.json",
            canonical_json({"status": error.kind, "summary": error.summary}) + "\n",
        )
        store.seal_evidence(run_id, attempt)
        raise error
    store.write_evidence_text(
        run_id, f"{attempt}/report.json", canonical_json(report) + "\n"
    )
    store.seal_evidence(run_id, attempt)

    return _finish_repair_candidate(
        store,
        run_id,
        identity=identity,
        worktree=worktree,
        branch=branch,
        previous_sha=previous_sha,
        attempt_number=attempt_number,
        report=report,
    )


def seal_interrupted_repair_attempt(
    store: RunStore,
    run_id: str,
    *,
    repair_brief: dict[str, Any],
) -> dict[str, Any]:
    """Seal an uncompleted repair slot without treating it as resumable output."""
    projection = store.status(run_id)
    attempt_number = repair_brief.get("repair_attempt")
    candidate_sha = repair_brief.get("candidate_sha")
    if (
        type(attempt_number) is not int
        or not 1 <= attempt_number <= 4
        or projection.get("repair_attempts_used") != attempt_number
        or projection.get("candidate_sha") != candidate_sha
    ):
        raise CandidateError("interrupted repair is not bound to the consumed slot")
    attempt = f"attempts/repair-{attempt_number}"
    attempt_path = store.root / "runs" / run_id / attempt
    if attempt_path.exists() and not attempt_path.is_dir():
        raise CandidateError("interrupted repair evidence is invalid")
    interruption = {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "repair_attempt": attempt_number,
        "status": "interrupted",
        "summary": "repair execution ended before evidence was sealed",
    }
    interruption_path = attempt_path / "interruption.json"
    if (attempt_path / "manifest.json").is_file():
        if not store.verify_evidence(run_id, attempt):
            raise CandidateError("interrupted repair evidence could not be verified")
        if not interruption_path.is_file():
            raise CandidateError("sealed repair attempt is not an interruption")
    elif interruption_path.exists():
        if not interruption_path.is_file():
            raise CandidateError("interrupted repair classification is invalid")
    else:
        store.write_evidence_value(
            run_id,
            f"{attempt}/interruption.json",
            interruption,
        )
    try:
        observed = json.loads(interruption_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("interrupted repair classification is invalid") from exc
    if observed != interruption:
        raise CandidateError("interrupted repair classification is ambiguous")
    if not (attempt_path / "manifest.json").is_file():
        store.seal_evidence(run_id, attempt)
    return interruption


def reconcile_interrupted_repair_worktree(
    store: RunStore,
    run_id: str,
    *,
    repair_brief: dict[str, Any],
) -> None:
    """Restore the dedicated worktree to the published pre-repair Candidate."""
    projection = store.status(run_id)
    candidate_sha = repair_brief.get("candidate_sha")
    worktree = Path(_field(projection, "worktree_path"))
    branch = _field(projection, "branch")
    if candidate_sha != projection.get("candidate_sha"):
        raise CandidateError("interrupted repair Candidate binding is invalid")
    expected_worktree = store.root / "worktrees" / run_id
    if worktree.resolve() != expected_worktree.resolve():
        raise CandidateError("interrupted repair worktree identity is invalid")
    observed_root = Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve()
    if observed_root != worktree.resolve():
        raise CandidateError("interrupted repair worktree identity is invalid")
    if _git(worktree, "branch", "--show-current") != branch:
        raise CandidateError("interrupted repair changed the intended branch")
    for command in (
        ["git", "reset", "--hard", candidate_sha],
        ["git", "clean", "-ffdx"],
    ):
        completed = _run(command, cwd=worktree)
        if completed.returncode != 0:
            raise CandidateError("interrupted repair worktree could not be restored")
    verify_candidate_publication(store.identity(run_id), store.status(run_id))


def _finish_repair_candidate(
    store: RunStore,
    run_id: str,
    *,
    identity: dict[str, Any],
    worktree: Path,
    branch: str,
    previous_sha: str,
    attempt_number: int,
    report: dict[str, Any],
) -> dict[str, Any]:
    candidate_sha = _verify_candidate(
        worktree,
        branch=branch,
        base_sha=previous_sha,
        report=report,
    )
    _reconcile_push(
        store,
        run_id,
        worktree,
        branch,
        candidate_sha,
        expected_previous_sha=previous_sha,
    )
    prs = _list_prs(worktree, identity["repository"], branch)
    if len(prs) != 1:
        raise CandidateError("Candidate branch does not have exactly one stable PR")
    pr = prs[0]
    _verify_published(identity, worktree, branch, candidate_sha, pr)
    return store.append_event(
        run_id,
        "candidate.repaired",
        state="candidate_ready",
        data={
            "checkpoint": "candidate_ready",
            "previous_candidate_sha": previous_sha,
            "candidate_sha": candidate_sha,
            "pr_number": pr["number"],
            "pr_url": pr["url"],
            "pr_head_sha": pr["headRefOid"],
            "repair_attempts_used": attempt_number,
            "repair_dispositions": report["dispositions"],
            "attention": {},
        },
    )


def _implementation_prompt(
    identity: dict[str, Any],
    bead: dict[str, Any],
    worktree: Path,
    branch: str,
) -> str:
    bead = redact_artifact_value(bead)
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

## Immutable Bead comments (latest first)

{_bead_comment_context(bead)}

## Contract

Work only in the dedicated worktree. Follow the repository's AGENTS.md files.
Implement the exact Bead and run safe, unprivileged local checks.
Commit after the safe checks available inside this sandbox pass.
AFK runs the full Validation Contract afterward against the immutable commit.
If repository instructions
require privileged or full-contract validation before commit, defer that check
to AFK. Do not report blocked solely because privileged validation is unavailable.
Candidate changes to `afk.toml` or its validation harness are proposals until
merged; they do not become privileged executable policy in this Run.
Do not access Docker, the Docker socket, or systemd.
Do not use the network, GitHub, Beads, AFK state, or credentials. Do not merge,
rewrite the starting commit, push, or create a pull request.

Finish with the schema-constrained report. `completed` means HEAD advanced with
one or more ordinary commits and the worktree is clean. Use `no_change` when no
commit is needed and `blocked` when safe completion is impossible.
"""


def _bead_comment_context(bead: dict[str, Any]) -> str:
    comments = bead.get("comments")
    if not isinstance(comments, list) or not comments:
        return "(none)"
    lines = []
    remaining = BEAD_COMMENT_CONTEXT_MAX_CHARS
    for comment in reversed(comments[-BEAD_COMMENT_CONTEXT_MAX_ITEMS:]):
        if remaining <= 0:
            break
        line = "- " + canonical_json(comment)
        if len(line) > remaining:
            line = line[: max(0, remaining - 1)] + "…"
        lines.append(line)
        remaining -= len(line) + 1
    return "\n".join(lines)


def _repair_prompt(
    identity: dict[str, Any],
    bead: dict[str, Any],
    repair_brief: dict[str, Any],
    worktree: Path,
    branch: str,
) -> str:
    bead = redact_artifact_value(bead)
    repair_brief = redact_artifact_value(repair_brief)
    return f"""# AFK repair attempt

Run: {identity['run_id']}
Attempt: repair-{repair_brief['repair_attempt']}
Repository: {identity['repository']}
Worktree: {worktree}
Branch: {branch}
Starting Candidate: {repair_brief['candidate_sha']}

## Exact Bead

ID: {_field(bead, 'id')}
Title: {_field(bead, 'title')}
Description: {_field(bead, 'description')}
Acceptance criteria: {_field(bead, 'acceptance_criteria')}

## Candidate-bound Repair Brief

{canonical_json(repair_brief)}

## Contract

Work only in the dedicated worktree and follow its AGENTS.md files. Address the
blocking findings, run safe unprivileged checks, and commit the repair. Do not
access Docker, systemd, the network, GitHub, Beads, AFK state, or credentials.
Do not merge, rewrite the Starting Candidate, push, or create a pull request.
Return one disposition for every blocking finding. Dispositions are claims for
the audit trail; the next full validation and review cycle decides correctness.
"""


def _codex_permission_args(worktree: Path, branch: str) -> list[str]:
    branch_parts = Path(branch).parts
    if (
        len(branch_parts) != 3
        or branch_parts[0] != "afk"
        or not branch_parts[1]
        or branch_parts[2] != "candidate"
    ):
        raise CandidateError("Candidate branch lacks a private per-Run namespace")
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
        str(worktree.resolve()): "write",
        str((worktree / ".git").resolve()): "read",
        str(common_dir): "read",
        str(git_dir): "write",
        str(common_dir / "objects"): "write",
        str(branch_ref_directory): "write",
        str(branch_log_directory): "write",
    }
    if shutil.which("codex") is None:
        raise CandidateError("Codex executable is unavailable")
    codex_package = codex_package_beneath_home()
    if codex_package is not None:
        filesystem[str(codex_package)] = "read"
    shell_environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(command_home),
        "TMPDIR": str(temporary),
        "GIT_TERMINAL_PROMPT": "0",
    }
    return codex_permission_args(
        profile_name="afk_candidate",
        description="AFK Candidate implementation",
        filesystem=filesystem,
        shell_environment=shell_environment,
    )


def _resolved_git_path(worktree: Path, argument: str) -> Path:
    path = Path(_git(worktree, "rev-parse", argument))
    return path.resolve() if path.is_absolute() else (worktree / path).resolve()


def _read_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("implementation report is missing or malformed") from exc
    if not isinstance(value, dict) or set(value) != set(REPORT_SCHEMA["required"]):
        raise CandidateError("implementation report is missing or malformed")
    _validate_report(value, label="implementation")
    return value


def _read_repair_report(path: Path, repair_brief: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError("repair report is missing or malformed") from exc
    expected = set(REPAIR_REPORT_SCHEMA["required"])
    if not isinstance(value, dict) or set(value) != expected:
        raise CandidateError("repair report is missing or malformed")
    base_report = {key: value[key] for key in REPORT_SCHEMA["required"]}
    _validate_report(base_report, label="repair")
    dispositions = value["dispositions"]
    if not isinstance(dispositions, list) or not all(
        isinstance(item, dict)
        and set(item) == {"finding_id", "disposition"}
        and isinstance(item["finding_id"], str)
        and item["disposition"] in {"addressed", "not_addressed", "disputed"}
        for item in dispositions
    ):
        raise CandidateError("repair report dispositions are malformed")
    expected_ids = [
        finding.get("id") for finding in repair_brief.get("blocking_findings", [])
    ]
    if [item["finding_id"] for item in dispositions] != expected_ids:
        raise CandidateError("repair report dispositions do not match the Repair Brief")
    return value


def _validate_report(value: dict[str, Any], *, label: str) -> None:
    if value.get("status") not in {"completed", "no_change", "blocked"}:
        raise CandidateError(f"{label} report is missing or malformed")
    for key in ("starting_sha", "ending_sha", "summary"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise CandidateError(f"{label} report is missing or malformed")
    for key in ("starting_sha", "ending_sha"):
        if re.fullmatch(r"[0-9a-f]{40}", value[key]) is None:
            raise CandidateError(f"{label} report is missing or malformed")
    if not isinstance(value.get("checks"), list) or not isinstance(
        value.get("changed_areas"), list
    ):
        raise CandidateError(f"{label} report is missing or malformed")
    if not all(
        isinstance(check, dict)
        and set(check) == {"command", "outcome"}
        and all(isinstance(check[field], str) for field in check)
        for check in value["checks"]
    ) or not all(isinstance(area, str) for area in value["changed_areas"]):
        raise CandidateError(f"{label} report is missing or malformed")


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
    *,
    expected_previous_sha: str | None = None,
) -> None:
    effect_id = f"branch-push-{candidate_sha}"
    effect = store.prepare_effect(
        run_id,
        effect_id,
        kind="branch-push",
        intended={"branch": branch, "candidate_sha": candidate_sha, "remote": "origin"},
    )
    remote_sha = _remote_sha(worktree, branch)
    allowed_remote = {candidate_sha}
    if expected_previous_sha is not None:
        allowed_remote.add(expected_previous_sha)
    if remote_sha and remote_sha not in allowed_remote:
        raise CandidateError("remote Candidate branch has a contradictory head")
    if remote_sha != candidate_sha:
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
    *,
    expected_pr_number: int | None = None,
    expected_pr_url: str | None = None,
    expected_draft: bool | None = True,
) -> None:
    local = _git(worktree, "rev-parse", "HEAD")
    dirty = _git(worktree, "status", "--porcelain")
    remote = _remote_sha(worktree, branch)
    target = _remote_sha(worktree, identity["base_branch"])
    if target != identity["base_sha"]:
        raise CandidateError(
            "target branch no longer equals the pinned base", kind="conflict"
        )
    if (
        local != candidate_sha
        or dirty
        or remote != candidate_sha
        or pr.get("headRefOid") != candidate_sha
    ):
        raise CandidateError(
            "local, remote, and PR heads disagree with the Candidate",
            kind="head_mismatch",
        )
    if (
        (expected_pr_number is not None and pr.get("number") != expected_pr_number)
        or (expected_pr_url is not None and pr.get("url") != expected_pr_url)
        or pr.get("headRefName") != branch
        or pr.get("baseRefName") != identity["base_branch"]
        or pr.get("state") != "OPEN"
        or type(pr.get("isDraft")) is not bool
        or (expected_draft is not None and pr.get("isDraft") is not expected_draft)
        or type(pr.get("number")) is not int
        or not isinstance(pr.get("url"), str)
    ):
        raise CandidateError("PR Candidate facts disagree", kind="conflict")


def verify_candidate_publication(
    identity: dict[str, Any], projection: dict[str, Any]
) -> dict[str, Any]:
    """Read and reconcile the exact published Candidate without mutating it."""
    worktree = Path(_field(projection, "worktree_path"))
    branch = _field(projection, "branch")
    candidate_sha = _field(projection, "candidate_sha")
    pr_number = projection.get("pr_number")
    pr_url = projection.get("pr_url")
    if pr_url is not None and (not isinstance(pr_url, str) or not pr_url):
        raise CandidateError("stable Candidate PR URL is invalid", kind="conflict")
    if type(pr_number) is not int or pr_number <= 0:
        raise CandidateError("stable Candidate PR number is invalid", kind="conflict")
    prs = _list_prs(worktree, identity["repository"], branch)
    if len(prs) != 1:
        raise CandidateError(
            "Candidate branch does not have exactly one stable PR", kind="conflict"
        )
    _verify_published(
        identity,
        worktree,
        branch,
        candidate_sha,
        prs[0],
        expected_pr_number=pr_number,
        expected_pr_url=pr_url,
    )
    return prs[0]


def mark_candidate_pr_ready(store: RunStore, run_id: str) -> dict[str, Any]:
    identity = store.identity(run_id)
    projection = store.status(run_id)
    worktree = Path(_field(projection, "worktree_path"))
    branch = _field(projection, "branch")
    candidate_sha = _field(projection, "candidate_sha")
    pr_number = projection.get("pr_number")
    pr_url = _field(projection, "pr_url")
    if type(pr_number) is not int or pr_number <= 0:
        raise CandidateError("stable Candidate PR number is invalid", kind="conflict")
    prs = _list_prs(worktree, identity["repository"], branch)
    if len(prs) != 1:
        raise CandidateError(
            "Candidate branch does not have exactly one stable PR", kind="conflict"
        )
    pr = prs[0]
    _verify_published(
        identity,
        worktree,
        branch,
        candidate_sha,
        pr,
        expected_pr_number=pr_number,
        expected_pr_url=pr_url,
        expected_draft=None,
    )
    if not pr["isDraft"]:
        try:
            store.effect(run_id, "pr-mark-ready")
        except RunStoreError as exc:
            raise CandidateError(
                "Candidate PR was marked ready without AFK authorization",
                kind="conflict",
            ) from exc
    observed = _ready_pr_observation(pr, candidate_sha)
    effect = store.prepare_effect(
        run_id,
        "pr-mark-ready",
        kind="pr-mark-ready",
        intended={
            "repository": identity["repository"],
            "number": pr_number,
            "url": pr.get("url"),
            "candidate_sha": candidate_sha,
            "head": branch,
            "base": identity["base_branch"],
            "base_sha": identity["base_sha"],
            "draft": False,
        },
    )
    if effect["status"] == "confirmed":
        _verify_published(
            identity,
            worktree,
            branch,
            candidate_sha,
            pr,
            expected_pr_number=pr_number,
            expected_pr_url=pr_url,
            expected_draft=False,
        )
        if effect.get("observed") != observed:
            raise CandidateError(
                "confirmed ready PR contradicts GitHub", kind="conflict"
            )
        return observed
    if pr["isDraft"]:
        completed = _run(
            [
                "gh",
                "pr",
                "ready",
                str(pr_number),
                "--repo",
                identity["repository"],
            ],
            cwd=worktree,
        )
        if completed.returncode != 0:
            raise CandidateError("Candidate PR could not be marked ready")
        prs = _list_prs(worktree, identity["repository"], branch)
        if len(prs) != 1:
            raise CandidateError(
                "Candidate branch does not have exactly one stable PR",
                kind="conflict",
            )
        pr = prs[0]
        observed = _ready_pr_observation(pr, candidate_sha)
    _verify_published(
        identity,
        worktree,
        branch,
        candidate_sha,
        pr,
        expected_pr_number=pr_number,
        expected_pr_url=pr_url,
        expected_draft=False,
    )
    store.confirm_effect(run_id, "pr-mark-ready", observed=observed)
    return observed


def _ready_pr_observation(pr: dict[str, Any], candidate_sha: str) -> dict[str, Any]:
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "candidate_sha": candidate_sha,
        "head": pr.get("headRefName"),
        "base": pr.get("baseRefName"),
        "draft": pr.get("isDraft"),
    }


def merge_candidate_pr(store: RunStore, run_id: str) -> dict[str, Any]:
    identity = store.identity(run_id)
    projection = store.status(run_id)
    worktree = Path(_field(projection, "worktree_path"))
    branch = _field(projection, "branch")
    candidate_sha = _field(projection, "candidate_sha")
    pr_number = projection.get("pr_number")
    pr_url = _field(projection, "pr_url")
    if type(pr_number) is not int or pr_number <= 0:
        raise CandidateError("stable Candidate PR number is invalid", kind="conflict")
    merge_intended, delete_intended = _candidate_merge_intended(
        identity,
        pr_number,
        pr_url,
        branch,
        candidate_sha,
    )
    pr = _view_pr(worktree, identity["repository"], pr_number)
    merge_effect = store.effect_if_present(run_id, "pr-squash-merge")
    delete_effect = store.effect_if_present(run_id, "remote-branch-delete")
    if pr.get("state") == "MERGED":
        _require_effect_identity(merge_effect, "pr-squash-merge", merge_intended)
        _require_effect_identity(delete_effect, "remote-branch-delete", delete_intended)
        return _reconcile_candidate_merge(
            store,
            run_id,
            identity,
            projection,
            pr,
            merge_effect,
        )
    _require_open_effect(merge_effect, "pr-squash-merge", merge_intended)
    _require_open_effect(delete_effect, "remote-branch-delete", delete_intended)
    _verify_published(
        identity,
        worktree,
        branch,
        candidate_sha,
        pr,
        expected_pr_number=pr_number,
        expected_pr_url=pr_url,
        expected_draft=False,
    )
    if projection.get("pr_ready") != _ready_pr_observation(pr, candidate_sha):
        raise CandidateError(
            "ready PR facts contradict the reviewed Run", kind="conflict"
        )
    store.prepare_effect(
        run_id,
        "pr-squash-merge",
        kind="pr-squash-merge",
        intended=merge_intended,
    )
    store.prepare_effect(
        run_id,
        "remote-branch-delete",
        kind="remote-branch-delete",
        intended=delete_intended,
    )
    pr = _view_pr(worktree, identity["repository"], pr_number)
    _verify_published(
        identity,
        worktree,
        branch,
        candidate_sha,
        pr,
        expected_pr_number=pr_number,
        expected_pr_url=pr_url,
        expected_draft=False,
    )
    if (
        projection.get("pr_ready") != _ready_pr_observation(pr, candidate_sha)
        or _view_pr(worktree, identity["repository"], pr_number) != pr
    ):
        raise CandidateError(
            "ready PR changed during final merge checks", kind="conflict"
        )
    completed = _run(
        [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            identity["repository"],
            "--squash",
            "--delete-branch",
            "--match-head-commit",
            candidate_sha,
        ],
        cwd=worktree,
    )
    if completed.returncode != 0:
        raise CandidateError("Candidate PR squash merge failed")
    pr = _view_pr(worktree, identity["repository"], pr_number)
    return _reconcile_candidate_merge(
        store,
        run_id,
        identity,
        projection,
        pr,
        store.effect(run_id, "pr-squash-merge"),
    )


def reconcile_candidate_branch_deletion(store: RunStore, run_id: str) -> bool:
    identity = store.identity(run_id)
    projection = store.status(run_id)
    worktree = Path(_field(projection, "worktree_path"))
    branch = _field(projection, "branch")
    candidate_sha = _field(projection, "candidate_sha")
    pr_number = projection.get("pr_number")
    pr_url = _field(projection, "pr_url")
    if type(pr_number) is not int or pr_number <= 0:
        raise CandidateError("stable Candidate PR number is invalid", kind="conflict")
    _, delete_intended = _candidate_merge_intended(
        identity,
        pr_number,
        pr_url,
        branch,
        candidate_sha,
    )
    delete_effect = store.effect_if_present(run_id, "remote-branch-delete")
    _require_effect_identity(delete_effect, "remote-branch-delete", delete_intended)
    remote_sha = _remote_sha(worktree, branch)
    if remote_sha not in {"", candidate_sha}:
        raise CandidateError(
            "remote Candidate branch was replaced after merge", kind="conflict"
        )
    deleted = remote_sha == ""
    delete_observed = {
        "repository": identity["repository"],
        "branch": branch,
        "deleted": True,
    }
    if delete_effect["status"] == "confirmed":
        _require_effect_observation(delete_effect, delete_observed)
        if not deleted:
            raise CandidateError(
                "confirmed branch deletion contradicts the remote", kind="conflict"
            )
    if deleted:
        store.confirm_effect(
            run_id,
            "remote-branch-delete",
            observed=delete_observed,
        )
    return deleted


def _candidate_merge_intended(
    identity: dict[str, Any],
    pr_number: int,
    pr_url: str,
    branch: str,
    candidate_sha: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "repository": identity["repository"],
            "number": pr_number,
            "url": pr_url,
            "candidate_sha": candidate_sha,
            "head": branch,
            "base": identity["base_branch"],
            "base_sha": identity["base_sha"],
            "strategy": "squash",
        },
        {
            "repository": identity["repository"],
            "branch": branch,
            "candidate_sha": candidate_sha,
        },
    )


def _require_effect_identity(
    effect: dict[str, Any] | None,
    kind: str,
    intended: dict[str, Any],
) -> None:
    if (
        effect is None
        or effect.get("kind") != kind
        or effect.get("intended") != intended
    ):
        raise CandidateError(
            "Candidate PR merge Effect authorization disagrees with the Run",
            kind="conflict",
        )


def _require_open_effect(
    effect: dict[str, Any] | None,
    kind: str,
    intended: dict[str, Any],
) -> None:
    if effect is None:
        return
    _require_effect_identity(effect, kind, intended)
    if effect.get("status") != "prepared" or "observed" in effect:
        raise CandidateError(
            "OPEN Candidate PR contradicts its merge Effect authorization",
            kind="conflict",
        )


def _reconcile_candidate_merge(
    store: RunStore,
    run_id: str,
    identity: dict[str, Any],
    projection: dict[str, Any],
    pr: dict[str, Any],
    merge_effect: dict[str, Any],
) -> dict[str, Any]:
    merge = pr.get("mergeCommit")
    merge_commit = merge.get("oid") if isinstance(merge, dict) else None
    if (
        pr.get("number") != projection.get("pr_number")
        or pr.get("url") != projection.get("pr_url")
        or pr.get("state") != "MERGED"
        or pr.get("isDraft") is not False
        or pr.get("headRefOid") != projection.get("candidate_sha")
        or pr.get("headRefName") != projection.get("branch")
        or pr.get("baseRefName") != identity["base_branch"]
        or not isinstance(merge_commit, str)
        or len(merge_commit) != 40
        or any(character not in "0123456789abcdef" for character in merge_commit)
    ):
        raise CandidateError(
            "merged PR facts disagree with the reviewed Candidate", kind="conflict"
        )
    observed = {
        "number": pr["number"],
        "url": pr["url"],
        "candidate_sha": pr["headRefOid"],
        "head": pr["headRefName"],
        "base": pr["baseRefName"],
        "merge_commit": merge_commit,
    }
    _require_effect_observation(merge_effect, observed)
    store.confirm_effect(run_id, "pr-squash-merge", observed=observed)
    return observed


def _require_effect_observation(
    effect: dict[str, Any], observed: dict[str, Any]
) -> None:
    if effect.get("status") == "confirmed" and effect.get("observed") != observed:
        raise CandidateError(
            "confirmed merge Effect observation disagrees with GitHub",
            kind="conflict",
        )


def _view_pr(worktree: Path, repository: str, pr_number: int) -> dict[str, Any]:
    completed = _run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repository,
            "--json",
            (
                "number,url,state,isDraft,headRefOid,headRefName,"
                "baseRefName,mergeCommit"
            ),
        ],
        cwd=worktree,
    )
    if completed.returncode != 0:
        raise CandidateError("Candidate PR observation failed")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CandidateError("Candidate PR observation was malformed") from exc
    if not isinstance(value, dict):
        raise CandidateError("Candidate PR observation was malformed")
    return value


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


def _run_codex(
    command: list[str], *, cwd: Path, input_text: str
) -> subprocess.CompletedProcess[str]:
    try:
        return run_supervised_command(
            command,
            cwd=cwd,
            environment=codex_environment(),
            input_text=input_text,
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
            label="repair agent",
        )
    except CandidateValidationError as exc:
        raise CandidateError(
            exc.summary,
            kind=exc.kind,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        ) from exc
    except OSError as exc:
        raise CandidateError("Codex command is unavailable") from exc


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
