from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from afk.jsonutil import canonical_json
from afk.run_store import RunStore


REVIEW_AXES = ("standards", "spec")
REVIEW_REPORT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "summary", "findings"],
    "properties": {
        "status": {"enum": ["passed", "rejected", "inconclusive"]},
        "summary": {"type": "string", "minLength": 1},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "priority",
                    "title",
                    "body",
                    "path",
                    "line",
                    "blocking",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "priority": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "body": {"type": "string", "minLength": 1},
                    "path": {"type": "string"},
                    "line": {"type": ["integer", "null"], "minimum": 1},
                    "blocking": {"type": "boolean"},
                },
            },
        },
    },
}


class GateError(RuntimeError):
    def __init__(self, summary: str, *, kind: str = "invalid"):
        super().__init__(summary)
        self.summary = summary
        self.kind = kind


def complete_gate_cycle(
    store: RunStore,
    run_id: str,
    *,
    bead: dict[str, Any],
) -> dict[str, Any]:
    projection = store.status(run_id)
    candidate_sha = _required_text(projection, "candidate_sha")
    validation_record = projection.get("validation")
    if (
        not isinstance(validation_record, dict)
        or validation_record.get("candidate_sha") != candidate_sha
    ):
        raise GateError("Gate Cycle validation does not match the current Candidate")
    if validation_record.get("status") not in {"passed", "rejected"}:
        raise GateError("Gate Cycle requires a conclusive validation result")
    validation_evidence = _required_text(validation_record, "evidence")
    if not store.verify_evidence(run_id, validation_evidence):
        raise GateError("validation evidence could not be verified")
    validation = _validation_snapshot(
        store, run_id, validation_record, candidate_sha=candidate_sha
    )
    used = projection.get("repair_attempts_used", 0)
    if type(used) is not int or not 0 <= used <= 4:
        raise GateError("repair budget state is invalid")
    cycle = used + 1
    evidence = f"gates/gate-cycle-{cycle}-{candidate_sha[:12]}"
    evidence_path = store.root / "runs" / run_id / evidence

    if (evidence_path / "manifest.json").exists():
        if not store.verify_evidence(run_id, evidence):
            raise GateError("Gate Cycle evidence could not be verified")
        outcome = _read_json(evidence_path / "outcome.json")
    else:
        if evidence_path.exists():
            entries = {path.name for path in evidence_path.iterdir()}
            expected = (
                (
                    {"review-bundle"},
                    {"review-bundle", "outcome.json"},
                )
                if validation["status"] == "passed"
                else ({"outcome.json"},)
            )
            if entries not in expected:
                raise GateError(
                    "unsealed Gate Cycle evidence is ambiguous", kind="inconclusive"
                )
        reviews = (
            run_candidate_reviews(store, run_id, cycle=cycle, bead=bead)
            if validation["status"] == "passed"
            else []
        )
        review_statuses = {review["status"] for review in reviews}
        if "inconclusive" in review_statuses:
            next_action = "attention"
        elif validation["status"] == "rejected" or "rejected" in review_statuses:
            next_action = "repair" if used < 4 else "attention"
        else:
            next_action = "complete"
        outcome = {
            "schema_version": 1,
            "cycle": cycle,
            "candidate_sha": candidate_sha,
            "validation": validation,
            "reviews": reviews,
            "prior_dispositions": projection.get("repair_dispositions", []),
            "next_action": next_action,
            "evidence": evidence,
        }
        if next_action == "repair":
            outcome["repair_brief"] = build_repair_brief(
                candidate_sha=candidate_sha,
                cycle=used + 1,
                validation=validation,
                reviews=reviews,
            )
        elif (
            next_action == "attention"
            and used >= 4
            and (validation["status"] == "rejected" or "rejected" in review_statuses)
        ):
            outcome["stop_reason"] = "repair budget exhausted after four attempts"
        outcome_path = evidence_path / "outcome.json"
        if outcome_path.exists():
            if _read_json(outcome_path) != outcome:
                raise GateError(
                    "unsealed Gate Cycle outcome is ambiguous", kind="inconclusive"
                )
        else:
            store.write_evidence_text(
                run_id,
                f"{evidence}/outcome.json",
                canonical_json(outcome) + "\n",
            )
        store.seal_evidence(run_id, evidence)

    pr_number = projection.get("pr_number")
    if type(pr_number) is not int or pr_number <= 0:
        raise GateError(
            "Gate Cycle requires the stable draft PR number", kind="inconclusive"
        )
    reconcile_gate_comment(
        store,
        run_id,
        pr_number=pr_number,
        worktree=Path(_required_text(projection, "worktree_path")),
        gate=outcome,
    )
    cycles = projection.get("gate_cycles", [])
    if not isinstance(cycles, list):
        raise GateError("Gate Cycle history is invalid")
    if not any(
        isinstance(item, dict)
        and item.get("cycle") == cycle
        and item.get("candidate_sha") == candidate_sha
        for item in cycles
    ):
        store.append_event(
            run_id,
            "gate.cycle_completed",
            state=(
                "reviewed"
                if outcome["next_action"] == "complete"
                else "candidate_ready"
            ),
            data={
                "checkpoint": (
                    "reviewed"
                    if outcome["next_action"] == "complete"
                    else "candidate_ready"
                ),
                "gate_cycles": [*cycles, outcome],
                "attention": {},
            },
        )
    return outcome


def reconcile_gate_comment(
    store: RunStore,
    run_id: str,
    *,
    pr_number: int,
    worktree: Path,
    gate: dict[str, Any],
) -> None:
    identity = store.identity(run_id)
    cycle = gate.get("cycle")
    if type(cycle) is not int or cycle <= 0:
        raise GateError("Gate Cycle number is invalid")
    marker = f"<!-- afk-gate:{run_id}:{cycle} -->"
    body = _gate_comment_body(gate, marker)
    effect_id = f"gate-comment-{cycle}"
    effect = store.prepare_effect(
        run_id,
        effect_id,
        kind="gate-comment",
        intended={
            "repository": identity["repository"],
            "pr_number": pr_number,
            "cycle": cycle,
            "candidate_sha": gate.get("candidate_sha"),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        },
    )
    matching = [
        comment
        for comment in _github_comments(identity["repository"], pr_number, worktree)
        if marker in str(comment.get("body", ""))
    ]
    if len(matching) > 1:
        raise GateError("Gate Cycle has duplicate PR evidence comments")
    if matching:
        if matching[0].get("body") != body:
            raise GateError(
                "Gate Cycle PR evidence comment content does not match",
                kind="inconclusive",
            )
        url = matching[0].get("html_url") or matching[0].get("url")
    else:
        url = _post_gate_comment(identity["repository"], pr_number, body, worktree)
    if not isinstance(url, str) or not url:
        raise GateError("Gate Cycle PR evidence comment has no URL")
    observed = {"url": url, "marker": marker}
    if effect["status"] == "confirmed" and effect.get("observed") != observed:
        raise GateError("confirmed Gate Cycle comment contradicts GitHub")
    store.confirm_effect(run_id, effect_id, observed=observed)


def run_candidate_reviews(
    store: RunStore,
    run_id: str,
    *,
    cycle: int,
    bead: dict[str, Any],
) -> list[dict[str, Any]]:
    projection = store.status(run_id)
    identity = store.identity(run_id)
    candidate_sha = _required_text(projection, "candidate_sha")
    worktree = Path(_required_text(projection, "worktree_path"))
    validation_record = projection.get("validation")
    if (
        not isinstance(validation_record, dict)
        or validation_record.get("status") != "passed"
    ):
        raise GateError("Candidate reviews require passed validation")
    if validation_record.get("candidate_sha") != candidate_sha:
        raise GateError("validation evidence belongs to another Candidate")
    validation = _validation_snapshot(
        store, run_id, validation_record, candidate_sha=candidate_sha
    )

    bundle = f"gates/gate-cycle-{cycle}-{candidate_sha[:12]}/review-bundle"
    bundle_path = store.root / "runs" / run_id / bundle
    if not bundle_path.exists():
        validation_evidence = _required_text(validation, "evidence")
        if not store.verify_evidence(run_id, validation_evidence):
            raise GateError("validation evidence could not be verified")
        store.write_evidence_text(
            run_id,
            f"{bundle}/bundle.json",
            canonical_json(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "base_sha": identity["base_sha"],
                    "candidate_sha": candidate_sha,
                    "repository": identity["repository"],
                    "bead": bead,
                    "validation": validation,
                    "validation_manifest": _read_json(
                        store.root
                        / "runs"
                        / run_id
                        / validation_evidence
                        / "manifest.json"
                    ),
                    "repository_instructions": _repository_instructions(
                        worktree, candidate_sha
                    ),
                    "diff": _git(
                        worktree,
                        "diff",
                        "--no-ext-diff",
                        "--binary",
                        identity["base_sha"],
                        candidate_sha,
                    ),
                    "prior_dispositions": projection.get("repair_dispositions", []),
                    "prior_gate_cycles": projection.get("gate_cycles", []),
                }
            )
            + "\n",
        )
        store.seal_evidence(run_id, bundle)
    elif not store.verify_evidence(run_id, bundle):
        raise GateError("review bundle could not be verified")

    reviews = []
    for axis in REVIEW_AXES:
        attempt = f"attempts/review-cycle-{cycle}-{axis}"
        attempt_path = store.root / "runs" / run_id / attempt
        if attempt_path.exists():
            if not (attempt_path / "manifest.json").is_file():
                raise GateError(
                    f"{axis} review attempt is incomplete", kind="inconclusive"
                )
            if not store.verify_evidence(run_id, attempt):
                raise GateError(f"{axis} review evidence could not be verified")
            result_paths = [
                path
                for name in ("report.json", "outcome.json")
                if (path := attempt_path / name).is_file()
            ]
            if len(result_paths) != 1:
                raise GateError(f"{axis} review evidence is ambiguous")
            reviews.append(_stored_review_result(axis, result_paths[0]))
            continue
        prompt = _review_prompt(axis, bundle_path)
        store.write_evidence_text(run_id, f"{attempt}/prompt.md", prompt)
        store.write_evidence_text(
            run_id,
            f"{attempt}/schema.json",
            canonical_json(REVIEW_REPORT_SCHEMA) + "\n",
        )
        try:
            exit_code, payload, stdout, stderr = _execute_reviewer(
                axis, bundle_path, attempt_path, worktree
            )
        except GateError as exc:
            result = {
                "axis": axis,
                "process_status": "failed",
                "status": "inconclusive",
                "summary": exc.summary,
                "findings": [],
            }
            store.write_evidence_text(
                run_id, f"{attempt}/outcome.json", canonical_json(result) + "\n"
            )
            store.seal_evidence(run_id, attempt)
            reviews.append(result)
            continue
        store.write_evidence_text(run_id, f"{attempt}/events.jsonl", stdout)
        store.write_evidence_text(run_id, f"{attempt}/stderr.txt", stderr)
        try:
            result = normalize_review_result(axis, payload, process_exit_code=exit_code)
        except GateError as exc:
            result = {
                "axis": axis,
                "process_status": "failed" if exit_code != 0 else "succeeded",
                "status": "inconclusive",
                "summary": exc.summary,
                "findings": [],
            }
            store.write_evidence_text(
                run_id,
                f"{attempt}/raw-report.txt",
                payload if isinstance(payload, str) else canonical_json(payload),
            )
            store.write_evidence_text(
                run_id,
                f"{attempt}/outcome.json",
                canonical_json(result) + "\n",
            )
            store.seal_evidence(run_id, attempt)
            reviews.append(result)
            continue
        store.write_evidence_text(
            run_id, f"{attempt}/report.json", canonical_json(result) + "\n"
        )
        store.seal_evidence(run_id, attempt)
        reviews.append(result)
    return reviews


def _stored_review_result(axis: str, path: Path) -> dict[str, Any]:
    result = _read_json(path)
    if not isinstance(result, dict) or set(result) != {
        "axis",
        "process_status",
        "status",
        "summary",
        "findings",
    }:
        raise GateError(f"{axis} stored review result is invalid")
    if result["axis"] != axis:
        raise GateError(f"{axis} stored review axis is invalid")
    process_status = result["process_status"]
    if process_status == "failed":
        if (
            result["status"] != "inconclusive"
            or not isinstance(result["summary"], str)
            or not result["summary"].strip()
            or result["findings"] != []
            or path.name != "outcome.json"
        ):
            raise GateError(f"{axis} stored failed review is invalid")
        return result
    if process_status != "succeeded":
        raise GateError(f"{axis} stored review process status is invalid")
    prefix = f"{axis}-"
    findings = result["findings"]
    if not isinstance(findings, list):
        raise GateError(f"{axis} stored review findings are invalid")
    payload_findings = []
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or not isinstance(finding.get("id"), str)
            or not finding["id"].startswith(prefix)
        ):
            raise GateError(f"{axis} stored review finding is invalid")
        payload_findings.append({**finding, "id": finding["id"][len(prefix) :]})
    normalized = normalize_review_result(
        axis,
        {
            "status": result["status"],
            "summary": result["summary"],
            "findings": payload_findings,
        },
        process_exit_code=0,
    )
    if normalized != result:
        raise GateError(f"{axis} stored review result is invalid")
    if path.name == "outcome.json" and result["status"] != "inconclusive":
        raise GateError(f"{axis} stored review outcome is invalid")
    return result


def normalize_review_result(
    axis: str, payload: Any, *, process_exit_code: int
) -> dict[str, Any]:
    if process_exit_code != 0:
        raise GateError(
            f"{axis} reviewer exited with status {process_exit_code}",
            kind="inconclusive",
        )
    if not isinstance(payload, dict) or set(payload) != {
        "status",
        "summary",
        "findings",
    }:
        raise GateError(
            f"{axis} review output must contain status, summary, and findings"
        )
    status = payload["status"]
    summary = payload["summary"]
    findings = payload["findings"]
    if status not in {"passed", "rejected", "inconclusive"}:
        raise GateError(f"{axis} review status is invalid")
    if not isinstance(summary, str) or not summary.strip():
        raise GateError(f"{axis} review summary is required")
    if not isinstance(findings, list):
        raise GateError(f"{axis} review findings must be a list")
    normalized_findings = [
        _normalize_finding(axis, finding, index)
        for index, finding in enumerate(findings, start=1)
    ]
    finding_ids = [finding["id"] for finding in normalized_findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise GateError(f"{axis} review findings contain duplicate IDs")
    blocking = any(finding["blocking"] for finding in normalized_findings)
    if status == "passed" and blocking:
        raise GateError(f"{axis} passed review contains blocking findings")
    if status == "rejected" and not blocking:
        raise GateError(f"{axis} rejected review has no blocking findings")
    return {
        "axis": axis,
        "process_status": "succeeded",
        "status": status,
        "summary": summary.strip(),
        "findings": normalized_findings,
    }


def _normalize_finding(axis: str, value: Any, index: int) -> dict[str, Any]:
    required = {"id", "priority", "title", "body", "path", "line", "blocking"}
    if not isinstance(value, dict) or set(value) != required:
        raise GateError(f"{axis} review findings[{index - 1}] is invalid")
    if any(
        not isinstance(value[key], str)
        for key in ("id", "priority", "title", "body", "path")
    ):
        raise GateError(f"{axis} review findings[{index - 1}] has invalid text")
    if not all(value[key].strip() for key in ("id", "priority", "title", "body")):
        raise GateError(f"{axis} review findings[{index - 1}] has empty required text")
    if value["line"] is not None and (
        type(value["line"]) is not int or value["line"] <= 0
    ):
        raise GateError(f"{axis} review findings[{index - 1}] has invalid line")
    if type(value["blocking"]) is not bool:
        raise GateError(
            f"{axis} review findings[{index - 1}] has invalid blocking flag"
        )
    return {**value, "id": f"{axis}-{_identifier(value['id'])}"}


def build_repair_brief(
    *,
    candidate_sha: str,
    cycle: int,
    validation: dict[str, Any],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize every blocking Gate Cycle item into one Candidate-bound brief."""
    findings: list[dict[str, Any]] = []
    diagnostic_text = "\n\n".join(
        f"[{item.get('path', 'validation log')}]\n{item.get('content', '')}"
        for item in validation.get("diagnostics", [])
        if isinstance(item, dict)
    )
    for check in validation.get("checks", []):
        if not isinstance(check, dict) or check.get("status") != "rejected":
            continue
        name = str(check.get("name", "validation")).strip() or "validation"
        findings.append(
            {
                "id": f"validation-{_identifier(name)}",
                "source": "validation",
                "priority": "high",
                "title": f"Validation check rejected: {name}",
                "body": "\n\n".join(
                    part
                    for part in (
                        str(validation.get("summary", "Validation rejected.")),
                        diagnostic_text,
                    )
                    if part
                ),
                "path": str(check.get("log_path", "")),
                "line": None,
                "blocking": True,
            }
        )
    if validation.get("status") == "rejected" and not findings:
        findings.append(
            {
                "id": "validation-contract",
                "source": "validation",
                "priority": "high",
                "title": "Validation Contract rejected the Candidate",
                "body": "\n\n".join(
                    part
                    for part in (
                        str(validation.get("summary", "Validation rejected.")),
                        diagnostic_text,
                    )
                    if part
                ),
                "path": str(validation.get("evidence", "")),
                "line": None,
                "blocking": True,
            }
        )
    for review in reviews:
        if not isinstance(review, dict):
            continue
        axis = str(review.get("axis", "review"))
        for finding in review.get("findings", []):
            if not isinstance(finding, dict) or finding.get("blocking") is not True:
                continue
            findings.append({"source": axis, **finding})
    return {
        "schema_version": 1,
        "candidate_sha": candidate_sha,
        "repair_attempt": cycle,
        "blocking_findings": findings,
    }


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "check"


def _execute_reviewer(
    axis: str,
    bundle_path: Path,
    attempt_path: Path,
    worktree: Path,
) -> tuple[int, Any, str, str]:
    prompt = _review_prompt(axis, bundle_path)
    with tempfile.TemporaryDirectory(prefix=f"afk-review-{axis}-") as temporary:
        temporary_path = Path(temporary)
        report_path = temporary_path / "report.json"
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            *_review_permission_args(worktree, bundle_path, temporary_path),
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
            completed = subprocess.run(
                command,
                cwd=worktree,
                env=_codex_environment(),
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=3600,
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"{axis} reviewer timed out", kind="inconclusive") from exc
        except OSError as exc:
            raise GateError(
                f"{axis} reviewer is unavailable", kind="inconclusive"
            ) from exc
        payload: Any = None
        if completed.returncode == 0:
            try:
                raw_payload = report_path.read_text(encoding="utf-8")
                payload = json.loads(raw_payload)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = raw_payload if "raw_payload" in locals() else ""
        return completed.returncode, payload, completed.stdout, completed.stderr


def _review_prompt(axis: str, bundle_path: Path) -> str:
    focus = {
        "standards": (
            "repository instructions, correctness, safety, maintainability, and tests"
        ),
        "spec": "the exact Bead description and acceptance criteria",
    }[axis]
    return f"""# AFK {axis} review

Independently review the immutable bundle at {bundle_path} with focus on {focus}.
This is a read-only review. Do not edit files, run network commands, or rely on
another review session. Report `rejected` only with at least one blocking
finding; advisory findings must set blocking=false. Use stable axis-local IDs.
Return only the schema-constrained result.
"""


def _review_permission_args(
    worktree: Path, bundle_path: Path, temporary: Path
) -> list[str]:
    home = temporary / "home"
    home.mkdir(mode=0o700)
    filesystem = {
        ":minimal": "read",
        str(worktree.resolve()): "read",
        str(bundle_path.resolve()): "read",
        str(temporary.resolve()): "write",
    }
    profile = (
        '{ description = "AFK Candidate review", filesystem = '
        f"{_toml_table(filesystem)}, network = {{ enabled = false }} }}"
    )
    shell_environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "GIT_TERMINAL_PROMPT": "0",
    }
    shell_policy = (
        '{ inherit = "none", ignore_default_excludes = false, set = '
        f"{_toml_table(shell_environment)} }}"
    )
    return [
        "-c",
        'default_permissions="afk_review"',
        "-c",
        f"permissions.afk_review={profile}",
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


def _repository_instructions(
    worktree: Path, candidate_sha: str
) -> list[dict[str, str]]:
    paths = _git(
        worktree,
        "ls-tree",
        "-r",
        "--name-only",
        candidate_sha,
    ).splitlines()
    selected = [
        path
        for path in paths
        if path == "CODING_STANDARDS.md"
        or path.endswith("/AGENTS.md")
        or path == "AGENTS.md"
    ]
    return [
        {
            "path": path,
            "content": _git(worktree, "show", f"{candidate_sha}:{path}"),
        }
        for path in selected
    ]


def _validation_snapshot(
    store: RunStore,
    run_id: str,
    validation: dict[str, Any],
    *,
    candidate_sha: str,
) -> dict[str, Any]:
    snapshot = dict(validation)
    evidence = _required_text(validation, "evidence")
    root = store.root / "runs" / run_id / evidence
    contract_result = root / "contract" / "result.json"
    if contract_result.is_file():
        result = _read_json(contract_result)
        if (
            isinstance(result, dict)
            and result.get("candidate_sha") == candidate_sha
            and result.get("status") == validation.get("status")
            and isinstance(result.get("checks"), list)
        ):
            snapshot["checks"] = result["checks"]
    diagnostics = []
    remaining = 65536
    manifest = _read_json(root / "manifest.json")
    entries = manifest.get("files", []) if isinstance(manifest, dict) else []
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if (
            not isinstance(path, str)
            or path == "manifest.json"
            or not (path.endswith(".log") or path.endswith("result.json"))
            or remaining <= 0
        ):
            continue
        try:
            content = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        content = content[: min(16384, remaining)]
        remaining -= len(content.encode("utf-8"))
        diagnostics.append({"path": path, "content": content})
    snapshot["diagnostics"] = diagnostics
    return snapshot


def _git(worktree: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError("Git evidence inspection failed", kind="inconclusive") from exc
    if completed.returncode != 0:
        raise GateError("Git evidence inspection failed", kind="inconclusive")
    return completed.stdout


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"evidence JSON is malformed: {path.name}") from exc


def _required_text(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise GateError(f"required Gate field is missing: {key}")
    return item


def _codex_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "CODEX_HOME")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _gate_comment_body(gate: dict[str, Any], marker: str) -> str:
    validation = gate.get("validation", {})
    reviews = gate.get("reviews", [])
    lines = [
        marker,
        f"## AFK Gate Cycle {gate['cycle']}",
        "",
        f"Candidate: `{gate.get('candidate_sha', '')}`",
        (
            f"Validation: **{validation.get('status', 'not_run')}** — "
            f"{validation.get('summary', '')}"
        ),
    ]
    for check in validation.get("checks", []):
        if check.get("status") != "passed":
            lines.append(
                f"- `validation-{_identifier(str(check.get('name', 'check')))}` "
                f"({check.get('status', 'unknown')}): {check.get('name', 'check')}"
            )
    for review in reviews:
        lines.append(
            f"{str(review.get('axis', 'review')).title()} review: "
            f"**{review.get('status', 'not_run')}** — {review.get('summary', '')}"
        )
        for finding in review.get("findings", []):
            blocking = "blocking" if finding.get("blocking") else "advisory"
            lines.append(
                f"- `{finding.get('id', '')}` ({blocking}): {finding.get('title', '')}"
            )
    dispositions = gate.get("prior_dispositions", [])
    if dispositions:
        lines.extend(["", "Prior repair dispositions:"])
        for disposition in dispositions:
            lines.append(
                f"- `{disposition.get('finding_id', '')}`: "
                f"{disposition.get('disposition', '')}"
            )
    lines.extend(["", f"Next action: **{gate.get('next_action', 'attention')}**"])
    return "\n".join(lines) + "\n"


def _github_comments(
    repository: str, pr_number: int, worktree: Path
) -> list[dict[str, Any]]:
    owner, name = _repository_parts(repository)
    completed = _run_gh(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/issues/{pr_number}/comments",
            "--paginate",
            "--slurp",
        ],
        worktree,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(
            "GitHub comment observation was malformed", kind="inconclusive"
        ) from exc
    if not isinstance(value, list) or not all(
        isinstance(page, list) and all(isinstance(item, dict) for item in page)
        for page in value
    ):
        raise GateError("GitHub comment observation was malformed", kind="inconclusive")
    return [item for page in value for item in page]


def _post_gate_comment(
    repository: str, pr_number: int, body: str, worktree: Path
) -> str:
    owner, name = _repository_parts(repository)
    completed = _run_gh(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{owner}/{name}/issues/{pr_number}/comments",
            "--input",
            "-",
        ],
        worktree,
        input_text=canonical_json({"body": body}),
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(
            "GitHub comment response was malformed", kind="inconclusive"
        ) from exc
    url = value.get("html_url") if isinstance(value, dict) else None
    if not isinstance(url, str) or not url:
        raise GateError("GitHub comment response was malformed", kind="inconclusive")
    return url


def _run_gh(
    command: list[str], worktree: Path, *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=worktree,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(
            "GitHub comment command is unavailable", kind="inconclusive"
        ) from exc
    if completed.returncode != 0:
        raise GateError("GitHub comment command failed", kind="inconclusive")
    return completed


def _repository_parts(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise GateError("GitHub repository identity is invalid")
    return parts[0], parts[1]
