import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_afk(*args, env_overrides=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "afk", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def git(cwd, *args):
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "AFK Test",
            "GIT_AUTHOR_EMAIL": "afk-test@example.test",
            "GIT_COMMITTER_NAME": "AFK Test",
            "GIT_COMMITTER_EMAIL": "afk-test@example.test",
        }
    )
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout.strip()


def init_checkout(path):
    path.mkdir(parents=True)
    git(path, "init", "--initial-branch", "main")
    git(path, "config", "user.name", "AFK Test")
    git(path, "config", "user.email", "afk-test@example.test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "seed")
    git(path, "checkout", "-b", "afk/validate")
    return git(path, "rev-parse", "HEAD")


def run_dir_text(run_dir):
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    )


class ValidateCliTest(unittest.TestCase):
    def test_validate_runs_local_worker_and_records_request_and_result_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                request_path = Path(os.environ["AFK_WORKER_REQUEST"])
                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                request = json.loads(request_path.read_text(encoding="utf-8"))
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(
                    json.dumps(
                        {
                            "profile": request["profile"],
                            "status": "pass",
                            "failureCount": 0,
                            "repo": request["repo"]["path"],
                            "checkout": {
                                "source": "local",
                                "path": request["repo"]["path"],
                                "requestedCommit": request["repo"]["commit"],
                                "resolvedCommit": request["repo"]["commit"],
                            },
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "pass",
                                    "category": "ok",
                                    "reason": "fake validation passed",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                print("fake validation worker complete")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--project",
                "bump-eqemu",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "repo_url": "git@github.com:thunderbump/bump-EQEmu.git",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "validation": {"dry_run": True, "timeout_seconds": 30},
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                            "timeout_seconds": 10,
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_request = json.loads((run_dir / "worker-request.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["step"], "validate")
            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(result["output"]["validation"]["requested_profile"], "tier3-harness")
            self.assertEqual(result["output"]["validation"]["worker_profile"], "tier3-harness")
            self.assertEqual(
                result["output"]["artifacts"],
                {
                    "worker_request": "worker-request.json",
                    "worker_result": "worker-result.json",
                },
            )

            self.assertEqual(worker_request["profile"], "tier3-harness")
            self.assertEqual(worker_request["repo"]["path"], str(checkout))
            self.assertEqual(worker_request["repo"]["commit"], start_commit)
            self.assertEqual(worker_request["dryRun"], True)
            self.assertEqual(worker_request["timeoutSeconds"], 30)
            self.assertTrue(Path(worker_request["evidence_dir"]).is_absolute())

            self.assertEqual(worker_result["artifact_type"], "worker-result")
            self.assertEqual(worker_result["result"]["normalized"]["status"], "validated")
            self.assertEqual(worker_result["result"]["raw"]["status"], "pass")
            self.assertEqual(worker_result["result"]["raw"]["steps"][0]["name"], "tier3_harness")
            self.assertIn(
                "fake validation worker complete",
                (run_dir / "stdout.log").read_text(encoding="utf-8"),
            )

    def test_validate_remote_command_adapter_builds_fetchable_repo_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text(encoding="utf-8"))
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": request["profile"],
                            "status": "pass",
                            "failureCount": 0,
                            "repo": request["repo"]["url"],
                            "checkout": {
                                "source": "fetch",
                                "requestedRef": request["repo"]["ref"],
                                "requestedCommit": request["repo"]["commit"],
                                "resolvedCommit": request["repo"]["commit"],
                            },
                            "metadata": {"host": os.environ["AFK_WORKER_REMOTE_HOST"]},
                            "steps": [],
                        }
                    ),
                    encoding="utf-8",
                )
                print("remote fake worker on " + os.environ["AFK_WORKER_REMOTE_HOST"])
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "preflight",
                "--project",
                "bump-eqemu",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "repo_url": "git@github.com:thunderbump/bump-EQEmu.git",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "refs/heads/afk/validate",
                            "start_commit": start_commit,
                        },
                        "validation": {"dry_run": True},
                        "worker": {
                            "type": "remote-command",
                            "host": "validation.example.test",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_request = json.loads((run_dir / "worker-request.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(worker_request["profile"], "preflight")
            self.assertEqual(
                worker_request["repo"],
                {
                    "url": "git@github.com:thunderbump/bump-EQEmu.git",
                    "ref": "refs/heads/afk/validate",
                    "commit": start_commit,
                },
            )
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["type"], "remote-command")
            self.assertEqual(
                worker_result["result"]["normalized"]["adapter"]["host"],
                "validation.example.test",
            )
            self.assertEqual(
                worker_result["result"]["raw"]["metadata"]["host"],
                "validation.example.test",
            )
            self.assertIn(
                "remote fake worker on validation.example.test",
                (run_dir / "stdout.log").read_text(encoding="utf-8"),
            )

    def test_validate_classifies_missing_worker_result_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", "print('no result produced')"],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_missing_result")
            self.assertEqual(result["output"]["classification"], "missing_worker_result")
            self.assertIsNone(worker_result["result"]["raw"])
            self.assertEqual(
                worker_result["result"]["normalized"]["summary"],
                "worker result file was not produced",
            )
            self.assertIn(
                "no result produced",
                worker_result["result"]["normalized"]["evidence"]["stdout_excerpt"],
            )

    def test_validate_classifies_missing_worker_result_even_when_adapter_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [
                                sys.executable,
                                "-c",
                                "import sys; print('no result before failure'); sys.exit(5)",
                            ],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_missing_result")
            self.assertEqual(result["output"]["classification"], "missing_worker_result")
            self.assertIsNone(worker_result["result"]["raw"])
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["returncode"], 5)
            self.assertEqual(
                worker_result["result"]["normalized"]["summary"],
                "worker result file was not produced",
            )

    def test_validate_classifies_invalid_worker_json_as_protocol_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import os
                from pathlib import Path

                Path(os.environ["AFK_WORKER_RESULT"]).write_text("{not valid json", encoding="utf-8")
                print("wrote malformed result")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertIsNone(worker_result["result"]["raw"])
            self.assertEqual(
                worker_result["result"]["normalized"]["summary"],
                "worker result file is not valid JSON",
            )
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["returncode"], 0)
            self.assertIn(
                "wrote malformed result",
                worker_result["result"]["normalized"]["evidence"]["stdout_excerpt"],
            )

    def test_validate_sanitizes_invalid_worker_json_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import os
                from pathlib import Path

                token = "invalid-token-" + "secret"
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    "API_TOKEN=" + token + " {not valid json",
                    encoding="utf-8",
                )
                print("wrote malformed result with token")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            artifact_text = run_dir_text(run_dir)
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertIsNone(worker_result["result"]["raw"])
            self.assertEqual(
                worker_result["result"]["normalized"]["summary"],
                "worker result file is not valid JSON",
            )
            self.assertNotIn("invalid-token-secret", artifact_text)
            self.assertEqual(evidence_result["status"], "failed_protocol")
            self.assertEqual(evidence_result["classification"], "protocol_failure")

    def test_validate_classifies_unknown_worker_status_as_protocol_failure_when_adapter_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"profile": "tier3-harness", "status": "bogus", "steps": []}),
                    encoding="utf-8",
                )
                print("reported bogus status before adapter failure")
                sys.exit(9)
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(worker_result["result"]["raw"]["status"], "bogus")
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["returncode"], 9)
            self.assertIn(
                "reported bogus status before adapter failure",
                worker_result["result"]["normalized"]["evidence"]["stdout_excerpt"],
            )

    def test_validate_classifies_worker_timeout_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", "import time; time.sleep(5)"],
                            "timeout_seconds": 0.1,
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_timeout")
            self.assertEqual(result["output"]["classification"], "timeout")
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["timed_out"], True)
            self.assertEqual(
                worker_result["result"]["normalized"]["summary"],
                "worker command timed out",
            )

    def test_validate_rejects_pass_result_when_adapter_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "pass",
                            "summary": "validation passed",
                            "steps": [],
                        }
                    ),
                    encoding="utf-8",
                )
                print("reported pass before adapter failure")
                sys.exit(7)
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["classification"], "runtime_failure")
            self.assertEqual(worker_result["result"]["raw"]["status"], "pass")
            self.assertEqual(worker_result["result"]["normalized"]["classification"], "runtime_failure")
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["returncode"], 7)
            self.assertIn(
                "reported pass before adapter failure",
                worker_result["result"]["normalized"]["evidence"]["stdout_excerpt"],
            )

    def test_validate_classifies_worker_reported_failure_even_when_adapter_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "fail",
                            "failureCount": 1,
                            "summary": "tier3 harness failed",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "zone harness exited 1",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                print("validation_failed")
                sys.exit(1)
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_validation")
            self.assertEqual(result["output"]["classification"], "worker_failure")
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["returncode"], 1)
            self.assertEqual(worker_result["result"]["normalized"]["summary"], "tier3 harness failed")
            self.assertEqual(
                worker_result["result"]["normalized"]["failures"][0]["category"],
                "validation_failed",
            )

    def test_validate_classifies_profile_skip_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "skipped",
                            "summary": "profile skipped by worker",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "skip",
                                    "category": "profile_disabled",
                                    "reason": "profile is disabled for this run",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                print("profile skipped")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "skipped_profile")
            self.assertEqual(result["output"]["classification"], "profile_skipped")
            self.assertEqual(
                worker_result["result"]["normalized"]["failures"][0]["category"],
                "profile_disabled",
            )
            self.assertIn("profile skipped", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_validate_uses_bump_eqemu_contract_profile_mapping_for_worker_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text(encoding="utf-8"))
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"profile": request["profile"], "status": "pass", "steps": []}),
                    encoding="utf-8",
                )
                print("mapped profile " + request["profile"])
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier1",
                "--project",
                "bump-eqemu",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "repo_url": "git@github.com:thunderbump/bump-EQEmu.git",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_request = json.loads((run_dir / "worker-request.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["validation"]["requested_profile"], "tier1")
            self.assertEqual(result["output"]["validation"]["worker_profile"], "safe")
            self.assertEqual(worker_request["profile"], "safe")
            self.assertIn("mapped profile safe", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_validate_redacts_request_result_and_worker_log_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            secret_url = "https://user:validate-secret@example.invalid/repo.git?token=query-secret"
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                import sys
                from pathlib import Path

                secret = "result-" + "secret"
                print("API_TOKEN=stdout-secret")
                print("{secret_url}", file=sys.stderr)
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {{
                            "profile": "preflight",
                            "status": "pass",
                            "token": secret,
                            "url": "{secret_url}",
                            "steps": [],
                        }}
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "preflight",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "repo_url": secret_url,
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "remote-command",
                            "host": "validation.example.test",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            artifact_text = run_dir_text(run_dir)

            self.assertNotIn("validate-secret", artifact_text)
            self.assertNotIn("query-secret", artifact_text)
            self.assertNotIn("stdout-secret", artifact_text)
            self.assertNotIn("result-secret", artifact_text)
            self.assertIn("https://example.invalid/repo.git", artifact_text)
            self.assertIn("API_TOKEN=[REDACTED]", artifact_text)

    def test_validate_redacts_nested_worker_result_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                token = "nested-token-" + "secret"
                password = "db-password-" + "secret"
                credential = "credential-" + "secret"
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "preflight",
                            "status": "pass",
                            "metadata": {
                                "token": token,
                                "database": {"password": password},
                                "service_credentials": {"value": credential},
                            },
                            "steps": [],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "preflight",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(evidence_result["metadata"]["token"], "[REDACTED]")
            self.assertEqual(evidence_result["metadata"]["database"]["password"], "[REDACTED]")
            self.assertEqual(evidence_result["metadata"]["service_credentials"], "[REDACTED]")
            artifact_text = run_dir_text(run_dir)
            self.assertNotIn("nested-token-secret", artifact_text)
            self.assertNotIn("db-password-secret", artifact_text)
            self.assertNotIn("credential-secret", artifact_text)

    def test_validate_defaults_to_bump_eqemu_validation_worker_script_when_project_is_bump_eqemu(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            init_checkout(checkout)
            worker_script = checkout / "scripts" / "validation-worker.sh"
            worker_script.parent.mkdir()
            worker_script.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    [[ "${1:-}" == "run" ]]
                    [[ "${2:-}" == "--request" ]]
                    python3 - "$3" <<'PY'
                    import json
                    import sys
                    from pathlib import Path

                    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
                    evidence_dir = Path(request["evidence_dir"])
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    (evidence_dir / "result.json").write_text(
                        json.dumps({"profile": request["profile"], "status": "pass", "steps": []}),
                        encoding="utf-8",
                    )
                    print("default validation worker " + request["profile"])
                    PY
                    """
                ),
                encoding="utf-8",
            )
            worker_script.chmod(0o755)
            git(checkout, "add", "scripts/validation-worker.sh")
            git(checkout, "commit", "-m", "add fake validation worker")
            start_commit = git(checkout, "rev-parse", "HEAD")
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier3-harness",
                "--project",
                "bump-eqemu",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "repo_url": "git@github.com:thunderbump/bump-EQEmu.git",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/validate",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        }
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(worker_result["result"]["raw"]["profile"], "tier3-harness")
            self.assertIn(
                "default validation worker tier3-harness",
                (run_dir / "stdout.log").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
