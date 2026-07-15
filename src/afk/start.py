from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afk.run_store import RunStore, RunStoreBusy, RunStoreError


WORKER_LOCK_ATTEMPTS = 40
WORKER_LOCK_RETRY_SECONDS = 0.05
COMMAND_TIMEOUT_SECONDS = 30


class StartError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartContext:
    root: Path
    repository: str
    base_branch: str
    base_sha: str
    bead_id: str
    claimant: str
    beads_workspace: Path


def start_run(bead_id: str, *, cwd: Path | None = None) -> tuple[str, int]:
    context = preflight(bead_id, cwd=cwd or Path.cwd())
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
            },
        )
        run_id = projection["run_id"]
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
                unit=unit,
            )
            return run_id, 2
    return run_id, 0


def resume_run(*, note: str | None = None) -> tuple[str, int]:
    store = RunStore()
    with store.lock():
        projection = store.status()
        run_id = projection["run_id"]
        effect = store.effect(run_id, "worker-launch-1")
        if effect["status"] == "confirmed":
            return run_id, 0
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
            _attention(
                store,
                run_id,
                checkpoint="worktree_ready",
                scope="implementation",
                kind="unavailable",
                summary="implementation is not available in this AFK slice",
                unit=worker_unit(run_id),
                worktree_path=str(worktree_path),
                branch=branch,
            )
            return 2
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
                unit=worker_unit(run_id),
            )
            return 2
        except (RunStoreError, OSError):
            return 1


def preflight(bead_id: str, *, cwd: Path) -> StartContext:
    root = Path(_required(["git", "rev-parse", "--show-toplevel"], cwd=cwd)).resolve()
    _validate_contract(root / "afk.toml")
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
    workspace = Path(
        os.environ.get("AFK_BEADS_WORKSPACE", "/home/bump/Projects/beads")
    ).resolve()
    if not workspace.is_dir():
        raise StartError(f"Beads workspace does not exist: {workspace}")
    bead = _show_bead(bead_id, workspace)
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
        "_worker",
        run_id,
    ]
    completed = _command(command, cwd=Path.cwd(), check=False)
    if completed.returncode != 0:
        raise StartError(completed.stderr.strip() or "systemd worker launch failed")


def _claim_bead(bead_id: str, claimant: str, workspace: Path) -> dict[str, str]:
    bead = _show_bead(bead_id, workspace)
    if bead.get("status") == "open" and not bead.get("assignee"):
        result = _json_command(
            ["bd", "update", bead_id, "--claim", "--json"], cwd=workspace
        )
        if isinstance(result, list):
            result = result[0] if result else {}
        if not isinstance(result, dict):
            raise StartError(f"Bead claim returned invalid data: {bead_id}")
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
            raise StartError(completed.stderr.strip() or "worktree preparation failed")
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
    result = _json_command(["bd", "show", bead_id, "--json"], cwd=workspace)
    if (
        not isinstance(result, list)
        or len(result) != 1
        or not isinstance(result[0], dict)
    ):
        raise StartError(f"Bead lookup was ambiguous: {bead_id}")
    return result[0]


def _validate_contract(path: Path) -> None:
    try:
        contract = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
    **details: Any,
) -> None:
    store.append_event(
        run_id,
        "run.attention_required",
        state="attention_required",
        data={
            "checkpoint": checkpoint,
            "attention": {"scope": scope, "kind": kind, "summary": summary},
            **details,
        },
    )


def _required(command: list[str], *, cwd: Path) -> str:
    completed = _command(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise StartError(completed.stderr.strip() or f"command failed: {command[0]}")
    return completed.stdout.strip()


def _json_command(command: list[str], *, cwd: Path) -> Any:
    output = _required(command, cwd=cwd)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise StartError(f"invalid JSON from {command[0]}") from exc


def _command(
    command: list[str], *, cwd: Path, check: bool
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise StartError(
            f"{command[0]} timed out after {COMMAND_TIMEOUT_SECONDS} seconds"
        ) from exc
    except OSError as exc:
        raise StartError(f"could not execute {command[0]}: {exc}") from exc
