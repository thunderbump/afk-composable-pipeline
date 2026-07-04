import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.jsonutil import sha256_json  # noqa: E402
from afk.registry import StepResult  # noqa: E402
from afk.workstream import (  # noqa: E402
    WorkstreamLedger,
    composed_step_input,
    equivalent_run_step_command,
    normalize_recipe,
    publish_terminal_pr,
    step_execution_record,
    update_state_from_step,
)
from afk.workstream_lifecycle import (  # noqa: E402
    LifecycleHooks,
    run_lifecycle,
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
                    "review-1",
                    "review",
                    {"status": "passed", "summary": "ready", "checkout": {"start_commit": "head-2"}, "reviewer_result": {"findings": []}},
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
                    "review-1",
                    "review",
                    {
                        "status": "request_revision",
                        "summary": "review requested changes",
                        "checkout": {"start_commit": "head-1"},
                        "reviewer_result": {
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
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
                    "review-2",
                    "review",
                    {"status": "passed", "summary": "ready", "checkout": {"start_commit": "head-2"}, "reviewer_result": {"findings": []}},
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
