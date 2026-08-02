from __future__ import annotations

import ctypes
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afk.candidate_broker import (
    CandidateBrokerError,
    run_candidate,
    trusted_runtime_environment,
    validate_candidate_request,
)
from afk.jsonutil import canonical_json


_CONFIG_BYTE_LIMIT = 1024 * 1024
_REQUEST_BYTE_LIMIT = 4 * 1024 * 1024
_PR_SET_PDEATHSIG = 1


class CandidateCapabilityError(RuntimeError):
    pass


class CandidateBrokerCapability:
    def __init__(
        self,
        *,
        candidate_path: Path,
        candidate_sha: str,
        socket_path: Path,
    ) -> None:
        self._candidate_path = candidate_path
        self._candidate_sha = candidate_sha
        self._socket_path = socket_path
        self._token = secrets.token_urlsafe(32)
        self._server: subprocess.Popen[bytes] | None = None
        self._channel: socket.socket | None = None

    def __enter__(self) -> CandidateBrokerCapability:
        try:
            parent_channel, server_channel = socket.socketpair()
        except OSError as exc:
            raise CandidateCapabilityError(
                "Candidate broker capability is unavailable"
            ) from exc
        try:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--serve",
                    str(os.getpid()),
                ],
                stdin=server_channel,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=trusted_runtime_environment(),
                start_new_session=True,
            )
        except OSError as exc:
            parent_channel.close()
            raise CandidateCapabilityError(
                "Candidate broker capability is unavailable"
            ) from exc
        finally:
            server_channel.close()
        self._server = server
        self._channel = parent_channel
        try:
            parent_channel.settimeout(5)
            _send_line(
                parent_channel,
                {
                    "schema_version": 1,
                    "candidate_path": str(self._candidate_path),
                    "candidate_sha": self._candidate_sha,
                    "socket_path": str(self._socket_path),
                    "token": self._token,
                },
            )
            if _receive_line(parent_channel, _CONFIG_BYTE_LIMIT) != {
                "schema_version": 1,
                "status": "ready",
            }:
                raise CandidateCapabilityError(
                    "Candidate broker capability failed to start"
                )
            parent_channel.settimeout(None)
            return self
        except BaseException as exc:
            self.close()
            if not isinstance(exc, Exception) or isinstance(
                exc, CandidateCapabilityError
            ):
                raise
            raise CandidateCapabilityError(
                "Candidate broker capability failed to start"
            ) from exc

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def request_value(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "socket_path": str(self._socket_path),
            "token": self._token,
        }

    @staticmethod
    def evidence_value() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "transport": "ephemeral_unix_socket",
        }

    def require_healthy(self) -> None:
        if self._server is None or self._server.poll() is not None:
            raise CandidateCapabilityError("Candidate broker capability failed")

    def close(self) -> None:
        channel, self._channel = self._channel, None
        server, self._server = self._server, None
        if channel is not None:
            channel.close()
        if server is None or server.poll() is not None:
            return
        try:
            server.terminate()
        except ProcessLookupError:
            server.wait()
            return
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


class _ServerInterrupted(BaseException):
    pass


def _interrupt(_signal_number: int, _frame: Any) -> None:
    raise _ServerInterrupted()


def _serve(parent_pid: int) -> int:
    signal.signal(signal.SIGTERM, _interrupt)
    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0
        or os.getppid() != parent_pid
    ):
        return 2
    channel = socket.socket(fileno=0)
    listener: socket.socket | None = None
    socket_path: Path | None = None
    socket_bound = False
    try:
        config = _receive_line(channel, _CONFIG_BYTE_LIMIT)
        if not _valid_config(config):
            return 2
        socket_path = Path(config["socket_path"])
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        socket_bound = True
        socket_path.chmod(0o600)
        listener.listen()
        _send_line(channel, {"schema_version": 1, "status": "ready"})
        while True:
            client, _ = listener.accept()
            with client:
                _handle_client(client, config)
    except _ServerInterrupted:
        return 0
    except (CandidateBrokerError, OSError, ValueError, json.JSONDecodeError):
        return 2
    finally:
        if listener is not None:
            listener.close()
        if socket_bound and socket_path is not None:
            socket_path.unlink(missing_ok=True)
        channel.close()


def _handle_client(client: socket.socket, config: dict[str, Any]) -> None:
    try:
        value = _receive_line(client, _REQUEST_BYTE_LIMIT)
        if not isinstance(value, dict) or not secrets.compare_digest(
            str(value.get("token", "")), config["token"]
        ):
            raise ValueError("invalid Candidate broker capability request")
        request = dict(value)
        request.pop("token", None)
        request["candidate_sha"] = config["candidate_sha"]
        request["candidate_path"] = config["candidate_path"]
        result = run_candidate(validate_candidate_request(request))
    except (CandidateBrokerError, OSError, ValueError, json.JSONDecodeError):
        result = {
            "schema_version": 1,
            "status": "invalid_request",
            "summary": "Candidate broker capability request is invalid",
        }
    _send_line(client, result)


def _valid_config(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value)
        == {
            "schema_version",
            "candidate_path",
            "candidate_sha",
            "socket_path",
            "token",
        }
        and value.get("schema_version") == 1
        and all(
            isinstance(value.get(name), str) and bool(value[name])
            for name in ("candidate_path", "candidate_sha", "socket_path", "token")
        )
        and Path(value["candidate_path"]).is_absolute()
        and Path(value["socket_path"]).is_absolute()
    )


def _send_line(channel: socket.socket, value: object) -> None:
    channel.sendall((canonical_json(value) + "\n").encode("utf-8"))


def _receive_line(channel: socket.socket, byte_limit: int) -> object:
    with channel.makefile("rb") as stream:
        payload = stream.readline(byte_limit + 1)
    if not payload.endswith(b"\n") or len(payload) > byte_limit:
        raise ValueError("Candidate broker capability message is invalid")
    return json.loads(payload.decode("utf-8"))


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--serve" or not sys.argv[2].isdigit():
        return 2
    return _serve(int(sys.argv[2]))


if __name__ == "__main__":
    raise SystemExit(main())
