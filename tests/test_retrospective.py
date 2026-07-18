import sys
import unittest
import warnings
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk import workstream  # noqa: E402
from afk import retrospective as retrospective_api  # noqa: E402
from afk.retrospective import (  # noqa: E402
    RetrospectiveContext,
    TerminalIntegrationRetrospectiveContext,
    build_pipeline_retrospective,
    build_terminal_integration_retrospective,
)


def retrospective_state():
    return {
        "selected_work": [
            {"external_id": "central-afk-pr.17", "title": "Delay tracker closure"}
        ],
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


def persisted_workstream_result_state(*, excerpt: str, log_path: str):
    return {
        "selected_work": [
            {
                "external_id": "central-umi2.5",
                "result": "failed",
                "source_id": "central-beads",
                "source_type": "beads",
                "title": "Align bump-EQEmu validation worker with portable AFK contract",
            }
        ],
        "steps": [
            {
                "name": "implement",
                "equivalent_command": [
                    "afk",
                    "run-step",
                    "implement",
                    "--input",
                    json.dumps(
                        {
                            "work_selection": {
                                "schema_version": 1,
                                "selected_work": [
                                    {
                                        "external_id": "central-umi2.5",
                                        "labels": [
                                            "project:bump-eqemu",
                                            "ready-for-agent",
                                            "validation-worker",
                                        ],
                                        "source_id": "central-beads",
                                        "source_type": "beads",
                                        "title": "Align bump-EQEmu validation worker with portable AFK contract",
                                    }
                                ],
                            }
                        }
                    ),
                    "--ledger",
                    "ledgers/dogfood/bump-eqemu-2026-07-07-1",
                    "--project",
                    "bump-eqemu",
                ],
            }
        ],
        "retry_attempts": [
            {
                "attempt": 6,
                "retry_number": 5,
                "repairing_failure_class": "failed_validation",
                "checkout_path": "/tmp/afk-dogfood-checkouts/bump-EQEmu",
                "review_branch": "afk/central-umi2-5",
                "commit": "e711bf2ff829eb5e231c632ba02b4e27026e6e5b",
                "status": "failed_validation",
            }
        ],
        "pipeline_retrospective": {
            "health": "failing",
            "signals": [
                {
                    "classification": "worker_failure",
                    "evidence_paths": [
                        log_path,
                        "/tmp/ledger/runs/validate/step-result.json",
                        "/tmp/ledger/runs/validate/worker-result.json",
                    ],
                    "excerpt": excerpt,
                    "kind": "validation-failure",
                    "scope": "pipeline-process",
                    "severity": "error",
                    "step": "worker",
                    "summary": excerpt,
                },
                {
                    "evidence_paths": [],
                    "kind": "retry-or-blocked",
                    "scope": "pipeline-process",
                    "severity": "error",
                    "summary": "repair budget exhausted: 5 attempts reached hard_cap=5",
                },
            ],
            "recommended_follow_up": [
                {
                    "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                    "summary": f"Fix worker [worker_failure]: {excerpt}",
                }
            ],
        },
        "cleanup": {"status": "clean", "resources": []},
    }


class RetrospectiveModuleTest(unittest.TestCase):
    def test_terminal_retrospective_records_merged_terminal_closure(self):
        record = build_terminal_integration_retrospective(
            TerminalIntegrationRetrospectiveContext(
                workstream={"workstream_id": "run-1"},
                integration={
                    "status": "tracker-closed",
                    "decision": "merge_ready",
                    "pr_url": "https://github.example/pr/17",
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                    },
                    "merge": {"status": "merged"},
                    "tracker_close": {"status": "closed"},
                },
            )
        )

        self.assertEqual(record["status"], "tracker-closed")
        self.assertEqual(record["terminal"]["status"], "tracker-closed")
        self.assertEqual(record["terminal"]["decision"]["status"], "merged")
        self.assertEqual(
            record["terminal"]["decision"]["merge_commit"],
            "deadbeef",
        )
        self.assertEqual(record["terminal"]["merge_status"], "merged")
        self.assertEqual(record["terminal"]["tracker_close_status"], "closed")

    def test_terminal_retrospective_records_no_merge_terminal_closure(self):
        record = build_terminal_integration_retrospective(
            TerminalIntegrationRetrospectiveContext(
                workstream={"workstream_id": "run-1"},
                integration={
                    "status": "tracker-closed",
                    "decision": "no_merge",
                    "terminal_decision": {
                        "status": "no-merge",
                        "reason": "checks failed",
                    },
                    "tracker_close": {"status": "closed"},
                },
            )
        )

        self.assertEqual(record["status"], "tracker-closed")
        self.assertEqual(record["terminal"]["status"], "tracker-closed")
        self.assertEqual(record["terminal"]["decision"]["status"], "no-merge")
        self.assertEqual(
            record["terminal"]["decision"]["reason"],
            "checks failed",
        )
        self.assertNotIn("merge_status", record["terminal"])
        self.assertEqual(record["terminal"]["tracker_close_status"], "closed")

    def test_terminal_retrospective_omits_terminal_block_before_closure(self):
        record = build_terminal_integration_retrospective(
            TerminalIntegrationRetrospectiveContext(
                workstream={"workstream_id": "run-1"},
                integration={
                    "status": "merge-ready",
                    "decision": "merge_ready",
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                    },
                    "merge": {"status": "merged"},
                },
            )
        )

        self.assertNotIn("terminal", record)

    def test_terminal_retrospective_omits_when_tracker_close_failed(self):
        record = build_terminal_integration_retrospective(
            TerminalIntegrationRetrospectiveContext(
                workstream={"workstream_id": "run-1"},
                integration={
                    "status": "tracker-close-failed",
                    "decision": "merge_ready",
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                    },
                    "merge": {"status": "merged"},
                    "tracker_close": {"status": "failed"},
                },
            )
        )

        self.assertNotIn("terminal", record)

    def test_retrospective_judge_runtime_preserves_literal_placeholders_in_python_code(self):
        import tempfile
        import textwrap

        judge_code = textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

            if "{request_path}" != "{" + "request_path}":
                raise SystemExit("request_path literal was rewritten")
            if "{result_path}" != "{" + "result_path}":
                raise SystemExit("result_path literal was rewritten")
            if "{prompt}" != "{" + "prompt}":
                raise SystemExit("prompt literal was rewritten")
            prompt = json.loads(sys.argv[1])
            if prompt["artifact_type"] != "retrospective-judge-request":
                raise SystemExit("prompt argument was not rendered")
            request_path = Path(sys.argv[2])
            result_path = Path(sys.argv[3])
            if not request_path.exists():
                raise SystemExit("request path argument was not rendered")
            result_path.write_text(
                json.dumps({"status": "pass", "summary": "runtime placeholders preserved"}),
                encoding="utf-8",
            )
            """
        ).strip()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            request_path = temp_path / "retrospective-judge-request.json"
            result_path = temp_path / "retrospective-judge-result.json"
            request = {"artifact_type": "retrospective-judge-request"}
            request_path.write_text(json.dumps(request), encoding="utf-8")
            adapter_result, raw_payload, raw_source = retrospective_api._run_retrospective_judge_command(
                {
                    "command": [
                        sys.executable,
                        "-c",
                        judge_code,
                        "{prompt}",
                        "{request_path}",
                        "{result_path}",
                    ],
                    "timeout_seconds": 10,
                },
                checkout_path=temp_path,
                request=request,
                request_prompt=request,
                request_path=request_path,
                result_path=result_path,
            )

        self.assertEqual(adapter_result["returncode"], 0)
        self.assertEqual(raw_source, "file")
        self.assertIn("runtime placeholders preserved", raw_payload)

    def test_retrospective_follow_up_runtime_preserves_literal_placeholders_in_python_code(self):
        import tempfile
        import textwrap

        follow_up_code = textwrap.dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path

            if "{request_path}" != "{" + "request_path}":
                raise SystemExit("request_path literal was rewritten")
            if "{result_path}" != "{" + "result_path}":
                raise SystemExit("result_path literal was rewritten")
            request_path = Path(sys.argv[1])
            result_path = Path(sys.argv[2])
            if request_path != Path(os.environ["AFK_RETROSPECTIVE_FOLLOW_UP_REQUEST"]):
                raise SystemExit("request path argument was not rendered")
            if result_path != Path(os.environ["AFK_RETROSPECTIVE_FOLLOW_UP_RESULT"]):
                raise SystemExit("result path argument was not rendered")
            if not request_path.exists():
                raise SystemExit("request path does not exist")
            result_path.write_text(
                json.dumps({"status": "pass", "summary": "follow-up placeholders preserved"}),
                encoding="utf-8",
            )
            """
        ).strip()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            request_path = temp_path / "retrospective-follow-up-request.json"
            result_path = temp_path / "retrospective-follow-up-result.json"
            request_path.write_text(
                json.dumps({"artifact_type": "retrospective-follow-up-request"}),
                encoding="utf-8",
            )
            adapter_result, raw_payload = retrospective_api._run_retrospective_follow_up_command(
                {
                    "command": [
                        sys.executable,
                        "-c",
                        follow_up_code,
                        "{request_path}",
                        "{result_path}",
                    ],
                    "timeout_seconds": 10,
                },
                checkout_path=temp_path,
                request_path=request_path,
                result_path=result_path,
            )

        self.assertEqual(adapter_result["returncode"], 0)
        self.assertIn("follow-up placeholders preserved", raw_payload)

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

    def test_build_pipeline_retrospective_reclassifies_persisted_target_owned_worker_failure(self):
        excerpt = "Zone |   Error    | Connect Connection [default] Failed to connect to database"
        state = persisted_workstream_result_state(
            excerpt=excerpt,
            log_path="/tmp/ledger/runs/validate/validation-evidence/logs/validation.log",
        )

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
                    "summary": f"central-umi2.5: Fix worker [worker_failure]: {excerpt}",
                    "labels": ["afk:follow-up", "area:validation", "project:bump-eqemu"],
                }
            ],
        )

    def test_build_pipeline_retrospective_replays_historical_dogfood_failure(self):
        excerpt = (
            "Zone |   Error    | Connect Connection [default] Failed to connect "
            "to database Error [#2002: Can't connect to server on 'mariadb' (115)]"
        )
        state = persisted_workstream_result_state(
            excerpt=excerpt,
            log_path="/tmp/ledger/runs/validate/validation-evidence/logs/validation.log",
        )
        state["publication"] = {
            "status": "blocked",
            "reason": "repair budget exhausted: 5 attempts reached hard_cap=5",
        }
        state["tracker"] = retrospective_tracker("implemented")

        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=state,
                publication=state["publication"],
                tracker=state["tracker"],
            )
        )

        self.assertEqual(record["signals"][0]["scope"], "target-work")
        self.assertEqual(record["signals"][1]["scope"], "target-work")
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": (
                        "central-umi2.5: Fix worker [worker_failure]: Zone |   Error    | Connect Connection "
                        "[default] Failed to connect to database Error [#2002: Can't connect to server on "
                        "'mariadb' (115)]"
                    ),
                    "labels": ["afk:follow-up", "area:validation", "project:bump-eqemu"],
                }
            ],
        )

    def test_build_pipeline_retrospective_ignores_dry_run_title_when_replaying_persisted_target_failure(self):
        excerpt = "Zone |   Error    | Connect Connection [default] Failed to connect to database"
        state = persisted_workstream_result_state(
            excerpt=excerpt,
            log_path="/tmp/ledger/runs/validate/validation-evidence/logs/validation.log",
        )
        state["selected_work"][0]["title"] = "Document dry-run validation smoke coverage"

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
                    "summary": f"central-umi2.5: Fix worker [worker_failure]: {excerpt}",
                    "labels": ["afk:follow-up", "area:validation", "project:bump-eqemu"],
                }
            ],
        )

    def test_build_pipeline_retrospective_does_not_infer_project_label_from_retry_checkout_basename(self):
        excerpt = "Zone |   Error    | Connect Connection [default] Failed to connect to database"
        state = persisted_workstream_result_state(
            excerpt=excerpt,
            log_path="/tmp/ledger/runs/validate/validation-evidence/logs/validation.log",
        )
        state["steps"] = []
        state["selected_work"][0]["title"] = "Document dry-run validation smoke coverage"
        state["retry_attempts"][0]["checkout_path"] = "/tmp/afk-dogfood-checkouts/dry-run"

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
                    "summary": f"Fix worker [worker_failure]: {excerpt}",
                    "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                }
            ],
        )

    def test_build_pipeline_retrospective_keeps_persisted_stack_binding_worker_failure_pipeline_owned(self):
        excerpt = "2026-07-01T02:30:42Z binding validation stack /tmp/stack code to /tmp/checkout"
        state = persisted_workstream_result_state(
            excerpt=excerpt,
            log_path="/tmp/ledger/runs/validate/validation-evidence/logs/stack.log",
        )

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
                    "summary": f"Fix worker [worker_failure]: {excerpt}",
                    "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                }
            ],
        )
