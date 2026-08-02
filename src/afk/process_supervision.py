from __future__ import annotations

import ctypes
import os
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from afk.process_io import BoundedProcessIO


__all__ = ["SupervisedCommandError", "run_supervised_command"]

_OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
_PROCESS_CLEANUP_SECONDS = 1
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


class SupervisedCommandError(RuntimeError):
    def __init__(
        self,
        classification: str,
        summary: str,
        *,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(summary)
        self.classification = classification
        self.summary = summary
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def run_supervised_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    input_text: str | None = None,
    label: str,
    output_byte_limit: int | None = None,
    cleanup_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    subject = label.strip() or "command"
    output_byte_limit = (
        _OUTPUT_BYTE_LIMIT if output_byte_limit is None else output_byte_limit
    )
    cleanup_seconds = (
        _PROCESS_CLEANUP_SECONDS if cleanup_seconds is None else cleanup_seconds
    )
    deadline = time.monotonic() + timeout_seconds
    with _LinuxDescendantSupervisor(cleanup_seconds, subject) as descendants:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            descendants.track(process.pid)
        except SupervisedCommandError:
            descendants.terminate_untracked(process)
            raise
        process_io = BoundedProcessIO(
            process,
            input_bytes=None if input_text is None else input_text.encode("utf-8"),
            output_byte_limit=output_byte_limit,
            cleanup_seconds=cleanup_seconds,
        )
        while process.poll() is None:
            descendants.discover(process.pid)
            stop_reason = process_io.observe(deadline)
            if stop_reason == "timeout":
                process_io.close_input()
                descendants.terminate(process.pid)
                process.poll()
                if not process_io.drain():
                    raise SupervisedCommandError(
                        "supervision_failure",
                        f"{subject} output streams could not be drained",
                    )
                stdout, stderr = process_io.diagnostics()
                raise SupervisedCommandError(
                    "timeout",
                    f"{subject} timed out and its process tree was terminated",
                    stdout=stdout,
                    stderr=stderr,
                )
            if stop_reason == "overflow":
                break
        process_io.close_input()
        descendants.terminate(process.pid)
        process.poll()
        if not process_io.drain():
            raise SupervisedCommandError(
                "supervision_failure",
                f"{subject} output streams could not be drained",
            )
        if process_io.overflowed:
            stdout, stderr = process_io.diagnostics()
            raise SupervisedCommandError(
                "output_overflow",
                f"{subject} output exceeds the size limit",
                stdout=stdout,
                stderr=stderr,
            )
        if process.returncode < 0:
            signal_number = -process.returncode
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = str(signal_number)
            stdout, stderr = process_io.diagnostics()
            raise SupervisedCommandError(
                "abnormal_exit",
                f"{subject} exited after signal {signal_name}",
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
            )
        try:
            stdout, stderr = process_io.decoded_output()
        except UnicodeDecodeError as exc:
            diagnostic_stdout, diagnostic_stderr = process_io.diagnostics()
            raise SupervisedCommandError(
                "invalid_utf8",
                f"{subject} output must be UTF-8 text",
                stdout=diagnostic_stdout,
                stderr=diagnostic_stderr,
            ) from exc
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class _LinuxDescendantSupervisor:
    def __init__(self, cleanup_seconds: float, subject: str) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._previous = ctypes.c_int()
        self._baseline: set[int] = set()
        self._pidfds: dict[int, int] = {}
        self._root_pid: int | None = None
        self._cleanup_seconds = cleanup_seconds
        self._subject = subject

    def __enter__(self) -> _LinuxDescendantSupervisor:
        if (
            self._libc.prctl(
                _PR_GET_CHILD_SUBREAPER, ctypes.byref(self._previous), 0, 0, 0
            )
            != 0
            or self._libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0
        ):
            raise SupervisedCommandError(
                "supervision_unavailable",
                f"Linux {self._subject} descendant supervision is unavailable",
            )
        self._baseline = set(_proc_children(os.getpid(), self._subject))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if self._root_pid is not None and self._pidfds:
                self.terminate(self._root_pid)
        finally:
            for pidfd in self._pidfds.values():
                os.close(pidfd)
            self._pidfds.clear()
            if (
                self._libc.prctl(_PR_SET_CHILD_SUBREAPER, self._previous.value, 0, 0, 0)
                != 0
            ):
                raise SupervisedCommandError(
                    "supervision_failure",
                    f"Linux {self._subject} descendant supervision was lost",
                )

    def track(self, pid: int) -> None:
        self._root_pid = pid
        self._track(pid)

    def discover(self, root_pid: int) -> None:
        pending = [
            root_pid,
            *(
                pid
                for pid in _proc_children(os.getpid(), self._subject)
                if pid not in self._baseline
            ),
        ]
        seen: set[int] = set()
        while pending:
            pid = pending.pop()
            if pid in seen or pid == os.getpid():
                continue
            seen.add(pid)
            self._track(pid)
            pending.extend(_proc_children(pid, self._subject))

    def terminate(self, root_pid: int) -> None:
        if self._wait_for_exit(root_pid, signal.SIGTERM):
            return
        if self._wait_for_exit(root_pid, signal.SIGKILL):
            return
        raise SupervisedCommandError(
            "supervision_failure",
            f"{self._subject} process tree could not be terminated",
        )

    def terminate_untracked(self, process: subprocess.Popen[bytes]) -> None:
        failure: OSError | None = None
        deadline = time.monotonic() + self._cleanup_seconds
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                failure = exc
            while time.monotonic() < deadline:
                process.poll()
                children = [
                    pid
                    for pid in _proc_children(os.getpid(), self._subject)
                    if pid not in self._baseline
                ]
                for pid in children:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError as exc:
                        failure = failure or exc
                for pid in children:
                    if pid == process.pid:
                        continue
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                    except OSError as exc:
                        failure = failure or exc
                process.poll()
                remaining = [
                    pid
                    for pid in _proc_children(os.getpid(), self._subject)
                    if pid not in self._baseline
                ]
                if process.returncode is not None and not remaining:
                    if failure is None:
                        return
                    break
                time.sleep(0.01)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise SupervisedCommandError(
            "supervision_failure", "untracked process tree could not be terminated"
        ) from failure

    def _wait_for_exit(self, root_pid: int, requested_signal: signal.Signals) -> bool:
        deadline = time.monotonic() + self._cleanup_seconds
        while time.monotonic() < deadline:
            self.discover(root_pid)
            self._discard_exited()
            if not self._pidfds:
                self.discover(root_pid)
                self._discard_exited()
                if not self._pidfds:
                    return True
            for pidfd in tuple(self._pidfds.values()):
                try:
                    signal.pidfd_send_signal(pidfd, requested_signal)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    raise SupervisedCommandError(
                        "supervision_failure",
                        f"{self._subject} process tree could not be signalled",
                    ) from exc
            time.sleep(0.01)
        return False

    def _track(self, pid: int) -> None:
        if pid in self._pidfds:
            return
        try:
            self._pidfds[pid] = os.pidfd_open(pid)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise SupervisedCommandError(
                "supervision_unavailable",
                f"Linux {self._subject} descendant supervision is unavailable",
            ) from exc

    def _discard_exited(self) -> None:
        if not self._pidfds:
            return
        poller = select.poll()
        for pidfd in self._pidfds.values():
            poller.register(pidfd, select.POLLIN)
        readable = {pidfd for pidfd, _ in poller.poll(0)}
        for pid, pidfd in tuple(self._pidfds.items()):
            if pidfd not in readable:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            os.close(pidfd)
            del self._pidfds[pid]


def _proc_children(pid: int, subject: str) -> list[int]:
    children: set[int] = set()
    try:
        tasks = list(Path(f"/proc/{pid}/task").iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SupervisedCommandError(
            "supervision_unavailable",
            f"Linux {subject} descendant supervision is unavailable",
        ) from exc
    for task in tasks:
        try:
            values = (task / "children").read_text(encoding="utf-8").split()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SupervisedCommandError(
                "supervision_unavailable",
                f"Linux {subject} descendant supervision is unavailable",
            ) from exc
        children.update(int(value) for value in values if value.isdigit())
    return sorted(children)
