import sys
import unittest
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk import workstream  # noqa: E402
from afk import retrospective as retrospective_api  # noqa: E402
from afk.retrospective import RetrospectiveContext, build_pipeline_retrospective  # noqa: E402


def retrospective_state():
    return {
        "selected_work": [{"external_id": "central-afk-pr.17", "title": "Delay tracker closure"}],
        "implementation": {
            "status": "implemented",
            "git": {"after_commit": "abc123"},
        },
        "implementation_selection": [{"external_id": "central-afk-pr.17"}],
        "implementation_result_path": "/tmp/ledger/runs/impl/step-result.json",
        "validations": [
            {
                "output": {
                    "status": "validated",
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ],
        "review": {
            "status": "passed",
            "summary": "ready for human review",
            "checkout": {"start_commit": "abc123"},
            "reviewer_result": {"findings": []},
        },
        "review_selection": [{"external_id": "central-afk-pr.17"}],
        "review_result_path": "runs/review/step-result.json",
        "cleanup": {"status": "clean", "resources": []},
    }


def retrospective_tracker(status="awaiting-review"):
    return {
        "status": status,
        "close_source_item": False,
        "close_reason": "",
        "comment": "",
        "pr_url": "https://github.example/pr/17",
        "merge_commit": "",
    }


class RetrospectiveModuleTest(unittest.TestCase):
    def test_workstream_does_not_expose_private_retrospective_internals(self):
        for symbol in (
            "_apply_retrospective_judge",
            "_retrospective_follow_up_bead_description",
            "_retrospective_follow_up_bead_labels",
            "_retrospective_follow_up_fingerprint",
        ):
            with self.subTest(symbol=symbol):
                self.assertFalse(hasattr(workstream, symbol))

    def test_retrospective_module_exposes_public_retrospective_helpers(self):
        namespace = {}
        exec(
            "from afk.retrospective import effective_retrospective, pipeline_retrospective_record",
            namespace,
        )

        self.assertIs(namespace["effective_retrospective"], retrospective_api.effective_retrospective)
        self.assertIs(
            namespace["pipeline_retrospective_record"],
            retrospective_api.pipeline_retrospective_record,
        )

    def test_workstream_keeps_legacy_retrospective_helper_imports(self):
        namespace = {}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            exec(
                "from afk.workstream import effective_retrospective, pipeline_retrospective_record",
                namespace,
            )

        self.assertIs(namespace["effective_retrospective"], retrospective_api.effective_retrospective)
        self.assertIs(
            namespace["pipeline_retrospective_record"],
            retrospective_api.pipeline_retrospective_record,
        )
        self.assertEqual(len(caught), 2)
        self.assertTrue(all(issubclass(item.category, DeprecationWarning) for item in caught))

    def test_build_pipeline_retrospective_reports_clean_published_run(self):
        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=retrospective_state(),
                publication={"status": "published", "url": "https://github.example/pr/17"},
                tracker=retrospective_tracker(),
            )
        )

        self.assertEqual(record["status"], "published")
        self.assertEqual(record["health"], "healthy")
        self.assertEqual(record["publication_status"], "published")
        self.assertEqual(record["tracker_status"], "awaiting-review")
        self.assertEqual(record["signals"], [])
        self.assertEqual(record["recommended_follow_up"], [])
        self.assertEqual(record["follow_up"]["recommended"], [])
        self.assertEqual(record["follow_up"]["created"], [])
        self.assertEqual(record["judge"], {"enabled": False, "status": "disabled"})

    def test_build_pipeline_retrospective_includes_target_work_repair_stop_evidence(self):
        state = retrospective_state()
        state["review"] = {
            "status": "request_revision",
            "summary": "review requested changes",
            "reviewer_result": {
                "findings": [
                    {
                        "classification": "correctness",
                        "summary": "Guard terminal publish when the cycle list is empty.",
                    }
                ]
            },
        }

        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=state,
                publication={
                    "status": "blocked",
                    "reason": (
                        "stuck_same_finding: correctness src/demo.py:41: "
                        "Guard terminal publish when the cycle list is empty."
                    ),
                },
                tracker=retrospective_tracker("validated"),
            )
        )

        self.assertEqual(record["repair_stop"]["classification"], "stuck_same_finding")
        self.assertEqual(record["repair_stop"]["scope"], "target-work")
        self.assertEqual(
            record["repair_stop"]["evidence_paths"],
            [
                "/tmp/ledger/runs/impl/step-result.json",
                "/tmp/ledger/runs/validate/step-result.json",
                "/tmp/ledger/runs/validate/worker-result.json",
                "runs/review/step-result.json",
            ],
        )
        self.assertEqual(record["recommended_follow_up"], [])

    def test_build_pipeline_retrospective_includes_regressed_validation_repair_stop_evidence(self):
        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=retrospective_state(),
                publication={
                    "status": "blocked",
                    "reason": "repair_regressed_validation: tests failed after repair",
                },
                tracker=retrospective_tracker("validated"),
            )
        )

        self.assertEqual(record["repair_stop"]["classification"], "repair_regressed_validation")
        self.assertEqual(record["repair_stop"]["scope"], "target-work")
        self.assertEqual(
            record["repair_stop"]["evidence_paths"],
            [
                "/tmp/ledger/runs/validate/step-result.json",
                "/tmp/ledger/runs/validate/worker-result.json",
            ],
        )
        self.assertEqual(record["recommended_follow_up"], [])

    def test_build_pipeline_retrospective_includes_no_repair_delta_evidence(self):
        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=retrospective_state(),
                publication={
                    "status": "blocked",
                    "reason": "no_repair_delta: repair produced no implementation commit",
                },
                tracker=retrospective_tracker("validated"),
            )
        )

        self.assertEqual(record["repair_stop"]["classification"], "no_repair_delta")
        self.assertEqual(record["repair_stop"]["scope"], "target-work")
        self.assertEqual(
            record["repair_stop"]["evidence_paths"],
            ["/tmp/ledger/runs/impl/step-result.json"],
        )
        self.assertEqual(record["recommended_follow_up"], [])

    def test_build_pipeline_retrospective_keeps_process_failure_out_of_repair_stop(self):
        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=retrospective_state(),
                publication={
                    "status": "failed-needs-human",
                    "reason": "gh command failed",
                    "command": ["gh", "auth", "status", "--hostname", "github.com"],
                    "stderr_excerpt": "gh auth status failed",
                },
                tracker=retrospective_tracker("validated"),
            )
        )

        self.assertEqual(record["repair_stop"], {})
        self.assertEqual(record["signals"][0]["scope"], "pipeline-process")

    def test_build_pipeline_retrospective_recommends_target_follow_up_for_target_owned_worker_failure(self):
        state = retrospective_state()
        state["selected_work"] = [
            {
                "external_id": "central-umi2.5",
                "title": "Align bump-EQEmu validation worker with portable AFK contract",
                "labels": ["project:bump-eqemu", "ready-for-agent", "validation-worker"],
            }
        ]
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "classification": "worker_failure",
                    "summary": "failed_validation",
                    "actionable_failures": [
                        {
                            "name": "worker",
                            "category": "worker_failure",
                            "reason": "worker exited 1",
                            "log_path": "/tmp/ledger/runs/validate/validation-evidence/logs/validation.log",
                            "excerpt": "Zone |   Error    | Connect Connection [default] Failed to connect to database",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]

        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=state,
                publication={"status": "blocked", "reason": "repair budget exhausted: 5 attempts reached hard_cap=5"},
                tracker=retrospective_tracker("implemented"),
            )
        )

        self.assertEqual(record["signals"][0]["scope"], "target-work")
        self.assertEqual(record["signals"][1]["scope"], "target-work")
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "central-umi2.5: Fix worker [worker_failure]: Zone |   Error    | Connect Connection [default] Failed to connect to database",
                    "labels": ["afk:follow-up", "area:validation", "project:bump-eqemu"],
                }
            ],
        )

    def test_build_pipeline_retrospective_keeps_stack_binding_worker_failure_pipeline_owned(self):
        state = retrospective_state()
        state["selected_work"] = [
            {
                "external_id": "central-umi2.5",
                "title": "Align bump-EQEmu validation worker with portable AFK contract",
                "labels": ["project:bump-eqemu", "ready-for-agent", "validation-worker"],
            }
        ]
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "classification": "worker_failure",
                    "summary": "failed_validation",
                    "actionable_failures": [
                        {
                            "name": "worker",
                            "category": "worker_failure",
                            "reason": "failed_validation",
                            "log_path": "/tmp/ledger/runs/validate/validation-evidence/logs/stack.log",
                            "excerpt": "2026-07-01T02:30:42Z binding validation stack /tmp/stack code to /tmp/checkout",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]

        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=state,
                publication={"status": "blocked", "reason": "repair budget exhausted: 5 attempts reached hard_cap=5"},
                tracker=retrospective_tracker("implemented"),
            )
        )

        self.assertEqual(record["signals"][0]["scope"], "pipeline-process")
        self.assertEqual(record["signals"][1]["scope"], "pipeline-process")
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "Fix worker [worker_failure]: 2026-07-01T02:30:42Z binding validation stack /tmp/stack code to /tmp/checkout",
                    "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                }
            ],
        )

    def test_build_pipeline_retrospective_keeps_pipeline_follow_up_for_pipeline_validation_failure(self):
        state = retrospective_state()
        state["selected_work"] = [
            {
                "external_id": "central-umi2.5",
                "title": "Align bump-EQEmu validation worker with portable AFK contract",
                "labels": ["project:bump-eqemu", "ready-for-agent", "validation-worker"],
            }
        ]
        state["validations"] = [
            {
                "output": {
                    "status": "failed_missing_result",
                    "classification": "missing_worker_result",
                    "summary": "worker result file was not produced",
                    "actionable_failures": [
                        {
                            "name": "worker",
                            "status": "failed_missing_result",
                            "category": "missing_result",
                            "reason": "worker result file was not produced",
                            "log_path": "/tmp/ledger/runs/validate/stdout.log",
                            "excerpt": "worker result file was not produced",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]

        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=state,
                publication={"status": "blocked", "reason": "validate did not reach validated: failed_missing_result"},
                tracker=retrospective_tracker("implemented"),
            )
        )

        self.assertEqual(record["signals"][0]["scope"], "pipeline-process")
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "Fix worker [missing_result]: worker result file was not produced",
                    "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                }
            ],
        )
