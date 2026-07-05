from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from afk.recipes import review_branch_for_workstream
from afk.redaction import is_secret_key, redact_artifact_value, redact_text


SCHEMA_VERSION = 1


class PublicationConfigError(ValueError):
    pass


class PublisherError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: list[str],
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.details = details or {}


class PublicationLedger(Protocol):
    path: Path

    def write_json(self, name: str, payload: dict[str, Any]) -> None: ...

    def write_text(self, name: str, content: str) -> None: ...


@dataclass(frozen=True)
class PublicationRequest:
    publisher: Any
    workstream_id: str
    review_branch: str
    checkout_path: Path
    checkout_base_commit: str
    next_allowed_command: str
    ledger: PublicationLedger
    build_pr_body: Callable[[], str]


def publish_terminal_pr(request: PublicationRequest) -> dict[str, Any]:
    if not isinstance(request.publisher, dict):
        return failed_publication_config("publisher must be an object", request)
    if not request.publisher.get("enabled", True):
        return validated_unpublished_publication(
            "workstream validated and reviewed, but publisher is disabled",
            next_allowed_command=request.next_allowed_command,
        )
    try:
        config = normalize_publisher_config(request.publisher, request)
    except PublicationConfigError as exc:
        return failed_publication_config(str(exc), request)
    try:
        config["gh_auth"] = validate_publisher_auth_config(config["gh_auth"], request.checkout_path)
    except PublicationConfigError as exc:
        return failed_publication_config(
            str(exc),
            request,
            auth=publisher_auth_artifact(config["gh_auth"]),
        )
    auth = config["gh_auth"]
    auth_artifact = publisher_auth_artifact(auth)
    git_push_result: dict[str, Any] | None = None
    git_push_command: list[str] = []
    git_push_retry_command: list[str] = []
    try:
        run_publisher_command(
            [config["gh_path"], "auth", "status", "--hostname", "github.com"],
            cwd=request.checkout_path,
            tool="gh",
            auth=auth,
            message_on_failure="gh auth status failed",
        )
        body = request.build_pr_body()
        request.ledger.write_text("pr-body.md", body)
        pr_body_path = (request.ledger.path / "pr-body.md").resolve(strict=False)
        if config["push"]:
            git_push = push_review_branch(config, request=request, auth=auth)
            git_push_result = git_push["result"]
            git_push_command = git_push["command"]
            git_push_retry_command = git_push["retry_command"]
        if config["mode"] == "create":
            command = [
                config["gh_path"],
                "pr",
                "create",
                "--repo",
                config["repo"],
                "--base",
                config["base"],
                "--head",
                config["head"],
                "--title",
                config["title"],
                "--body-file",
                str(pr_body_path),
            ]
            completed = run_publisher_command(command, cwd=request.checkout_path, tool="gh", auth=auth)
        else:
            command = [
                config["gh_path"],
                "pr",
                "edit",
                config["pr"],
                "--repo",
                config["repo"],
                "--title",
                config["title"],
                "--body-file",
                str(pr_body_path),
            ]
            completed, command = run_pr_update_command(command, config=config, request=request, auth=auth, body=body)
    except PublisherError as exc:
        if git_push_result is not None:
            details = dict(exc.details)
            details.setdefault("git_push", git_push_result)
            command_details = details.get("commands") if isinstance(details.get("commands"), dict) else {}
            if git_push_command and "git_push" not in command_details:
                command_details["git_push"] = git_push_command
            if git_push_retry_command and "git_push_retry" not in command_details:
                command_details["git_push_retry"] = git_push_retry_command
            if command_details:
                details["commands"] = command_details
            exc.details = details
        return failed_publication(exc, request, auth=auth_artifact)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "published",
        "enabled": True,
        "mode": config["mode"],
        "reason": "terminal PR published",
        "auth": auth_artifact,
        "url": successful_publisher_url(completed.stdout),
        "next_allowed_command": "none",
        "retry": "",
        "commands": {
            "gh": redact_artifact_value(command),
            "git_push": redact_artifact_value(git_push_command),
            "git_push_retry": redact_artifact_value(git_push_retry_command),
        },
        "body_path": str(pr_body_path),
    }
    if git_push_result is not None:
        result["git_push"] = redact_artifact_value(git_push_result)
    return result


def push_review_branch(
    config: dict[str, Any],
    *,
    request: PublicationRequest,
    auth: dict[str, Any],
) -> dict[str, Any]:
    push_ref = f"refs/heads/{config['head']}"
    push_command = [config["git_path"], "push", config["remote"], f"HEAD:{push_ref}"]
    git_push_result = {
        "branch": config["head"],
        "remote": config["remote"],
        "retry_handling": "not-needed",
        "lease_expected": "",
        "base_commit": request.checkout_base_commit,
        "remote_tip": "",
        "retry_reason": "",
        "attempts": [],
    }
    initial_error: PublisherError | None = None
    try:
        completed = run_publisher_command(push_command, cwd=request.checkout_path, tool="git", auth=auth)
        git_push_result["attempts"].append(publisher_command_attempt(push_command, completed=completed, outcome="pushed"))
        return {"command": push_command, "retry_command": [], "result": git_push_result}
    except PublisherError as exc:
        initial_error = exc
        initial_outcome = "non-fast-forward" if publisher_error_is_non_fast_forward_push(exc) else "failed"
        git_push_result["attempts"].append(publisher_command_attempt(push_command, error=exc, outcome=initial_outcome))
        if not publisher_error_is_non_fast_forward_push(exc):
            exc.details = {"git_push": git_push_result}
            raise

    retry_context = afk_review_branch_retry_context(config, request=request, auth=auth)
    git_push_result.update(
        {
            "lease_expected": retry_context["lease_expected"],
            "base_commit": retry_context["base_commit"],
            "remote_tip": retry_context["remote_tip"],
            "local_head": retry_context["local_head"],
            "merge_base": retry_context["merge_base"],
            "owned_branch": retry_context["owned_branch"],
            "retry_reason": retry_context["reason"],
        }
    )
    if not retry_context["eligible"]:
        git_push_result["retry_handling"] = "not-eligible"
        raise PublisherError(
            f"git push rejected as non-fast-forward and AFK review-branch retry is not eligible: {retry_context['reason']}",
            command=push_command,
            returncode=initial_error.returncode if initial_error is not None else None,
            stdout=initial_error.stdout if initial_error is not None else "",
            stderr=initial_error.stderr if initial_error is not None else "",
            details={"git_push": git_push_result},
        )

    retry_command = [
        config["git_path"],
        "push",
        f"--force-with-lease={push_ref}:{retry_context['lease_expected']}",
        config["remote"],
        f"HEAD:{push_ref}",
    ]
    try:
        completed = run_publisher_command(retry_command, cwd=request.checkout_path, tool="git", auth=auth)
    except PublisherError as exc:
        git_push_result["retry_handling"] = "force-with-lease-failed"
        git_push_result["attempts"].append(
            publisher_command_attempt(retry_command, error=exc, outcome="force-with-lease-failed")
        )
        raise PublisherError(
            "git push rejected as non-fast-forward and AFK review-branch retry with --force-with-lease failed",
            command=retry_command,
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
            details={"git_push": git_push_result},
        ) from exc

    git_push_result["retry_handling"] = "force-with-lease-replaced"
    git_push_result["attempts"].append(publisher_command_attempt(retry_command, completed=completed, outcome="pushed"))
    return {"command": push_command, "retry_command": retry_command, "result": git_push_result}


def publisher_command_attempt(
    command: list[str],
    *,
    completed: subprocess.CompletedProcess[str] | None = None,
    error: PublisherError | None = None,
    outcome: str,
) -> dict[str, Any]:
    if completed is not None:
        return {
            "command": redact_artifact_value(command),
            "returncode": completed.returncode,
            "stdout_excerpt": redact_text(completed.stdout[-2000:]),
            "stderr_excerpt": redact_text(completed.stderr[-2000:]),
            "outcome": outcome,
        }
    if error is None:
        raise ValueError("completed or error is required")
    return {
        "command": redact_artifact_value(command),
        "returncode": error.returncode,
        "stdout_excerpt": redact_text(error.stdout[-2000:]),
        "stderr_excerpt": redact_text(error.stderr[-2000:]),
        "outcome": outcome,
    }


def publisher_error_is_non_fast_forward_push(exc: PublisherError) -> bool:
    text = f"{exc.message}\n{exc.stdout}\n{exc.stderr}".lower()
    return "non-fast-forward" in text or ("[rejected]" in text and "fetch first" in text)


def afk_review_branch_retry_context(
    config: dict[str, Any],
    *,
    request: PublicationRequest,
    auth: dict[str, Any],
) -> dict[str, Any]:
    owned_branch = review_branch_for_workstream(request.workstream_id) if request.workstream_id else ""
    remote_tip = publisher_remote_branch_oid(config, checkout_path=request.checkout_path, auth=auth)
    base_commit = publisher_resolved_commit(
        config["git_path"],
        request.checkout_base_commit,
        checkout_path=request.checkout_path,
        auth=auth,
    )
    local_head = publisher_resolved_commit(
        config["git_path"],
        "HEAD",
        checkout_path=request.checkout_path,
        auth=auth,
    )
    merge_base = publisher_merge_base(
        config["git_path"],
        local_head,
        remote_tip,
        checkout_path=request.checkout_path,
        auth=auth,
    )
    remote_descends_from_base = publisher_commit_descends_from(
        config["git_path"],
        remote_tip,
        base_commit,
        checkout_path=request.checkout_path,
        auth=auth,
    )
    local_descends_from_base = publisher_commit_descends_from(
        config["git_path"],
        local_head,
        base_commit,
        checkout_path=request.checkout_path,
        auth=auth,
    )
    if not config["head"].startswith("afk/"):
        reason = "review branch retry is only allowed for afk/ branches"
    elif config["head"] != request.review_branch:
        reason = "publisher head does not match the normalized review branch"
    elif not owned_branch:
        reason = "workstream id is required to prove AFK review-branch ownership"
    elif config["head"] != owned_branch:
        reason = f"review branch does not match the workstream-owned AFK branch {owned_branch}"
    elif not remote_tip:
        reason = "remote review branch could not be resolved for retry"
    elif not base_commit:
        reason = "checkout base commit is required for retry safety"
    elif not local_head:
        reason = "local HEAD could not be resolved for retry safety"
    elif not remote_descends_from_base:
        reason = "remote review branch does not descend from the checkout base commit"
    elif not local_descends_from_base:
        reason = "local HEAD does not descend from the checkout base commit"
    else:
        reason = "remote and local heads descend from the checkout base commit"
    eligible = (
        bool(remote_tip)
        and bool(base_commit)
        and bool(local_head)
        and remote_descends_from_base
        and local_descends_from_base
        and config["head"].startswith("afk/")
        and config["head"] == request.review_branch
        and bool(owned_branch)
        and config["head"] == owned_branch
    )
    return {
        "eligible": eligible,
        "reason": reason,
        "lease_expected": remote_tip,
        "remote_tip": remote_tip,
        "base_commit": base_commit,
        "local_head": local_head,
        "merge_base": merge_base,
        "owned_branch": owned_branch,
    }


def publisher_remote_branch_oid(config: dict[str, Any], *, checkout_path: Path, auth: dict[str, Any]) -> str:
    push_ref = f"refs/heads/{config['head']}"
    completed = run_publisher_diagnostic_command(
        [config["git_path"], "ls-remote", "--heads", config["remote"], push_ref],
        cwd=checkout_path,
        auth=auth,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return ""
    remote_tip = completed.stdout.strip().split()[0]
    if publisher_commit_exists_locally(config["git_path"], remote_tip, checkout_path=checkout_path, auth=auth):
        return remote_tip
    fetch_completed = run_publisher_diagnostic_command(
        [config["git_path"], "fetch", config["remote"], push_ref],
        cwd=checkout_path,
        auth=auth,
    )
    if fetch_completed.returncode != 0:
        return remote_tip
    if publisher_commit_exists_locally(config["git_path"], remote_tip, checkout_path=checkout_path, auth=auth):
        return remote_tip
    return (
        publisher_resolved_commit(config["git_path"], "FETCH_HEAD", checkout_path=checkout_path, auth=auth)
        or remote_tip
    )


def publisher_resolved_commit(git_path: str, ref: str, *, checkout_path: Path, auth: dict[str, Any]) -> str:
    if not ref:
        return ""
    completed = run_publisher_diagnostic_command([git_path, "rev-parse", ref], cwd=checkout_path, auth=auth)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""


def publisher_commit_exists_locally(
    git_path: str,
    commit: str,
    *,
    checkout_path: Path,
    auth: dict[str, Any],
) -> bool:
    if not commit:
        return False
    completed = run_publisher_diagnostic_command(
        [git_path, "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=checkout_path,
        auth=auth,
    )
    return completed.returncode == 0


def publisher_merge_base(
    git_path: str,
    local_head: str,
    remote_head: str,
    *,
    checkout_path: Path,
    auth: dict[str, Any],
) -> str:
    if not local_head or not remote_head:
        return ""
    completed = run_publisher_diagnostic_command(
        [git_path, "merge-base", local_head, remote_head],
        cwd=checkout_path,
        auth=auth,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""


def publisher_commit_descends_from(
    git_path: str,
    commit: str,
    ancestor: str,
    *,
    checkout_path: Path,
    auth: dict[str, Any],
) -> bool:
    if not commit or not ancestor:
        return False
    completed = run_publisher_diagnostic_command(
        [git_path, "merge-base", "--is-ancestor", ancestor, commit],
        cwd=checkout_path,
        auth=auth,
    )
    return completed.returncode == 0


def run_publisher_diagnostic_command(
    command: list[str],
    *,
    cwd: Path,
    auth: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="afk-publisher-") as temp_dir:
        env = minimal_publisher_environment(Path(temp_dir), auth=auth)
        try:
            return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
        except OSError as exc:
            return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr=str(exc))


def successful_publisher_url(stdout: str) -> str:
    return redact_text(stdout.strip().splitlines()[-1]) if stdout.strip() else ""


def run_pr_update_command(
    command: list[str],
    *,
    config: dict[str, Any],
    request: PublicationRequest,
    auth: dict[str, Any],
    body: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    try:
        return run_publisher_command(command, cwd=request.checkout_path, tool="gh", auth=auth), command
    except PublisherError as exc:
        if not publisher_error_is_projects_classic_graphql_failure(exc):
            raise
    pr_number = pr_number_for_rest_update(config["pr"], config=config, checkout_path=request.checkout_path, auth=auth)
    request.ledger.write_json("pr-update.json", {"title": config["title"], "body": body})
    pr_update_path = (request.ledger.path / "pr-update.json").resolve(strict=False)
    fallback_command = [
        config["gh_path"],
        "api",
        "--method",
        "PATCH",
        f"repos/{config['repo']}/pulls/{pr_number}",
        "--input",
        str(pr_update_path),
        "--jq",
        ".html_url",
    ]
    return run_publisher_command(fallback_command, cwd=request.checkout_path, tool="gh", auth=auth), fallback_command


def publisher_error_is_projects_classic_graphql_failure(exc: PublisherError) -> bool:
    text = f"{exc.message}\n{exc.stdout}\n{exc.stderr}".lower()
    if "graphql" not in text:
        return False
    return (
        "projects (classic)" in text
        or "projects classic" in text
        or "projectcards" in text
        or "classic projects" in text
    )


def pr_number_for_rest_update(
    pr_ref: str,
    *,
    config: dict[str, Any],
    checkout_path: Path,
    auth: dict[str, Any],
) -> str:
    direct_number = string_field({"pr": pr_ref}, "pr")
    if direct_number and direct_number.isdigit():
        return direct_number
    command = [
        config["gh_path"],
        "pr",
        "view",
        pr_ref,
        "--repo",
        config["repo"],
        "--json",
        "number",
        "--jq",
        ".number",
    ]
    completed = run_publisher_command(command, cwd=checkout_path, tool="gh", auth=auth)
    resolved = string_field({"number": completed.stdout}, "number")
    if resolved and resolved.isdigit():
        return resolved
    raise PublisherError(
        "could not resolve PR number for REST update fallback",
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def normalize_publisher_config(publisher: dict[str, Any], request: PublicationRequest) -> dict[str, Any]:
    mode = string_field(publisher, "mode") or "create"
    if mode not in {"create", "update"}:
        raise PublicationConfigError("publisher.mode must be create or update")
    gh = publisher.get("gh", {})
    git = publisher.get("git", {})
    if not isinstance(gh, dict) or not isinstance(git, dict):
        raise PublicationConfigError("publisher.gh and publisher.git must be objects when present")
    head = string_field(publisher, "head") or request.review_branch
    if head != request.review_branch:
        raise PublicationConfigError("publisher.head must match review_branch")
    title = string_field(publisher, "title") or f"{request.workstream_id}: workstream"
    repo = string_field(publisher, "repo") or ""
    base = string_field(publisher, "base") or ""
    raw_pr = string_field(publisher, "pr") or ""
    pr = raw_pr or head
    if not repo:
        raise PublicationConfigError("publisher.repo is required")
    if mode == "create" and not base:
        raise PublicationConfigError("publisher.base is required for create")
    gh_auth = normalize_publisher_gh_auth(gh)
    return {
        "mode": mode,
        "gh_path": string_field(gh, "path") or "gh",
        "gh_auth": gh_auth,
        "git_path": string_field(git, "path") or "git",
        "push": bool(git.get("push", False)),
        "remote": string_field(git, "remote") or "origin",
        "repo": repo,
        "base": base,
        "head": head,
        "title": title,
        "pr": pr,
    }


def normalize_publisher_gh_auth(gh: dict[str, Any]) -> dict[str, Any]:
    for key in gh:
        if key in {"path", "auth"}:
            continue
        if is_secret_key(key):
            raise PublicationConfigError(f"publisher.gh.{key} is not supported; mount gh auth config instead")
    raw_auth = gh.get("auth")
    if raw_auth is None:
        return {"configured": False, "source": "minimal_env", "config_dir": ""}
    if not isinstance(raw_auth, dict):
        raise PublicationConfigError("publisher.gh.auth must be an object")
    unsupported = [key for key in raw_auth.keys() if key != "config_dir"]
    if unsupported:
        raise PublicationConfigError("publisher.gh.auth only supports config_dir")
    config_dir = string_field(raw_auth, "config_dir")
    if not config_dir:
        raise PublicationConfigError("publisher.gh.auth.config_dir is required")
    return {
        "configured": True,
        "source": "gh_config_dir",
        "config_dir": config_dir,
    }


def validate_publisher_auth_config(auth: dict[str, Any], checkout_path: Path) -> dict[str, Any]:
    if not auth.get("configured"):
        return {"configured": False, "source": "minimal_env", "config_dir": ""}
    config_dir = Path(str(auth["config_dir"]))
    if not config_dir.is_absolute():
        raise PublicationConfigError("publisher.gh.auth.config_dir must be absolute")
    if not config_dir.is_dir():
        raise PublicationConfigError("publisher.gh.auth.config_dir must be an existing directory")
    if path_is_equal_to_or_inside(config_dir, checkout_path):
        raise PublicationConfigError("publisher.gh.auth.config_dir must be outside checkout")
    return {
        "configured": True,
        "source": "gh_config_dir",
        "config_dir": str(config_dir),
    }


def publisher_auth_artifact(auth: dict[str, Any]) -> dict[str, Any]:
    artifact = {
        "configured": bool(auth.get("configured")),
        "source": str(auth.get("source") or "minimal_env"),
    }
    if auth.get("configured"):
        artifact["path"] = "[REDACTED]"
    return artifact


def run_publisher_command(
    command: list[str],
    *,
    cwd: Path,
    tool: str,
    auth: dict[str, Any],
    message_on_failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="afk-publisher-") as temp_dir:
        env = minimal_publisher_environment(Path(temp_dir), auth=auth)
        return run_publisher_command_once(command, cwd=cwd, tool=tool, env=env, message_on_failure=message_on_failure)


def run_publisher_command_once(
    command: list[str],
    *,
    cwd: Path,
    tool: str,
    env: dict[str, str],
    message_on_failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise PublisherError(str(exc), command=command, returncode=None, stderr=str(exc)) from exc
    if completed.returncode != 0:
        raise PublisherError(
            message_on_failure or f"{tool} command failed",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def minimal_publisher_environment(temp_path: Path, *, auth: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    home_path = temp_path / "home"
    xdg_config_home = temp_path / "xdg-config"
    xdg_cache_home = temp_path / "xdg-cache"
    xdg_state_home = temp_path / "xdg-state"
    tmp_path = temp_path / "tmp"
    for path in (home_path, xdg_config_home, xdg_cache_home, xdg_state_home, tmp_path):
        path.mkdir()
    env["HOME"] = str(home_path)
    env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    env["XDG_CACHE_HOME"] = str(xdg_cache_home)
    env["XDG_STATE_HOME"] = str(xdg_state_home)
    env["TMPDIR"] = str(tmp_path)
    if auth.get("configured") and auth.get("source") == "gh_config_dir":
        env["GH_CONFIG_DIR"] = str(auth["config_dir"])
    return env


def validated_unpublished_publication(reason: str, *, next_allowed_command: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "validated-unpublished",
        "enabled": False,
        "reason": reason,
        "next_allowed_command": next_allowed_command,
        "retry": "",
    }


def failed_publication(exc: PublisherError, request: PublicationRequest, *, auth: dict[str, Any]) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "failed-needs-human",
        "enabled": True,
        "reason": exc.message,
        "auth": auth,
        "returncode": exc.returncode,
        "command": redact_artifact_value(exc.command),
        "stdout_excerpt": redact_text(exc.stdout[-2000:]),
        "stderr_excerpt": redact_text(exc.stderr[-2000:]),
        "next_allowed_command": request.next_allowed_command,
        "retry": retry_instructions(request.next_allowed_command, auth_hint=True),
    }
    if exc.details:
        result.update(redact_artifact_value(exc.details))
    return result


def failed_publication_config(
    reason: str,
    request: PublicationRequest,
    *,
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed-needs-human",
        "enabled": True,
        "reason": reason,
        "auth": auth or {"configured": False, "source": "minimal_env"},
        "returncode": None,
        "command": [],
        "stdout_excerpt": "",
        "stderr_excerpt": "",
        "next_allowed_command": request.next_allowed_command,
        "retry": retry_instructions(request.next_allowed_command, auth_hint=True),
    }


def retry_instructions(next_allowed_command: str, auth_hint: bool = False) -> str:
    instructions = (
        "Fix the failed evidence, keep the shared review branch, and rerun "
        f"{next_allowed_command}; previous workstream run: latest"
    )
    if auth_hint:
        instructions += (
            ". For GitHub publication, mount a GitHub CLI config directory outside the checkout "
            "and set publisher.gh.auth.config_dir in the recipe before rerunning"
        )
    return instructions


def string_field(input_data: dict[str, Any], key: str) -> str | None:
    value = input_data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def path_is_equal_to_or_inside(path: Path, parent: Path) -> bool:
    for candidate in (path, path.resolve()):
        try:
            candidate.relative_to(parent.resolve())
            return True
        except ValueError:
            pass
    return False
