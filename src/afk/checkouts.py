from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from afk.redaction import redact_text, redact_url


SCHEMA_VERSION = 1
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{4,40}$")


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
    checkout_root = Path(request["checkout_root"])
    repo_url = request["repo_url"]
    base_ref = request["base_ref"]
    requested_ref = request["requested_ref"]
    review_branch = request["review_branch"]
    base_commit = request["base_commit"]

    try:
        if checkout_path.exists():
            if not is_git_checkout(checkout_path):
                if checkout_path.is_dir() and is_empty_directory(checkout_path):
                    git(["clone", repo_url, str(checkout_path)])
                else:
                    return failure_result(
                        "failed_existing_checkout",
                        "checkout_path exists but does not have a local .git directory",
                        request,
                        dirty=False,
                    )
            else:
                origin_url = git(["remote", "get-url", "origin"], cwd=checkout_path)
                if not repo_urls_match(origin_url, repo_url):
                    return failure_result(
                        "failed_repo_mismatch",
                        "existing checkout origin does not match requested repo_url",
                        request,
                        dirty=False,
                        origin_url=origin_url,
                    )
                dirty = dirty_tree(checkout_path)
                if dirty["dirty"]:
                    if clean_reserved_checkout_artifacts(checkout_path, dirty["status_lines"]):
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

        try:
            git(["fetch", "origin", requested_ref], cwd=checkout_path)
            target_commit = git(["rev-parse", "FETCH_HEAD"], cwd=checkout_path)
        except GitCommandError:
            target_commit = resolve_requested_commit(checkout_path, requested_ref)
            if not target_commit:
                raise
        branch_commit = local_branch_commit(checkout_path, review_branch)
        if branch_commit and branch_commit != target_commit:
            return failure_result(
                "failed_existing_branch",
                "review_branch already exists at a different commit; choose a fresh branch or remove it before reuse",
                request,
                dirty=False,
                branch_commit=branch_commit,
                target_commit=target_commit,
            )
        if branch_commit:
            git(["checkout", review_branch], cwd=checkout_path)
        else:
            git(["checkout", "-b", review_branch, target_commit], cwd=checkout_path)
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
            "checkout_root": str(checkout_root),
            "base_commit": base_commit or start_commit,
            "start_commit": start_commit,
            "review_branch": review_branch,
            "checkout_path": str(checkout_path),
            "dirty": dirty["dirty"],
            "dirty_status": dirty["status_lines"],
            "submodules": submodule_records(checkout_path),
            "publication": publication,
            "artifacts": {"publication": "publication-result.json"},
        }
    except GitCommandError as exc:
        return git_failure_result(exc, request)


def is_empty_directory(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    except OSError:
        return False
    return False


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
    checkout_root = string_field(input_data, "checkout_root")
    review_branch = string_field(input_data, "review_branch") or f"afk/{run_id}"
    publish = input_data.get("publish", {"enabled": False})

    for key in (
        "repo_url",
        "base_ref",
        "requested_ref",
        "ref",
        "checkout_path",
        "checkout_root",
        "review_branch",
        "base_commit",
    ):
        if key in input_data and input_data[key] is not None and not isinstance(input_data[key], str):
            return invalid_request(f"{key} must be a string")
    if not repo_url:
        return invalid_request("repo_url is required")
    if url_has_secret_material(repo_url):
        return invalid_request("repo_url must not contain embedded credentials or query parameters")
    if not base_ref:
        return invalid_request("base_ref is required")
    if not requested_ref:
        return invalid_request("requested_ref is required")
    if not checkout_root:
        return invalid_request("checkout_root is required")
    if not checkout_path:
        return invalid_request("checkout_path is required")
    path_error = checkout_path_error(checkout_root, checkout_path)
    if path_error is not None:
        return invalid_request(path_error)
    if not review_branch_allowed(review_branch):
        return invalid_request("review_branch must be a safe afk/* branch name")
    if "publish" in input_data and not isinstance(publish, dict):
        return invalid_request("publish must be an object")
    if not isinstance(publish.get("enabled", False), bool):
        return invalid_request("publish.enabled must be a boolean")
    for key in ("remote", "branch"):
        if key in publish and publish[key] is not None and not isinstance(publish[key], str):
            return invalid_request(f"publish.{key} must be a string")
    if publish.get("remote") not in (None, "origin"):
        return invalid_request("publish.remote must be origin")
    publish_branch = string_field(publish, "branch") or review_branch
    if not review_branch_allowed(publish_branch):
        return invalid_request("publish.branch must be a safe afk/* branch name")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "repo_url": repo_url,
        "base_ref": base_ref,
        "requested_ref": requested_ref,
        "checkout_root": checkout_root,
        "checkout_path": checkout_path,
        "review_branch": review_branch,
        "base_commit": string_field(input_data, "base_commit"),
        "publish": publish,
    }


def invalid_request(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "failed_invalid_payload",
        "message": message,
        "publication": disabled_publication(),
        "artifacts": {"publication": "publication-result.json"},
    }


def string_field(input_data: dict[str, Any], key: str) -> str | None:
    value = input_data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def is_git_checkout(path: Path) -> bool:
    return (path / ".git").is_dir()


def checkout_path_error(checkout_root: str, checkout_path: str) -> str | None:
    root = Path(checkout_root)
    path = Path(checkout_path)
    if not root.is_absolute():
        return "checkout_root must be absolute"
    if not path.is_absolute():
        return "checkout_path must be absolute"
    root_resolved = root.resolve(strict=False)
    path_resolved = path.resolve(strict=False)
    if path_resolved == root_resolved:
        return "checkout_path must be inside checkout_root, not equal to it"
    try:
        path_resolved.relative_to(root_resolved)
    except ValueError:
        return "checkout_path must be inside checkout_root"
    return None


def review_branch_allowed(branch: str) -> bool:
    if not branch.startswith("afk/"):
        return False
    if branch.endswith("/") or branch.endswith(".lock"):
        return False
    if ".." in branch or "//" in branch or "@{" in branch:
        return False
    if re.fullmatch(r"[A-Za-z0-9._/-]+", branch) is None:
        return False
    for component in branch.split("/"):
        if not component:
            return False
        if component.startswith(".") or component.endswith("."):
            return False
        if component.endswith(".lock"):
            return False
    return True


def resolve_requested_commit(checkout_path: Path, requested_ref: str) -> str:
    if not GIT_COMMIT_PATTERN.fullmatch(requested_ref):
        return ""
    try:
        candidates = [
            line.strip()
            for line in git(["rev-parse", f"--disambiguate={requested_ref}"], cwd=checkout_path).splitlines()
            if line.strip()
        ]
    except GitCommandError:
        return ""
    commit_candidates = []
    for candidate in candidates:
        try:
            if git(["cat-file", "-t", candidate], cwd=checkout_path) == "commit":
                commit_candidates.append(candidate)
        except GitCommandError:
            continue
    if len(commit_candidates) != 1:
        return ""
    return commit_candidates[0]


def dirty_tree(path: Path) -> dict[str, Any]:
    status = git(["status", "--porcelain=v1"], cwd=path)
    status_lines = [line for line in status.splitlines() if line]
    return {"dirty": bool(status_lines), "status_lines": status_lines}


def is_exact_clean_commit(path: Path, expected_commit: str) -> bool:
    try:
        head = run_trusted_read_git(["rev-parse", "HEAD"], cwd=path)
    except OSError:
        return False
    return (
        head.returncode == 0
        and head.stdout.strip() == expected_commit
        and _worktree_matches_commit(path, expected_commit)
    )


def _worktree_matches_commit(path: Path, commit: str) -> bool:
    tree = run_trusted_read_git(
        ["ls-tree", "-rz", "--full-tree", commit], cwd=path, text=False
    )
    index = run_trusted_read_git(["ls-files", "--stage", "-z"], cwd=path, text=False)
    if tree.returncode != 0 or index.returncode != 0:
        return False
    try:
        entries = _parse_tree_entries(tree.stdout)
        if _parse_index_entries(index.stdout) != {
            item_path: (mode, object_id)
            for item_path, (mode, _object_type, object_id) in entries.items()
        }:
            return False
        expected_paths = set(entries)
        gitlinks = {
            item_path
            for item_path, (mode, object_type, _object_id) in entries.items()
            if (mode, object_type) == (b"160000", b"commit")
        }
        regular_blob_ids = {
            object_id
            for mode, object_type, object_id in entries.values()
            if (mode, object_type)
            in {(b"100644", b"blob"), (b"100755", b"blob")}
        }
        blob_sizes = _git_blob_sizes(path, regular_blob_ids)
        with tempfile.TemporaryDirectory(prefix="afk-ignore-") as temporary:
            evaluation = Path(temporary)
            if not _materialize_committed_ignores(
                path, entries, blob_sizes, evaluation
            ):
                return False
            untracked_paths = (
                _worktree_paths(
                    path, gitlinks, expected_paths, evaluation
                )
                - expected_paths
            )
            if _check_ignored(evaluation, untracked_paths) != untracked_paths:
                return False
        for item_path, (mode, object_type, object_id) in entries.items():
            target = path.joinpath(*PurePosixPath(os.fsdecode(item_path)).parts)
            if (mode, object_type) in {
                (b"100644", b"blob"),
                (b"100755", b"blob"),
            }:
                if not _git_regular_file_matches(
                    target, mode, object_id, blob_sizes[object_id]
                ):
                    return False
            elif (mode, object_type) == (b"120000", b"blob"):
                target_stat = target.lstat()
                if not stat.S_ISLNK(target_stat.st_mode):
                    return False
                if _git_blob_id(os.fsencode(os.readlink(target))) != object_id:
                    return False
            elif (mode, object_type) == (b"160000", b"commit"):
                target_stat = target.lstat()
                if not stat.S_ISDIR(target_stat.st_mode) or not is_exact_clean_commit(
                    target, object_id.decode("ascii")
                ):
                    return False
            else:
                return False
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def _parse_tree_entries(
    output: bytes,
) -> dict[bytes, tuple[bytes, bytes, bytes]]:
    entries = {}
    for record in output.rstrip(b"\0").split(b"\0") if output else []:
        metadata, item_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.split()
        _require_git_path(item_path)
        if item_path in entries:
            raise ValueError("duplicate tree path")
        entries[item_path] = (mode, object_type, object_id)
    return entries


def _parse_index_entries(output: bytes) -> dict[bytes, tuple[bytes, bytes]]:
    entries = {}
    for record in output.rstrip(b"\0").split(b"\0") if output else []:
        metadata, item_path = record.split(b"\t", 1)
        mode, object_id, stage = metadata.split()
        _require_git_path(item_path)
        if stage != b"0" or item_path in entries:
            raise ValueError("unmerged index")
        entries[item_path] = (mode, object_id)
    return entries


def _require_git_path(item_path: bytes) -> None:
    relative = PurePosixPath(os.fsdecode(item_path))
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("invalid Git path")


def _worktree_paths(
    path: Path,
    gitlinks: set[bytes],
    expected_paths: set[bytes],
    ignore_evaluation: Path,
) -> set[bytes]:
    observed = set()
    tracked_directories = {
        b"/".join(item_path.split(b"/")[:depth])
        for item_path in expected_paths
        for depth in range(1, len(item_path.split(b"/")))
    }
    pending = [(path, b"")]
    while pending:
        child_directories = []
        for directory, prefix in pending:
            with os.scandir(directory) as children:
                for child in children:
                    name = os.fsencode(child.name)
                    relative = name if not prefix else prefix + b"/" + name
                    if relative == b".git":
                        continue
                    if relative in gitlinks or child.is_symlink():
                        observed.add(relative)
                    elif child.is_dir(follow_symlinks=False):
                        child_directories.append((Path(child.path), relative))
                    elif child.is_file(follow_symlinks=False):
                        observed.add(relative)
                    else:
                        raise ValueError("unsupported worktree entry")
        prunable = {
            relative + b"/"
            for _directory, relative in child_directories
            if relative not in tracked_directories
        }
        ignored_directories = {
            item_path.rstrip(b"/")
            for item_path in _check_ignored(ignore_evaluation, prunable)
        }
        pending = [
            (child_directory, relative)
            for child_directory, relative in child_directories
            if relative not in ignored_directories
        ]
    return observed


def _materialize_committed_ignores(
    candidate: Path,
    entries: dict[bytes, tuple[bytes, bytes, bytes]],
    blob_sizes: dict[bytes, int],
    evaluation: Path,
) -> bool:
    initialized = run_trusted_read_git(["init", "--quiet"], cwd=evaluation)
    if initialized.returncode != 0:
        return False
    for item_path, (mode, object_type, object_id) in entries.items():
        if (
            PurePosixPath(os.fsdecode(item_path)).name != ".gitignore"
            or (mode, object_type)
            not in {(b"100644", b"blob"), (b"100755", b"blob")}
        ):
            continue
        target = evaluation.joinpath(*PurePosixPath(os.fsdecode(item_path)).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            blob = run_trusted_read_git(
                ["cat-file", "blob", object_id.decode("ascii")],
                cwd=candidate,
                text=False,
                stdout=output,
            )
        if blob.returncode != 0 or target.stat().st_size != blob_sizes[object_id]:
            return False
    return True


def _check_ignored(evaluation: Path, item_paths: set[bytes]) -> set[bytes]:
    if not item_paths:
        return set()
    for item_path in item_paths:
        _require_git_path(item_path.rstrip(b"/"))
    checked = run_trusted_read_git(
        ["check-ignore", "--no-index", "-z", "--stdin"],
        cwd=evaluation,
        text=False,
        input_data=b"\0".join(sorted(item_paths)) + b"\0",
    )
    if checked.returncode not in {0, 1}:
        raise ValueError("ignore evaluation failed")
    ignored = {
        item_path
        for item_path in checked.stdout.rstrip(b"\0").split(b"\0")
        if item_path
    }
    if not ignored.issubset(item_paths):
        raise ValueError("invalid ignore evaluation")
    return ignored


def _git_blob_id(content: bytes) -> bytes:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest().encode(
        "ascii"
    )


def _git_blob_sizes(path: Path, object_ids: set[bytes]) -> dict[bytes, int]:
    if not object_ids:
        return {}
    result = run_trusted_read_git(
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        cwd=path,
        text=False,
        input_data=b"\n".join(sorted(object_ids)) + b"\n",
    )
    if result.returncode != 0:
        raise ValueError("blob sizes unavailable")
    sizes = {}
    for record in result.stdout.splitlines():
        object_id, object_type, size = record.split()
        if object_id not in object_ids or object_type != b"blob" or object_id in sizes:
            raise ValueError("invalid blob size response")
        sizes[object_id] = int(size)
    if sizes.keys() != object_ids:
        raise ValueError("incomplete blob size response")
    return sizes


def _git_regular_file_matches(
    path: Path, mode: bytes, object_id: bytes, expected_size: int
) -> bool:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        target_stat = os.fstat(descriptor)
        if not stat.S_ISREG(target_stat.st_mode):
            return False
        if target_stat.st_size != expected_size:
            return False
        if bool(target_stat.st_mode & stat.S_IXUSR) != (mode == b"100755"):
            return False
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(f"blob {expected_size}\0".encode("ascii"))
        with os.fdopen(descriptor, "rb", buffering=0, closefd=False) as source:
            remaining = expected_size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
            if source.read(1):
                return False
        if os.fstat(descriptor).st_size != expected_size:
            return False
    finally:
        os.close(descriptor)
    return digest.hexdigest().encode("ascii") == object_id


def run_trusted_read_git(
    args: list[str],
    *,
    cwd: Path,
    text: bool = True,
    input_data: str | bytes | None = None,
    stdout: Any = subprocess.PIPE,
) -> subprocess.CompletedProcess[Any]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=text,
            input=input_data,
            stdout=stdout,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        empty = "" if text else b""
        message = (
            "trusted Git command timed out"
            if text
            else b"trusted Git command timed out"
        )
        return subprocess.CompletedProcess(
            command, 124, stdout=empty, stderr=message
        )


def clean_reserved_checkout_artifacts(checkout_path: Path, status_lines: list[str]) -> bool:
    if status_lines != ["?? agent-result.json"]:
        return False
    result_path = checkout_path / "agent-result.json"
    if not result_path.is_file() or result_path.is_symlink():
        return False
    result_path.unlink()
    return True


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
    origin_url: str | None = None,
    branch_commit: str | None = None,
    target_commit: str | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "message": message,
        "repo_url": redact_url(str(request.get("repo_url") or "")),
        "base_ref": request.get("base_ref"),
        "requested_ref": request.get("requested_ref"),
        "checkout_root": request.get("checkout_root"),
        "review_branch": request.get("review_branch"),
        "checkout_path": request.get("checkout_path"),
        "dirty": dirty,
        "dirty_status": dirty_status or [],
        "submodules": [],
        "publication": disabled_publication(),
        "artifacts": {"publication": "publication-result.json"},
    }
    if origin_url is not None:
        result["origin_url"] = redact_url(origin_url)
    if branch_commit is not None:
        result["branch_commit"] = branch_commit
    if target_commit is not None:
        result["target_commit"] = target_commit
    return result


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
        "stderr": redact_text(exc.stderr[-2000:]),
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


def local_branch_commit(checkout_path: Path, branch: str) -> str | None:
    try:
        return git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=checkout_path)
    except GitCommandError:
        return None


def repo_urls_match(existing_url: str, requested_url: str) -> bool:
    return repo_url_identity(existing_url) == repo_url_identity(requested_url)


def repo_url_identity(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme == "file":
        return ("path", str(Path(unquote(parsed.path)).resolve(strict=False)))
    if parsed.scheme:
        netloc = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return ("url", f"{parsed.scheme.lower()}://{netloc}{path}")
    if "://" not in value and not value.startswith("git@") and ":" not in value:
        return ("path", str(Path(value).resolve(strict=False)))
    normalized = value.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return ("url", normalized)


def url_has_secret_material(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme and (parsed.username or parsed.password or parsed.query or parsed.fragment))
