from __future__ import annotations

import ctypes
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afk.process_supervision import (  # noqa: E402
    SupervisedCommandError,
    _decode_protocol_text,
    _encode_protocol_text,
    _receive_protocol_message,
    _run_supervised_command_local,
    _send_protocol_message,
)


_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4


class _HelperInterrupted(BaseException):
    pass


def _interrupt(_signal_number: int, _frame: Any) -> None:
    raise _HelperInterrupted()


def _execute(request: object) -> dict[str, object]:
    required = {
        "schema_version",
        "command",
        "cwd",
        "environment",
        "timeout_seconds",
        "input_text",
        "label",
        "output_byte_limit",
        "cleanup_seconds",
        "decode_errors",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("invalid supervision request")
    command = request["command"]
    environment = request["environment"]
    if (
        request["schema_version"] != 1
        or not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) for value in command)
        or not isinstance(request["cwd"], str)
        or not isinstance(environment, dict)
        or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in environment.items()
        )
        or not isinstance(request["timeout_seconds"], (int, float))
        or not isinstance(request["label"], str)
        or not isinstance(request["decode_errors"], str)
        or (
            request["output_byte_limit"] is not None
            and type(request["output_byte_limit"]) is not int
        )
        or (
            request["cleanup_seconds"] is not None
            and not isinstance(request["cleanup_seconds"], (int, float))
        )
    ):
        raise ValueError("invalid supervision request")
    try:
        completed = _run_supervised_command_local(
            command,
            cwd=Path(request["cwd"]),
            environment=environment,
            timeout_seconds=request["timeout_seconds"],
            input_text=_decode_protocol_text(request["input_text"]),
            label=request["label"],
            output_byte_limit=request["output_byte_limit"],
            cleanup_seconds=request["cleanup_seconds"],
            decode_errors=request["decode_errors"],
        )
    except OSError:
        return {"schema_version": 1, "status": "command_launch_error"}
    except SupervisedCommandError as exc:
        return {
            "schema_version": 1,
            "status": "supervised_error",
            "classification": exc.classification,
            "summary": exc.summary,
            "stdout": _encode_protocol_text(exc.stdout),
            "stderr": _encode_protocol_text(exc.stderr),
            "exit_code": exc.exit_code,
        }
    return {
        "schema_version": 1,
        "status": "completed",
        "returncode": completed.returncode,
        "stdout": _encode_protocol_text(completed.stdout),
        "stderr": _encode_protocol_text(completed.stderr),
    }


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
            response = _execute(_receive_protocol_message(channel))
        except _HelperInterrupted:
            response = {
                "schema_version": 1,
                "status": "supervised_error",
                "classification": "supervision_failure",
                "summary": "command supervision helper was interrupted",
                "stdout": None,
                "stderr": None,
                "exit_code": None,
            }
        except BaseException:
            response = {
                "schema_version": 1,
                "status": "supervised_error",
                "classification": "supervision_failure",
                "summary": "command supervision helper failed",
                "stdout": None,
                "stderr": None,
                "exit_code": None,
            }
        _send_protocol_message(channel, response)
    except (OSError, ValueError):
        return 2
    finally:
        channel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
