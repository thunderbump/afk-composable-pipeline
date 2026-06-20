from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from afk.contracts import ContractError, ProjectContract, load_project_contract
from afk.jsonutil import canonical_json, sha256_json
from afk.redaction import redact_artifact_value
from afk.registry import (
    StepContext,
    StepRegistry,
    StepResult,
    UnknownStepError,
    default_step_registry,
)


SCHEMA_VERSION = 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run-step":
        try:
            input_data = json.loads(args.input)
        except json.JSONDecodeError as exc:
            parser.error(f"--input must be valid JSON: {exc.msg}")

        project_contract = None
        if args.project:
            try:
                project_contract = load_project_contract(
                    args.project,
                    Path(args.contracts_dir),
                    cwd=Path.cwd(),
                )
            except ContractError as exc:
                parser.error(str(exc))

        try:
            result = run_step(args.step, input_data, Path(args.ledger), project_contract)
        except UnknownStepError as exc:
            parser.error(str(exc))
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
    run_step_parser.add_argument("step")
    run_step_parser.add_argument("--input", required=True, help="JSON input payload")
    run_step_parser.add_argument("--ledger", required=True, help="Ledger output directory")
    run_step_parser.add_argument("--project", help="Project slug for contract resolution")
    run_step_parser.add_argument(
        "--contracts-dir",
        default="project-contracts",
        help="Directory containing project contract JSON files",
    )

    return parser


def run_step(
    step: str,
    input_data: Any,
    ledger_dir: Path,
    project_contract: ProjectContract | None = None,
    registry: StepRegistry | None = None,
) -> StepResult:
    registry = registry or default_step_registry()
    registry.require_known_step(step)

    run_id = new_run_id()
    ledger = RunLedger(ledger_dir, run_id)

    input_sha256 = sha256_json(input_data)
    ledger.prepare()
    ledger.write_command(step, input_data, input_sha256, project_contract)
    ledger.append_event(
        "run.started",
        step=step,
        input_sha256=input_sha256,
        **project_contract_fields(project_contract),
    )
    ledger.append_event("step.started", step=step)

    result = registry.run(
        step,
        StepContext(
            input_data=input_data,
            run_id=run_id,
            project_contract=project_contract,
        ),
    )
    ledger.write_logs(result.stdout, result.stderr)
    ledger.write_result(result, input_sha256, project_contract)
    artifact_paths = result_artifact_paths(result.step, result.output)
    ledger.append_event(
        "step.completed",
        step=step,
        status=result.status,
        result_path="step-result.json",
        result_sha256=result.result_sha256,
        stdout_path="stdout.log",
        stderr_path="stderr.log",
        artifacts=artifact_paths,
    )
    ledger.append_event("run.completed", step=step, status=result.status)
    return result


class RunLedger:
    def __init__(self, ledger_dir: Path, run_id: str):
        self.run_id = run_id
        self.run_dir = ledger_dir / "runs" / run_id
        self.ledger_path = self.run_dir / "ledger.jsonl"

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=False)

    def write_command(
        self,
        step: str,
        input_data: Any,
        input_sha256: str,
        project_contract: ProjectContract | None,
    ) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": utc_now(),
            "command": ["afk", "run-step", step],
            "step": step,
            "input": redact_artifact_value(input_data),
            "input_sha256": input_sha256,
            **project_contract_fields(project_contract),
        }
        self.write_json("command.json", payload)

    def write_logs(self, stdout: str, stderr: str) -> None:
        (self.run_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (self.run_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    def write_result(
        self,
        result: StepResult,
        input_sha256: str,
        project_contract: ProjectContract | None,
    ) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": result.run_id,
            "step": result.step,
            "status": result.status,
            "input_sha256": input_sha256,
            "output": result.output,
            "result_sha256": result.result_sha256,
            **project_contract_fields(project_contract),
        }
        self.write_json("step-result.json", payload)
        artifact_paths = result_artifact_paths(result.step, result.output)
        if artifact_paths.get("publication") and isinstance(result.output, dict):
            self.write_json(
                artifact_paths["publication"],
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": result.run_id,
                    "step": result.step,
                    "artifact_type": "checkout-publication",
                    "output": result.output.get("publication"),
                },
            )
        if result.step == "implement" and isinstance(result.output, dict):
            if artifact_paths.get("job_capsule"):
                self.write_json(
                    artifact_paths["job_capsule"],
                    {
                        "schema_version": SCHEMA_VERSION,
                        "run_id": result.run_id,
                        "step": result.step,
                        "artifact_type": "job-capsule",
                        "capsule": result.output.get("job_capsule"),
                    },
                )
            if artifact_paths.get("agent_result"):
                self.write_json(
                    artifact_paths["agent_result"],
                    {
                        "schema_version": SCHEMA_VERSION,
                        "run_id": result.run_id,
                        "step": result.step,
                        "artifact_type": "agent-result",
                        "result": result.output.get("agent_result"),
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


def project_contract_fields(project_contract: ProjectContract | None) -> dict[str, Any]:
    if project_contract is None:
        return {}
    return {
        "project": project_contract.project_slug,
        "project_contract": project_contract.identity.as_json(),
    }


def result_artifact_paths(step: str, output: Any) -> dict[str, str]:
    if step == "implement":
        if not isinstance(output, dict):
            return {}
        artifacts = output.get("artifacts")
        if not isinstance(artifacts, dict):
            return {}
        paths = {}
        if artifacts.get("job_capsule") == "job-capsule.json":
            paths["job_capsule"] = "job-capsule.json"
        if artifacts.get("agent_result") == "agent-result.json":
            paths["agent_result"] = "agent-result.json"
        return paths
    if step != "prepare-checkout":
        return {}
    if not isinstance(output, dict):
        return {}
    artifacts = output.get("artifacts")
    if not isinstance(artifacts, dict):
        return {}
    publication = artifacts.get("publication")
    if publication == "publication-result.json":
        return {"publication": publication}
    return {}


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
