from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from afk.jsonutil import canonical_json
from afk.process_io import BoundedProcessIO
from afk.redaction import redact_command_list, redact_text
from afk.retrospective_outcome import normalize_retrospective_outcome
from afk.retrospective_result import (
    CATEGORIES,
    CONFIDENCE,
    PRIORITIES,
    SCOPES,
    RetrospectiveResultError,
    normalize_retrospective_result,
)
from afk.run_store import RunStore, RunStoreError
from afk.run_summary import MAX_RUN_SUMMARY_BYTES, build_run_summary


RETROSPECTIVE_TIMEOUT_SECONDS = 10 * 60
RETROSPECTIVE_EFFECT_KIND = "retrospective-analysis"
RETROSPECTIVE_PERMISSION_PROFILE = "retrospective-analysis"
AUTH_BYTE_LIMIT = 1024 * 1024
AUTH_DESCRIPTOR_MINIMUM = 200
RETROSPECTIVE_OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
PROCESS_CLEANUP_SECONDS = 1
ANALYSIS_STATUS_BYTE_LIMIT = 32
_EXEC_GUARD = """
import ctypes
import os
import signal
import subprocess
import sys

expected_parent = int(sys.argv[1])
auth_descriptor = int(sys.argv[2])
status_descriptor = int(sys.argv[3])
command = sys.argv[4:]
libc = ctypes.CDLL(None, use_errno=True)

def terminate_group(signum, frame):
    os.killpg(0, signal.SIGKILL)

def report_status(returncode):
    payload = f"{returncode}\\n".encode("ascii")
    while payload:
        payload = payload[os.write(status_descriptor, payload):]
    os.close(status_descriptor)

signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
signal.signal(signal.SIGTERM, terminate_group)
if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
if os.getppid() != expected_parent:
    terminate_group(signal.SIGTERM, None)
descriptors = () if auth_descriptor < 0 else (auth_descriptor,)
signal.signal(signal.SIGCHLD, signal.SIG_DFL)
try:
    child = subprocess.Popen(command, pass_fds=descriptors)
except OSError:
    report_status(127)
    raise
returncode = child.wait()
report_status(returncode)
if returncode < 0:
    if -returncode != signal.SIGKILL:
        signal.signal(-returncode, signal.SIG_DFL)
    os.kill(os.getpid(), -returncode)
raise SystemExit(returncode)
""".strip()


class RetrospectiveProcessError(RuntimeError):
    def __init__(
        self,
        kind: str,
        summary: str,
        *,
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(summary)
        self.kind = kind
        self.summary = summary
        self.stdout = stdout
        self.stderr = stderr


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
) -> dict[str, Any]:
    """Run and seal the sole analysis attempt for one retrospective episode."""
    with store.lock():
        return _run_retrospective_attempt_locked(
            store,
            run_id,
            episode_sequence=episode_sequence,
        )


def _run_retrospective_attempt_locked(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
) -> dict[str, Any]:
    claim_id = f"retrospective-analysis-{episode_sequence}"
    evidence = retrospective_evidence_identity(
        store,
        run_id,
        episode_sequence=episode_sequence,
    )
    sealed = store.sealed_evidence_result(run_id, evidence)
    if sealed is not None:
        outcome = normalize_retrospective_outcome(
            sealed,
            run_id=run_id,
            episode_sequence=episode_sequence,
        )
        claim = store.effect_if_present(run_id, claim_id)
        if claim is None:
            raise RunStoreError("sealed retrospective evidence lacks a valid claim")
        summary = build_run_summary(store, run_id, episode_sequence=episode_sequence)
        identity = {
            "episode_sequence": episode_sequence,
            "evidence": evidence,
            "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        }
        command_record = _claimed_command(claim, identity)
        _validate_sealed_attempt(
            store,
            run_id,
            episode_sequence=episode_sequence,
            evidence=evidence,
            summary=summary,
            command_record=command_record,
            outcome=outcome,
            claim=claim,
        )
        if claim["status"] == "prepared":
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

    command = _contained_command()
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
            try:
                completed = _run_retrospective_process(
                    command,
                    cwd=runtime["workspace"],
                    environment=_contained_environment(
                        home=runtime["home"],
                        codex_home=runtime["codex_home"],
                        temporary=runtime["temporary"],
                    ),
                    timeout_seconds=RETROSPECTIVE_TIMEOUT_SECONDS,
                    input_text=summary,
                    auth_descriptor=runtime["auth_descriptor"],
                )
            finally:
                if runtime["auth_descriptor"] is not None:
                    os.close(runtime["auth_descriptor"])
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
            outcome = _successful_outcome(
                run_id,
                episode_sequence,
                analysis,
            )
    except OSError as exc:
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            "unavailable",
            str(exc),
        )
    except RetrospectiveProcessError as exc:
        stdout = exc.stdout
        stderr = exc.stderr
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
    _validate_command_record(command)
    return command


def _validate_command_record(command_record: dict[str, Any]) -> None:
    if command_record != _command_record(_contained_command()):
        raise RunStoreError("retrospective analysis command is invalid")


def _validate_sealed_attempt(
    store: RunStore,
    run_id: str,
    *,
    episode_sequence: int,
    evidence: str,
    summary: str,
    command_record: dict[str, Any],
    outcome: dict[str, Any],
    claim: dict[str, Any],
) -> None:
    expected_observed = {"evidence": evidence, "status": outcome["status"]}
    if claim["status"] == "confirmed" and claim.get("observed") != expected_observed:
        raise RunStoreError("sealed retrospective evidence claim is invalid")

    payloads = store.sealed_evidence_payloads(run_id, evidence)
    expected_files = {
        "command.json",
        "input.json",
        "outcome.json",
        "result.json",
        "stderr.log",
        "stdout.log",
    }
    if not outcome["warning"]:
        expected_files.add("analysis.json")
    if set(payloads) != expected_files:
        raise RunStoreError("sealed retrospective evidence files are invalid")

    expected_input = canonical_json(json.loads(summary)) + "\n"
    expected_command_payload = canonical_json(command_record) + "\n"
    expected_outcome = canonical_json(outcome) + "\n"
    if (
        payloads["input.json"] != expected_input
        or payloads["command.json"] != expected_command_payload
        or payloads["result.json"] != expected_outcome
        or payloads["outcome.json"] != expected_outcome
    ):
        raise RunStoreError("sealed retrospective evidence is contradictory")

    if outcome["warning"]:
        return
    try:
        raw_analysis = json.loads(payloads["analysis.json"])
        analysis = normalize_retrospective_result(summary, raw_analysis)
    except (json.JSONDecodeError, RetrospectiveResultError) as exc:
        raise RunStoreError("sealed retrospective analysis is invalid") from exc
    if payloads["analysis.json"] != canonical_json(analysis) + "\n":
        raise RunStoreError("sealed retrospective analysis is invalid")
    expected = _successful_outcome(
        run_id,
        episode_sequence,
        analysis,
    )
    if outcome != expected:
        raise RunStoreError("sealed retrospective analysis contradicts outcome")


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
    files = set(store.partial_evidence_files(run_id, evidence))
    allowed = {
        "analysis.json",
        "command.json",
        "input.json",
        "outcome.json",
        "result.json",
        "stderr.log",
        "stdout.log",
    }
    if files - allowed:
        raise RunStoreError("partial retrospective evidence is invalid")
    predecessors = {
        "command.json",
        "input.json",
        "stderr.log",
        "stdout.log",
    }
    if (
        ("outcome.json" in files and "result.json" not in files)
        or ("result.json" in files and not predecessors.issubset(files))
        or ("analysis.json" in files and not predecessors.issubset(files))
    ):
        raise RunStoreError("partial retrospective evidence order is invalid")

    analysis = None
    if "analysis.json" in files:
        try:
            analysis = normalize_retrospective_result(
                summary,
                store.partial_evidence_value(
                    run_id,
                    f"{evidence}/analysis.json",
                ),
            )
        except (RetrospectiveResultError, ValueError) as exc:
            raise RunStoreError("partial retrospective analysis is invalid") from exc

    if "result.json" not in files:
        recorded = None
    else:
        recorded = store.partial_evidence_result(run_id, evidence)
    if recorded is None and analysis is None:
        outcome = _warning_outcome(
            run_id,
            episode_sequence,
            "interrupted",
            "prepared retrospective analysis has no sealed outcome",
        )
    elif recorded is None:
        outcome = _successful_outcome(
            run_id,
            episode_sequence,
            analysis,
        )
    else:
        outcome = normalize_retrospective_outcome(
            recorded,
            run_id=run_id,
            episode_sequence=episode_sequence,
        )
    if outcome["warning"]:
        if analysis is not None:
            raise RunStoreError("partial retrospective analysis contradicts outcome")
    else:
        if analysis is None:
            raise RunStoreError("partial retrospective analysis is missing")
        expected = _successful_outcome(
            run_id,
            episode_sequence,
            analysis,
        )
        if outcome != expected:
            raise RunStoreError("partial retrospective analysis contradicts outcome")
    if "outcome.json" in files:
        recorded_outcome = store.partial_evidence_value(
            run_id,
            f"{evidence}/outcome.json",
        )
        if recorded_outcome != outcome:
            raise RunStoreError("partial retrospective outcome is contradictory")
    return _persist_attempt(
        store,
        run_id,
        episode_sequence=episode_sequence,
        evidence=evidence,
        summary=summary,
        command_record=command_record,
        stdout="",
        stderr="",
        analysis=analysis,
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
    outcome = normalize_retrospective_outcome(
        outcome,
        run_id=run_id,
        episode_sequence=episode_sequence,
    )
    store.reconcile_evidence_value(
        run_id,
        f"{evidence}/input.json",
        json.loads(summary),
    )
    store.reconcile_evidence_value(
        run_id,
        f"{evidence}/command.json",
        command_record,
    )
    _write_text_if_absent(store, run_id, f"{evidence}/stdout.log", stdout)
    _write_text_if_absent(store, run_id, f"{evidence}/stderr.log", stderr)
    if analysis is not None:
        store.reconcile_evidence_value(
            run_id,
            f"{evidence}/analysis.json",
            analysis,
        )
    store.reconcile_evidence_value(run_id, f"{evidence}/result.json", outcome)
    store.reconcile_evidence_value(run_id, f"{evidence}/outcome.json", outcome)
    store.seal_evidence(run_id, evidence)
    sealed = normalize_retrospective_outcome(
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


def _contained_command() -> list[str]:
    return [
        "codex",
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


def _run_retrospective_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    input_text: str,
    auth_descriptor: int | None,
) -> subprocess.CompletedProcess[str]:
    input_bytes = input_text.encode("utf-8")
    if len(input_bytes) > MAX_RUN_SUMMARY_BYTES:
        raise RetrospectiveProcessError("invalid", "retrospective input is too large")
    status_read, status_write = os.pipe2(os.O_CLOEXEC)
    wrapper = [
        sys.executable,
        "-c",
        _EXEC_GUARD,
        str(os.getpid()),
        str(auth_descriptor if auth_descriptor is not None else -1),
        str(status_write),
        *command,
    ]
    inherited = (status_write,)
    if auth_descriptor is not None:
        inherited = (auth_descriptor, status_write)
    try:
        process = subprocess.Popen(
            wrapper,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=inherited,
        )
    except BaseException:
        os.close(status_read)
        os.close(status_write)
        raise
    os.close(status_write)
    try:
        pid_descriptor = os.pidfd_open(process.pid)
    except OSError:
        try:
            _terminate_process_group(process.pid)
            process.wait(timeout=PROCESS_CLEANUP_SECONDS)
        finally:
            try:
                _close_process_streams(process)
            finally:
                os.close(status_read)
        raise
    process_io = None
    timed_out = False
    analysis_returncode = None
    try:
        process_io = BoundedProcessIO(
            process,
            input_bytes=input_bytes,
            output_byte_limit=RETROSPECTIVE_OUTPUT_BYTE_LIMIT,
            cleanup_seconds=PROCESS_CLEANUP_SECONDS,
            combined_output_limit=True,
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while not _process_exited(pid_descriptor):
                stop_reason = process_io.observe(deadline)
                if stop_reason == "timeout":
                    timed_out = True
                    break
                if stop_reason == "overflow":
                    break
        finally:
            process_io.close_input()
            try:
                _terminate_process_group(process.pid)
            finally:
                try:
                    returncode = process.wait(timeout=PROCESS_CLEANUP_SECONDS)
                except subprocess.TimeoutExpired as exc:
                    raise RetrospectiveProcessError(
                        "interrupted",
                        "retrospective process group could not be terminated",
                    ) from exc
                finally:
                    if not process_io.drain():
                        raise RetrospectiveProcessError(
                            "interrupted",
                            "retrospective output streams could not be drained",
                        )
        if not timed_out and not process_io.overflowed:
            analysis_returncode = _read_analysis_status(status_read)
    finally:
        try:
            if process_io is None:
                try:
                    _terminate_process_group(process.pid)
                    process.wait(timeout=PROCESS_CLEANUP_SECONDS)
                finally:
                    _close_process_streams(process)
            else:
                process_io.close_input()
        finally:
            try:
                os.close(pid_descriptor)
            finally:
                os.close(status_read)
    if timed_out:
        stdout, stderr = process_io.diagnostics()
        raise RetrospectiveProcessError(
            "interrupted",
            "retrospective analysis timed out and its process group was terminated",
            stdout=stdout,
            stderr=stderr,
        )
    if process_io.overflowed:
        raise RetrospectiveProcessError(
            "invalid",
            "retrospective analysis output exceeds the size limit",
        )
    assert analysis_returncode is not None
    returncode = analysis_returncode
    if returncode < 0:
        stdout, stderr = process_io.diagnostics()
        try:
            signal_name = signal.Signals(-returncode).name
        except ValueError:
            signal_name = str(-returncode)
        raise RetrospectiveProcessError(
            "interrupted",
            f"retrospective analysis exited after signal {signal_name}",
            stdout=stdout,
            stderr=stderr,
        )
    try:
        stdout, stderr = process_io.decoded_output()
    except UnicodeDecodeError as exc:
        diagnostic_stdout, diagnostic_stderr = process_io.diagnostics()
        raise RetrospectiveProcessError(
            "invalid",
            "retrospective analysis output must be UTF-8 text",
            stdout=diagnostic_stdout,
            stderr=diagnostic_stderr,
        ) from exc
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout,
        stderr,
    )


def _read_analysis_status(descriptor: int) -> int:
    try:
        payload = os.read(descriptor, ANALYSIS_STATUS_BYTE_LIMIT + 1)
    except OSError as exc:
        raise RetrospectiveProcessError(
            "interrupted",
            "retrospective analysis status is unavailable",
        ) from exc
    if not payload:
        raise RetrospectiveProcessError(
            "interrupted",
            "retrospective analysis status is unavailable",
        )
    if len(payload) > ANALYSIS_STATUS_BYTE_LIMIT:
        raise RetrospectiveProcessError(
            "invalid",
            "retrospective analysis status is invalid",
        )
    try:
        returncode = int(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RetrospectiveProcessError(
            "invalid",
            "retrospective analysis status is invalid",
        ) from exc
    if (
        payload != f"{returncode}\n".encode("ascii")
        or returncode >= 256
        or returncode <= -signal.NSIG
    ):
        raise RetrospectiveProcessError(
            "invalid",
            "retrospective analysis status is invalid",
        )
    return returncode


def _process_exited(pid_descriptor: int) -> bool:
    poller = select.poll()
    poller.register(pid_descriptor, select.POLLIN)
    return bool(poller.poll(0))


def _close_process_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _terminate_process_group(process_group: int) -> None:
    for requested_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process_group, requested_signal)
        except ProcessLookupError:
            return
        if requested_signal == signal.SIGTERM:
            time.sleep(0.05)


def _prepare_private_runtime(root: Path) -> dict[str, Any]:
    home = root / "home"
    codex_home = home / ".codex"
    workspace = root / "workspace"
    temporary = root / "tmp"
    for path in (home, codex_home, workspace, temporary):
        path.mkdir(mode=0o700)
    config = codex_home / "config.toml"
    config.write_text(_runtime_config(), encoding="utf-8")
    config.chmod(0o600)
    auth_descriptor = _copy_auth_material(codex_home)
    return {
        "home": home,
        "codex_home": codex_home,
        "workspace": workspace,
        "temporary": temporary,
        "auth_descriptor": auth_descriptor,
    }


def _copy_auth_material(runtime_codex_home: Path) -> int | None:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        configured_codex_home = Path(configured).expanduser()
    else:
        configured_codex_home = Path.home() / ".codex"
    source = configured_codex_home / "auth.json"
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > AUTH_BYTE_LIMIT:
            raise OSError("configured Codex auth is invalid")
        identity = _auth_file_identity(metadata)
        payload = bytearray()
        while chunk := os.read(descriptor, AUTH_BYTE_LIMIT + 1 - len(payload)):
            payload.extend(chunk)
            if len(payload) > AUTH_BYTE_LIMIT:
                raise OSError("configured Codex auth is invalid")
        if (
            len(payload) != metadata.st_size
            or _auth_file_identity(os.fstat(descriptor)) != identity
        ):
            raise OSError("configured Codex auth changed while being copied")
    finally:
        os.close(descriptor)
    initial_descriptor = os.memfd_create(
        "afk-codex-auth",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    try:
        auth_descriptor = fcntl.fcntl(
            initial_descriptor,
            fcntl.F_DUPFD_CLOEXEC,
            AUTH_DESCRIPTOR_MINIMUM,
        )
    finally:
        os.close(initial_descriptor)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(auth_descriptor, remaining)
            remaining = remaining[written:]
        os.lseek(auth_descriptor, 0, os.SEEK_SET)
        seals = (
            fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_WRITE
        )
        fcntl.fcntl(auth_descriptor, fcntl.F_ADD_SEALS, seals)
        (runtime_codex_home / "auth.json").symlink_to(
            f"/proc/self/fd/{auth_descriptor}"
        )
    except BaseException:
        os.close(auth_descriptor)
        raise
    return auth_descriptor


def _auth_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


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
    analysis: dict[str, Any],
) -> dict[str, Any]:
    status = (
        "empty"
        if not analysis["process_findings"] and not analysis["improvement_proposals"]
        else "passed"
    )
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
