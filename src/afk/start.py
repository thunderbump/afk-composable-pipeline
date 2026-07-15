from __future__ import annotations

import getpass
import json
import os
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afk.candidate import CandidateError, produce_candidate
from afk.run_store import RunStore, RunStoreBusy, RunStoreError


WORKER_LOCK_ATTEMPTS = 40
WORKER_LOCK_RETRY_SECONDS = 0.05
COMMAND_TIMEOUT_SECONDS = 30
CREDENTIAL_FILE_BYTE_LIMIT = 4096


class StartError(RuntimeError):
    pass


class ExternalCommandError(StartError):
    def __init__(self, classification: str, summary: str):
        super().__init__(summary)
        self.classification = classification


@dataclass(frozen=True)
class StartContext:
    root: Path
    repository: str
    base_branch: str
    base_sha: str
    bead_id: str
    claimant: str
    beads_workspace: Path
    validation_contract: str


def start_run(
    bead_id: str,
    *,
    cwd: Path | None = None,
    bootstrap_contract: bool = False,
) -> tuple[str, int]:
    context = preflight(
        bead_id,
        cwd=cwd or Path.cwd(),
        bootstrap_contract=bootstrap_contract,
    )
    store = RunStore()
    with store.lock():
        projection = store.create_run(
            bead_id=bead_id,
            repository=context.repository,
            base_branch=context.base_branch,
            base_sha=context.base_sha,
            start_request={
                "repository_root": str(context.root),
                "beads_workspace": str(context.beads_workspace),
                "claimant": context.claimant,
                "validation_contract": context.validation_contract,
            },
        )
        run_id = projection["run_id"]
        try:
            bead = _show_bead(bead_id, context.beads_workspace)
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint="created",
                scope="bead_preflight",
                kind="unavailable",
                summary=str(exc),
                classification=_error_classification(exc),
                validation_contract=context.validation_contract,
            )
            return run_id, 2
        try:
            _validate_start_bead(bead, bead_id, context.repository)
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint="created",
                scope="bead_preflight",
                kind="invalid",
                summary=str(exc),
                classification=_error_classification(exc),
                validation_contract=context.validation_contract,
            )
            return run_id, 2
        unit = worker_unit(run_id)
        lingering = _lingering(context.claimant)
        store.prepare_effect(
            run_id,
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": unit},
        )
        store.append_event(
            run_id,
            "worker.launch_prepared",
            data={
                "unit": unit,
                "checkpoint": "created",
                "lingering": lingering,
                "validation_contract": context.validation_contract,
            },
        )
        try:
            _launch_worker(run_id, unit)
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint="created",
                scope="worker_launch",
                kind="unavailable",
                summary=str(exc),
                classification=_error_classification(exc),
                unit=unit,
            )
            return run_id, 2
    return run_id, 0


def resume_run(*, note: str | None = None) -> tuple[str, int]:
    store = RunStore()
    with store.lock():
        projection = store.status()
        run_id = projection["run_id"]
        if "worker_exit_code" in projection:
            attention = projection.get("attention", {})
            if (
                projection["checkpoint"] == "worktree_ready"
                and isinstance(attention, dict)
                and attention.get("scope") == "implementation"
                and attention.get("kind") == "unavailable"
            ):
                return run_id, _advance_candidate(store, run_id)
            return run_id, projection["worker_exit_code"]
        effect = store.effect(run_id, "worker-launch-1")
        unit = effect["intended"]["unit"]
        try:
            completed = _command(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState",
                    "--property=ActiveState",
                ],
                cwd=Path.cwd(),
                check=False,
            )
        except StartError as exc:
            _attention(
                store,
                run_id,
                checkpoint=projection["checkpoint"],
                scope="worker_launch",
                kind="unavailable",
                summary=str(exc),
                classification=_error_classification(exc),
                unit=unit,
            )
            return run_id, 2
        properties: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in properties:
                properties = {}
                break
            properties[key] = value
        active = completed.returncode == 0 and properties == {
            "LoadState": "loaded",
            "ActiveState": "active",
        }
        absent = properties == {
            "LoadState": "not-found",
            "ActiveState": "inactive",
        }
        if active:
            if effect["status"] != "confirmed":
                store.confirm_effect(run_id, "worker-launch-1", observed={"unit": unit})
                store.append_event(
                    run_id,
                    "worker.launch_reconciled",
                    data={
                        "unit": unit,
                        "checkpoint": projection["checkpoint"],
                        "note": note or "",
                    },
                )
            return run_id, 0
        if absent:
            if effect["status"] == "confirmed":
                _attention(
                    store,
                    run_id,
                    checkpoint=projection["checkpoint"],
                    scope="worker_launch",
                    kind="inconclusive",
                    summary=(
                        "confirmed worker launch was collected without a terminal "
                        "observation"
                    ),
                    unit=unit,
                )
                return run_id, 2
            try:
                _launch_worker(run_id, unit)
            except StartError as exc:
                _attention(
                    store,
                    run_id,
                    checkpoint=projection["checkpoint"],
                    scope="worker_launch",
                    kind="unavailable",
                    summary=str(exc),
                    classification=_error_classification(exc),
                    unit=unit,
                )
                return run_id, 2
            store.append_event(
                run_id,
                "worker.launch_retried",
                data={
                    "unit": unit,
                    "checkpoint": projection["checkpoint"],
                    "note": note or "",
                },
            )
            return run_id, 0
        _attention(
            store,
            run_id,
            checkpoint=projection["checkpoint"],
            scope="worker_launch",
            kind="inconclusive",
            summary="prepared worker launch could not be confirmed",
            unit=unit,
        )
        return run_id, 2


def run_worker(run_id: str) -> int:
    store = RunStore()
    for attempt in range(WORKER_LOCK_ATTEMPTS):
        try:
            return _run_worker_with_lock(store, run_id)
        except RunStoreBusy:
            if attempt + 1 == WORKER_LOCK_ATTEMPTS:
                return 1
            time.sleep(WORKER_LOCK_RETRY_SECONDS)
    return 1


def run_worker_unit(run_id: str) -> int:
    exit_code = run_worker(run_id)
    worker_result = {
        0: "completed",
        2: "attention_required",
    }.get(exit_code, "failed")
    store = RunStore()
    while True:
        try:
            with store.lock():
                projection = store.status(run_id)
                terminal_keys = ("worker_exit_code", "worker_result")
                terminal_fields = [key for key in terminal_keys if key in projection]
                if terminal_fields:
                    if len(terminal_fields) != len(terminal_keys):
                        raise StartError("worker terminal observation is incomplete")
                    if (
                        type(projection["worker_exit_code"]) is int
                        and projection["worker_exit_code"] == exit_code
                        and projection["worker_result"] == worker_result
                    ):
                        return exit_code
                    raise StartError(
                        "worker terminal observation conflicts with result"
                    )
                store.append_event(
                    run_id,
                    "worker.terminal",
                    data={
                        "checkpoint": projection["checkpoint"],
                        "unit": worker_unit(run_id),
                        "worker_exit_code": exit_code,
                        "worker_result": worker_result,
                    },
                )
            return exit_code
        except (OSError, RunStoreError) as exc:
            print(
                f"worker terminal observation pending: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(WORKER_LOCK_RETRY_SECONDS)


def _run_worker_with_lock(store: RunStore, run_id: str) -> int:
    try:
        with store.lock():
            identity = store.identity(run_id)
            request = identity.get("start_request", {})
            bead_id = identity["bead_id"]
            beads_workspace = Path(request["beads_workspace"])
            claimant = request["claimant"]
            launch = store.effect(run_id, "worker-launch-1")
            unit = worker_unit(run_id)
            if launch["intended"] != {"unit": unit}:
                raise StartError("worker launch Effect does not match this worker")
            store.confirm_effect(run_id, "worker-launch-1", observed={"unit": unit})
            store.append_event(
                run_id,
                "worker.launched",
                data={"checkpoint": "created", "unit": unit},
            )
            claim = store.prepare_effect(
                run_id,
                "bead-claim",
                kind="bead-claim",
                intended={"bead_id": bead_id, "claimant": claimant},
            )
            if claim["status"] != "confirmed":
                observed = _claim_bead(bead_id, claimant, beads_workspace)
                store.confirm_effect(run_id, "bead-claim", observed=observed)
            store.append_event(
                run_id,
                "bead.claimed",
                state="claimed",
                data={"checkpoint": "claimed", "unit": worker_unit(run_id)},
            )
            worktree_path, branch = _prepare_worktree(store, identity)
            store.append_event(
                run_id,
                "worktree.ready",
                state="worktree_ready",
                data={
                    "checkpoint": "worktree_ready",
                    "unit": worker_unit(run_id),
                    "worktree_path": str(worktree_path),
                    "branch": branch,
                },
            )
            return _advance_candidate(store, run_id)
    except RunStoreBusy:
        raise
    except (KeyError, OSError, StartError, RunStoreError, ValueError) as exc:
        try:
            checkpoint = store.status(run_id)["checkpoint"]
            _attention(
                store,
                run_id,
                checkpoint=checkpoint,
                scope="worker",
                kind="unavailable",
                summary=str(exc),
                classification=(
                    _error_classification(exc) if isinstance(exc, StartError) else None
                ),
                unit=worker_unit(run_id),
            )
            return 2
        except (RunStoreError, OSError):
            return 1


def _advance_candidate(store: RunStore, run_id: str) -> int:
    identity = store.identity(run_id)
    request = identity.get("start_request", {})
    try:
        bead = _show_bead(identity["bead_id"], Path(request["beads_workspace"]))
        produce_candidate(store, run_id, bead=bead)
    except CandidateError as exc:
        checkpoint = store.status(run_id)["checkpoint"]
        _attention(
            store,
            run_id,
            checkpoint=checkpoint,
            scope="candidate",
            kind=exc.kind,
            summary=exc.summary,
        )
        return 2
    except (KeyError, OSError, StartError, RunStoreError, ValueError) as exc:
        checkpoint = store.status(run_id)["checkpoint"]
        _attention(
            store,
            run_id,
            checkpoint=checkpoint,
            scope="candidate",
            kind="unavailable",
            summary=str(exc),
            classification=(
                _error_classification(exc) if isinstance(exc, StartError) else None
            ),
        )
        return 2
    _attention(
        store,
        run_id,
        checkpoint="candidate_ready",
        scope="validation",
        kind="unavailable",
        summary="validation is not available in this AFK slice",
    )
    return 2


def preflight(
    bead_id: str, *, cwd: Path, bootstrap_contract: bool = False
) -> StartContext:
    root = Path(_required(["git", "rev-parse", "--show-toplevel"], cwd=cwd)).resolve()
    repository_data = _json_command(
        ["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"],
        cwd=root,
    )
    if not isinstance(repository_data, dict):
        raise StartError("GitHub repository or default branch is unavailable")
    repository = repository_data.get("nameWithOwner")
    default_branch_data = repository_data.get("defaultBranchRef")
    default_branch = (
        default_branch_data.get("name")
        if isinstance(default_branch_data, dict)
        else None
    )
    if (
        not isinstance(repository, str)
        or not repository
        or not isinstance(default_branch, str)
        or not default_branch
    ):
        raise StartError("GitHub repository or default branch is unavailable")
    remote_ref = f"refs/heads/{default_branch}"
    remote_line = _required(
        ["git", "ls-remote", "--exit-code", "origin", remote_ref], cwd=root
    )
    fields = remote_line.split()
    base_sha = fields[0] if len(fields) == 2 and fields[1] == remote_ref else ""
    if len(base_sha) != 40 or any(
        character not in "0123456789abcdef" for character in base_sha
    ):
        raise StartError("GitHub default branch does not resolve to a full Git SHA")
    _required(["git", "fetch", "--no-tags", "origin", remote_ref], cwd=root)
    fetched_sha = _required(["git", "rev-parse", "FETCH_HEAD"], cwd=root)
    if fetched_sha != base_sha:
        raise StartError("fetched default branch does not match the pinned GitHub SHA")
    validation_contract = _pinned_validation_contract(
        root,
        base_sha,
        bootstrap_contract=bootstrap_contract,
    )
    workspace = Path(
        os.environ.get("AFK_BEADS_WORKSPACE", "/home/bump/Projects/beads")
    ).resolve()
    if not workspace.is_dir():
        raise StartError(f"Beads workspace does not exist: {workspace}")
    claimant = (
        os.environ.get("BEADS_ACTOR") or os.environ.get("USER") or getpass.getuser()
    )
    return StartContext(
        root=root,
        repository=repository,
        base_branch=default_branch,
        base_sha=base_sha,
        bead_id=bead_id,
        claimant=claimant,
        beads_workspace=workspace,
        validation_contract=validation_contract,
    )


def worker_unit(run_id: str) -> str:
    return f"afk-{run_id}-worker-1"


def _launch_worker(run_id: str, unit: str) -> None:
    environment = []
    for name in (
        "HOME",
        "PATH",
        "PYTHONPATH",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "AFK_BEADS_WORKSPACE",
    ):
        value = os.environ.get(name)
        if value is not None:
            environment.append(f"--setenv={name}={value}")
    command = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--property=Type=exec",
        "--property=Restart=no",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=30",
        "--property=UMask=0077",
        "--collect",
        *environment,
        sys.executable,
        "-m",
        "afk",
        "_worker_unit",
        run_id,
    ]
    completed = _command(command, cwd=Path.cwd(), check=False)
    if completed.returncode != 0:
        raise _external_failure(command[0], completed)


def _claim_bead(bead_id: str, claimant: str, workspace: Path) -> dict[str, str]:
    bead = _show_bead(bead_id, workspace)
    if bead.get("status") == "open" and not bead.get("assignee"):
        result = _bd_json(["bd", "update", bead_id, "--claim", "--json"], cwd=workspace)
        if isinstance(result, list):
            if len(result) != 1 or not isinstance(result[0], dict):
                raise _malformed_beads_output()
            result = result[0]
        elif not isinstance(result, dict):
            raise _malformed_beads_output()
        bead = result
    if bead.get("id") != bead_id:
        raise StartError(f"Bead claim returned unexpected Bead: {bead_id}")
    if bead.get("status") != "in_progress" or bead.get("assignee") != claimant:
        raise StartError(f"Bead claim conflicts with current owner: {bead_id}")
    return {"bead_id": bead_id, "claimant": claimant}


def _prepare_worktree(store: RunStore, identity: dict[str, Any]) -> tuple[Path, str]:
    run_id = identity["run_id"]
    root = Path(identity["start_request"]["repository_root"])
    worktree = store.root / "worktrees" / run_id
    branch = f"afk/{identity['bead_id'].replace('.', '-')}-{run_id}"
    if not worktree.exists():
        worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        completed = _command(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                identity["base_sha"],
            ],
            cwd=root,
            check=False,
        )
        if completed.returncode != 0:
            raise _external_failure("git", completed)
    listing = _required(["git", "worktree", "list", "--porcelain"], cwd=root)
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in [*listing.splitlines(), ""]:
        if not line:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, value = line.partition(" ")
        record[key] = value
    registered = next(
        (
            item
            for item in records
            if item.get("worktree")
            and Path(item["worktree"]).resolve() == worktree.resolve()
        ),
        None,
    )
    if registered is None:
        raise StartError("prepared worktree is not registered")
    if registered.get("HEAD") != identity["base_sha"]:
        raise StartError("prepared worktree is not at pinned base")
    if registered.get("branch") != f"refs/heads/{branch}":
        raise StartError("prepared worktree is not on the intended branch")
    dirty = _required(["git", "status", "--porcelain"], cwd=worktree)
    if dirty:
        raise StartError("prepared worktree is dirty")
    return worktree, branch


def _show_bead(bead_id: str, workspace: Path) -> dict[str, Any]:
    result = _bd_json(["bd", "show", bead_id, "--json"], cwd=workspace)
    if (
        not isinstance(result, list)
        or len(result) != 1
        or not isinstance(result[0], dict)
    ):
        raise _malformed_beads_output()
    return result[0]


def _validate_start_bead(bead: dict[str, Any], bead_id: str, repository: str) -> None:
    if bead.get("id") != bead_id or bead.get("status") != "open":
        raise StartError(f"Bead is not open and exact: {bead_id}")
    project_label = f"project:{repository.rsplit('/', 1)[-1]}"
    labels = bead.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        raise StartError(f"Bead labels are invalid: {bead_id}")
    if project_label not in labels:
        raise StartError(f"Bead does not belong to {project_label}")


def _pinned_validation_contract(
    root: Path, base_sha: str, *, bootstrap_contract: bool
) -> str:
    listing = _required(["git", "ls-tree", base_sha, "--", "afk.toml"], cwd=root)
    if not listing:
        if bootstrap_contract:
            return "bootstrap_required"
        raise StartError("pinned base does not contain afk.toml")
    fields = listing.split()
    if len(fields) != 4 or fields[0] != "100644" or fields[1] != "blob":
        raise StartError("pinned afk.toml must be one regular file")
    if bootstrap_contract:
        raise StartError("pinned base already contains afk.toml")
    value = _required(["git", "cat-file", "blob", f"{base_sha}:afk.toml"], cwd=root)
    _validate_contract(value)
    return "pinned"


def _validate_contract(value: str) -> None:
    try:
        contract = tomllib.loads(value)
    except tomllib.TOMLDecodeError as exc:
        raise StartError(f"invalid afk.toml: {exc}") from exc
    validation = contract.get("validation")
    if (
        set(contract) != {"schema_version", "validation"}
        or type(contract.get("schema_version")) is not int
        or contract["schema_version"] != 1
        or not isinstance(validation, dict)
        or set(validation) != {"command", "timeout_seconds"}
        or not isinstance(validation.get("command"), list)
        or not validation["command"]
        or not all(isinstance(item, str) and item for item in validation["command"])
        or type(validation.get("timeout_seconds")) is not int
        or validation["timeout_seconds"] <= 0
    ):
        raise StartError("invalid afk.toml Validation Contract")


def _lingering(claimant: str) -> str:
    completed = _command(
        ["loginctl", "show-user", claimant, "--property=Linger", "--value"],
        cwd=Path.cwd(),
        check=False,
    )
    if completed.returncode != 0:
        return "unknown"
    return "enabled" if completed.stdout.strip().lower() == "yes" else "disabled"


def _attention(
    store: RunStore,
    run_id: str,
    *,
    checkpoint: str,
    scope: str,
    kind: str,
    summary: str,
    classification: str | None = None,
    **details: Any,
) -> None:
    store.append_event(
        run_id,
        "run.attention_required",
        state="attention_required",
        data={
            "checkpoint": checkpoint,
            "attention": {
                "scope": scope,
                "kind": kind,
                "summary": summary,
                **({"classification": classification} if classification else {}),
            },
            **details,
        },
    )


def _required(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> str:
    completed = _command(command, cwd=cwd, check=False, env=env)
    if completed.returncode != 0:
        raise _external_failure(command[0], completed)
    return completed.stdout.strip()


def _json_command(command: list[str], *, cwd: Path) -> Any:
    output = _required(command, cwd=cwd)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError(
            "malformed_output", f"{_tool_name(command[0])} returned malformed output"
        ) from exc


def _bd_json(command: list[str], *, cwd: Path) -> Any:
    environment = os.environ.copy()
    environment["BEADS_DOLT_PASSWORD"] = _beads_password()
    completed = _command(command, cwd=cwd, check=False, env=environment)
    if completed.returncode != 0:
        raise _external_failure("bd", completed)
    output = completed.stdout.strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise _malformed_beads_output() from exc


def _malformed_beads_output() -> ExternalCommandError:
    return ExternalCommandError("malformed_output", "Beads returned malformed output")


def _error_classification(error: StartError) -> str | None:
    value = getattr(error, "classification", None)
    return value if isinstance(value, str) else None


def _external_failure(
    tool: str, completed: subprocess.CompletedProcess[str]
) -> ExternalCommandError:
    raw = f"{completed.stdout}\n{completed.stderr}".lower()
    authentication_markers = (
        "authentication denied",
        "authentication failed",
        "access denied",
        "unauthorized",
        "invalid password",
    )
    name = _tool_name(tool)
    if any(marker in raw for marker in authentication_markers):
        return ExternalCommandError(
            "authentication_denied", f"{name} authentication failed"
        )
    return ExternalCommandError("command_failed", f"{name} command failed")


def _tool_name(tool: str) -> str:
    return {"bd": "Beads", "gh": "GitHub"}.get(tool, tool)


def _beads_password() -> str:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    config_path = config_home / "afk" / "config.toml"
    try:
        config = tomllib.loads(_read_private_text(config_path))
        beads = config.get("beads")
        if (
            set(config) != {"schema_version", "beads"}
            or type(config.get("schema_version")) is not int
            or config["schema_version"] != 1
            or not isinstance(beads, dict)
            or set(beads) != {"password_file"}
            or not isinstance(beads["password_file"], str)
        ):
            raise ValueError
        password_path = Path(beads["password_file"])
        if not password_path.is_absolute():
            raise ValueError
        lines = _read_private_text(password_path).splitlines()
        if not lines or not lines[0]:
            raise ValueError
        return lines[0]
    except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise StartError(
            "Beads credential configuration is missing or invalid"
        ) from exc


def _read_private_text(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > CREDENTIAL_FILE_BYTE_LIMIT
        ):
            raise ValueError
        chunks = []
        remaining = CREDENTIAL_FILE_BYTE_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > CREDENTIAL_FILE_BYTE_LIMIT:
            raise ValueError
        return payload.decode("utf-8")
    finally:
        os.close(descriptor)


def _command(
    command: list[str],
    *,
    cwd: Path,
    check: bool,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
            env=env,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalCommandError(
            "command_timeout", f"{_tool_name(command[0])} command timed out"
        ) from exc
    except OSError as exc:
        raise ExternalCommandError(
            "command_unavailable", f"{_tool_name(command[0])} command is unavailable"
        ) from exc
