from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from afk.checkouts import prepare_checkout_step
from afk.contracts import ProjectContract
from afk.implement import implement_step
from afk.jsonutil import sha256_json
from afk.review import review_step
from afk.validation import validate_step
from afk.work_sources import select_work_step


SUCCESSFUL_STEP_OUTPUT_STATUSES = {
    "prepared",
    "implemented",
    "passed",
    "published",
    "request_revision",
    "selected",
    "skip",
    "skipped",
    "skipped_disabled",
    "succeeded",
    "success",
    "validated",
}


@dataclass(frozen=True)
class StepContext:
    input_data: Any
    run_id: str
    run_dir: Path | None = None
    project_contract: ProjectContract | None = None


@dataclass(frozen=True)
class StepResult:
    run_id: str
    step: str
    status: str
    output: Any
    stdout: str
    stderr: str
    result_sha256: str


class StepHandler(Protocol):
    def __call__(self, context: StepContext) -> Any:
        pass


class UnknownStepError(ValueError):
    def __init__(self, step: str, known_steps: tuple[str, ...]):
        known = ", ".join(known_steps) if known_steps else "(none)"
        super().__init__(f"unknown step {step!r}; known steps: {known}")


class StepRegistry:
    def __init__(self, steps: Mapping[str, StepHandler]):
        self._steps = dict(steps)

    @property
    def step_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._steps))

    def require_known_step(self, step: str) -> None:
        if step not in self._steps:
            raise UnknownStepError(step, self.step_names)

    def run(self, step: str, context: StepContext) -> StepResult:
        self.require_known_step(step)
        handler = self._steps[step]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            output = handler(context)

        return StepResult(
            run_id=context.run_id,
            step=step,
            status=top_level_step_status(output),
            output=output,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            result_sha256=sha256_json(output),
        )


def default_step_registry() -> StepRegistry:
    return StepRegistry(
        {
            "implement": implement_step,
            "noop": noop_step,
            "prepare-checkout": prepare_checkout_step,
            "review": review_step,
            "select-work": select_work_step,
            "validate": validate_step,
        }
    )


def noop_step(context: StepContext) -> Any:
    return context.input_data


def top_level_step_status(output: Any) -> str:
    if not isinstance(output, dict):
        return "succeeded"
    raw_status = output.get("status")
    if not isinstance(raw_status, str):
        return "succeeded"
    status = raw_status.strip()
    if not status:
        return "succeeded"
    if status in SUCCESSFUL_STEP_OUTPUT_STATUSES:
        return "succeeded"
    if status.startswith("skipped_"):
        return "succeeded"
    if status.startswith("failed_") or status in {"error", "failed", "fail"}:
        return "failed"
    return "failed"
