from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from afk.checkouts import checkout_path_error
from afk.contracts import ContractError, ProjectContract, load_project_contract
from afk.jsonutil import canonical_json, sha256_json
from afk.redaction import redact_artifact_value
from afk.recipes import (
    branch_slug,
    create_recipe_publisher,
    generate_workstream_recipe,
    real_local_recipe_agent,
    write_recipe,
)
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
        if args.profile:
            if not isinstance(input_data, dict):
                parser.error("--profile requires --input to be a JSON object")
            validation = input_data.get("validation", {})
            if validation is None:
                validation = {}
            if not isinstance(validation, dict):
                parser.error("--profile requires input.validation to be a JSON object when present")
            input_data = {
                **input_data,
                "validation": {
                    **validation,
                    "profile": args.profile,
                },
            }

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

    if args.command == "run-workstream":
        from afk.workstream import WorkstreamError, run_workstream

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
            result = run_workstream(
                input_data,
                ledger_dir=Path(args.ledger),
                step_runner=run_step,
                parent=args.parent,
                workstream_id=args.workstream_id,
                project_contract=project_contract,
            )
        except (UnknownStepError, WorkstreamError) as exc:
            parser.error(str(exc))
        print(
            canonical_json(
                {
                    "command": "run-workstream",
                    "run_id": result.run_id,
                    "workstream_id": result.workstream_id,
                    "parent": result.parent,
                    "status": result.status,
                    "publication_status": result.publication_status,
                    "result_path": result.result_path,
                }
            )
        )
        return 0

    if args.command == "generate-recipe":
        try:
            project_contract = load_project_contract(
                args.project,
                Path(args.contracts_dir),
                cwd=Path.cwd(),
            )
        except ContractError as exc:
            parser.error(str(exc))
        if args.validation_profile not in project_contract.validation_profiles:
            parser.error(
                f"--validation-profile must be declared by project {project_contract.project_slug}: "
                f"{', '.join(project_contract.validation_profiles)}"
            )
        path_error = checkout_path_error(args.checkout_root, args.checkout_path)
        if path_error is not None:
            parser.error(path_error)
        try:
            recipe_agent = recipe_agent_from_args(args, checkout_path=Path(args.checkout_path))
            recipe_publisher = recipe_publisher_from_args(
                args,
                review_branch=f"afk/{branch_slug(args.workstream_id)}",
                checkout_path=Path(args.checkout_path),
            )
        except ValueError as exc:
            parser.error(str(exc))
        try:
            recipe = generate_workstream_recipe(
                workstream_id=args.workstream_id,
                project_contract=project_contract,
                beads_workspace=Path(args.beads_workspace),
                checkout_root=Path(args.checkout_root),
                checkout_path=Path(args.checkout_path),
                validation_profile=args.validation_profile,
                agent=recipe_agent,
                publisher=recipe_publisher,
            )
        except ValueError as exc:
            parser.error(str(exc))
        output_path = Path(args.output)
        write_recipe(output_path, recipe)
        print(
            canonical_json(
                {
                    "command": "generate-recipe",
                    "workstream_id": recipe["workstream_id"],
                    "output_path": str(output_path),
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
    run_step_parser.add_argument("--profile", help="Validation profile for profile-aware steps")
    run_step_parser.add_argument("--ledger", required=True, help="Ledger output directory")
    run_step_parser.add_argument("--project", help="Project slug for contract resolution")
    run_step_parser.add_argument(
        "--contracts-dir",
        default="project-contracts",
        help="Directory containing project contract JSON files",
    )

    run_workstream_parser = subcommands.add_parser(
        "run-workstream",
        help="Run a declarative workstream recipe and terminal PR publisher",
    )
    run_workstream_parser.add_argument("--input", required=True, help="JSON workstream recipe")
    run_workstream_parser.add_argument("--ledger", required=True, help="Ledger output directory")
    run_workstream_parser.add_argument("--parent", help="Parent workstream or issue id")
    run_workstream_parser.add_argument("--workstream-id", help="Workstream id")
    run_workstream_parser.add_argument("--project", help="Project slug for contract resolution")
    run_workstream_parser.add_argument(
        "--contracts-dir",
        default="project-contracts",
        help="Directory containing project contract JSON files",
    )

    generate_recipe_parser = subcommands.add_parser(
        "generate-recipe",
        help="Generate an inspectable run-workstream recipe from a Beads work item",
    )
    generate_recipe_parser.add_argument("--workstream-id", required=True, help="Beads item/workstream id to run")
    generate_recipe_parser.add_argument("--project", required=True, help="Project slug for contract resolution")
    generate_recipe_parser.add_argument(
        "--contracts-dir",
        default="project-contracts",
        help="Directory containing project contract JSON files",
    )
    generate_recipe_parser.add_argument("--ledger", required=True, help="Ledger directory used when running the recipe")
    generate_recipe_parser.add_argument("--beads-workspace", required=True, help="Absolute mounted central Beads workspace")
    generate_recipe_parser.add_argument("--checkout-root", required=True, help="Explicit checkout root mount")
    generate_recipe_parser.add_argument("--checkout-path", required=True, help="Explicit checkout path under checkout root")
    generate_recipe_parser.add_argument("--validation-profile", required=True, help="Project validation profile name")
    generate_recipe_parser.add_argument(
        "--agent-mode",
        choices=("fake", "real-local"),
        default="fake",
        help="Implementation adapter mode to embed in the generated recipe",
    )
    generate_recipe_parser.add_argument(
        "--agent-command-json",
        help="JSON array command for real-local agent mode",
    )
    generate_recipe_parser.add_argument("--agent-codex-home", help="Absolute mounted codex home for real-local mode")
    generate_recipe_parser.add_argument("--agent-config-home", help="Absolute mounted config home for real-local mode")
    generate_recipe_parser.add_argument(
        "--agent-pi-config-home",
        help="Absolute mounted PI_CONFIG_HOME directory for real-local mode",
    )
    generate_recipe_parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        help="Optional real-local agent timeout in seconds",
    )
    generate_recipe_parser.add_argument(
        "--publisher-mode",
        choices=("disabled", "create"),
        default="disabled",
        help="Terminal publisher mode to embed in the generated recipe",
    )
    generate_recipe_parser.add_argument("--publisher-repo", help="owner/repo for publisher create mode")
    generate_recipe_parser.add_argument("--publisher-base", help="Base branch for publisher create mode")
    generate_recipe_parser.add_argument(
        "--publisher-gh-config-dir",
        help="Absolute mounted gh config dir for publisher create mode",
    )
    generate_recipe_parser.add_argument("--output", required=True, help="Path to write the JSON recipe")

    return parser


def recipe_agent_from_args(args: argparse.Namespace, *, checkout_path: Path) -> dict[str, Any] | None:
    if args.agent_mode == "fake":
        return None
    try:
        command = json.loads(args.agent_command_json)
    except TypeError:
        raise ValueError("--agent-command-json is required when --agent-mode=real-local") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"--agent-command-json must be valid JSON: {exc.msg}") from exc
    if args.agent_timeout_seconds is not None and args.agent_timeout_seconds <= 0:
        raise ValueError("--agent-timeout-seconds must be greater than zero")
    return real_local_recipe_agent(
        command=command,
        codex_home=args.agent_codex_home,
        config_home=args.agent_config_home,
        pi_config_home=args.agent_pi_config_home,
        checkout_path=checkout_path,
        timeout_seconds=args.agent_timeout_seconds,
    )


def recipe_publisher_from_args(
    args: argparse.Namespace,
    *,
    review_branch: str,
    checkout_path: Path,
) -> dict[str, Any] | None:
    if args.publisher_mode == "disabled":
        return None
    return create_recipe_publisher(
        review_branch=review_branch,
        repo=args.publisher_repo,
        base=args.publisher_base,
        gh_config_dir=args.publisher_gh_config_dir,
        checkout_path=checkout_path,
    )


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
            run_dir=ledger.run_dir,
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
    if step == "validate":
        if not isinstance(output, dict):
            return {}
        artifacts = output.get("artifacts")
        if not isinstance(artifacts, dict):
            return {}
        paths = {}
        if artifacts.get("worker_request") == "worker-request.json":
            paths["worker_request"] = "worker-request.json"
        if artifacts.get("worker_result") == "worker-result.json":
            paths["worker_result"] = "worker-result.json"
        return paths
    if step == "review":
        if not isinstance(output, dict):
            return {}
        artifacts = output.get("artifacts")
        if not isinstance(artifacts, dict):
            return {}
        paths = {}
        if artifacts.get("evidence_pack") == "evidence-pack.json":
            paths["evidence_pack"] = "evidence-pack.json"
        if artifacts.get("reviewer_request") == "reviewer-request.json":
            paths["reviewer_request"] = "reviewer-request.json"
        if artifacts.get("reviewer_result") == "reviewer-result.json":
            paths["reviewer_result"] = "reviewer-result.json"
        if artifacts.get("review_summary") == "review-summary.md":
            paths["review_summary"] = "review-summary.md"
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
