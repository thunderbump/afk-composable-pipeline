from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from afk.checkouts import checkout_path_error
from afk.contracts import ContractError, ProjectContract, load_project_contract
from afk.jsonutil import canonical_json, sha256_json
from afk.redaction import redact_artifact_value
from afk.recipes import (
    branch_slug,
    create_recipe_publisher,
    default_worker_code,
    generate_workstream_recipe,
    real_local_recipe_agent,
    write_recipe,
)
from afk.run_next import run_next
from afk.pi_workers import (
    PONYTAIL_EXTENSION_SOURCE,
    build_pi_mount_config,
    build_provider_pi_mount_config,
    build_pi_real_worker_agent,
    build_pi_print_command,
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
            validation_input = recipe_validation_input_from_args(args, project_contract=project_contract)
            recipe_agent = recipe_agent_from_args(args, checkout_path=Path(args.checkout_path))
            reviewer = recipe_reviewer_from_args(args, checkout_path=Path(args.checkout_path))
            retrospective_judge = recipe_retrospective_judge_from_args(args, checkout_path=Path(args.checkout_path))
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
                validation_input=validation_input,
                agent=recipe_agent,
                reviewer=reviewer,
                retrospective_judge=retrospective_judge,
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

    if args.command == "run-next":
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
        if args.execute and not args.ledger:
            parser.error("--ledger is required when --execute is set")
        try:
            validation_input = recipe_validation_input_from_args(args, project_contract=project_contract)
            recipe_agent = recipe_agent_from_args(args, checkout_path=Path(args.checkout_path))
            reviewer = recipe_reviewer_from_args(args, checkout_path=Path(args.checkout_path))
            retrospective_judge = recipe_retrospective_judge_from_args(args, checkout_path=Path(args.checkout_path))
            recipe_publisher_factory = recipe_publisher_factory_from_args(
                args,
                checkout_path=Path(args.checkout_path),
            )
            workstream_runner = None
            if args.execute:
                from afk.workstream import run_workstream

                workstream_runner = lambda recipe, *, ledger_dir, project_contract: run_workstream(
                    recipe,
                    ledger_dir=ledger_dir,
                    step_runner=run_step,
                    project_contract=project_contract,
                )
            payload = run_next(
                project_contract=project_contract,
                beads_workspace=Path(args.beads_workspace),
                checkout_root=Path(args.checkout_root),
                checkout_path=Path(args.checkout_path),
                validation_profile=args.validation_profile,
                validation_input=validation_input,
                agent=recipe_agent,
                reviewer=reviewer,
                retrospective_judge=retrospective_judge,
                publisher_factory=recipe_publisher_factory,
                ready_tag=args.ready_tag,
                selector_mode=args.selector_mode,
                selector_model=args.selector_model,
                selector_choice_json=args.selector_choice_json,
                execute=args.execute,
                ledger_dir=Path(args.ledger) if args.ledger else None,
                workstream_runner=workstream_runner,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(canonical_json(payload))
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
        "--validation-mode",
        choices=("fake", "project-worker"),
        default="fake",
        help="Validation adapter mode to embed in the generated recipe",
    )
    generate_recipe_parser.add_argument(
        "--validation-timeout-seconds",
        type=int,
        help="Optional validation timeout in seconds",
    )
    generate_recipe_parser.add_argument(
        "--validation-stack-path",
        help=(
            "Absolute validation stack path for project-worker recipes. "
            "Overrides the default host sibling contract when checkout_root is a nested mount."
        ),
    )
    add_implementation_agent_flags(generate_recipe_parser)
    add_reviewer_flags(generate_recipe_parser)
    add_retrospective_judge_flags(generate_recipe_parser)
    add_publisher_flags(generate_recipe_parser)
    generate_recipe_parser.add_argument("--output", required=True, help="Path to write the JSON recipe")

    run_next_parser = subcommands.add_parser(
        "run-next",
        help="Discover the next project item and emit an inspectable run-workstream recipe",
    )
    run_next_parser.add_argument("--project", required=True, help="Project slug for contract resolution")
    run_next_parser.add_argument(
        "--contracts-dir",
        default="project-contracts",
        help="Directory containing project contract JSON files",
    )
    run_next_parser.add_argument(
        "--beads-workspace",
        required=True,
        help="Absolute mounted central Beads workspace",
    )
    run_next_parser.add_argument("--checkout-root", required=True, help="Explicit checkout root mount")
    run_next_parser.add_argument("--checkout-path", required=True, help="Explicit checkout path under checkout root")
    run_next_parser.add_argument("--validation-profile", required=True, help="Project validation profile name")
    run_next_parser.add_argument(
        "--validation-mode",
        choices=("fake", "project-worker"),
        default="fake",
        help="Validation adapter mode to embed in the generated recipe",
    )
    run_next_parser.add_argument(
        "--validation-timeout-seconds",
        type=int,
        help="Optional validation timeout in seconds",
    )
    run_next_parser.add_argument(
        "--validation-stack-path",
        help=(
            "Absolute validation stack path for project-worker recipes. "
            "Overrides the default host sibling contract when checkout_root is a nested mount."
        ),
    )
    run_next_parser.add_argument(
        "--ready-tag",
        default="ready-for-agent",
        help="Ready tag required on issues considered for autonomous selection",
    )
    run_next_parser.add_argument(
        "--selector-mode",
        choices=("deterministic", "model"),
        default="deterministic",
        help="Selector policy for choosing among valid candidates",
    )
    run_next_parser.add_argument(
        "--selector-model",
        help="Optional lightweight selector model name",
    )
    run_next_parser.add_argument(
        "--selector-choice-json",
        help="Optional JSON selector choice payload for model mode",
    )
    run_next_parser.add_argument("--ledger", help="Optional ledger directory for downstream execution")
    run_next_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the selected recipe through run-workstream after selection",
    )
    add_implementation_agent_flags(run_next_parser)
    add_reviewer_flags(run_next_parser)
    add_retrospective_judge_flags(run_next_parser)
    add_publisher_flags(run_next_parser)

    return parser


def add_implementation_agent_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent-mode",
        choices=("fake", "real-local", "pi"),
        default="fake",
        help="Implementation adapter mode to embed in the generated recipe",
    )
    parser.add_argument(
        "--agent-command-json",
        help="JSON array command for real-local agent mode",
    )
    parser.add_argument(
        "--agent-codex-home",
        help="Absolute mounted codex home for real-local and pi modes",
    )
    parser.add_argument(
        "--agent-config-home",
        help="Absolute mounted config home for real-local and pi modes",
    )
    parser.add_argument(
        "--agent-pi-config-home",
        help="Absolute mounted PI_CONFIG_HOME directory for real-local and pi modes",
    )
    parser.add_argument(
        "--agent-pi-coding-agent-dir",
        help="Absolute mounted PI_CODING_AGENT_DIR directory for pi mode Codex subscription auth",
    )
    parser.add_argument(
        "--agent-timeout-seconds",
        type=int,
        help="Optional real-local and pi agent timeout in seconds",
    )
    parser.add_argument("--agent-pi-bin", default="pi", help="Pi binary for pi mode")
    parser.add_argument(
        "--agent-pi-provider",
        default="openai-codex",
        help="Pi provider for pi mode",
    )
    parser.add_argument(
        "--agent-pi-model",
        default="gpt-5.4-mini",
        help="Pi model for pi mode (gpt-5.4 or lower)",
    )
    parser.add_argument("--agent-pi-thinking", help="Optional Pi thinking level")
    parser.add_argument(
        "--agent-ponytail",
        action="store_true",
        help="Enable the default ponytail extension in pi mode",
    )
    parser.add_argument(
        "--agent-ponytail-extension",
        help="Ponytail extension package name for pi mode",
    )
    parser.add_argument(
        "--agent-ponytail-extension-source",
        help="Ponytail extension source string for pi mode",
    )
    parser.add_argument(
        "--agent-wrapper-secret-file",
        help="Path to wrapper secret file for pi mode",
    )


def add_reviewer_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reviewer-mode",
        choices=("fake", "pi"),
        default="fake",
        help="Reviewer mode to embed in the generated recipe",
    )
    parser.add_argument("--reviewer-timeout-seconds", type=int, help="Optional reviewer timeout in seconds")
    parser.add_argument("--reviewer-pi-bin", default="pi", help="Reviewer Pi binary for pi mode")
    parser.add_argument(
        "--reviewer-pi-provider",
        default="openai-codex",
        help="Reviewer Pi provider for pi mode",
    )
    parser.add_argument(
        "--reviewer-pi-model",
        default="gpt-5.4-mini",
        help="Reviewer Pi model for pi mode (gpt-5.4 or lower)",
    )
    parser.add_argument("--reviewer-pi-thinking", help="Optional reviewer Pi thinking level")
    parser.add_argument(
        "--reviewer-ponytail",
        action="store_true",
        help="Enable default ponytail extension for reviewer pi mode",
    )
    parser.add_argument("--reviewer-ponytail-extension", help="Reviewer ponytail extension package name for pi mode")
    parser.add_argument(
        "--reviewer-ponytail-extension-source",
        help="Reviewer ponytail extension source string for pi mode",
    )


def add_retrospective_judge_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--retrospective-judge-mode",
        choices=("disabled", "pi"),
        default="disabled",
        help="Retrospective judge mode to embed in generated recipe",
    )
    parser.add_argument(
        "--retrospective-judge-timeout-seconds",
        type=int,
        help="Optional retrospective judge timeout in seconds",
    )
    parser.add_argument(
        "--retrospective-judge-pi-bin",
        default="pi",
        help="Retrospective judge Pi binary for pi mode",
    )
    parser.add_argument(
        "--retrospective-judge-pi-provider",
        default="openai-codex",
        help="Retrospective judge Pi provider for pi mode",
    )
    parser.add_argument(
        "--retrospective-judge-pi-model",
        default="gpt-5.4-mini",
        help="Retrospective judge Pi model for pi mode (gpt-5.4 or lower)",
    )
    parser.add_argument(
        "--retrospective-judge-pi-thinking",
        help="Optional retrospective judge Pi thinking level",
    )
    parser.add_argument(
        "--retrospective-judge-ponytail",
        action="store_true",
        help="Enable default ponytail extension for retrospective judge pi mode",
    )
    parser.add_argument(
        "--retrospective-judge-ponytail-extension",
        help="Retrospective judge ponytail extension package name for pi mode",
    )
    parser.add_argument(
        "--retrospective-judge-ponytail-extension-source",
        help="Retrospective judge ponytail extension source string for pi mode",
    )


def add_publisher_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--publisher-mode",
        choices=("disabled", "create"),
        default="disabled",
        help="Terminal publisher mode to embed in the generated recipe",
    )
    parser.add_argument("--publisher-repo", help="owner/repo for publisher create mode")
    parser.add_argument("--publisher-base", help="Base branch for publisher create mode")
    parser.add_argument(
        "--publisher-gh-config-dir",
        help="Absolute mounted gh config dir for publisher create mode",
    )


def recipe_agent_from_args(args: argparse.Namespace, *, checkout_path: Path) -> dict[str, Any] | None:
    if args.agent_mode == "fake":
        return None
    try:
        if args.agent_mode == "real-local":
            command = json.loads(args.agent_command_json)
        else:
            command = None
    except TypeError:
        raise ValueError("--agent-command-json is required when --agent-mode=real-local") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"--agent-command-json must be valid JSON: {exc.msg}") from exc

    if args.agent_timeout_seconds is not None and args.agent_timeout_seconds <= 0:
        raise ValueError("--agent-timeout-seconds must be greater than zero")

    if args.agent_mode == "real-local":
        return real_local_recipe_agent(
            command=command,
            codex_home=args.agent_codex_home,
            config_home=args.agent_config_home,
            pi_config_home=args.agent_pi_config_home,
            checkout_path=checkout_path,
            timeout_seconds=args.agent_timeout_seconds,
        )
    if args.agent_mode == "pi":
        ponytail_extension = None
        ponytail_extension_source = None
        if args.agent_ponytail:
            if args.agent_ponytail_extension is not None or args.agent_ponytail_extension_source is not None:
                raise ValueError("--agent-ponytail cannot be combined with explicit ponytail extension values")
            ponytail_extension_source = PONYTAIL_EXTENSION_SOURCE
        else:
            ponytail_extension = args.agent_ponytail_extension
            ponytail_extension_source = args.agent_ponytail_extension_source
            if ponytail_extension is not None and ponytail_extension_source is not None:
                raise ValueError("Specify ponytail-extension or ponytail-extension-source, not both")

        return build_pi_real_worker_agent(
            pi_bin=args.agent_pi_bin,
            provider=args.agent_pi_provider,
            model=args.agent_pi_model,
            codex_home=args.agent_codex_home,
            config_home=args.agent_config_home,
            pi_config_home=args.agent_pi_config_home,
            pi_coding_agent_dir=args.agent_pi_coding_agent_dir,
            checkout_path=checkout_path,
            thinking=args.agent_pi_thinking,
            ponytail_extension=ponytail_extension,
            ponytail_extension_source=ponytail_extension_source,
            wrapper_secret_file=args.agent_wrapper_secret_file,
            timeout_seconds=args.agent_timeout_seconds,
        )
    raise ValueError(f"Unsupported --agent-mode: {args.agent_mode}")


def recipe_reviewer_from_args(args: argparse.Namespace, *, checkout_path: Path) -> dict[str, Any] | None:
    if args.reviewer_mode == "fake":
        return None
    if args.reviewer_mode == "pi":
        if args.reviewer_ponytail and (
            args.reviewer_ponytail_extension is not None
            or args.reviewer_ponytail_extension_source is not None
        ):
            raise ValueError("--reviewer-ponytail cannot be combined with explicit ponytail extension values")
        ponytail_extension = None
        ponytail_extension_source = None
        if args.reviewer_ponytail:
            ponytail_extension_source = PONYTAIL_EXTENSION_SOURCE
        else:
            ponytail_extension = args.reviewer_ponytail_extension
            ponytail_extension_source = args.reviewer_ponytail_extension_source
            if ponytail_extension is not None and ponytail_extension_source is not None:
                raise ValueError("Specify ponytail-extension or ponytail-extension-source, not both")
        command = build_pi_print_command(
            pi_bin=args.reviewer_pi_bin,
            provider=args.reviewer_pi_provider,
            model=args.reviewer_pi_model,
            thinking=args.reviewer_pi_thinking,
            ponytail_extension=ponytail_extension,
            ponytail_extension_source=ponytail_extension_source,
        )
        reviewer_timeout = 30 if args.reviewer_timeout_seconds is None else args.reviewer_timeout_seconds
        if reviewer_timeout <= 0:
            raise ValueError("--reviewer-timeout-seconds must be greater than zero")
        return {
            "type": "fake-reviewer-command",
            "command": command,
            "timeout_seconds": reviewer_timeout,
            **build_provider_pi_mount_config(
                provider=args.reviewer_pi_provider,
                codex_home=args.agent_codex_home,
                config_home=args.agent_config_home,
                pi_config_home=args.agent_pi_config_home,
                pi_coding_agent_dir=args.agent_pi_coding_agent_dir,
                checkout_path=checkout_path,
                field_prefix="reviewer",
            ),
        }
    raise ValueError(f"Unsupported --reviewer-mode: {args.reviewer_mode}")


def recipe_retrospective_judge_from_args(args: argparse.Namespace, *, checkout_path: Path) -> dict[str, Any] | None:
    if args.retrospective_judge_mode == "disabled":
        return None
    if args.retrospective_judge_mode == "pi":
        if args.retrospective_judge_ponytail and (
            args.retrospective_judge_ponytail_extension is not None
            or args.retrospective_judge_ponytail_extension_source is not None
        ):
            raise ValueError(
                "--retrospective-judge-ponytail cannot be combined with explicit ponytail extension values"
            )
        ponytail_extension = None
        ponytail_extension_source = None
        if args.retrospective_judge_ponytail:
            ponytail_extension_source = PONYTAIL_EXTENSION_SOURCE
        else:
            ponytail_extension = args.retrospective_judge_ponytail_extension
            ponytail_extension_source = args.retrospective_judge_ponytail_extension_source
            if ponytail_extension is not None and ponytail_extension_source is not None:
                raise ValueError("Specify ponytail-extension or ponytail-extension-source, not both")
        command = build_pi_print_command(
            pi_bin=args.retrospective_judge_pi_bin,
            provider=args.retrospective_judge_pi_provider,
            model=args.retrospective_judge_pi_model,
            thinking=args.retrospective_judge_pi_thinking,
            ponytail_extension=ponytail_extension,
            ponytail_extension_source=ponytail_extension_source,
        )
        judge_timeout = 120 if args.retrospective_judge_timeout_seconds is None else args.retrospective_judge_timeout_seconds
        if judge_timeout <= 0:
            raise ValueError("--retrospective-judge-timeout-seconds must be greater than zero")
        return {
            "enabled": True,
            "type": "local-command",
            "command": command,
            "timeout_seconds": judge_timeout,
            **build_provider_pi_mount_config(
                provider=args.retrospective_judge_pi_provider,
                codex_home=args.agent_codex_home,
                config_home=args.agent_config_home,
                pi_config_home=args.agent_pi_config_home,
                pi_coding_agent_dir=args.agent_pi_coding_agent_dir,
                checkout_path=checkout_path,
                field_prefix="retrospective_judge",
            ),
        }
    raise ValueError(f"Unsupported --retrospective-judge-mode: {args.retrospective_judge_mode}")


def recipe_validation_input_from_args(args: argparse.Namespace, *, project_contract: ProjectContract) -> dict[str, Any]:
    timeout_seconds = args.validation_timeout_seconds
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("--validation-timeout-seconds must be greater than 0")
    if args.validation_mode == "fake":
        timeout_seconds = 30 if timeout_seconds is None else timeout_seconds
        return {
            "validation": {
                "profile": args.validation_profile,
                "dry_run": True,
                "timeout_seconds": timeout_seconds,
            },
            "worker": {
                "type": "local-command",
                "command": ["python3", "-c", default_worker_code()],
                "timeout_seconds": timeout_seconds,
            },
        }
    if not project_contract_has_default_worker(project_contract):
        raise ValueError(
            "--validation-mode=project-worker requires a project contract with a default validation worker"
        )
    timeout_seconds = 3600 if timeout_seconds is None else timeout_seconds
    checkout_root = Path(args.checkout_root)
    checkout_path = Path(args.checkout_path)
    validation_stack_path = project_worker_validation_stack_path_from_args(
        args,
        checkout_path=checkout_path,
    )
    return {
        "validation": {
            "profile": args.validation_profile,
            "dry_run": False,
            "timeout_seconds": timeout_seconds,
            "worker_home": str(checkout_root / ".validation-worker" / checkout_path.name),
            "stack": {
                "role": "validation",
                "path": str(validation_stack_path),
            },
        }
    }


def project_worker_validation_stack_path_from_args(
    args: argparse.Namespace,
    *,
    checkout_path: Path,
) -> Path:
    if not args.validation_stack_path:
        return checkout_path.parent / "bump-akk-stack-validation"
    validation_stack_path = Path(args.validation_stack_path)
    if not validation_stack_path.is_absolute():
        raise ValueError("--validation-stack-path must be absolute")
    resolved_stack_path = validation_stack_path.resolve(strict=False)
    resolved_checkout_path = checkout_path.resolve(strict=False)
    if resolved_stack_path == resolved_checkout_path or resolved_checkout_path in resolved_stack_path.parents:
        raise ValueError("--validation-stack-path must be outside checkout")
    return validation_stack_path


def project_contract_has_default_worker(project_contract: ProjectContract) -> bool:
    return project_contract.project_slug == "bump-eqemu"


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


def recipe_publisher_factory_from_args(
    args: argparse.Namespace,
    *,
    checkout_path: Path,
) -> Callable[[str], dict[str, Any] | None] | None:
    if args.publisher_mode == "disabled":
        return None
    if args.publisher_mode != "create":
        raise ValueError(f"Unsupported --publisher-mode: {args.publisher_mode}")
    # Fail fast so run-next validates misconfiguration before selection work runs.
    recipe_publisher_from_args(
        args,
        review_branch="afk/dry-run",
        checkout_path=checkout_path,
    )

    def _factory(workstream_id: str) -> dict[str, Any] | None:
        return recipe_publisher_from_args(
            args,
            review_branch=f"afk/{branch_slug(workstream_id)}",
            checkout_path=checkout_path,
        )

    return _factory


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
