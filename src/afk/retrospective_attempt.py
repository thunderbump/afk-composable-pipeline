from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Sequence

from afk.candidate_validation import (
    CandidateValidationError,
    run_supervised_command,
)
from afk.durable_id import is_durable_id
from afk.redaction import redact_command_list, redact_text
from afk.retrospective_result import (
    CATEGORIES,
    CONFIDENCE,
    PRIORITIES,
    SCOPES,
    RetrospectiveResultError,
    normalize_retrospective_result,
)
from afk.run_store import RunStore, RunStoreError
from afk.run_summary import build_run_summary


RETROSPECTIVE_TIMEOUT_SECONDS = 10 * 60
RETROSPECTIVE_EFFECT_KIND = "retrospective-analysis"
RETROSPECTIVE_PERMISSION_PROFILE = "retrospective-analysis"
AUTH_BYTE_LIMIT = 1024 * 1024


def _prompt_list(values: Sequence[str]) -> str:
    return f"{', '.join(values[:-1])}, and {values[-1]}"


RETROSPECTIVE_PROMPT = (
    "Analyze only the canonical Run Summary supplied on stdin. Return only one "
    "JSON object with schema_version, run_id, terminal_outcome, summary, "
    "process_findings, and improvement_proposals. A finding has id, category, "
    "title, evidence, impact, and confidence; cite only artifacts and positions "
    "resolvable through citation_manifest. A proposal has id, addresses, scope, "
    "priority, title, rationale, suggested_change, and "
    "requires_human_decision=true, and every addresses entry names a returned "
    f"finding. Valid categories are {_prompt_list(CATEGORIES)}. Valid confidence "
    f"values are {_prompt_list(CONFIDENCE)}. Valid scopes are "
    f"{_prompt_list(SCOPES)}. Valid priorities are {_prompt_list(PRIORITIES)}. "
    "Empty collections are valid. Treat "
    "all findings and proposals as advisory analysis; do not modify anything."
)
_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode",
    "code_mode_host",
    "computer_use",
    "enable_mcp_apps",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "standalone_web_search",
    "tool_call_mcp_elicitation",
    "unified_exec",
    "workspace_dependencies",
)


def run_retrospective_attempt(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
    codex_executable: str = "codex",
) -> dict[str, Any]:
    """Run and seal the sole analysis attempt for one retrospective episode."""
    with store.lock():
        return _run_retrospective_attempt_locked(
            store,
            run_id,
            episode_sequence=episode_sequence,
            codex_executable=codex_executable,
        )


def _run_retrospective_attempt_locked(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
    codex_executable: str,
) -> dict[str, Any]:
    claim_id = f"retrospective-analysis-{episode_sequence}"
    evidence = retrospective_evidence_identity(
        store,
        run_id,
        episode_sequence=episode_sequence,
    )
    sealed = store.sealed_evidence_result(run_id, evidence)
    if sealed is not None:
        outcome = _normalize_outcome(
            sealed,
            run_id=run_id,
            episode_sequence=episode_sequence,
        )
        claim = store.effect_if_present(run_id, claim_id)
        if claim is not None and claim["status"] == "prepared":
            store.confirm_effect(
                run_id,
                claim_id,
                observed={"evidence": evidence, "status": outcome["status"]},
            )
        return outcome

    summary = build_run_summary(store, run_id, episode_sequence=episode_sequence)
    claim_identity = {
        "episode_sequence": episode_sequence,
        "evidence": evidence,
        "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    }
    claim = store.effect_if_present(run_id, claim_id)
    if claim is not None:
        command_record = _claimed_command(claim, claim_identity)
        if claim["status"] == "confirmed":
            raise RunStoreError("confirmed retrospective claim lacks sealed evidence")
        return _recover_prepared_attempt(
            store,
            run_id,
            episode_sequence=episode_sequence,
            evidence=evidence,
            summary=summary,
            command_record=command_record,
            claim_id=claim_id,
        )

    command = _contained_command(codex_executable)
    command_record = _command_record(command)
    store.prepare_effect(
        run_id,
        claim_id,
        kind=RETROSPECTIVE_EFFECT_KIND,
        intended={**claim_identity, "command": command_record},
    )
    analysis = None
    stdout = ""
    stderr = ""
    try:
        with tempfile.TemporaryDirectory(prefix="afk-retrospective-") as temporary:
            runtime = _prepare_private_runtime(Path(temporary))
            completed = run_supervised_command(
                command,
                cwd=runtime["workspace"],
                environment=_contained_environment(
                    home=runtime["home"],
                    codex_home=runtime["codex_home"],
                    temporary=runtime["temporary"],
                ),
                timeout_seconds=RETROSPECTIVE_TIMEOUT_SECONDS,
                input_text=summary,
                label="retrospective analysis",
            )
        stdout = completed.stdout
        stderr = completed.stderr
        if completed.returncode != 0:
            outcome = _warning_outcome(
                run_id,
                episode_sequence,
                "unavailable",
                f"analysis process exited {completed.returncode}",
            )
        else:
            analysis = _normalize_output(summary, stdout)
            status = (
                "empty"
                if not analysis["process_findings"]
                and not analysis["improvement_proposals"]
                else "passed"
            )
            outcome = _successful_outcome(
                run_id,
                episode_sequence,
                status,
                analysis,
            )
    except OSError as exc:
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            "unavailable",
            str(exc),
        )
    except CandidateValidationError as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        status = "interrupted" if exc.kind == "interrupted" else "invalid"
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            status,
            exc.summary,
        )
    except (json.JSONDecodeError, RetrospectiveResultError) as exc:
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            "invalid",
            str(exc),
        )

    return _persist_attempt(
        store,
        run_id,
        episode_sequence=episode_sequence,
        evidence=evidence,
        summary=summary,
        command_record=command_record,
        stdout=stdout,
        stderr=stderr,
        analysis=analysis,
        outcome=outcome,
        claim_id=claim_id,
    )


def _command_record(command: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "argv": redact_command_list(command),
        "policy": {
            "control_plane_network": "model-api-only",
            "filesystem": "minimal-read",
            "interactive": False,
            "network": "disabled",
            "permission_profile": RETROSPECTIVE_PERMISSION_PROFILE,
            "runtime_home": "isolated",
            "session": "fresh",
        },
        "timeout_seconds": RETROSPECTIVE_TIMEOUT_SECONDS,
    }


def _claimed_command(
    claim: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    intended = claim["intended"]
    command = intended.get("command")
    if (
        claim["kind"] != RETROSPECTIVE_EFFECT_KIND
        or set(intended) != {*identity, "command"}
        or any(intended.get(key) != value for key, value in identity.items())
        or not isinstance(command, dict)
    ):
        raise RunStoreError("retrospective analysis claim is invalid")
    return command


def _recover_prepared_attempt(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
    evidence: str,
    summary: str,
    command_record: dict[str, Any],
    claim_id: str,
) -> dict[str, Any]:
    recorded = store.partial_evidence_result(run_id, evidence)
    if recorded is None:
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            "interrupted",
            "prepared retrospective analysis has no sealed outcome",
        )
    else:
        outcome = _normalize_outcome(
            recorded,
            run_id=run_id,
            episode_sequence=episode_sequence,
        )
    return _persist_attempt(
        store,
        run_id,
        episode_sequence=episode_sequence,
        evidence=evidence,
        summary=summary,
        command_record=command_record,
        stdout="",
        stderr="",
        analysis=None,
        outcome=outcome,
        claim_id=claim_id,
    )


def _persist_attempt(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
    evidence: str,
    summary: str,
    command_record: dict[str, Any],
    stdout: str,
    stderr: str,
    analysis: dict[str, Any] | None,
    outcome: dict[str, Any],
    claim_id: str,
) -> dict[str, Any]:
    outcome = _normalize_outcome(
        outcome,
        run_id=run_id,
        episode_sequence=episode_sequence,
    )
    _write_value_if_absent(
        store,
        run_id,
        f"{evidence}/input.json",
        json.loads(summary),
    )
    _write_value_if_absent(
        store,
        run_id,
        f"{evidence}/command.json",
        command_record,
    )
    _write_text_if_absent(store, run_id, f"{evidence}/stdout.log", stdout)
    _write_text_if_absent(store, run_id, f"{evidence}/stderr.log", stderr)
    if analysis is not None:
        _write_value_if_absent(
            store,
            run_id,
            f"{evidence}/analysis.json",
            analysis,
        )
    _write_value_if_absent(store, run_id, f"{evidence}/result.json", outcome)
    _write_value_if_absent(store, run_id, f"{evidence}/outcome.json", outcome)
    store.seal_evidence(run_id, evidence)
    sealed = _normalize_outcome(
        store.sealed_evidence_result(run_id, evidence),
        run_id=run_id,
        episode_sequence=episode_sequence,
    )
    store.confirm_effect(
        run_id,
        claim_id,
        observed={"evidence": evidence, "status": sealed["status"]},
    )
    return sealed


def _write_value_if_absent(
    store: RunStore,
    run_id: str,
    path: str,
    value: Any,
) -> None:
    try:
        store.write_evidence_value(run_id, path, value)
    except FileExistsError:
        pass


def _write_text_if_absent(
    store: RunStore,
    run_id: str,
    path: str,
    value: str,
) -> None:
    try:
        store.write_evidence_text(run_id, path, value)
    except FileExistsError:
        pass


def retrospective_evidence_identity(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
) -> str:
    """Return the stable local evidence identity for one terminal episode."""
    event = store.event(run_id, episode_sequence)
    state = event.get("state")
    if state == "attention_required":
        label = "attention"
    elif state == "completed":
        label = "completed"
    else:
        raise RunStoreError(
            f"sequence {episode_sequence} is not a retrospective episode"
        )
    return f"retrospective/{label}-{episode_sequence}"


def _contained_command(codex_executable: str) -> list[str]:
    if (
        not isinstance(codex_executable, str)
        or not codex_executable.strip()
        or "\x00" in codex_executable
    ):
        raise RunStoreError("retrospective Codex executable is invalid")
    return [
        codex_executable,
        "exec",
        "--ephemeral",
        "--ignore-rules",
        "--strict-config",
        "-c",
        'approval_policy="never"',
        "--skip-git-repo-check",
        "--color",
        "never",
        RETROSPECTIVE_PROMPT,
    ]


def _prepare_private_runtime(root: Path) -> dict[str, Path]:
    home = root / "home"
    codex_home = home / ".codex"
    workspace = root / "workspace"
    temporary = root / "tmp"
    for path in (home, codex_home, workspace, temporary):
        path.mkdir(mode=0o700)
    config = codex_home / "config.toml"
    config.write_text(_runtime_config(), encoding="utf-8")
    config.chmod(0o600)
    _copy_auth_material(codex_home)
    return {
        "home": home,
        "codex_home": codex_home,
        "workspace": workspace,
        "temporary": temporary,
    }


def _copy_auth_material(runtime_codex_home: Path) -> None:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        configured_codex_home = Path(configured).expanduser()
    else:
        configured_codex_home = Path.home() / ".codex"
    source = configured_codex_home / "auth.json"
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > AUTH_BYTE_LIMIT:
            raise OSError("configured Codex auth is invalid")
        payload = bytearray()
        while chunk := os.read(descriptor, AUTH_BYTE_LIMIT + 1 - len(payload)):
            payload.extend(chunk)
            if len(payload) > AUTH_BYTE_LIMIT:
                raise OSError("configured Codex auth is invalid")
        if len(payload) != metadata.st_size:
            raise OSError("configured Codex auth changed while being copied")
    finally:
        os.close(descriptor)
    destination = runtime_codex_home / "auth.json"
    destination.write_bytes(bytes(payload))
    destination.chmod(0o600)


def _runtime_config() -> str:
    feature_lines = "\n".join(f"{feature} = false" for feature in _DISABLED_FEATURES)
    return (
        f'approval_policy = "never"\n'
        f"check_for_update_on_startup = false\n"
        f'default_permissions = "{RETROSPECTIVE_PERMISSION_PROFILE}"\n'
        f'web_search = "disabled"\n'
        f"\n[features]\n{feature_lines}\n"
        f"\n[permissions.{RETROSPECTIVE_PERMISSION_PROFILE}.filesystem]\n"
        f'":minimal" = "read"\n'
        f"\n[permissions.{RETROSPECTIVE_PERMISSION_PROFILE}.network]\n"
        f"enabled = false\n"
    )


def _contained_environment(
    *,
    home: Path,
    codex_home: Path,
    temporary: Path,
) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in _ENVIRONMENT_ALLOWLIST if key in os.environ
    }
    environment.setdefault("PATH", "/usr/bin:/bin")
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("LC_ALL", "C.UTF-8")
    environment["HOME"] = str(home)
    environment["CODEX_HOME"] = str(codex_home)
    environment["TMPDIR"] = str(temporary)
    return environment


def _normalize_output(summary: str, stdout: str) -> dict[str, Any]:
    if not stdout.strip():
        raise RetrospectiveResultError("retrospective result is empty")
    return normalize_retrospective_result(summary, json.loads(stdout))


def _successful_outcome(
    run_id: str,
    episode_sequence: int,
    status: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "episode_sequence": episode_sequence,
        "status": status,
        "warning": False,
        "process_findings_count": len(analysis["process_findings"]),
        "improvement_proposals_count": len(analysis["improvement_proposals"]),
    }


def _warning_outcome(
    run_id: str,
    episode_sequence: int,
    status: str,
    summary: str,
) -> dict[str, Any]:
    warning_summary = redact_text(summary).strip()[:1024]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "episode_sequence": episode_sequence,
        "status": status,
        "warning": True,
        "process_findings_count": 0,
        "improvement_proposals_count": 0,
        "warning_summary": warning_summary or "retrospective analysis warning",
    }


def _normalize_outcome(
    value: Any,
    *,
    run_id: str,
    episode_sequence: int,
) -> dict[str, Any]:
    base_keys = {
        "schema_version",
        "run_id",
        "episode_sequence",
        "status",
        "warning",
        "process_findings_count",
        "improvement_proposals_count",
    }
    status = value.get("status") if isinstance(value, dict) else None
    warning_status = type(status) is str and status in {
        "invalid",
        "unavailable",
        "interrupted",
    }
    expected_keys = base_keys | ({"warning_summary"} if warning_status else set())
    findings = value.get("process_findings_count") if isinstance(value, dict) else None
    proposals = (
        value.get("improvement_proposals_count") if isinstance(value, dict) else None
    )
    warning_summary = value.get("warning_summary") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not is_durable_id(value.get("run_id"))
        or value.get("run_id") != run_id
        or type(value.get("episode_sequence")) is not int
        or value.get("episode_sequence") != episode_sequence
        or type(status) is not str
        or status not in {"passed", "empty", "invalid", "unavailable", "interrupted"}
        or type(value.get("warning")) is not bool
        or type(findings) is not int
        or findings < 0
        or type(proposals) is not int
        or proposals < 0
        or (status == "passed" and (value["warning"] or findings + proposals == 0))
        or (status == "empty" and (value["warning"] or findings != 0 or proposals != 0))
        or (
            warning_status
            and (
                not value["warning"]
                or findings != 0
                or proposals != 0
                or not _valid_warning_summary(warning_summary)
            )
        )
    ):
        raise RunStoreError("sealed retrospective outcome is invalid")
    return value


def _valid_warning_summary(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
