from __future__ import annotations

import ctypes
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afk.process_supervision import (  # noqa: E402
    SupervisedCommandError,
    _receive_protocol_message,
    _send_protocol_message,
    run_supervised_command,
)


_CLEANUP_SECONDS = 5
_RETRY_SECONDS = 0.1
_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4


_channel: socket.socket | None = None
_parent_died = False


def _interrupt(_signal_number: int, _frame: Any) -> None:
    global _parent_died
    _parent_died = True
    if _channel is not None:
        try:
            _channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _valid_configuration(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "runtime",
        "container_name",
    }:
        return False
    runtime = value.get("runtime")
    name = value.get("container_name")
    return (
        type(value.get("schema_version")) is int
        and value.get("schema_version") == 1
        and isinstance(runtime, str)
        and 0 < len(runtime) <= 4096
        and Path(runtime).is_absolute()
        and Path(runtime).name in {"docker", "podman"}
        and isinstance(name, str)
        and name.startswith("afk-candidate-")
        and len(name) == len("afk-candidate-") + 32
        and all(character in "0123456789abcdef" for character in name[14:])
    )


def _cleanup(runtime: str, container_name: str) -> int:
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        try:
            completed = run_supervised_command(
                [runtime, "rm", "--force", "--volumes", container_name],
                cwd=Path.cwd(),
                environment=os.environ.copy(),
                timeout_seconds=2,
                output_byte_limit=64 * 1024,
                cleanup_seconds=1,
                input_text=None,
                label="Candidate container watchdog cleanup",
                decode_errors="replace",
                _trusted_host_command=True,
            )
        except (OSError, SupervisedCommandError):
            pass
        else:
            if completed.returncode == 0:
                return 0
        time.sleep(_RETRY_SECONDS)
    return 2


def main() -> int:
    global _channel
    if len(sys.argv) != 3 or not sys.argv[1].isdigit() or not sys.argv[2].isdigit():
        return 2
    channel = socket.socket(fileno=int(sys.argv[1]))
    _channel = channel
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
    configuration: dict[str, object] | None = None
    try:
        configuration = _receive_protocol_message(channel)
        if not _valid_configuration(configuration):
            return 2
        _send_protocol_message(channel, {"schema_version": 1, "status": "armed"})
        message = _receive_protocol_message(channel)
        if _parent_died:
            raise EOFError("watchdog parent died")
        if message != {"schema_version": 1, "action": "disarm"}:
            raise ValueError("invalid watchdog command")
        _send_protocol_message(channel, {"schema_version": 1, "status": "disarmed"})
        return 0
    except (EOFError, OSError, ValueError):
        pass
    finally:
        _channel = None
        channel.close()
    if configuration is None or not _valid_configuration(configuration):
        return 2
    return _cleanup(str(configuration["runtime"]), str(configuration["container_name"]))


if __name__ == "__main__":
    raise SystemExit(main())
