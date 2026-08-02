from __future__ import annotations

import base64
import ctypes
import json
import os
import select
import signal
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from afk.process_io import BoundedProcessIO


__all__ = ["SupervisedCommandError", "run_supervised_command"]

_OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
_PROCESS_CLEANUP_SECONDS = 1
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_SUPERVISOR_LOCK = threading.Lock()
_HELPER_PATH = Path(__file__).with_name("process_supervision_helper.py")
_PROTOCOL_BYTE_LIMIT = 256 * 1024 * 1024


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
    decode_errors: str = "strict",
    _precontained_command: bool = False,
    _trusted_host_command: bool = False,
) -> subprocess.CompletedProcess[str]:
    if _precontained_command and _trusted_host_command:
        raise SupervisedCommandError(
            "supervision_failure",
            "trusted host and pre-contained command modes are mutually exclusive",
        )
    request = {
        "schema_version": 1,
        "command": command,
        "cwd": str(cwd),
        "environment": environment,
        "timeout_seconds": timeout_seconds,
        "input_text": _encode_protocol_text(input_text),
        "label": label,
        "output_byte_limit": output_byte_limit,
        "cleanup_seconds": cleanup_seconds,
        "decode_errors": decode_errors,
    }
    try:
        parent_socket, helper_socket = socket.socketpair()
    except OSError as exc:
        raise SupervisedCommandError(
            "supervision_failure", "command supervision helper is unavailable"
        ) from exc
    try:
        try:
            helper_command = [
                sys.executable,
                "-I",
                str(_HELPER_PATH),
                "0",
                str(os.getpid()),
            ]
            if _trusted_host_command:
                pass
            elif _precontained_command:
                if not _is_precontained_bwrap_command(command):
                    raise SupervisedCommandError(
                        "supervision_failure",
                        "pre-contained command is invalid",
                    )
            else:
                bwrap = shutil.which("bwrap")
                if bwrap is None:
                    raise SupervisedCommandError(
                        "supervision_failure",
                        "command supervision containment is unavailable",
                    )
                helper_command = [
                    bwrap,
                    "--bind",
                    "/",
                    "/",
                    "--dev-bind",
                    "/dev",
                    "/dev",
                    "--unshare-pid",
                    "--proc",
                    "/proc",
                    "--die-with-parent",
                    "--",
                    sys.executable,
                    "-I",
                    str(_HELPER_PATH),
                    "0",
                    "1",
                ]
            helper = subprocess.Popen(
                helper_command,
                stdin=helper_socket,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": os.defpath,
                },
                start_new_session=True,
            )
        except OSError as exc:
            raise SupervisedCommandError(
                "supervision_failure", "command supervision helper is unavailable"
            ) from exc
        finally:
            helper_socket.close()
        try:
            effective_cleanup = (
                _PROCESS_CLEANUP_SECONDS if cleanup_seconds is None else cleanup_seconds
            )
            parent_socket.settimeout(
                max(float(timeout_seconds), 0) + 4 * effective_cleanup + 5
            )
            _send_protocol_message(parent_socket, request)
            response = _receive_protocol_message(parent_socket)
            returncode = helper.wait(timeout=5)
        except BaseException as exc:
            _terminate_helper(helper, cleanup_seconds)
            if not isinstance(exc, Exception):
                raise
            raise SupervisedCommandError(
                "supervision_failure", "command supervision helper protocol failed"
            ) from exc
        if returncode != 0:
            raise SupervisedCommandError(
                "supervision_failure", "command supervision helper failed"
            )
    finally:
        parent_socket.close()
    try:
        return _decode_helper_response(command, response)
    except (OSError, SupervisedCommandError):
        raise
    except Exception as exc:
        raise SupervisedCommandError(
            "supervision_failure", "command supervision helper protocol failed"
        ) from exc


def _is_precontained_bwrap_command(command: list[str]) -> bool:
    bwrap = shutil.which("bwrap")
    try:
        separator = command.index("--")
    except ValueError:
        return False
    options = command[:separator]
    return (
        bool(command)
        and bwrap is not None
        and command[0] == bwrap
        and "--unshare-all" in options
        and "--die-with-parent" in options
        and any(
            options[index : index + 2] == ["--proc", "/proc"]
            for index in range(len(options) - 1)
        )
    )


def _run_supervised_command_local(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    input_text: str | None = None,
    label: str,
    output_byte_limit: int | None = None,
    cleanup_seconds: float | None = None,
    decode_errors: str = "strict",
) -> subprocess.CompletedProcess[str]:
    subject = label.strip() or "command"
    output_byte_limit = (
        _OUTPUT_BYTE_LIMIT if output_byte_limit is None else output_byte_limit
    )
    cleanup_seconds = (
        _PROCESS_CLEANUP_SECONDS if cleanup_seconds is None else cleanup_seconds
    )
    with _LinuxDescendantSupervisor(cleanup_seconds, subject) as descendants:
        deadline = time.monotonic() + timeout_seconds
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
                if process_io.reader_failed:
                    raise SupervisedCommandError(
                        "supervision_failure",
                        f"{subject} output streams could not be read",
                    )
                stdout, stderr = process_io.diagnostics()
                raise SupervisedCommandError(
                    "timeout",
                    f"{subject} timed out and its process tree was terminated",
                    stdout=stdout,
                    stderr=stderr,
                )
            if stop_reason in {"overflow", "reader_failure"}:
                break
        process_io.close_input()
        descendants.terminate(process.pid)
        process.poll()
        if not process_io.drain():
            raise SupervisedCommandError(
                "supervision_failure",
                f"{subject} output streams could not be drained",
            )
        if process_io.reader_failed:
            raise SupervisedCommandError(
                "supervision_failure",
                f"{subject} output streams could not be read",
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
            stdout, stderr = process_io.decoded_output(errors=decode_errors)
        except UnicodeDecodeError as exc:
            diagnostic_stdout, diagnostic_stderr = process_io.diagnostics()
            raise SupervisedCommandError(
                "invalid_utf8",
                f"{subject} output must be UTF-8 text",
                stdout=diagnostic_stdout,
                stderr=diagnostic_stderr,
            ) from exc
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _encode_protocol_text(value: str | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode_protocol_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid protocol text")
    return base64.b64decode(value, validate=True).decode("utf-8")


def _send_protocol_message(channel: socket.socket, value: object) -> None:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > _PROTOCOL_BYTE_LIMIT:
        raise ValueError("supervision protocol message is too large")
    channel.sendall(struct.pack("!Q", len(payload)) + payload)


def _receive_protocol_message(channel: socket.socket) -> object:
    header = _receive_protocol_bytes(channel, 8)
    size = struct.unpack("!Q", header)[0]
    if size > _PROTOCOL_BYTE_LIMIT:
        raise ValueError("supervision protocol message is too large")
    return json.loads(_receive_protocol_bytes(channel, size).decode("utf-8"))


def _receive_protocol_bytes(channel: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = channel.recv(size - len(chunks))
        if not chunk:
            raise EOFError("supervision protocol message ended early")
        chunks.extend(chunk)
    return bytes(chunks)


def _terminate_helper(
    helper: subprocess.Popen[bytes], cleanup_seconds: float | None
) -> None:
    if helper.poll() is not None:
        return
    helper.terminate()
    try:
        helper.wait(timeout=4 * (cleanup_seconds or _PROCESS_CLEANUP_SECONDS) + 5)
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.wait()


def _decode_helper_response(
    command: list[str], response: object
) -> subprocess.CompletedProcess[str]:
    if not isinstance(response, dict) or response.get("schema_version") != 1:
        raise SupervisedCommandError(
            "supervision_failure", "command supervision helper protocol failed"
        )
    status = response.get("status")
    if status == "completed":
        returncode = response.get("returncode")
        stdout = _decode_protocol_text(response.get("stdout"))
        stderr = _decode_protocol_text(response.get("stderr"))
        if type(returncode) is not int or stdout is None or stderr is None:
            raise SupervisedCommandError(
                "supervision_failure", "command supervision helper protocol failed"
            )
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if status == "command_launch_error":
        raise OSError("supervised command could not be launched")
    if status != "supervised_error":
        raise SupervisedCommandError(
            "supervision_failure", "command supervision helper protocol failed"
        )
    classification = response.get("classification")
    summary = response.get("summary")
    exit_code = response.get("exit_code")
    stdout = _decode_protocol_text(response.get("stdout"))
    stderr = _decode_protocol_text(response.get("stderr"))
    if (
        not isinstance(classification, str)
        or not isinstance(summary, str)
        or (exit_code is not None and type(exit_code) is not int)
    ):
        raise SupervisedCommandError(
            "supervision_failure", "command supervision helper protocol failed"
        )
    raise SupervisedCommandError(
        classification,
        summary,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )


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
        _SUPERVISOR_LOCK.acquire()
        subreaper_enabled = False
        try:
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
            subreaper_enabled = True
            self._baseline = set(_proc_children(os.getpid(), self._subject))
            return self
        except BaseException as exc:
            try:
                if (
                    subreaper_enabled
                    and self._libc.prctl(
                        _PR_SET_CHILD_SUBREAPER, self._previous.value, 0, 0, 0
                    )
                    != 0
                ):
                    raise SupervisedCommandError(
                        "supervision_failure",
                        f"Linux {self._subject} descendant supervision was lost",
                    ) from exc
            finally:
                _SUPERVISOR_LOCK.release()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            try:
                if self._root_pid is not None and self._pidfds:
                    self.terminate(self._root_pid)
            finally:
                for pidfd in self._pidfds.values():
                    os.close(pidfd)
                self._pidfds.clear()
                if (
                    self._libc.prctl(
                        _PR_SET_CHILD_SUBREAPER, self._previous.value, 0, 0, 0
                    )
                    != 0
                ):
                    raise SupervisedCommandError(
                        "supervision_failure",
                        f"Linux {self._subject} descendant supervision was lost",
                    )
        finally:
            _SUPERVISOR_LOCK.release()

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
