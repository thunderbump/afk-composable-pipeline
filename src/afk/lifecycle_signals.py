from __future__ import annotations

import os
import signal
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from afk.cli import build_parser, main as dispatch
from afk.run_store import RunStore, RunStoreError, new_durable_run_id


LIFECYCLE_COMMANDS = {"start", "resume", "_worker", "_worker_unit", "complete"}


class LifecycleSignal(BaseException):
    def __init__(self, signal_number: int):
        self.signal_number = signal_number


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments[0] not in LIFECYCLE_COMMANDS:
        return dispatch(arguments)
    target_run_id = lifecycle_target(arguments)
    interrupted: LifecycleSignal | None = None
    with lifecycle_signal_handlers():
        try:
            return dispatch(
                arguments,
                start_run_id=target_run_id if arguments[0] == "start" else None,
            )
        except LifecycleSignal as exc:
            record_lifecycle_interruption(target_run_id, exc.signal_number)
            interrupted = exc
    assert interrupted is not None
    signal.signal(interrupted.signal_number, signal.SIG_DFL)
    os.kill(os.getpid(), interrupted.signal_number)
    return 128 + interrupted.signal_number


def lifecycle_target(arguments: list[str]) -> str | None:
    parsed = build_parser().parse_args(arguments)
    if parsed.command == "start":
        return new_durable_run_id()
    run_id = getattr(parsed, "run_id", None)
    if run_id is not None:
        return run_id
    try:
        return RunStore().status()["run_id"]
    except RunStoreError:
        return None


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


def record_lifecycle_interruption(run_id: str | None, signal_number: int) -> None:
    if run_id is None:
        return
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
            projection = store.status(run_id)
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
