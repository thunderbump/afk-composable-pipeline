from __future__ import annotations

import os
import signal
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from afk.cli import main as dispatch
from afk.run_store import RunStore, RunStoreError


LIFECYCLE_COMMANDS = {"start", "resume", "_worker", "_worker_unit", "complete"}


class LifecycleSignal(BaseException):
    def __init__(self, signal_number: int):
        self.signal_number = signal_number


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments[0] not in LIFECYCLE_COMMANDS:
        return dispatch(arguments)
    interrupted: LifecycleSignal | None = None
    with lifecycle_signal_handlers():
        try:
            return dispatch(arguments)
        except LifecycleSignal as exc:
            record_lifecycle_interruption(exc.signal_number)
            interrupted = exc
    assert interrupted is not None
    signal.signal(interrupted.signal_number, signal.SIG_DFL)
    os.kill(os.getpid(), interrupted.signal_number)
    return 128 + interrupted.signal_number


@contextmanager
def lifecycle_signal_handlers() -> Iterator[None]:
    received = False
    previous = {
        signal_number: signal.getsignal(signal_number)
        for signal_number in (signal.SIGTERM, signal.SIGINT)
    }

    def interrupt(signal_number: int, frame: Any) -> None:
        nonlocal received
        if received:
            return
        received = True
        raise LifecycleSignal(signal_number)

    try:
        for signal_number in previous:
            signal.signal(signal_number, interrupt)
        yield
    finally:
        for signal_number, handler in previous.items():
            signal.signal(signal_number, handler)


def record_lifecycle_interruption(signal_number: int) -> None:
    try:
        signal_name = signal.Signals(signal_number).name
    except ValueError:
        signal_name = str(signal_number)
    interruption = {
        "schema_version": 1,
        "status": "interrupted",
        "signal": signal_name,
    }
    try:
        store = RunStore()
        with store.lock(validate_root_permissions=True):
            projection = store.status()
            if projection["state"] == "completed":
                return
            if (
                projection["last_event"] == "lifecycle.signal_interrupted"
                and projection.get("lifecycle_interruption") == interruption
            ):
                return
            store.append_event(
                projection["run_id"],
                "lifecycle.signal_interrupted",
                state="attention_required",
                data={
                    "checkpoint": projection["checkpoint"],
                    "attention": {
                        "scope": "lifecycle",
                        "kind": "interrupted",
                        "summary": f"AFK lifecycle received {signal_name}",
                    },
                    "lifecycle_interruption": interruption,
                },
            )
    except (OSError, RunStoreError):
        return
