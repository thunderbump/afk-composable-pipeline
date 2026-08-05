from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from afk.candidate_validation import (
    BOOTSTRAP_ADAPTER,
    CandidateValidationError,
    tracked_regular_file_identity,
)
from afk.jsonutil import canonical_json
from afk.redaction import redact_artifact_value
from afk.run_store import RunStore, RunStoreError
from afk.retrospective_status import build_retrospective_status
from afk.start import (
    StartError,
    complete_run,
    observe_worker_unit,
    resume_run,
    run_worker,
    run_worker_unit,
    start_run,
)


SCHEMA_VERSION = 1


def main(
    argv: list[str] | None = None,
    *,
    start_run_id: str | None = None,
    on_lifecycle_target: Callable[[str], None] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        try:
            run_id, exit_code = start_run(
                args.bead_id,
                bootstrap_contract=args.bootstrap_contract,
                run_id=start_run_id,
            )
        except (StartError, RunStoreError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(run_id)
        return exit_code

    if args.command == "resume":
        try:
            run_id, exit_code = resume_run(
                args.run_id,
                note=args.note,
                on_selected=on_lifecycle_target,
            )
        except (StartError, RunStoreError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(run_id)
        return exit_code

    if args.command == "supersede":
        try:
            projection = RunStore().supersede_active_run(args.reason)
        except RunStoreError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(projection["run_id"])
        return 0

    if args.command == "_worker":
        return run_worker(args.run_id)

    if args.command == "_worker_unit":
        return run_worker_unit(args.run_id)

    if args.command == "status":
        store = RunStore()
        exit_code = 0
        try:
            projection = store.status(args.run_id)
            observation = None
            observation_error = None
            if "worker_exit_code" not in projection and projection["state"] not in {
                "completed",
                "superseded",
            }:
                active_run_id = (
                    projection["run_id"]
                    if args.run_id is None
                    else store.active_run_id()
                )
                if active_run_id == projection["run_id"]:
                    try:
                        observation = observe_worker_unit(projection["run_id"])
                    except StartError as exc:
                        observation_error = exc
                projection = store.status(projection["run_id"])
                if (
                    observation_error is not None
                    and "worker_exit_code" not in projection
                    and projection["state"] not in {"completed", "superseded"}
                ):
                    raise observation_error
            output = dict(projection)
            output["retrospective"] = build_retrospective_status(
                store, projection["run_id"]
            )
            if projection["state"] == "attention_required":
                output["recommended_resume"] = ["afk", "resume"]
                exit_code = 2
            if projection["state"] != "superseded" and "worker_exit_code" in projection:
                unit = projection.get("unit")
                if not isinstance(unit, str) or not unit:
                    raise RunStoreError("terminal worker observation unit is invalid")
                output["unit_observation"] = {
                    "status": "terminal",
                    "unit": unit,
                    "worker_exit_code": projection["worker_exit_code"],
                    "worker_result": projection["worker_result"],
                }
            elif observation is not None:
                output["unit_observation"] = observation
                if observation["status"] == "interrupted":
                    output["recommended_resume"] = ["afk", "resume"]
                    exit_code = 2
            if "recommended_resume" in output:
                output["resume_precondition"] = {"active_run_id": output["run_id"]}
        except (RunStoreError, StartError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(canonical_json(output))
        else:
            fields = [
                output["run_id"],
                output["state"],
                f"bead={output['bead_id']}",
                f"sequence={output['last_sequence']}",
            ]
            for key in (
                "checkpoint",
                "unit",
                "worker_exit_code",
                "worker_result",
            ):
                if key in output:
                    fields.append(f"{key}={output[key]}")
            observation = output.get("unit_observation")
            if isinstance(observation, dict):
                if "unit" not in output:
                    fields.append(f"unit={observation['unit']}")
                fields.append(f"unit_status={observation['status']}")
                if observation["status"] != "terminal":
                    fields.extend(
                        (
                            f"load_state={observation['load_state']}",
                            f"active_state={observation['active_state']}",
                        )
                    )
            print(" ".join(fields))
            if "recommended_resume" in output:
                print("recommended_resume=afk resume")
                print(
                    "resume_precondition=active_run_id:"
                    f"{output['resume_precondition']['active_run_id']}"
                )
            retrospective = output["retrospective"]
            counts = retrospective["episode_counts"]
            latest = retrospective["latest"]
            fields = [
                f"retrospective_status={retrospective['status']}",
                (
                    "retrospective_latest_sequence="
                    f"{latest['episode_sequence'] if latest is not None else 'absent'}"
                ),
                (
                    "retrospective_latest_event="
                    f"{latest['event'] if latest is not None else 'absent'}"
                ),
                (
                    "retrospective_latest_state="
                    f"{latest['state'] if latest is not None else 'absent'}"
                ),
                f"retrospective_episodes={counts['total']}",
                f"retrospective_sealed={counts['sealed']}",
                f"retrospective_warnings={counts['warning']}",
                f"retrospective_absent={counts['absent']}",
                (
                    "retrospective_findings="
                    f"{retrospective['process_findings_count']}"
                ),
                (
                    "retrospective_proposals="
                    f"{retrospective['improvement_proposals_count']}"
                ),
                (
                    "retrospective_path="
                    f"{latest['evidence_path'] if latest is not None else 'absent'}"
                ),
            ]
            print(" ".join(fields))
        return exit_code

    if args.command == "report":
        try:
            store = RunStore()
            projection = store.status(args.run_id)
            projection["retrospective"] = build_retrospective_status(
                store, projection["run_id"]
            )
            report = _run_report(projection)
        except (CandidateValidationError, RunStoreError) as exc:
            parser.error(str(exc))
        print(canonical_json(report))
        return 0

    if args.command == "complete":
        try:
            projection = complete_run(
                args.run_id,
                on_selected=on_lifecycle_target,
            )
            projection["retrospective"] = build_retrospective_status(
                RunStore(), projection["run_id"]
            )
            report = _run_report(projection)
        except (StartError, RunStoreError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(canonical_json(report))
        return 0

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="afk")
    subcommands = parser.add_subparsers(dest="command")

    start_parser = subcommands.add_parser("start", help="Start one durable AFK Run")
    start_parser.add_argument("bead_id")
    start_parser.add_argument(
        "--bootstrap-contract",
        action="store_true",
        help="Start only when the pinned base lacks afk.toml",
    )

    resume_parser = subcommands.add_parser("resume", help="Reconcile the Active Run")
    resume_parser.add_argument("run_id", nargs="?")
    resume_parser.add_argument("--note")

    supersede_parser = subcommands.add_parser(
        "supersede", help="Retire the Active Run after retained attention"
    )
    supersede_parser.add_argument("--reason", required=True)

    worker_parser = subcommands.add_parser("_worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("run_id")

    worker_unit_parser = subcommands.add_parser("_worker_unit", help=argparse.SUPPRESS)
    worker_unit_parser.add_argument("run_id")

    status_parser = subcommands.add_parser("status", help="Inspect a durable Run")
    status_parser.add_argument(
        "run_id", nargs="?", help="Run id; defaults to the Active Run"
    )
    status_parser.add_argument(
        "--json", action="store_true", help="Print the Run projection as JSON"
    )

    report_parser = subcommands.add_parser(
        "report", help="Serialize an incremental or final Run report"
    )
    report_parser.add_argument(
        "run_id", nargs="?", help="Run id; defaults to the Active Run"
    )

    complete_parser = subcommands.add_parser(
        "complete", help="Reconcile terminal publication for a reviewed Run"
    )
    complete_parser.add_argument(
        "run_id", nargs="?", help="Run id; defaults to the Active Run"
    )

    return parser


def _run_report(projection: dict[str, Any]) -> dict[str, Any]:
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": projection["run_id"],
        "bead_id": projection["bead_id"],
        "state": projection["state"],
        "checkpoint": projection["checkpoint"],
        "complete": projection["state"] == "completed",
        "paused": projection["state"] == "attention_required",
        "updated_at": projection["updated_at"],
    }
    for key in ("candidate_sha", "attention", "supersession"):
        if key in projection:
            report[key] = projection[key]
    report["retrospective"] = projection["retrospective"]
    contract = projection.get("validation_contract")
    candidate_sha = projection.get("candidate_sha")
    if (
        projection["state"] == "attention_required"
        and isinstance(projection.get("attention"), dict)
        and projection["attention"].get("scope") == "validation"
        and isinstance(contract, dict)
        and contract.get("source") == "approved_bootstrap"
        and isinstance(candidate_sha, str)
    ):
        approval = contract.get("approval")
        if (
            set(contract) == {"source", "base_sha", "adapter_id"}
            and contract.get("adapter_id") == BOOTSTRAP_ADAPTER
        ):
            report["authorization"] = {
                "status": "required",
                "candidate_sha": candidate_sha,
                "reason": "bootstrap validation harness approval is unavailable",
                "continuation": {
                    "approve": [
                        sys.executable,
                        "-m",
                        "afk.bootstrap_approval",
                        "<tracked-executable-harness>",
                        "--run-id",
                        projection["run_id"],
                    ],
                    "resume": [sys.executable, "-m", "afk", "resume"],
                    "resume_precondition": {"active_run_id": projection["run_id"]},
                },
            }
        elif (
            isinstance(approval, dict)
            and approval.get("candidate_sha") != candidate_sha
            and isinstance(approval.get("harness"), dict)
        ):
            harness = approval["harness"]
            observed = tracked_regular_file_identity(
                Path(projection["worktree_path"]), candidate_sha, harness["path"]
            )
            if observed is None:
                raise CandidateValidationError(
                    "invalid",
                    "current Candidate bootstrap harness identity is unavailable",
                )
            report["authorization"] = {
                "status": "required",
                "candidate_sha": candidate_sha,
                "artifact": {
                    "path": harness["path"],
                    "mode": observed[0],
                    "blob_sha": observed[1],
                },
                "reason": (
                    "bootstrap approval is Candidate-bound; prior approval targets "
                    f"{approval.get('candidate_sha')}"
                ),
                "continuation": {
                    "approve": [
                        sys.executable,
                        "-m",
                        "afk.bootstrap_approval",
                        harness.get("path"),
                        "--run-id",
                        projection["run_id"],
                        "--timeout-seconds",
                        str(approval.get("timeout_seconds")),
                    ],
                    "resume": [sys.executable, "-m", "afk", "resume"],
                    "resume_precondition": {"active_run_id": projection["run_id"]},
                },
            }
    return redact_artifact_value(report)
