import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.workstream import (  # noqa: E402
    _apply_retrospective_judge,
    WorkstreamError,
    normalize_retrospective_follow_up_config,
    normalize_retrospective_judge,
    pipeline_retrospective_record,
    tracker_record,
    workstream_status_from_publication,
)


def tracker_state(*, terminal_decision=None):
    return {
        "selected_work": [{"external_id": "central-afk-pr.17", "title": "Delay tracker closure"}],
        "implementation": {
            "status": "implemented",
            "git": {"after_commit": "abc123"},
        },
        "validations": [
            {
                "output": {
                    "status": "validated",
                    "checkout": {"start_commit": "abc123"},
                }
            }
        ],
        "review": {
            "status": "passed",
            "summary": "ready for human review",
            "reviewer_result": {"findings": []},
        },
        "tracker": {
            "terminal_decision": terminal_decision
            or {
                "status": "",
                "merge_commit": "",
                "reason": "",
                "pr_url": "",
                "review_feedback_status": "",
            }
        },
    }


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


class WorkstreamStatusMappingTest(unittest.TestCase):
    def test_normalize_retrospective_follow_up_config_rejects_empty_command(self):
        with self.assertRaisesRegex(WorkstreamError, "command must not be empty"):
            normalize_retrospective_follow_up_config(
                {
                    "enabled": True,
                    "type": "fake-follow-up-command",
                    "command": [],
                }
            )

    def test_normalize_retrospective_follow_up_config_rejects_secret_literals_in_command(self):
        with self.assertRaisesRegex(WorkstreamError, "secret-looking values"):
            normalize_retrospective_follow_up_config(
                {
                    "enabled": True,
                    "type": "fake-follow-up-command",
                    "command": [
                        sys.executable,
                        "-c",
                        "print('Bearer abc123secretXYZ')",
                    ],
                }
            )

    def test_normalize_retrospective_follow_up_config_rejects_auth_env_keys(self):
        with self.assertRaisesRegex(WorkstreamError, "retrospective_follow_up.env is not supported"):
            normalize_retrospective_follow_up_config(
                {
                    "enabled": True,
                    "type": "fake-follow-up-command",
                    "command": [sys.executable, "-c", "print('ok')"],
                    "env": {"GH_TOKEN": "ghp_secret_1234567890"},
                }
            )

    def test_normalize_retrospective_judge_rejects_empty_command(self):
        with self.assertRaisesRegex(WorkstreamError, "command must not be empty"):
            normalize_retrospective_judge(
                {
                    "enabled": True,
                    "type": "fake-judge-command",
                    "command": [],
                }
            )

    def test_normalize_retrospective_judge_rejects_secret_literals_in_command(self):
        with self.assertRaisesRegex(WorkstreamError, "secret-looking values"):
            normalize_retrospective_judge(
                {
                    "enabled": True,
                    "type": "fake-judge-command",
                    "command": [
                        sys.executable,
                        "-c",
                        "print('Bearer abc123secretXYZ')",
                    ],
                }
            )

    def test_normalize_retrospective_judge_rejects_non_pi_mounts_without_checkout_path(self):
        with self.subTest("existing absolute paths"), self.assertRaisesRegex(
            WorkstreamError,
            "only supported when retrospective_judge.command uses pi --provider openai-codex",
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                codex_home = temp_path / "codex-home"
                config_home = temp_path / "xdg-config"
                pi_config_home = temp_path / "pi-config"
                pi_coding_agent_dir = temp_path / "pi-coding-agent"
                codex_home.mkdir()
                config_home.mkdir()
                pi_config_home.mkdir()
                pi_coding_agent_dir.mkdir()
                normalize_retrospective_judge(
                    {
                        "enabled": True,
                        "type": "local-command",
                        "command": [sys.executable, "-c", "print('judge should not run')"],
                        "codex_home": str(codex_home),
                        "config_home": str(config_home),
                        "env": {
                            "PI_CONFIG_HOME": str(pi_config_home),
                            "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                        },
                    }
                )

    def test_normalize_retrospective_judge_rejects_relative_openai_codex_mounts_without_checkout_path(self):
        with self.assertRaisesRegex(WorkstreamError, "retrospective_judge.codex_home must be absolute"):
            normalize_retrospective_judge(
                {
                    "enabled": True,
                    "type": "local-command",
                    "command": ["pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                    "codex_home": "relative-codex-home",
                    "config_home": "/tmp/xdg-config",
                    "env": {
                        "PI_CONFIG_HOME": "/tmp/pi-config",
                        "PI_CODING_AGENT_DIR": "/tmp/pi-coding-agent",
                    },
                }
            )

    def test_normalize_retrospective_judge_rejects_mount_inside_later_checkout_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout_one = temp_path / "checkout-one"
            checkout_two = temp_path / "checkout-two"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = checkout_two / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            checkout_one.mkdir()
            checkout_two.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()

            with self.assertRaisesRegex(WorkstreamError, "retrospective_judge.env.PI_CONFIG_HOME must be outside checkout"):
                normalize_retrospective_judge(
                    {
                        "enabled": True,
                        "type": "local-command",
                        "command": ["pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                        "codex_home": str(codex_home),
                        "config_home": str(config_home),
                        "env": {
                            "PI_CONFIG_HOME": str(pi_config_home),
                            "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                        },
                    },
                    checkout_path=checkout_one,
                    checkout_paths=[checkout_one, checkout_two],
                )

    def test_pipeline_retrospective_record_marks_judge_disabled_by_default(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
        )

        self.assertEqual(
            record["judge"],
            {
                "enabled": False,
                "status": "disabled",
            },
        )
        self.assertEqual(
            record["follow_up"]["creation"],
            {
                "enabled": False,
                "status": "recommendation-only",
            },
        )

    def test_pipeline_retrospective_record_reports_clean_published_run_without_signals(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
        )

        self.assertEqual(record["status"], "published")
        self.assertEqual(record["health"], "healthy")
        self.assertEqual(record["publication_status"], "published")
        self.assertEqual(record["tracker_status"], "awaiting-review")
        self.assertEqual(record["signals"], [])
        self.assertEqual(record["recommended_follow_up"], [])
        self.assertEqual(record["follow_up"]["recommended"], [])
        self.assertEqual(record["follow_up"]["created"], [])

    def test_pipeline_retrospective_record_surfaces_missing_tool_validation_signal(self):
        state = retrospective_state()
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "summary": "failed_validation",
                    "actionable_failures": [
                        {
                            "category": "validation",
                            "reason": "python3.13: command not found",
                            "log_path": "/tmp/ledger/runs/validate/stdout.log",
                            "excerpt": "python3.13: command not found token=ghp_validation_secret_1234567890",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]
        record = pipeline_retrospective_record(
            state,
            {"status": "blocked", "reason": "required final validation evidence did not pass: tier1"},
            retrospective_tracker("selected"),
        )

        self.assertEqual(record["health"], "failing")
        self.assertEqual(
            [signal["kind"] for signal in record["signals"]],
            ["missing-tool-or-config", "retry-or-blocked"],
        )
        self.assertEqual(record["signals"][0]["severity"], "error")
        self.assertIn("python3.13: command not found", record["signals"][0]["summary"])
        self.assertIn("[REDACTED]", record["signals"][0]["summary"])
        self.assertEqual(
            record["signals"][0]["evidence_paths"],
            [
                "/tmp/ledger/runs/validate/stdout.log",
                "/tmp/ledger/runs/validate/step-result.json",
                "/tmp/ledger/runs/validate/worker-result.json",
            ],
        )
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "Fix tier1 [validation]: python3.13: command not found token=[REDACTED]",
                    "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                }
            ],
        )
        self.assertEqual(record["follow_up"]["recommended"][0]["kind"], "missing-tool-or-config")
        self.assertEqual(
            record["follow_up"]["recommended"][0]["summary"],
            "Fix tier1 [validation]: python3.13: command not found token=[REDACTED]",
        )
        self.assertEqual(
            record["follow_up"]["recommended"][0]["labels"],
            ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
        )
        self.assertTrue(record["follow_up"]["recommended"][0]["fingerprint"].startswith("retro-follow-up:"))

    def test_pipeline_retrospective_record_surfaces_missing_tool_validation_signal_without_log_path(self):
        state = retrospective_state()
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "summary": "failed_validation",
                    "actionable_failures": [
                        {
                            "category": "validation",
                            "reason": "python3.13: command not found",
                            "excerpt": "python3.13: command not found token=ghp_validation_secret_1234567890",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]
        record = pipeline_retrospective_record(
            state,
            {"status": "blocked", "reason": "required final validation evidence did not pass: tier1"},
            retrospective_tracker("selected"),
        )

        self.assertEqual(record["signals"][0]["kind"], "missing-tool-or-config")
        self.assertEqual(record["signals"][0]["step"], "tier1")
        self.assertEqual(record["signals"][0]["classification"], "validation")
        self.assertIn("python3.13: command not found", record["signals"][0]["excerpt"])
        self.assertEqual(
            record["signals"][0]["evidence_paths"],
            [
                "/tmp/ledger/runs/validate/step-result.json",
                "/tmp/ledger/runs/validate/worker-result.json",
            ],
        )
        self.assertEqual(
            record["recommended_follow_up"][0]["summary"],
            "Fix tier1 [validation]: python3.13: command not found token=[REDACTED]",
        )

    def test_pipeline_retrospective_record_skips_ignorable_validation_failure_before_missing_tool_signal(self):
        state = retrospective_state()
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "summary": "failed_validation",
                    "actionable_failures": [
                        {
                            "category": "validation",
                            "reason": "command exited with status 1",
                        },
                        {
                            "category": "validation",
                            "reason": "python3.13: command not found",
                            "excerpt": "python3.13: command not found token=ghp_validation_secret_1234567890",
                        },
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]
        record = pipeline_retrospective_record(
            state,
            {"status": "blocked", "reason": "required final validation evidence did not pass: tier1"},
            retrospective_tracker("selected"),
        )

        self.assertEqual(record["signals"][0]["kind"], "missing-tool-or-config")
        self.assertEqual(
            record["recommended_follow_up"][0]["summary"],
            "Fix tier1 [validation]: python3.13: command not found token=[REDACTED]",
        )

    def test_pipeline_retrospective_record_prefers_specific_validation_follow_up_over_judge_generic(self):
        state = retrospective_state()
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "summary": "failed_validation",
                    "actionable_failures": [
                        {
                            "name": "tier1",
                            "category": "compiler",
                            "reason": "command exited with status 1",
                            "log_path": "/tmp/ledger/runs/validate/validation-evidence/logs/validation.log",
                            "excerpt": "zone/harness/zone_harness_runtime.cpp:98:9 error: SetBotID is a private member of Bot",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]
        record = pipeline_retrospective_record(
            state,
            {"status": "blocked", "reason": "required final validation evidence did not pass: tier1"},
            retrospective_tracker("selected"),
        )

        record = _apply_retrospective_judge(
            record,
            {
                "enabled": True,
                "status": "failed",
                "summary": "review judge findings",
                "evidence": {
                    "request_path": "retrospective-judge-request.json",
                    "result_path": "retrospective-judge-result.json",
                    "stdout_path": "retrospective-judge-stdout.log",
                    "stderr_path": "retrospective-judge-stderr.log",
                },
            },
            normalized=None,
            publication={"status": "blocked", "reason": "required final validation evidence did not pass: tier1"},
        )

        self.assertEqual(record["signals"][0]["kind"], "validation-failure")
        self.assertEqual(record["follow_up"]["recommended"][0]["kind"], "validation-failure")
        self.assertIn("tier1", record["follow_up"]["recommended"][0]["summary"])
        self.assertIn("compiler", record["follow_up"]["recommended"][0]["summary"])
        self.assertIn("SetBotID is a private member of Bot", record["follow_up"]["recommended"][0]["summary"])
        self.assertNotIn(
            "Review and address retrospective judge findings before treating the run as complete.",
            [item["summary"] for item in record["follow_up"]["recommended"]],
        )

    def test_pipeline_retrospective_record_keeps_retrospective_judge_follow_up_for_generic_publication_failure(self):
        publication = {
            "status": "failed-needs-human",
            "reason": "git command failed",
            "command": ["git", "push", "origin", "HEAD"],
            "stderr_excerpt": "fatal: unable to access https://github.example/repo.git/",
        }
        record = pipeline_retrospective_record(
            retrospective_state(),
            publication,
            retrospective_tracker("validated"),
        )

        record = _apply_retrospective_judge(
            record,
            {
                "enabled": True,
                "status": "failed",
                "summary": "review judge findings",
                "evidence": {
                    "request_path": "retrospective-judge-request.json",
                    "result_path": "retrospective-judge-result.json",
                    "stdout_path": "retrospective-judge-stdout.log",
                    "stderr_path": "retrospective-judge-stderr.log",
                },
            },
            normalized=None,
            publication=publication,
        )

        self.assertEqual(
            [item["kind"] for item in record["follow_up"]["recommended"]],
            ["publisher-failure", "retrospective-judge"],
        )

    def test_pipeline_retrospective_record_keeps_retrospective_judge_follow_up_for_publisher_auth_failure(self):
        publication = {
            "status": "failed-needs-human",
            "reason": "gh command failed",
            "command": ["gh", "auth", "status", "--hostname", "github.com"],
            "stderr_excerpt": "gh auth status failed token=ghp_auth_secret_1234567890",
        }
        record = pipeline_retrospective_record(
            retrospective_state(),
            publication,
            retrospective_tracker("validated"),
        )

        record = _apply_retrospective_judge(
            record,
            {
                "enabled": True,
                "status": "failed",
                "summary": "review judge findings",
                "evidence": {
                    "request_path": "retrospective-judge-request.json",
                    "result_path": "retrospective-judge-result.json",
                    "stdout_path": "retrospective-judge-stdout.log",
                    "stderr_path": "retrospective-judge-stderr.log",
                },
            },
            normalized=None,
            publication=publication,
        )

        self.assertEqual(
            [item["kind"] for item in record["follow_up"]["recommended"]],
            ["publisher-auth", "retrospective-judge"],
        )

    def test_pipeline_retrospective_record_omits_retrospective_judge_follow_up_for_publication_missing_tool_failure(self):
        publication = {
            "status": "failed-needs-human",
            "reason": "publisher.gh.auth.config_dir must be outside checkout token=ghp_publication_secret_1234567890",
            "command": ["gh", "pr", "create"],
        }
        record = pipeline_retrospective_record(
            retrospective_state(),
            publication,
            retrospective_tracker("validated"),
        )

        record = _apply_retrospective_judge(
            record,
            {
                "enabled": True,
                "status": "failed",
                "summary": "review judge findings",
                "evidence": {
                    "request_path": "retrospective-judge-request.json",
                    "result_path": "retrospective-judge-result.json",
                    "stdout_path": "retrospective-judge-stdout.log",
                    "stderr_path": "retrospective-judge-stderr.log",
                },
            },
            normalized=None,
            publication=publication,
        )

        self.assertEqual(
            [item["kind"] for item in record["follow_up"]["recommended"]],
            ["missing-tool-or-config"],
        )

    def test_pipeline_retrospective_record_omits_generic_retry_follow_up_when_specific_validation_failure_exists(self):
        state = retrospective_state()
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "summary": "failed_validation",
                    "actionable_failures": [
                        {
                            "name": "tier1",
                            "category": "compiler",
                            "reason": "command exited with status 1",
                            "log_path": "/tmp/ledger/runs/validate/validation-evidence/logs/validation.log",
                            "excerpt": "actor_action_queue_repository.h:355:5: error: no viable conversion from 'int' to 'std::string'",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]
        record = pipeline_retrospective_record(
            state,
            {"status": "blocked", "reason": "validate did not reach validated: failed_validation"},
            retrospective_tracker("implemented"),
        )

        self.assertEqual(
            [item["kind"] for item in record["follow_up"]["recommended"]],
            ["validation-failure"],
        )
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "Fix tier1 [compiler]: actor_action_queue_repository.h:355:5: error: no viable conversion from 'int' to 'std::string'",
                    "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                }
            ],
        )
        self.assertEqual(record["signals"][1]["kind"], "retry-or-blocked")

    def test_pipeline_retrospective_record_surfaces_blocked_reason_without_retry_keyword(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "blocked", "reason": "required final validation evidence is missing"},
            retrospective_tracker("selected"),
        )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["health"], "failing")
        self.assertEqual(record["signals"][0]["kind"], "retry-or-blocked")
        self.assertEqual(record["signals"][0]["severity"], "error")
        self.assertIn("required final validation evidence is missing", record["signals"][0]["summary"])

    def test_pipeline_retrospective_record_does_not_treat_schema_errors_as_missing_config(self):
        state = retrospective_state()
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "summary": "validation.required_artifacts must be a non-empty list",
                    "actionable_failures": [
                        {
                            "category": "validation",
                            "reason": "validation.required_artifacts must be a non-empty list",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]

        record = pipeline_retrospective_record(
            state,
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
        )

        self.assertEqual(record["health"], "healthy")
        self.assertEqual(record["signals"], [])
        self.assertEqual(record["recommended_follow_up"], [])

    def test_pipeline_retrospective_record_does_not_treat_app_no_such_file_text_as_missing_config(self):
        state = retrospective_state()
        state["validations"] = [
            {
                "output": {
                    "status": "failed_validation",
                    "summary": "AssertionError: expected app to report 'No such file or directory'",
                    "actionable_failures": [
                        {
                            "category": "validation",
                            "reason": "AssertionError: expected app to report 'No such file or directory'",
                        }
                    ],
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ]

        record = pipeline_retrospective_record(
            state,
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
        )

        self.assertEqual(record["health"], "healthy")
        self.assertEqual(record["signals"], [])
        self.assertEqual(record["recommended_follow_up"], [])

    def test_pipeline_retrospective_record_reads_missing_config_signal_from_publication_reason(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {
                "status": "failed-needs-human",
                "reason": "publisher.gh.auth.config_dir must be outside checkout token=ghp_publication_secret_1234567890",
                "command": ["gh", "pr", "create"],
            },
            retrospective_tracker("validated"),
        )

        self.assertEqual(record["health"], "failing")
        self.assertEqual(record["signals"][0]["kind"], "missing-tool-or-config")
        self.assertEqual(record["signals"][0]["severity"], "error")
        self.assertIn("publisher.gh.auth.config_dir must be outside checkout", record["signals"][0]["summary"])
        self.assertIn("[REDACTED]", record["signals"][0]["summary"])
        self.assertEqual(record["signals"][0]["step"], "gh pr create")
        self.assertEqual(record["signals"][0]["classification"], "missing-tool-or-config")
        self.assertIn("publisher.gh.auth.config_dir must be outside checkout", record["signals"][0]["excerpt"])
        self.assertEqual(record["signals"][0]["evidence_paths"], ["publication-result.json"])
        self.assertEqual(
            record["recommended_follow_up"][0]["labels"],
            ["afk:follow-up", "area:publication", "project:afk-composable-pipeline"],
        )

    def test_pipeline_retrospective_record_surfaces_publisher_auth_failure(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {
                "status": "failed-needs-human",
                "reason": "gh command failed",
                "command": ["gh", "auth", "status", "--hostname", "github.com"],
                "stderr_excerpt": "gh auth status failed token=ghp_auth_secret_1234567890",
            },
            retrospective_tracker("validated"),
        )

        self.assertEqual(record["health"], "failing")
        self.assertEqual(record["signals"][0]["kind"], "publisher-auth")
        self.assertEqual(record["signals"][0]["severity"], "error")
        self.assertEqual(record["signals"][0]["step"], "gh auth status")
        self.assertEqual(record["signals"][0]["classification"], "publisher-auth")
        self.assertIn("gh auth status failed", record["signals"][0]["summary"])
        self.assertIn("[REDACTED]", record["signals"][0]["summary"])
        self.assertIn("gh auth status failed", record["signals"][0]["excerpt"])
        self.assertEqual(record["signals"][0]["evidence_paths"], ["publication-result.json"])
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "Repair GitHub publisher authentication evidence before rerunning terminal publication.",
                    "labels": ["afk:follow-up", "area:publication", "project:afk-composable-pipeline"],
                }
            ],
        )

    def test_pipeline_retrospective_record_keeps_generic_publisher_follow_up_fingerprint_compatible(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {
                "status": "failed-needs-human",
                "reason": "git command failed",
                "command": ["git", "push", "origin", "HEAD"],
                "stderr_excerpt": "fatal: unable to access https://github.example/repo.git/",
            },
            retrospective_tracker("validated"),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "created": [
                            {
                                "summary": "Address the blocked publication or retry evidence before rerunning the workstream.",
                                "labels": ["afk:follow-up", "area:workstream"],
                            }
                        ]
                    }
                }
            },
        )

        self.assertEqual(record["signals"][0]["kind"], "publisher-failure")
        self.assertEqual(record["signals"][0]["step"], "git push")
        self.assertEqual(record["signals"][0]["classification"], "publisher-failure")
        self.assertIn("fatal: unable to access", record["signals"][0]["excerpt"])
        self.assertEqual(record["signals"][0]["evidence_paths"], ["publication-result.json"])
        self.assertEqual(record["recommended_follow_up"], [])

    def test_pipeline_retrospective_record_surfaces_retry_block_and_dirty_cleanup(self):
        state = retrospective_state()
        state["cleanup"] = {
            "status": "dirty_retry_checkouts",
            "resources": [
                {
                    "kind": "retry_checkout",
                    "path": "/tmp/ledger/retries/checkout-2",
                    "branch": "afk/central-afk-pr.17",
                    "commit": "abc123",
                    "status": "dirty",
                }
            ],
        }
        record = pipeline_retrospective_record(
            state,
            {
                "status": "blocked",
                "reason": "retry checkout blocked: prior retry checkout is dirty and still needs cleanup",
            },
            retrospective_tracker("implemented"),
        )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["health"], "failing")
        self.assertEqual(
            [signal["kind"] for signal in record["signals"]],
            ["retry-or-blocked", "dirty-cleanup"],
        )
        self.assertEqual(record["signals"][0]["severity"], "error")
        self.assertEqual(record["signals"][1]["severity"], "warning")
        self.assertEqual(
            record["signals"][1]["evidence_paths"],
            ["/tmp/ledger/retries/checkout-2"],
        )
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "Address the blocked publication or retry evidence before rerunning the workstream.",
                    "labels": ["afk:follow-up", "area:workstream", "project:afk-composable-pipeline"],
                },
                {
                    "summary": "Clean up leftover workstream resources before starting another retry or publication attempt.",
                    "labels": ["afk:follow-up", "area:cleanup", "project:afk-composable-pipeline"],
                },
            ],
        )

    def test_pipeline_retrospective_record_includes_redacted_configured_follow_up(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "recommended": [
                            {
                                "summary": "Capture token=ghp_follow_up_secret_1234567890 remediation notes.",
                                "labels": ["project:afk-composable-pipeline"],
                            }
                        ]
                    }
                }
            },
        )

        self.assertEqual(record["health"], "healthy")
        self.assertEqual(
            record["recommended_follow_up"][0]["labels"],
            ["project:afk-composable-pipeline"],
        )
        self.assertIn("[REDACTED]", record["recommended_follow_up"][0]["summary"])

    def test_pipeline_retrospective_record_deduplicates_configured_and_signal_follow_up(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {
                "status": "failed-needs-human",
                "reason": "gh auth status failed",
            },
            retrospective_tracker("validated"),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "recommended": [
                            {
                                "summary": "Repair GitHub publisher authentication evidence before rerunning terminal publication.",
                                "labels": ["afk:follow-up", "area:publication"],
                            }
                        ]
                    }
                }
            },
        )

        self.assertEqual(record["health"], "failing")
        self.assertEqual(len(record["follow_up"]["recommended"]), 1)
        self.assertEqual(len(record["recommended_follow_up"]), 1)
        configured_record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "recommended": [
                            {
                                "summary": "Repair GitHub publisher authentication evidence before rerunning terminal publication.",
                                "labels": ["afk:follow-up", "area:publication"],
                            }
                        ]
                    }
                }
            },
        )
        self.assertEqual(
            record["follow_up"]["recommended"][0]["fingerprint"],
            configured_record["follow_up"]["recommended"][0]["fingerprint"],
        )

    def test_pipeline_retrospective_record_does_not_recommend_created_follow_up_again(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {
                "status": "failed-needs-human",
                "reason": "gh auth status failed",
            },
            retrospective_tracker("validated"),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "created": [
                            {
                                "summary": "Repair GitHub publisher authentication evidence before rerunning terminal publication.",
                                "labels": ["afk:follow-up", "area:publication"],
                            }
                        ]
                    }
                }
            },
        )

        self.assertEqual(record["health"], "failing")
        self.assertEqual(record["signals"][0]["kind"], "publisher-auth")
        self.assertEqual(record["recommended_follow_up"], [])

    def test_pipeline_retrospective_record_recommends_when_created_follow_up_summary_has_different_labels(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {
                "status": "failed-needs-human",
                "reason": "gh auth status failed",
            },
            retrospective_tracker("validated"),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "created": [
                            {
                                "summary": "Repair GitHub publisher authentication evidence before rerunning terminal publication.",
                                "labels": ["project:other"],
                            }
                        ]
                    }
                }
            },
        )

        self.assertEqual(record["health"], "failing")
        self.assertEqual(record["signals"][0]["kind"], "publisher-auth")
        self.assertEqual(len(record["recommended_follow_up"]), 1)

    def test_pipeline_retrospective_record_recommends_when_created_follow_up_has_only_id(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {
                "status": "failed-needs-human",
                "reason": "gh auth status failed",
            },
            retrospective_tracker("validated"),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "created": [
                            {
                                "id": "central-4x9.99",
                            }
                        ]
                    }
                }
            },
        )

        self.assertEqual(record["health"], "failing")
        self.assertEqual(record["signals"][0]["kind"], "publisher-auth")
        self.assertEqual(
            record["recommended_follow_up"],
            [
                {
                    "summary": "Repair GitHub publisher authentication evidence before rerunning terminal publication.",
                    "labels": ["afk:follow-up", "area:publication", "project:afk-composable-pipeline"],
                }
            ],
        )

    def test_pipeline_retrospective_record_deduplicates_configured_created_follow_up(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "created": [
                            {
                                "id": "central-4x9.99",
                            },
                            {
                                "id": "central-4x9.99",
                                "summary": "Document follow-up creation.",
                                "labels": ["area:retro"],
                            },
                        ]
                    }
                }
            },
        )

        self.assertEqual(len(record["follow_up"]["created"]), 1)
        self.assertEqual(record["follow_up"]["created"][0]["id"], "central-4x9.99")
        self.assertEqual(record["follow_up"]["created"][0]["summary"], "Document follow-up creation.")

    def test_pipeline_retrospective_record_ignores_blank_configured_follow_up_entries(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "recommended": [{}],
                        "created": [{}],
                    }
                }
            },
        )

        self.assertEqual(record["follow_up"]["recommended"], [])
        self.assertEqual(record["follow_up"]["created"], [])

    def test_pipeline_retrospective_record_removes_recommended_follow_up_already_created(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "recommended": [
                            {
                                "summary": "Document follow-up creation.",
                                "labels": ["area:retro"],
                            }
                        ],
                        "created": [
                            {
                                "id": "central-4x9.99",
                                "summary": "Document follow-up creation.",
                                "labels": ["area:retro"],
                            }
                        ],
                    }
                }
            },
        )

        self.assertEqual(record["follow_up"]["recommended"], [])
        self.assertEqual(record["recommended_follow_up"], [])
        self.assertEqual(len(record["follow_up"]["created"]), 1)

    def test_pipeline_retrospective_record_merges_fingerprint_only_and_id_created_follow_up(self):
        record = pipeline_retrospective_record(
            retrospective_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
            retrospective_tracker(),
            normalized={
                "retrospective": {
                    "follow_up": {
                        "created": [
                            {
                                "summary": "Document follow-up creation.",
                                "labels": ["area:retro"],
                                "fingerprint": "retro-follow-up:123",
                            },
                            {
                                "id": "central-4x9.99",
                                "summary": "Document follow-up creation.",
                                "labels": ["area:retro"],
                            },
                        ]
                    }
                }
            },
        )

        self.assertEqual(len(record["follow_up"]["created"]), 1)
        self.assertEqual(record["follow_up"]["created"][0]["id"], "central-4x9.99")
        self.assertTrue(record["follow_up"]["created"][0]["fingerprint"].startswith("retro-follow-up:"))

    def test_workstream_status_from_publication_explicit_terminal_states(self):
        self.assertEqual(
            workstream_status_from_publication({"status": "published"}),
            "published",
        )
        self.assertEqual(
            workstream_status_from_publication(
                {"status": "validated-unpublished"},
            ),
            "validated-unpublished",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "tracker-close-blocked"}),
            "review-findings-open",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "tracker-closed"}),
            "closed",
        )

    def test_workstream_status_from_tracker_close_blocked_uses_tracker_status_when_available(self):
        self.assertEqual(
            workstream_status_from_publication(
                {"status": "tracker-close-blocked"},
                {"status": "validated"},
            ),
            "validated",
        )
        self.assertEqual(
            workstream_status_from_publication(
                {"status": "tracker-close-blocked"},
                {"status": "review-findings-open"},
            ),
            "review-findings-open",
        )

    def test_workstream_status_from_publication_unknown_status_defaults(
        self,
    ):
        self.assertEqual(
            workstream_status_from_publication({"status": "mystery_status"}),
            "failed-needs-human",
        )

    def test_tracker_record_keeps_published_pr_backed_work_open(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
            },
            tracker_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
        )

        self.assertEqual(record["status"], "awaiting-review")
        self.assertFalse(record["close_source_item"])
        self.assertEqual(record["close_reason"], "")
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")

    def test_tracker_record_closes_only_after_merge_commit_is_recorded(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
            },
            tracker_state(
                terminal_decision={
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "reason": "",
                    "pr_url": "https://github.example/pr/17",
                    "review_feedback_status": "",
                }
            ),
            {"status": "tracker-closed", "url": "https://github.example/pr/17"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(record["merge_commit"], "deadbeef")
        self.assertEqual(record["close_reason"], "merged via deadbeef")
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")

    def test_tracker_record_closes_after_explicit_no_merge_decision(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "no-merge",
                        "merge_commit": "",
                        "reason": "Superseded by follow-up PR",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
            },
            tracker_state(
                terminal_decision={
                    "status": "no-merge",
                    "merge_commit": "",
                    "reason": "Superseded by follow-up PR",
                    "pr_url": "https://github.example/pr/17",
                    "review_feedback_status": "",
                }
            ),
            {"status": "tracker-closed", "url": "https://github.example/pr/17"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(
            record["close_reason"],
            "Superseded by follow-up PR",
        )
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")

    def test_tracker_record_surfaces_open_review_cycle_findings(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
                "review_cycles": [
                    {
                        "cycle": 1,
                        "status": "findings-open",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "findings-open",
                                "summary": "Needs response",
                                "requires_response": True,
                            }
                        ],
                    }
                ],
            },
            tracker_state(),
            {"status": "validated-unpublished"},
        )

        self.assertEqual(record["status"], "review-findings-open")
        self.assertIn("response-required review findings", record["comment"])

    def test_tracker_record_keeps_request_changes_open_until_response_is_addressed(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
                "review_cycles": [
                    {
                        "cycle": 1,
                        "status": "request-changes",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "request-changes",
                                "summary": "Please fix the tracker semantics.",
                                "requires_response": True,
                                "response": {"status": "wip", "summary": "Investigating"},
                            }
                        ],
                    }
                ],
            },
            tracker_state(),
            {"status": "validated-unpublished"},
        )

        self.assertEqual(record["status"], "review-findings-open")
        self.assertIn("response-required review findings", record["comment"])

    def test_tracker_record_accepts_freeform_response_string_as_addressed_evidence(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
                "review_cycles": [
                    {
                        "cycle": 1,
                        "status": "request-changes",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "request-changes",
                                "summary": "Please fix the tracker semantics.",
                                "requires_response": True,
                                "response": "Patched in follow-up commit abc123.",
                            }
                        ],
                    }
                ],
            },
            tracker_state(),
            {"status": "validated-unpublished"},
        )

        self.assertEqual(record["status"], "review-feedback-addressed")
        self.assertNotIn("response-required review findings", record["comment"])

    def test_tracker_record_keeps_terminal_merge_open_until_feedback_resolution_is_recorded(self):
        review_cycles = [
            {
                "cycle": 1,
                "status": "request-changes",
                "reviews": [
                    {
                        "role": "correctness",
                        "status": "request-changes",
                        "summary": "Please fix the tracker semantics.",
                        "requires_response": True,
                    }
                ],
            }
        ]
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
                "review_cycles": review_cycles,
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "review-findings-open")
        self.assertFalse(record["close_source_item"])
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")
        self.assertIn("terminal decision is recorded", record["comment"])

    def test_tracker_record_closes_terminal_merge_when_feedback_is_explicitly_resolved(self):
        review_cycles = [
            {
                "cycle": 1,
                "status": "request-changes",
                "reviews": [
                    {
                        "role": "correctness",
                        "status": "request-changes",
                        "summary": "Please fix the tracker semantics.",
                        "requires_response": True,
                    }
                ],
            }
        ]
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "resolved",
                    }
                },
                "review_cycles": review_cycles,
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(
            record["close_reason"],
            "merged via deadbeef",
        )
        self.assertIn("resolved before closure", record["comment"])

    def test_tracker_record_keeps_terminal_merge_open_without_recorded_review_cycles(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "resolved",
                    }
                },
                "review_cycles": [],
            },
            tracker_state(),
            {"status": "tracker-close-blocked"},
        )

        self.assertEqual(record["status"], "validated")
        self.assertFalse(record["close_source_item"])
        self.assertEqual(record["close_reason"], "")
        self.assertIn("review cycle evidence", record["comment"])

    def test_tracker_record_closes_terminal_merge_when_addressed_review_cycles_are_recorded(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
                "review_cycles": [
                    {
                        "cycle": 1,
                        "status": "findings-addressed",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "request-changes",
                                "summary": "Please tighten the close guidance.",
                                "requires_response": True,
                                "response": {"status": "addressed", "summary": "Fixed in follow-up."},
                            }
                        ],
                    }
                ],
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(record["close_reason"], "merged via deadbeef")

    def test_tracker_record_closes_terminal_merge_when_missing_review_cycles_are_explicitly_waived(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "waived",
                    }
                },
                "review_cycles": [],
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(record["close_reason"], "merged via deadbeef")
        self.assertIn("explicitly waived", record["comment"])

    def test_tracker_record_closes_terminal_no_merge_when_feedback_is_explicitly_waived(self):
        review_cycles = [
            {
                "cycle": 1,
                "status": "findings-open",
                "reviews": [
                    {
                        "role": "bug-risk",
                        "status": "findings-open",
                        "summary": "One follow-up remains.",
                        "requires_response": True,
                    }
                ],
            }
        ]
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "no-merge",
                        "merge_commit": "",
                        "reason": "Superseded by follow-up PR",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "waived",
                    }
                },
                "review_cycles": review_cycles,
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(
            record["close_reason"],
            "Superseded by follow-up PR",
        )
        self.assertIn("waived before closure", record["comment"])

    def test_tracker_record_includes_redacted_retrospective_for_terminal_no_merge(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "no-merge",
                        "merge_commit": "",
                        "reason": "Superseded by follow-up PR",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
                "review_cycles": [],
                "retrospective": {
                    "summary": "No-merge after token=ghp_no_merge_retrospective_secret_1234567890 follow-up.",
                    "changes": ["Documented why the branch will not merge."],
                    "validation": ["Validation remained green before the no-merge decision."],
                    "review": ["Final review passed; publication was intentionally skipped."],
                    "unresolved_risks": ["Follow-up work still needs manual tracking."],
                    "process_findings": ["Terminal no-merge decisions still need retrospective evidence."],
                    "follow_up": {
                        "recommended": [
                            {
                                "id": "central-3x6.7",
                                "summary": "Track the superseding workstream.",
                                "labels": ["project:afk-composable-pipeline"],
                            }
                        ],
                        "created": [],
                    },
                    "notes": {
                        "personal_work": [
                            "~/Documents/rmd/Ceremonies/Personal Work/work/2026-06-27-personal.md",
                        ],
                        "spikes": [],
                    },
                },
            },
            tracker_state(
                terminal_decision={
                    "status": "no-merge",
                    "merge_commit": "",
                    "reason": "Superseded by follow-up PR",
                    "pr_url": "https://github.example/pr/17",
                    "review_feedback_status": "",
                }
            ),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertIn("[REDACTED]", record["retrospective"]["summary"])
        self.assertEqual(
            record["retrospective"]["follow_up"]["recommended"][0]["labels"],
            ["project:afk-composable-pipeline"],
        )
        self.assertEqual(
            record["retrospective"]["notes"]["personal_work"],
            ["~/Documents/rmd/Ceremonies/Personal Work/work/2026-06-27-personal.md"],
        )
