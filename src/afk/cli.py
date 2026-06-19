from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-step":
        try:
            input_data = json.loads(args.input)
        except json.JSONDecodeError as exc:
            parser.error(f"--input must be valid JSON: {exc.msg}")

        result = run_step(args.step, input_data, Path(args.ledger))
        print(
            canonical_json(
                {
                    "run_id": result.run_id,
                    "step": result.step,
                    "status": result.status,
                    "result_path": f"runs/{result.run_id}/step-result.json",
                }
            )
        )
        return 0

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="afk")
    subcommands = parser.add_subparsers(dest="command")

    run_step_parser = subcommands.add_parser("run-step", help="Run one pipeline step")
    run_step_parser.add_argument("step", choices=["noop"])
    run_step_parser.add_argument("--input", required=True, help="JSON input payload")
    run_step_parser.add_argument("--ledger", required=True, help="Ledger output directory")

    return parser


def run_step(step: str, input_data: Any, ledger_dir: Path) -> "StepResult":
    run_id = new_run_id()
    ledger = RunLedger(ledger_dir, run_id)
    runner = StepRunner({"noop": noop_step})

    input_sha256 = sha256_json(input_data)
    ledger.prepare()
    ledger.write_command(step, input_data, input_sha256)
    ledger.append_event("run.started", step=step, input_sha256=input_sha256)
    ledger.append_event("step.started", step=step)

    result = runner.run(step, StepContext(input_data=input_data, run_id=run_id))
    ledger.write_logs(result.stdout, result.stderr)
    ledger.write_result(result, input_sha256)
    ledger.append_event(
        "step.completed",
        step=step,
        status=result.status,
        result_path="step-result.json",
        result_sha256=result.result_sha256,
        stdout_path="stdout.log",
        stderr_path="stderr.log",
    )
    ledger.append_event("run.completed", step=step, status=result.status)
    return result


@dataclass(frozen=True)
class StepContext:
    input_data: Any
    run_id: str


@dataclass(frozen=True)
class StepResult:
    run_id: str
    step: str
    status: str
    output: Any
    stdout: str
    stderr: str
    result_sha256: str


class StepRunner:
    def __init__(self, steps: dict[str, Callable[[StepContext], Any]]):
        self._steps = steps

    def run(self, step: str, context: StepContext) -> StepResult:
        handler = self._steps[step]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            output = handler(context)

        return StepResult(
            run_id=context.run_id,
            step=step,
            status="succeeded",
            output=output,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            result_sha256=sha256_json(output),
        )


class RunLedger:
    def __init__(self, ledger_dir: Path, run_id: str):
        self.run_id = run_id
        self.run_dir = ledger_dir / "runs" / run_id
        self.ledger_path = self.run_dir / "ledger.jsonl"

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=False)

    def write_command(self, step: str, input_data: Any, input_sha256: str) -> None:
        self.write_json(
            "command.json",
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "created_at": utc_now(),
                "command": ["afk", "run-step", step],
                "step": step,
                "input": input_data,
                "input_sha256": input_sha256,
            },
        )

    def write_logs(self, stdout: str, stderr: str) -> None:
        (self.run_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (self.run_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    def write_result(self, result: StepResult, input_sha256: str) -> None:
        self.write_json(
            "step-result.json",
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": result.run_id,
                "step": result.step,
                "status": result.status,
                "input_sha256": input_sha256,
                "output": result.output,
                "result_sha256": result.result_sha256,
            },
        )

    def append_event(self, event: str, **fields: Any) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "timestamp": utc_now(),
            "event": event,
            **fields,
        }
        with self.ledger_path.open("a", encoding="utf-8") as ledger_file:
            ledger_file.write(canonical_json(payload))
            ledger_file.write("\n")

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        (self.run_dir / name).write_text(canonical_json(payload) + "\n", encoding="utf-8")


def noop_step(context: StepContext) -> Any:
    return context.input_data


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
