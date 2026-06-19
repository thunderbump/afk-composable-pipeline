from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1


class GitCommandError(RuntimeError):
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path | None,
        returncode: int | None,
        stderr: str,
    ):
        super().__init__("git command failed")
        self.command = command
        self.cwd = cwd
        self.returncode = returncode
        self.stderr = stderr


def prepare_checkout_step(context: Any) -> dict[str, Any]:
    return prepare_checkout(
        context.input_data,
        project_contract=context.project_contract,
        run_id=context.run_id,
    )


def prepare_checkout(
    input_data: Any,
    *,
    project_contract: Any = None,
    run_id: str,
) -> dict[str, Any]:
    request = normalize_request(input_data, project_contract=project_contract, run_id=run_id)
    if request["status"] != "valid":
        return request

    checkout_path = Path(request["checkout_path"])
    repo_url = request["repo_url"]
    base_ref = request["base_ref"]
    requested_ref = request["requested_ref"]
    review_branch = request["review_branch"]

    try:
        if checkout_path.exists():
            if not is_git_checkout(checkout_path):
                return failure_result(
                    "failed_existing_checkout",
                    "checkout_path exists but does not have a local .git directory",
                    request,
                    dirty=False,
                )
            dirty = dirty_tree(checkout_path)
            if dirty["dirty"]:
                return failure_result(
                    "failed_dirty_checkout",
                    "existing checkout has uncommitted changes; commit, stash, or remove it before reuse",
                    request,
                    dirty=True,
                    dirty_status=dirty["status_lines"],
                )
        else:
            checkout_path.parent.mkdir(parents=True, exist_ok=True)
            git(["clone", repo_url, str(checkout_path)])

        git(["fetch", "origin", requested_ref], cwd=checkout_path)
        git(["checkout", "-B", review_branch, "FETCH_HEAD"], cwd=checkout_path)
        git(["submodule", "update", "--init", "--recursive"], cwd=checkout_path)

        start_commit = git(["rev-parse", "HEAD"], cwd=checkout_path)
        dirty = dirty_tree(checkout_path)
        publication = publish_review_branch(checkout_path, request)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "prepared",
            "repo_url": redact_url(repo_url),
            "base_ref": base_ref,
            "requested_ref": requested_ref,
            "start_commit": start_commit,
            "review_branch": review_branch,
            "checkout_path": str(checkout_path),
            "dirty": dirty["dirty"],
            "dirty_status": dirty["status_lines"],
            "submodules": submodule_records(checkout_path),
            "publication": publication,
        }
    except GitCommandError as exc:
        return git_failure_result(exc, request)


def normalize_request(input_data: Any, *, project_contract: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(input_data, dict):
        return invalid_request("request must be an object")

    repo_url = string_field(input_data, "repo_url") or getattr(project_contract, "repo_url", None)
    base_ref = string_field(input_data, "base_ref") or getattr(project_contract, "base_branch", None)
    requested_ref = (
        string_field(input_data, "requested_ref")
        or string_field(input_data, "ref")
        or base_ref
    )
    checkout_path = string_field(input_data, "checkout_path")
    review_branch = string_field(input_data, "review_branch") or f"afk/{run_id}"
    publish = input_data.get("publish", {"enabled": False})

    for key in ("repo_url", "base_ref", "requested_ref", "ref", "checkout_path", "review_branch"):
        if key in input_data and input_data[key] is not None and not isinstance(input_data[key], str):
            return invalid_request(f"{key} must be a string")
    if not repo_url:
        return invalid_request("repo_url is required")
    if not base_ref:
        return invalid_request("base_ref is required")
    if not requested_ref:
        return invalid_request("requested_ref is required")
    if not checkout_path:
        return invalid_request("checkout_path is required")
    if "publish" in input_data and not isinstance(publish, dict):
        return invalid_request("publish must be an object")
    if not isinstance(publish.get("enabled", False), bool):
        return invalid_request("publish.enabled must be a boolean")
    for key in ("remote", "branch"):
        if key in publish and publish[key] is not None and not isinstance(publish[key], str):
            return invalid_request(f"publish.{key} must be a string")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "repo_url": repo_url,
        "base_ref": base_ref,
        "requested_ref": requested_ref,
        "checkout_path": checkout_path,
        "review_branch": review_branch,
        "publish": publish,
    }


def invalid_request(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed_invalid_payload",
        "message": message,
        "publication": disabled_publication(),
    }


def string_field(input_data: dict[str, Any], key: str) -> str | None:
    value = input_data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def is_git_checkout(path: Path) -> bool:
    return (path / ".git").is_dir()


def dirty_tree(path: Path) -> dict[str, Any]:
    status = git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=path)
    status_lines = [line for line in status.splitlines() if line]
    return {"dirty": bool(status_lines), "status_lines": status_lines}


def submodule_records(checkout_path: Path) -> list[dict[str, str]]:
    output = git(["submodule", "status", "--recursive"], cwd=checkout_path)
    records = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        sha = parts[0].lstrip("+-U")
        path = parts[1]
        records.append(
            {
                "path": path,
                "sha": sha,
                "gitdir": submodule_gitdir(checkout_path, path),
            }
        )
    return records


def submodule_gitdir(checkout_path: Path, path: str) -> str:
    git_file = checkout_path / path / ".git"
    if git_file.is_dir():
        return git_file.resolve().relative_to(checkout_path.resolve()).as_posix()
    if not git_file.is_file():
        return ""
    gitdir_text = git_file.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not gitdir_text.startswith(prefix):
        return ""
    gitdir_path = (git_file.parent / gitdir_text[len(prefix) :]).resolve()
    try:
        return gitdir_path.relative_to(checkout_path.resolve()).as_posix()
    except ValueError:
        return str(gitdir_path)


def publish_review_branch(checkout_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    publish = request.get("publish") or {}
    if not publish.get("enabled"):
        return disabled_publication()

    remote = str(publish.get("remote") or "origin")
    branch = str(publish.get("branch") or request["review_branch"])
    git(["push", remote, f"HEAD:refs/heads/{branch}"], cwd=checkout_path)
    return {
        "status": "published",
        "enabled": True,
        "remote": remote,
        "branch": branch,
        "ref": f"{remote}/{branch}",
    }


def disabled_publication() -> dict[str, Any]:
    return {"status": "skipped_disabled", "enabled": False}


def failure_result(
    status: str,
    message: str,
    request: dict[str, Any],
    *,
    dirty: bool,
    dirty_status: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "message": message,
        "repo_url": redact_url(str(request.get("repo_url") or "")),
        "base_ref": request.get("base_ref"),
        "requested_ref": request.get("requested_ref"),
        "review_branch": request.get("review_branch"),
        "checkout_path": request.get("checkout_path"),
        "dirty": dirty,
        "dirty_status": dirty_status or [],
        "submodules": [],
        "publication": disabled_publication(),
    }


def git_failure_result(exc: GitCommandError, request: dict[str, Any]) -> dict[str, Any]:
    result = failure_result(
        "failed_git_command",
        "git command failed",
        request,
        dirty=False,
    )
    result["error"] = {
        "command": safe_command(exc.command),
        "cwd": str(exc.cwd) if exc.cwd else None,
        "returncode": exc.returncode,
        "stderr": exc.stderr[-2000:],
    }
    return result


def git(args: list[str], *, cwd: Path | None = None) -> str:
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except OSError as exc:
        raise GitCommandError(command, cwd=cwd, returncode=None, stderr=str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(command, cwd=cwd, returncode=None, stderr="git command timed out") from exc
    if completed.returncode != 0:
        raise GitCommandError(
            command,
            cwd=cwd,
            returncode=completed.returncode,
            stderr=completed.stderr,
        )
    return completed.stdout.strip()


def safe_command(command: list[str]) -> list[str]:
    return [redact_url(part) for part in command]


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or "@" not in parsed.netloc:
        return value
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
