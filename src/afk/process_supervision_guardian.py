from __future__ import annotations

import ctypes
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afk.process_supervision import (  # noqa: E402
    _LinuxDescendantSupervisor,
    _PROCESS_CLEANUP_SECONDS,
    _receive_protocol_message,
    _send_protocol_message,
)


_HELPER_PATH = Path(__file__).with_name("process_supervision_helper.py")
_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4


class _GuardianInterrupted(BaseException):
    pass


def _interrupt(_signal_number: int, _frame: Any) -> None:
    raise _GuardianInterrupted()


def _failure_response() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "supervised_error",
        "classification": "supervision_failure",
        "summary": "command supervision helper failed",
        "stdout": None,
        "stderr": None,
        "exit_code": None,
    }


def _proxy_request(channel: socket.socket, request: object) -> object:
    cleanup_seconds = _PROCESS_CLEANUP_SECONDS
    subject = "command"
    if isinstance(request, dict):
        configured_cleanup = request.get("cleanup_seconds")
        if isinstance(configured_cleanup, (int, float)) and configured_cleanup > 0:
            cleanup_seconds = float(configured_cleanup)
        configured_subject = request.get("label")
        if isinstance(configured_subject, str) and configured_subject.strip():
            subject = configured_subject.strip()
    guardian_channel, worker_channel = socket.socketpair()
    try:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(_HELPER_PATH),
                "0",
                str(os.getpid()),
            ],
            stdin=worker_channel,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.defpath,
            },
            start_new_session=True,
        )
    finally:
        worker_channel.close()
    try:
        with _LinuxDescendantSupervisor(cleanup_seconds, subject) as descendants:
            try:
                descendants.track(worker.pid)
            except BaseException:
                descendants.terminate_untracked(worker)
                raise
            _send_protocol_message(guardian_channel, request)
            response = _receive_protocol_message(guardian_channel)
            if worker.wait(timeout=5) != 0:
                raise ValueError("trusted supervision worker failed")
            return response
    finally:
        guardian_channel.close()


def main() -> int:
    if len(sys.argv) != 3 or not sys.argv[1].isdigit() or not sys.argv[2].isdigit():
        return 2
    channel = socket.socket(fileno=int(sys.argv[1]))
    signal.signal(signal.SIGTERM, _interrupt)
    parent_pid = int(sys.argv[2])
    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0
        or libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0
        or os.getppid() != parent_pid
    ):
        channel.close()
        return 2
    try:
        try:
            request = _receive_protocol_message(channel)
            response = _proxy_request(channel, request)
        except BaseException:
            response = _failure_response()
        _send_protocol_message(channel, response)
    except (OSError, ValueError):
        return 2
    finally:
        channel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
