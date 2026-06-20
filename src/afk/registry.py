from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from afk.checkouts import prepare_checkout_step
from afk.contracts import ProjectContract
from afk.implement import implement_step
from afk.jsonutil import sha256_json
from afk.work_sources import select_work_step


@dataclass(frozen=True)
class StepContext:
    input_data: Any
    run_id: str
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
            status="succeeded",
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
            "select-work": select_work_step,
        }
    )


def noop_step(context: StepContext) -> Any:
    return context.input_data
