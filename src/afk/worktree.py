from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from afk.process_io import BoundedProcessIO


EXACT_CANDIDATE_UNTRACKED_PATH_LIMIT = 4096
EXACT_CANDIDATE_UNTRACKED_BYTES_LIMIT = 1024 * 1024
EXACT_CANDIDATE_TRACKED_PATH_LIMIT = 4096
EXACT_CANDIDATE_TRACKED_BYTES_LIMIT = 1024 * 1024
EXACT_CANDIDATE_TRACKED_DEPTH_LIMIT = 64
EXACT_CANDIDATE_IGNORE_FILE_LIMIT = 256
EXACT_CANDIDATE_IGNORE_BYTES_LIMIT = 1024 * 1024
EXACT_CANDIDATE_REPOSITORY_LIMIT = 64
_REGULAR_BLOB_KINDS = (
    (b"100644", b"blob"),
    (b"100755", b"blob"),
)


class _RepositoryBudget:
    def __init__(self, remaining: int) -> None:
        self._remaining = remaining

    def consume(self) -> bool:
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


def is_exact_clean_commit(path: Path, expected_commit: str) -> bool:
    return _is_exact_clean_commit(
        path,
        expected_commit,
        _RepositoryBudget(EXACT_CANDIDATE_REPOSITORY_LIMIT),
    )


def _is_exact_clean_commit(
    path: Path, expected_commit: str, repository_budget: _RepositoryBudget
) -> bool:
    if not repository_budget.consume():
        return False
    try:
        with _pinned_repository(path) as repository:
            head = _run_repository_git(repository, ["rev-parse", "HEAD"])
            return (
                head.returncode == 0
                and head.stdout.strip() == expected_commit
                and _worktree_matches_commit(
                    repository, expected_commit, repository_budget
                )
            )
    except (OSError, UnicodeError, ValueError):
        return False


def _worktree_matches_commit(
    repository: tuple[Path, Path],
    commit: str,
    repository_budget: _RepositoryBudget,
) -> bool:
    path, _git_directory = repository
    tree = _run_repository_git(
        repository,
        ["ls-tree", "-rz", "--full-tree", commit],
        text=False,
        output_byte_limit=_tracked_git_output_limit(),
    )
    index = _run_repository_git(
        repository,
        ["ls-files", "--stage", "-z"],
        text=False,
        output_byte_limit=_tracked_git_output_limit(),
    )
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
            if (mode, object_type) in _REGULAR_BLOB_KINDS
        }
        blob_sizes = _git_blob_sizes(repository, regular_blob_ids)
        with tempfile.TemporaryDirectory(prefix="afk-ignore-") as temporary:
            evaluation = Path(temporary)
            if not _materialize_committed_ignores(
                repository, entries, blob_sizes, evaluation
            ):
                return False
            untracked_paths = _collect_untracked_worktree_paths(
                path, gitlinks, expected_paths, evaluation
            )
            if _check_ignored(evaluation, untracked_paths) != untracked_paths:
                return False
            if not all(
                _ignored_path_is_supported(path, item_path)
                for item_path in untracked_paths
            ):
                return False
        for item_path, (mode, object_type, object_id) in entries.items():
            parent_descriptor, name = _open_parent_descriptor(path, item_path)
            try:
                if (mode, object_type) in _REGULAR_BLOB_KINDS:
                    if not _git_regular_file_matches(
                        name,
                        mode,
                        object_id,
                        blob_sizes[object_id],
                        dir_fd=parent_descriptor,
                    ):
                        return False
                elif (mode, object_type) == (b"120000", b"blob"):
                    target_stat = os.stat(
                        name, dir_fd=parent_descriptor, follow_symlinks=False
                    )
                    if not stat.S_ISLNK(target_stat.st_mode):
                        return False
                    if (
                        _git_blob_id(os.readlink(name, dir_fd=parent_descriptor))
                        != object_id
                    ):
                        return False
                elif (mode, object_type) == (b"160000", b"commit"):
                    target_descriptor = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                        dir_fd=parent_descriptor,
                    )
                    try:
                        with os.scandir(target_descriptor) as children:
                            uninitialized = next(children, None) is None
                        if not uninitialized and not _is_exact_clean_commit(
                            Path(f"/proc/self/fd/{target_descriptor}"),
                            object_id.decode("ascii"),
                            repository_budget,
                        ):
                            return False
                    finally:
                        os.close(target_descriptor)
                else:
                    return False
            finally:
                os.close(parent_descriptor)
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def _parse_tree_entries(
    output: bytes,
) -> dict[bytes, tuple[bytes, bytes, bytes]]:
    entries = {}
    for metadata, item_path in _bounded_tracked_records(output):
        mode, object_type, object_id = metadata.split()
        if item_path in entries:
            raise ValueError("duplicate tree path")
        entries[item_path] = (mode, object_type, object_id)
    return entries


def _parse_index_entries(output: bytes) -> dict[bytes, tuple[bytes, bytes]]:
    entries = {}
    for metadata, item_path in _bounded_tracked_records(output):
        mode, object_id, stage = metadata.split()
        if stage != b"0" or item_path in entries:
            raise ValueError("unmerged index")
        entries[item_path] = (mode, object_id)
    return entries


def _bounded_tracked_records(output: bytes) -> Iterator[tuple[bytes, bytes]]:
    encoded_bytes = 0
    records = output.rstrip(b"\0").split(b"\0") if output else []
    for path_count, record in enumerate(records):
        metadata, item_path = record.split(b"\t", 1)
        _require_git_path(item_path)
        encoded_bytes = _require_tracked_path_budget(
            item_path, path_count, encoded_bytes
        )
        yield metadata, item_path


def _require_git_path(item_path: bytes) -> None:
    relative = PurePosixPath(os.fsdecode(item_path))
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("invalid Git path")


def _tracked_git_output_limit() -> int:
    return (
        EXACT_CANDIDATE_TRACKED_BYTES_LIMIT
        + EXACT_CANDIDATE_TRACKED_PATH_LIMIT * 64
    )


def _require_tracked_path_budget(
    item_path: bytes, path_count: int, encoded_bytes: int
) -> int:
    next_encoded_bytes = encoded_bytes + len(item_path) + 1
    if (
        path_count >= EXACT_CANDIDATE_TRACKED_PATH_LIMIT
        or next_encoded_bytes > EXACT_CANDIDATE_TRACKED_BYTES_LIMIT
        or item_path.count(b"/") + 1 > EXACT_CANDIDATE_TRACKED_DEPTH_LIMIT
    ):
        raise ValueError("tracked Candidate metadata is too large")
    return next_encoded_bytes


def _open_parent_descriptor(path: Path, item_path: bytes) -> tuple[int, bytes]:
    parts = item_path.split(b"/")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _pinned_repository(path: Path) -> Iterator[tuple[Path, Path]]:
    root_descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    git_descriptor = None
    try:
        git_descriptor = _open_git_directory(root_descriptor)
        process = os.getpid()
        yield (
            Path(f"/proc/{process}/fd/{root_descriptor}"),
            Path(f"/proc/{process}/fd/{git_descriptor}"),
        )
    finally:
        if git_descriptor is not None:
            os.close(git_descriptor)
        os.close(root_descriptor)


def _open_git_directory(root_descriptor: int) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(
            b".git", flags | os.O_DIRECTORY, dir_fd=root_descriptor
        )
    except NotADirectoryError:
        git_file = os.open(b".git", flags, dir_fd=root_descriptor)
        try:
            if not stat.S_ISREG(os.fstat(git_file).st_mode):
                raise ValueError("invalid Git directory file")
            content = os.read(git_file, 4097)
        finally:
            os.close(git_file)
        if len(content) > 4096 or b"\0" in content:
            raise ValueError("invalid Git directory file")
        line = content.rstrip(b"\r\n")
        if not line.startswith(b"gitdir: ") or not line.removeprefix(b"gitdir: "):
            raise ValueError("invalid Git directory file")
        git_directory = Path(os.fsdecode(line.removeprefix(b"gitdir: ")))
        if not git_directory.is_absolute():
            root = Path(os.readlink(f"/proc/self/fd/{root_descriptor}"))
            git_directory = root / git_directory
        return os.open(
            git_directory, flags | os.O_DIRECTORY
        )


def _run_repository_git(
    repository: tuple[Path, Path],
    args: list[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    work_tree, git_directory = repository
    return run_trusted_read_git(
        args,
        cwd=work_tree,
        git_directory=git_directory,
        work_tree=work_tree,
        **kwargs,
    )


def _ignored_path_is_supported(path: Path, item_path: bytes) -> bool:
    parent_descriptor, name = _open_parent_descriptor(path, item_path)
    try:
        target_stat = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        return stat.S_ISREG(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode)
    finally:
        os.close(parent_descriptor)


def _collect_untracked_worktree_paths(
    path: Path,
    gitlinks: set[bytes],
    expected_paths: set[bytes],
    ignore_evaluation: Path,
) -> set[bytes]:
    observed = set()
    observed_bytes = 0

    def record_untracked(relative: bytes) -> None:
        nonlocal observed_bytes
        if relative in expected_paths or relative in observed:
            return
        encoded_size = len(relative) + 1
        if (
            len(observed) >= EXACT_CANDIDATE_UNTRACKED_PATH_LIMIT
            or observed_bytes + encoded_size > EXACT_CANDIDATE_UNTRACKED_BYTES_LIMIT
        ):
            raise ValueError("too many untracked worktree paths")
        observed.add(relative)
        observed_bytes += encoded_size

    tracked_directories = {
        b"/".join(item_path.split(b"/")[:depth])
        for item_path in expected_paths
        for depth in range(1, len(item_path.split(b"/")))
    }
    pending = [(path, b"")]
    untracked_directory_count = 0
    untracked_directory_bytes = 0
    while pending:
        child_directories = []
        for directory, prefix in pending:
            with os.scandir(directory) as children:
                for child in children:
                    name = os.fsencode(child.name)
                    relative = name if not prefix else prefix + b"/" + name
                    if relative == b".git":
                        continue
                    if child.is_symlink() and relative in tracked_directories:
                        raise ValueError("tracked worktree directory is a symlink")
                    if relative in gitlinks or child.is_symlink():
                        record_untracked(relative)
                    elif child.is_dir(follow_symlinks=False):
                        if relative not in tracked_directories:
                            untracked_directory_count += 1
                            untracked_directory_bytes += len(relative) + 2
                            if (
                                untracked_directory_count
                                > EXACT_CANDIDATE_UNTRACKED_PATH_LIMIT
                                or untracked_directory_bytes
                                > EXACT_CANDIDATE_UNTRACKED_BYTES_LIMIT
                            ):
                                raise ValueError(
                                    "too many untracked worktree directories"
                                )
                        child_directories.append((Path(child.path), relative))
                    elif child.is_file(follow_symlinks=False):
                        record_untracked(relative)
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
    repository: tuple[Path, Path],
    entries: dict[bytes, tuple[bytes, bytes, bytes]],
    blob_sizes: dict[bytes, int],
    evaluation: Path,
) -> bool:
    ignore_entries = [
        (item_path, object_id)
        for item_path, (mode, object_type, object_id) in entries.items()
        if PurePosixPath(os.fsdecode(item_path)).name == ".gitignore"
        and (mode, object_type) in _REGULAR_BLOB_KINDS
    ]
    if (
        len(ignore_entries) > EXACT_CANDIDATE_IGNORE_FILE_LIMIT
        or sum(blob_sizes[object_id] for _item_path, object_id in ignore_entries)
        > EXACT_CANDIDATE_IGNORE_BYTES_LIMIT
    ):
        return False
    initialized = run_trusted_read_git(["init", "--quiet"], cwd=evaluation)
    if initialized.returncode != 0:
        return False
    for item_path, object_id in ignore_entries:
        target = evaluation.joinpath(*PurePosixPath(os.fsdecode(item_path)).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            blob = _run_repository_git(
                repository,
                ["cat-file", "blob", object_id.decode("ascii")],
                text=False,
                stdout=output,
            )
        if blob.returncode != 0 or target.stat().st_size != blob_sizes[object_id]:
            return False
    return True


def _check_ignored(evaluation: Path, item_paths: set[bytes]) -> set[bytes]:
    if not item_paths:
        return set()
    if (
        len(item_paths) > EXACT_CANDIDATE_UNTRACKED_PATH_LIMIT
        or sum(len(item_path) + 1 for item_path in item_paths)
        > EXACT_CANDIDATE_UNTRACKED_BYTES_LIMIT
    ):
        raise ValueError("ignore evaluation input is too large")
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


def _git_blob_sizes(
    repository: tuple[Path, Path], object_ids: set[bytes]
) -> dict[bytes, int]:
    if not object_ids:
        return {}
    result = _run_repository_git(
        repository,
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
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
    path: Path | bytes,
    mode: bytes,
    object_id: bytes,
    expected_size: int,
    *,
    dir_fd: int | None = None,
) -> bool:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags, dir_fd=dir_fd)
    try:
        target_stat = os.fstat(descriptor)
        if not stat.S_ISREG(target_stat.st_mode):
            return False
        if target_stat.st_size != expected_size:
            return False
        if bool(target_stat.st_mode & stat.S_IXUSR) != (mode == b"100755"):
            return False
        initial_metadata = (
            target_stat.st_dev,
            target_stat.st_ino,
            target_stat.st_mode,
            target_stat.st_size,
            target_stat.st_mtime_ns,
            target_stat.st_ctime_ns,
        )
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
        final_stat = os.fstat(descriptor)
        if (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_mode,
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        ) != initial_metadata:
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
    output_byte_limit: int | None = None,
    git_directory: Path | None = None,
    work_tree: Path | None = None,
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
    repository_args = []
    if git_directory is not None:
        repository_args.append(f"--git-dir={git_directory}")
    if work_tree is not None:
        repository_args.append(f"--work-tree={work_tree}")
    command = ["git", *repository_args, *args]
    if output_byte_limit is not None:
        if text or stdout != subprocess.PIPE:
            raise ValueError("bounded Git output requires captured bytes")
        return _run_bounded_trusted_git(
            command,
            cwd=cwd,
            environment=environment,
            input_data=input_data,
            output_byte_limit=output_byte_limit,
        )
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


def _run_bounded_trusted_git(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    input_data: str | bytes | None,
    output_byte_limit: int,
) -> subprocess.CompletedProcess[bytes]:
    if isinstance(input_data, str):
        input_bytes = input_data.encode()
    else:
        input_bytes = input_data
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process_io = None
    try:
        process_io = BoundedProcessIO(
            process,
            input_bytes=input_bytes,
            output_byte_limit=output_byte_limit,
            cleanup_seconds=1,
            combined_output_limit=True,
        )
        deadline = time.monotonic() + 120
        stop_reason = None
        while process.poll() is None:
            stop_reason = process_io.observe(deadline)
            if stop_reason is not None:
                process.kill()
                break
        process_io.close_input()
        try:
            returncode = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
            stop_reason = stop_reason or "timeout"
        if not process_io.drain():
            stop_reason = stop_reason or "timeout"
        if stop_reason == "overflow" or process_io.overflowed:
            return subprocess.CompletedProcess(
                command, 1, stdout=b"", stderr=b"trusted Git output is too large"
            )
        if stop_reason == "timeout":
            return subprocess.CompletedProcess(
                command, 124, stdout=b"", stderr=b"trusted Git command timed out"
            )
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=bytes(process_io.captured["stdout"]),
            stderr=bytes(process_io.captured["stderr"]),
        )
    except BaseException:
        _cleanup_bounded_git_process(process, process_io)
        raise


def _cleanup_bounded_git_process(
    process: subprocess.Popen[bytes], process_io: BoundedProcessIO | None
) -> None:
    if process_io is not None:
        try:
            process_io.close_input()
        except BaseException:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except BaseException:
        try:
            process.kill()
        except BaseException:
            pass
        try:
            process.wait(timeout=1)
        except BaseException:
            pass
    if process_io is not None:
        try:
            process_io.drain()
        except BaseException:
            pass
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except BaseException:
                pass

