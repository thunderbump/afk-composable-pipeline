import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.jsonutil import sha256_json  # noqa: E402
from afk.registry import StepResult  # noqa: E402
from afk.workstream import (  # noqa: E402
    PipelineEngine,
    PipelinePlan,
    WorkstreamLedger,
    composed_step_input,
    equivalent_run_step_command,
    normalize_recipe,
    publish_terminal_pr,
    run_workstream,
    step_execution_record,
    update_state_from_step,
)
from afk.workstream_lifecycle import (  # noqa: E402
    LifecycleHooks,
    run_lifecycle,
    stuck_same_finding_blocked_reason,
    terminal_selected_work_status,
    workstream_status_from_publication,
)


def selected_fixture_item(external_id="central-wfc9", title="Extract workstream lifecycle"):
    return {
        "source_id": "fixture",
        "source_type": "fixture",
        "external_id": external_id,
        "url": f"https://tracker.example/{external_id}",
        "title": title,
        "status": "open",
        "labels": ["project:afk-composable-pipeline", "afk:ready"],
        "parent": "central",
        "workstream": "central",
        "acceptance_criteria": ["Keep the external workstream path stable."],
        "dependencies": [],
        "blockers": [],
        "afk": {"ready": True},
    }


def step_result(run_id, step, output):
    return StepResult(
        run_id=run_id,
        step=step,
        status="succeeded",
        output=output,
        stdout="",
        stderr="",
        result_sha256=sha256_json(output),
    )


def lifecycle_hooks():
    return LifecycleHooks(
        composed_step_input=composed_step_input,
        equivalent_run_step_command=equivalent_run_step_command,
        step_execution_record=step_execution_record,
        update_state_from_step=update_state_from_step,
        publish_terminal_pr=publish_terminal_pr,
    )


class WorkstreamLifecycleTest(unittest.TestCase):
    def test_pipeline_engine_runs_normalized_plan_through_step_runner(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {}},
                ],
                "publisher": {"enabled": False},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-1")
            ledger.prepare()

            outcome = PipelineEngine().run(
                PipelinePlan(normalized=recipe, run_id="run-1"),
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
            )

        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review"],
        )
        self.assertEqual(outcome.publication["status"], "validated-unpublished")
        self.assertEqual(len(outcome.state["runtime_review_cycles"]), 1)

    def test_run_workstream_keeps_existing_result_artifacts_compatible(self):
        recipe = {
            "workstream_id": "central-wfc9",
            "parent": "central",
            "review_branch": "afk/central-wfc9",
            "steps": [
                {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                {"name": "implement", "input": {}},
                {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                {"name": "review", "input": {}},
            ],
            "publisher": {"enabled": False},
        }
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            outcome = run_workstream(recipe, ledger_dir=ledger_root, step_runner=runner)
            result_path = ledger_root / outcome.result_path
            publication_path = result_path.parent / "publication-result.json"
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            publication = json.loads(publication_path.read_text(encoding="utf-8"))

        self.assertEqual(outcome.workstream_id, "central-wfc9")
        self.assertEqual(outcome.status, "validated-unpublished")
        self.assertEqual(outcome.publication_status, "validated-unpublished")
        self.assertEqual(payload["status"], "validated-unpublished")
        self.assertEqual(payload["publication"]["status"], "validated-unpublished")
        self.assertEqual(publication["status"], "validated-unpublished")
        self.assertEqual(
            [step["name"] for step in payload["steps"]],
            ["select-work", "prepare-checkout", "implement", "validate", "review"],
        )

    def test_run_lifecycle_records_clean_two_pass_review_cycle(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {}},
                ],
                "publisher": {"enabled": False},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-1")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-1",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review"],
        )
        self.assertEqual(outcome.publication["status"], "validated-unpublished")
        self.assertEqual(len(outcome.state["runtime_review_cycles"]), 1)
        cycle = outcome.state["runtime_review_cycles"][0]
        self.assertEqual(cycle["status"], "passed")
        self.assertEqual([review["role"] for review in cycle["reviews"]], ["correctness", "bug-risk"])
        self.assertEqual([review["status"] for review in cycle["reviews"]], ["passed", "passed"])

    def test_run_lifecycle_repairs_validation_feedback_before_review(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {}},
                ],
                "publisher": {"enabled": False},
                "retry_policy": {"max_retries": 1},
                "validation_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "failed_validation",
                        "classification": "compiler",
                        "summary": "compile failed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "actionable_failures": [
                            {
                                "category": "compiler",
                                "excerpt": "missing_header.h: No such file or directory",
                                "log_path": "/tmp/compiler.log",
                            }
                        ],
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-1")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-1",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "prepare-checkout", "implement", "validate", "review"],
        )
        self.assertEqual(outcome.publication["status"], "validated-unpublished")
        self.assertEqual(workstream_status_from_publication(outcome.publication), "validated-unpublished")

    def test_run_lifecycle_repairs_review_feedback_and_records_runtime_cycle(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "retry_policy": {"max_retries": 1},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "correctness",
                                    "summary": "Handle the empty review cycle before publishing.",
                                    "required_fix": "Handle the empty review cycle before publishing.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-2")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-2",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review", "prepare-checkout", "implement", "validate", "review"],
        )
        self.assertEqual(outcome.publication["status"], "validated-unpublished")
        self.assertEqual(len(outcome.state["runtime_review_cycles"]), 2)
        self.assertEqual(outcome.state["runtime_review_cycles"][0]["status"], "findings-addressed")
        self.assertEqual(
            [review["role"] for review in outcome.state["runtime_review_cycles"][0]["reviews"]],
            ["correctness", "bug-risk"],
        )
        self.assertEqual(outcome.state["runtime_review_cycles"][1]["status"], "passed")
        self.assertEqual(
            [review["role"] for review in outcome.state["runtime_review_cycles"][1]["reviews"]],
            ["correctness", "bug-risk"],
        )

    def test_run_lifecycle_routes_pipeline_only_review_findings_without_target_repair(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {}},
                ],
                "publisher": {"enabled": False},
                "retry_policy": {"max_retries": 1},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review found a pipeline issue",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "classification": "pipeline_failure",
                                    "severity": "medium",
                                    "summary": "Capture missing reviewer adapter config as follow-up evidence.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-3")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-3",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review"],
        )
        self.assertEqual(outcome.publication["status"], "blocked")
        self.assertIn("pipeline follow-up", outcome.publication["reason"])
        self.assertEqual(len(outcome.state["runtime_review_cycles"]), 1)
        cycle = outcome.state["runtime_review_cycles"][0]
        self.assertEqual(cycle["status"], "request-changes")
        self.assertEqual(cycle["reviews"][0]["role"], "correctness")
        self.assertEqual(
            cycle["reviews"][0]["pipeline_follow_up"],
            [
                {
                    "role": "correctness",
                    "classification": "pipeline_failure",
                    "severity": "medium",
                    "summary": "Capture missing reviewer adapter config as follow-up evidence.",
                }
            ],
        )

    def test_run_lifecycle_blocks_when_same_review_finding_survives_repair(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "repair_policy": {"mode": "progress_aware", "hard_cap": 2},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        repeated_finding = {
            "status": "request_revision",
            "severity": "high",
            "role": "correctness",
            "file": "src/demo.py",
            "line": 41,
            "summary": "Tracker close path still misses the empty review cycle case.",
            "required_fix": "Handle the empty review cycle before publishing.",
        }
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": [dict(repeated_finding)]},
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-2",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes again",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": [dict(repeated_finding)]},
                    },
                ),
                step_result(
                    "review-bug-risk-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-stuck-review")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-stuck-review",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(outcome.publication["status"], "blocked")
        self.assertIn("stuck_same_finding", outcome.publication["reason"])
        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review", "prepare-checkout", "implement", "validate", "review"],
        )

    def test_run_lifecycle_blocks_when_same_issue_key_survives_repair_with_rephrased_text(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "repair_policy": {"mode": "progress_aware", "hard_cap": 2},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        first_finding = {
            "status": "request_revision",
            "severity": "high",
            "role": "correctness",
            "file": "src/demo.py",
            "line": 41,
            "fingerprint": "correctness:empty-review-cycle",
            "summary": "Publisher leaves terminal review state dangling after the cycle list is empty.",
            "required_fix": "Guard terminal publish when the cycle list is empty.",
        }
        rephrased_finding = {
            "status": "request_revision",
            "severity": "high",
            "role": "correctness",
            "file": "src/demo.py",
            "line": 41,
            "fingerprint": "correctness:empty-review-cycle",
            "summary": "Empty-cycle publish still breaks terminal state handling in the tracker path.",
            "required_fix": "Add the missing empty-cycle publish guard.",
        }
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": [dict(first_finding)]},
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-2",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes again",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": [dict(rephrased_finding)]},
                    },
                ),
                step_result(
                    "review-bug-risk-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-stuck-review-rephrased")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-stuck-review-rephrased",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(outcome.publication["status"], "blocked")
        self.assertIn("stuck_same_finding", outcome.publication["reason"])
        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review", "prepare-checkout", "implement", "validate", "review"],
        )

    def test_stuck_same_finding_ignores_same_location_different_issue_keys(self):
        state = {
            "repair_history": [
                {
                    "trigger": "review_feedback",
                    "review_fingerprints": [
                        {
                            "role": "correctness",
                            "file": "src/demo.py",
                            "line": 41,
                            "stable_key": "correctness:empty-review-cycle",
                            "required_fix": "guard terminal publish when the cycle list is empty",
                            "summary": "publisher leaves terminal review state dangling after the cycle list is empty",
                            "key": "guard terminal publish when the cycle list is empty",
                        }
                    ],
                }
            ]
        }
        findings = [
            {
                "role": "correctness",
                "file": "src/demo.py",
                "line": 41,
                "stable_key": "correctness:mutated-status-after-publish",
                "required_fix": "preserve tracker status after publish",
                "summary": "tracker status mutates after publish on the same branch path",
            }
        ]

        self.assertEqual(stuck_same_finding_blocked_reason(state, findings), "")

    def test_stuck_same_finding_ignores_same_stable_key_across_different_roles(self):
        state = {
            "repair_history": [
                {
                    "trigger": "review_feedback",
                    "review_fingerprints": [
                        {
                            "role": "correctness",
                            "file": "src/demo.py",
                            "line": 41,
                            "stable_key": "empty-review-cycle",
                            "required_fix": "guard terminal publish when the cycle list is empty",
                            "summary": "publisher leaves terminal review state dangling after the cycle list is empty",
                            "key": "guard terminal publish when the cycle list is empty",
                        }
                    ],
                }
            ]
        }
        findings = [
            {
                "role": "bug-risk",
                "file": "src/demo.py",
                "line": 41,
                "stable_key": "empty-review-cycle",
                "required_fix": "guard terminal publish when the cycle list is empty",
                "summary": "publisher leaves terminal review state dangling after the cycle list is empty",
            }
        ]

        self.assertEqual(stuck_same_finding_blocked_reason(state, findings), "")

    def test_run_lifecycle_blocks_when_repair_has_no_delta(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "repair_policy": {"mode": "progress_aware", "hard_cap": 2},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "correctness",
                                    "summary": "Add the missing follow-up behavior.",
                                    "required_fix": "Add the missing follow-up behavior.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": [], "dirty": False, "dirty_status": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-no-delta")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-no-delta",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(outcome.publication["status"], "blocked")
        self.assertIn("no_repair_delta", outcome.publication["reason"])
        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review", "prepare-checkout", "implement"],
        )

    def test_run_lifecycle_blocks_when_repair_regresses_validation(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "repair_policy": {"mode": "progress_aware", "hard_cap": 2},
                "validation_feedback": {"enabled": True},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "correctness",
                                    "summary": "Add the missing follow-up behavior.",
                                    "required_fix": "Add the missing follow-up behavior.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "failed_validation",
                        "summary": "tests failed after repair",
                        "classification": "worker_failure",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "actionable_failures": [
                            {
                                "category": "compiler",
                                "reason": "demo.cpp:1: error: missing semicolon",
                            }
                        ],
                        "worker_result": {
                            "normalized": {
                                "status": "failed_validation",
                                "classification": "worker_failure",
                                "summary": "tests failed after repair",
                            }
                        },
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-regressed-validation")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-regressed-validation",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(outcome.publication["status"], "blocked")
        self.assertIn("repair_regressed_validation", outcome.publication["reason"])
        self.assertEqual(
            [step["name"] for step in outcome.steps],
            ["select-work", "prepare-checkout", "implement", "validate", "review", "prepare-checkout", "implement", "validate"],
        )

    def test_run_lifecycle_allows_changed_review_feedback_to_continue_repair_loop(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "repair_policy": {"mode": "progress_aware", "hard_cap": 2},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "correctness",
                                    "file": "src/demo.py",
                                    "line": 41,
                                    "summary": "Fix the empty review cycle path.",
                                    "required_fix": "Handle the empty review cycle before publishing.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "first repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair-1.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-2",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested different changes",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "correctness",
                                    "file": "src/other.py",
                                    "line": 12,
                                    "summary": "Fix the new post-repair issue.",
                                    "required_fix": "Handle the follow-up edge case in the repair path.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-3",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-2"},
                ),
                step_result(
                    "implement-3",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "second repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-3", "changed_files": ["repair-2.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-3",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-3"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-3",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-3"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-3",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-3"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-changed-feedback")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-changed-feedback",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(outcome.publication["status"], "validated-unpublished")
        self.assertEqual(
            [step["name"] for step in outcome.steps],
            [
                "select-work",
                "prepare-checkout",
                "implement",
                "validate",
                "review",
                "prepare-checkout",
                "implement",
                "validate",
                "review",
                "prepare-checkout",
                "implement",
                "validate",
                "review",
            ],
        )

    def test_run_lifecycle_allows_same_location_different_issue_to_continue_repair_loop(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "repair_policy": {"mode": "progress_aware", "hard_cap": 2},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "correctness",
                                    "file": "src/demo.py",
                                    "line": 41,
                                    "summary": "Fix the empty review cycle path.",
                                    "required_fix": "Handle the empty review cycle before publishing.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "first repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair-1.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-2",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "correctness review requested different changes",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "correctness",
                                    "file": "src/demo.py",
                                    "line": 41,
                                    "summary": "Fix the publish-state regression.",
                                    "required_fix": "Stop mutating tracker status after publish.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "review-bug-risk-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "checkout-3",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-2"},
                ),
                step_result(
                    "implement-3",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "second repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-3", "changed_files": ["repair-2.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-3",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-3"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-3",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-3"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-3",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-3"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-changed-same-location-feedback")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-changed-same-location-feedback",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(outcome.publication["status"], "validated-unpublished")
        self.assertEqual(
            [step["name"] for step in outcome.steps],
            [
                "select-work",
                "prepare-checkout",
                "implement",
                "validate",
                "review",
                "prepare-checkout",
                "implement",
                "validate",
                "review",
                "prepare-checkout",
                "implement",
                "validate",
                "review",
            ],
        )

    def test_run_lifecycle_keeps_mixed_review_cycle_open_after_partial_follow_up(self):
        recipe = normalize_recipe(
            {
                "workstream_id": "central-wfc9",
                "parent": "central",
                "review_branch": "afk/central-wfc9",
                "steps": [
                    {"name": "select-work", "input": {"sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}]}},
                    {"name": "prepare-checkout", "input": {"checkout_path": "/tmp/checkout"}},
                    {"name": "implement", "input": {}},
                    {"name": "validate", "profile": "tier1", "input": {"validation": {}}},
                    {"name": "review", "input": {"role": "correctness"}},
                ],
                "publisher": {"enabled": False},
                "retry_policy": {"max_retries": 1},
                "review_feedback": {"enabled": True},
            },
            parent=None,
            workstream_id=None,
        )
        runs = iter(
            [
                step_result("select-1", "select-work", {"status": "selected", "selected_work": [selected_fixture_item()]}),
                step_result(
                    "checkout-1",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "base-1"},
                ),
                step_result(
                    "implement-1",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "initial implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-1", "changed_files": ["implemented.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-1",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-1"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-1",
                    "review",
                    {
                        "status": "failed",
                        "summary": "correctness review could not complete",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "bug-risk review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "role": "bug-risk",
                                    "summary": "Handle the empty review cycle before publishing.",
                                    "required_fix": "Handle the empty review cycle before publishing.",
                                }
                            ]
                        },
                    },
                ),
                step_result(
                    "checkout-2",
                    "prepare-checkout",
                    {"status": "prepared", "checkout_path": "/tmp/checkout", "review_branch": "afk/central-wfc9", "start_commit": "head-1"},
                ),
                step_result(
                    "implement-2",
                    "implement",
                    {
                        "status": "implemented",
                        "summary": "repair implementation",
                        "work_item": selected_fixture_item(),
                        "git": {"after_commit": "head-2", "changed_files": ["repair.txt"], "dirty": False, "dirty_status": []},
                    },
                ),
                step_result(
                    "validate-2",
                    "validate",
                    {
                        "status": "validated",
                        "summary": "tests passed",
                        "checkout": {"start_commit": "head-2"},
                        "validation": {"requested_profile": "tier1"},
                        "worker_result": {
                            "normalized": {
                                "status": "validated",
                                "classification": "success",
                                "summary": "tests passed",
                            }
                        },
                    },
                ),
                step_result(
                    "review-correctness-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "correctness review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
                step_result(
                    "review-bug-risk-2",
                    "review",
                    {
                        "status": "passed",
                        "summary": "bug-risk review passed",
                        "checkout": {"start_commit": "head-2"},
                        "reviewer_result": {"findings": []},
                    },
                ),
            ]
        )

        def runner(step_name, step_input, ledger_dir, project_contract):
            return next(runs)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_root = Path(temp_dir) / "ledger"
            ledger = WorkstreamLedger(ledger_root, "run-4")
            ledger.prepare()

            outcome = run_lifecycle(
                normalized=recipe,
                run_id="run-4",
                ledger_dir=ledger_root,
                ledger=ledger,
                step_runner=runner,
                project_contract=None,
                hooks=lifecycle_hooks(),
            )

        self.assertEqual(len(outcome.state["runtime_review_cycles"]), 2)
        first_cycle = outcome.state["runtime_review_cycles"][0]
        self.assertEqual(first_cycle["status"], "findings-open")
        self.assertEqual(
            [review["status"] for review in first_cycle["reviews"]],
            ["findings-open", "request-changes"],
        )
        self.assertNotIn("response", first_cycle["reviews"][0])
        self.assertEqual(first_cycle["reviews"][1]["response"]["status"], "addressed")
        self.assertEqual(outcome.state["runtime_review_cycles"][1]["status"], "passed")

    def test_terminal_selected_work_status_returns_validated_after_review_pass(self):
        status = terminal_selected_work_status(
            {
                "selected_work": [selected_fixture_item()],
                "implementation": {"status": "implemented", "git": {"after_commit": "head-1"}},
                "implementation_selection": [selected_fixture_item()],
                "validations": [{"output": {"status": "validated", "checkout": {"start_commit": "head-1"}}}],
                "review": {"status": "passed", "checkout": {"start_commit": "head-1"}},
                "review_selection": [selected_fixture_item()],
            }
        )

        self.assertEqual(status, "validated")


if __name__ == "__main__":
    unittest.main()
