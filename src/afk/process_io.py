from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

from afk.redaction import redact_text


def _bounded_redacted_text(
    value: bytes, byte_limit: int, *, errors: str = "strict"
) -> str:
    encoded = redact_text(value.decode("utf-8", errors=errors)).encode("utf-8")
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


class BoundedProcessIO:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        input_bytes: bytes | None,
        output_byte_limit: int,
        cleanup_seconds: float,
        combined_output_limit: bool = False,
    ) -> None:
        assert process.stdout is not None and process.stderr is not None
        self.process = process
        self.input_bytes = None if input_bytes is None else memoryview(input_bytes)
        self.input_offset = 0
        self.output_byte_limit = output_byte_limit
        self.cleanup_seconds = cleanup_seconds
        self.combined_output_limit = combined_output_limit
        self.captured = {"stdout": bytearray(), "stderr": bytearray()}
        self.captured_bytes = 0
        self.capture_lock = threading.Lock()
        self.overflow = threading.Event()
        self._reader_failed = False
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
        self.readers = [
            threading.Thread(
                target=self._capture_output,
                args=(stream, self.captured[name]),
                daemon=True,
            )
            for name, stream in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            )
        ]
        for reader in self.readers:
            reader.start()

    @property
    def overflowed(self) -> bool:
        return self.overflow.is_set()

    @property
    def reader_failed(self) -> bool:
        with self.capture_lock:
            return self._reader_failed

    def observe(self, deadline: float) -> str | None:
        if self.overflowed:
            return "overflow"
        if time.monotonic() >= deadline:
            return "timeout"
        self._feed_input()
        self.overflow.wait(min(0.01, max(deadline - time.monotonic(), 0)))
        if self.overflowed:
            return "overflow"
        if time.monotonic() >= deadline:
            return "timeout"
        return None

    def close_input(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
            self.process.stdin = None

    def drain(self) -> bool:
        deadline = time.monotonic() + self.cleanup_seconds
        for reader in self.readers:
            reader.join(timeout=max(deadline - time.monotonic(), 0))
        return not any(reader.is_alive() for reader in self.readers)

    def diagnostics(self) -> tuple[str, str]:
        return tuple(
            _bounded_redacted_text(
                bytes(self.captured[name]),
                self.output_byte_limit,
                errors="replace",
            )
            for name in ("stdout", "stderr")
        )

    def decoded_output(self) -> tuple[str, str]:
        return tuple(
            _bounded_redacted_text(bytes(self.captured[name]), self.output_byte_limit)
            for name in ("stdout", "stderr")
        )

    def _feed_input(self) -> None:
        if self.process.stdin is None:
            return
        if self.input_bytes is None or self.input_offset == len(self.input_bytes):
            self.close_input()
            return
        try:
            written = os.write(
                self.process.stdin.fileno(),
                self.input_bytes[self.input_offset :],
            )
        except BlockingIOError:
            return
        except BrokenPipeError:
            self.close_input()
            return
        self.input_offset += written
        if self.input_offset == len(self.input_bytes):
            self.close_input()

    def _capture_output(self, stream: Any, captured: bytearray) -> None:
        try:
            while chunk := os.read(stream.fileno(), 64 * 1024):
                with self.capture_lock:
                    if self.combined_output_limit:
                        self.captured_bytes += len(chunk)
                        size = self.captured_bytes
                    else:
                        size = len(captured) + len(chunk)
                    if size > self.output_byte_limit:
                        self.overflow.set()
                    elif not self.overflowed:
                        captured.extend(chunk)
        except OSError:
            with self.capture_lock:
                self._reader_failed = True
        finally:
            stream.close()
