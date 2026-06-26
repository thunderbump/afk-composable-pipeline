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

    def test_validate_rejects_external_worker_config_for_remote_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            init_checkout(checkout)
            start_commit = git(checkout, "rev-parse", "HEAD")
            ledger = temp_path / "ledger"

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
                        "validation": {
                            "worker_home": str(temp_path / "validation-worker-home"),
                            "stack": {"role": "validation", "path": str(temp_path / "bump-akk-stack-validation")},
                        },
                        "worker": {
                            "type": "remote-command",
                            "host": "validation.example.test",
                            "command": [sys.executable, "-c", "raise SystemExit('worker should not run')"],
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

            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertEqual(
                result["output"]["message"],
                "validation.worker_home and validation.stack are only supported for the default project worker",
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

    def test_validate_summarizes_missing_worker_result_from_adapter_output(self):
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
                                "import sys; print('warning: cache warmup'); "
                                "print('error: worker never wrote result'); sys.exit(5)",
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(result["output"]["status"], "failed_missing_result")
            self.assertEqual(actionable[0]["category"], "missing_result")
            self.assertEqual(actionable[0]["exit_code"], 5)
            self.assertEqual(actionable[0]["log_path"], str(run_dir / "stdout.log"))
            self.assertIn("error: worker never wrote result", actionable[0]["excerpt"])
            self.assertIn(sys.executable, actionable[0]["command"])
            self.assertIn(str(run_dir / "stdout.log"), result["output"]["summary"])
            self.assertIn(sys.executable, result["output"]["summary"])
            self.assertIn("error: worker never wrote result", result["output"]["summary"])

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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]
            self.assertEqual(actionable[0]["log_path"], str(run_dir / "stdout.log"))
            self.assertIn(sys.executable, result["output"]["summary"])
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
            self.assertEqual(
                worker_result["result"]["normalized"]["actionable_failures"][0]["category"],
                "timeout",
            )
            self.assertEqual(
                worker_result["result"]["normalized"]["actionable_failures"][0]["log_path"],
                str(run_dir / "stderr.log"),
            )
            self.assertIn(sys.executable, result["output"]["summary"])
            self.assertIn("worker command timed out", result["output"]["summary"])

    def test_validate_sanitizes_worker_result_evidence_after_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                import time
                from pathlib import Path

                token = "timeout-token-" + "secret"
                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                result_path.write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "pass",
                            "token": token,
                            "steps": [],
                        }
                    ),
                    encoding="utf-8",
                )
                time.sleep(5)
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
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )
            artifact_text = run_dir_text(run_dir)

            self.assertEqual(result["output"]["status"], "failed_timeout")
            self.assertEqual(result["output"]["classification"], "timeout")
            self.assertEqual(evidence_result["token"], "[REDACTED]")
            self.assertNotIn("timeout-token-secret", artifact_text)

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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]
            self.assertEqual(actionable[0]["log_path"], str(run_dir / "stdout.log"))
            self.assertIn(sys.executable, result["output"]["summary"])
            self.assertIn(
                "reported pass before adapter failure",
                worker_result["result"]["normalized"]["evidence"]["stdout_excerpt"],
            )

    def test_validate_summarizes_adapter_failure_from_full_output_before_warning_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            warning_tail = "\\n".join(f"warning: cache warmup {index:04d}" for index in range(400))
            worker_code = textwrap.dedent(
                f"""
                import sys

                print("error: worker never wrote result")
                print({warning_tail!r})
                sys.exit(5)
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(result["output"]["status"], "failed_missing_result")
            self.assertEqual(actionable[0]["log_path"], str(run_dir / "stdout.log"))
            self.assertIn("error: worker never wrote result", actionable[0]["excerpt"])
            self.assertNotIn("warning: cache warmup 0399", actionable[0]["excerpt"])

    def test_validate_tolerates_copying_evidence_result_into_distinct_worker_result_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                import shutil
                from pathlib import Path

                evidence_dir = Path(os.environ["AFK_WORKER_EVIDENCE_DIR"])
                evidence_result = evidence_dir / "result.json"
                request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text(encoding="utf-8"))
                evidence_result.write_text(
                    json.dumps(
                        {
                            "profile": request["profile"],
                            "status": "pass",
                            "summary": "copied from evidence",
                            "steps": [],
                        }
                    ),
                    encoding="utf-8",
                )
                shutil.copyfile(evidence_result, Path(os.environ["AFK_WORKER_RESULT"]))
                print(str(evidence_result))
                print(os.environ["AFK_WORKER_RESULT"])
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
            evidence_result = run_dir / "validation-evidence" / "result.json"
            worker_output = run_dir / "validation-evidence" / "worker-output.json"

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(worker_result["result"]["raw"]["summary"], "copied from evidence")
            self.assertEqual(evidence_result.read_text(encoding="utf-8"), worker_output.read_text(encoding="utf-8"))
            stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")
            self.assertIn(str(evidence_result), stdout_log)
            self.assertIn(str(worker_output), stdout_log)
            self.assertNotEqual(str(evidence_result), str(worker_output))

    def test_validate_classifies_worker_reported_failure_even_when_adapter_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = (
                "import json\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "evidence_dir = Path(os.environ['AFK_WORKER_RESULT']).parent\n"
                "steps_dir = evidence_dir / 'steps'\n"
                "steps_dir.mkdir(parents=True, exist_ok=True)\n"
                "step_log = steps_dir / 'tier3_harness.log'\n"
                "step_log.write_text('zone harness exited 1\\n', encoding='utf-8')\n"
                "payload = {\n"
                "    'profile': 'tier3-harness',\n"
                "    'status': 'fail',\n"
                "    'failureCount': 1,\n"
                "    'summary': 'tier3 harness failed',\n"
                "    'steps': [\n"
                "        {\n"
                "            'name': 'tier3_harness',\n"
                "            'status': 'fail',\n"
                "            'category': 'validation_failed',\n"
                "            'reason': 'zone harness exited 1',\n"
                "            'command': 'python3 -m unittest tests.test_auth.AuthTest.test_login --failfast',\n"
                "            'exitCode': 1,\n"
                "            'log': str(step_log),\n"
                "        }\n"
                "    ],\n"
                "}\n"
                "Path(os.environ['AFK_WORKER_RESULT']).write_text(json.dumps(payload), encoding='utf-8')\n"
                "print('validation_failed')\n"
                "sys.exit(1)"
            )

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
            self.assertIn("tier3 harness failed", result["output"]["summary"])
            self.assertIn("cmd:", result["output"]["summary"])
            self.assertIn("exit:", result["output"]["summary"])
            self.assertIn(
                str(run_dir / "validation-evidence" / "steps" / "tier3_harness.log"),
                result["output"]["summary"],
            )
            self.assertIn("zone harness exited 1", result["output"]["summary"])

    def test_validate_resolves_relative_worker_log_path_for_step_failures(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                steps_dir = evidence_dir / "steps"
                steps_dir.mkdir(parents=True, exist_ok=True)
                step_log = steps_dir / "tier3_harness.log"
                step_log.write_text("AssertionError: relative log failed", encoding="utf-8")
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
                                    "reason": "relative log path test",
                                    "command": "python3 -m unittest tests.test_auth.AuthTest.test_login --failfast",
                                    "exitCode": 1,
                                    "log": "steps/tier3_harness.log",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                print("relative log test")
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(result["output"]["status"], "failed_validation")
            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "steps" / "tier3_harness.log"),
            )
            self.assertEqual(actionable[0]["log_path_status"], "exact")
            self.assertEqual(actionable[0]["excerpt"], "AssertionError: relative log failed")

    def test_validate_failed_validation_marks_top_level_step_status_failed(self):
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
                            "status": "fail",
                            "summary": "commit mismatch",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "checkout commit did not match requested commit",
                                    "exitCode": 0,
                                }
                            ],
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

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_validation")

    def test_validate_reports_missing_worker_log_path_as_unavailable(self):
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
                            "status": "fail",
                            "failureCount": 1,
                            "summary": "tier3 harness failed",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "log field omitted",
                                    "command": "python3 -m unittest tests.test_auth.AuthTest.test_login --failfast",
                                    "exitCode": 1,
                                }
                            ],
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
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(
                actionable[0]["log_path"],
                None,
            )
            self.assertEqual(actionable[0]["log_path_status"], "unavailable")
            self.assertEqual(actionable[0]["excerpt"], "log field omitted")

    def test_validate_includes_compact_excerpt_with_non_generic_worker_summary(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                steps_dir = evidence_dir / "steps"
                steps_dir.mkdir(parents=True, exist_ok=True)
                step_log = steps_dir / "tier3_harness.log"
                step_log.write_text(
                    "\\n".join(
                        [
                            "warning: cached environment reused",
                            "AssertionError: expected 200 != 500",
                            "API_TOKEN=super-secret-token",
                        ]
                    ),
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "fail",
                            "summary": "tier3 harness failed",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "worker reported test failure",
                                    "command": "python3 -m unittest tests.test_auth.AuthTest.test_login",
                                    "exitCode": 1,
                                    "log": str(step_log),
                                }
                            ],
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(actionable[0]["excerpt"], "AssertionError: expected 200 != 500")
            self.assertIn("tier3 harness failed", result["output"]["summary"])
            self.assertIn("AssertionError: expected 200 != 500", result["output"]["summary"])
            self.assertNotIn("super-secret-token", result["output"]["summary"])

    def test_validate_prioritizes_actual_failure_before_prerequisite_skip_in_summary(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                steps_dir = evidence_dir / "steps"
                steps_dir.mkdir(parents=True, exist_ok=True)
                tier1_log = steps_dir / "tier1.log"
                tier3_log = steps_dir / "tier3_harness.log"
                tier1_log.write_text(
                    "\\n".join(
                        [
                            "CMake Error: could not configure build directory",
                            "CMake Error: missing dependency package",
                        ]
                    ),
                    encoding="utf-8",
                )
                tier3_log.write_text(
                    "reason: tier1 failed; skipped tier3-harness\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier1-tier3-harness",
                            "status": "fail",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "skip",
                                    "category": "prerequisite_failed",
                                    "reason": "tier1 failed; skipped tier3-harness",
                                    "command": "internal:tier3_harness",
                                    "exitCode": 0,
                                    "log": str(tier3_log),
                                },
                                {
                                    "name": "tier1",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "command exited with status 1",
                                    "command": "cmake --build build",
                                    "exitCode": 1,
                                    "log": str(tier1_log),
                                },
                            ],
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(actionable[0]["name"], "tier1")
            self.assertEqual(actionable[0]["category"], "compiler")
            self.assertEqual(actionable[1]["name"], "tier3_harness")
            self.assertEqual(actionable[1]["category"], "prerequisite_skip")
            self.assertTrue(result["output"]["summary"].startswith("tier1 [compiler]"))
            self.assertIn("CMake Error: could not configure build directory", result["output"]["summary"])

    def test_validate_summarizes_compiler_failure_from_step_log_before_warning_tail(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                steps_dir = evidence_dir / "steps"
                steps_dir.mkdir(parents=True, exist_ok=True)
                tier1_log = steps_dir / "tier1.log"
                tier3_log = steps_dir / "tier3_harness.log"
                tier1_log.write_text(
                    "\\n".join(
                        [
                            'time="2026-06-20T22:22:40-07:00" level=warning msg="No services to build"',
                            'time="2026-06-20T22:22:40-07:00" level=warning msg="No services to build"',
                            "CMake Error: The current CMakeCache.txt directory /tmp/build is different than the directory /expected/build where CMakeCache.txt was created.",
                            'CMake Error: The source "/tmp/CMakeLists.txt" does not match the source "/expected/CMakeLists.txt" used to generate cache.',
                            "",
                            "Preset CMake variables:",
                            '  CMAKE_BUILD_TYPE="Debug"',
                            '  EQEMU_BUILD_TESTS="ON"',
                        ]
                    ),
                    encoding="utf-8",
                )
                tier3_log.write_text(
                    "\\n".join(
                        [
                            "step: tier3_harness",
                            "status: skip",
                            "category: prerequisite_failed",
                            "reason: tier1 failed; skipped tier3-harness",
                            "command: internal:tier3_harness",
                            "exitCode: 0",
                        ]
                    ),
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier1-tier3-harness",
                            "status": "fail",
                            "steps": [
                                {
                                    "name": "tier1",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "command exited with status 1",
                                    "command": "cmake --build build",
                                    "exitCode": 1,
                                    "log": str(tier1_log),
                                },
                                {
                                    "name": "tier3_harness",
                                    "status": "skip",
                                    "category": "prerequisite_failed",
                                    "reason": "tier1 failed; skipped tier3-harness",
                                    "command": "internal:tier3_harness",
                                    "exitCode": 0,
                                    "log": str(tier3_log),
                                },
                            ],
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(result["output"]["status"], "failed_validation")
            self.assertEqual(result["output"]["actionable_failures"], actionable)
            self.assertEqual(actionable[0]["name"], "tier1")
            self.assertEqual(actionable[0]["category"], "compiler")
            self.assertEqual(actionable[0]["command"], "cmake --build build")
            self.assertEqual(actionable[0]["exit_code"], 1)
            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "steps" / "tier1.log"),
            )
            self.assertIn("CMake Error:", actionable[0]["excerpt"])
            self.assertNotIn("Preset CMake variables", actionable[0]["excerpt"])
            self.assertEqual(actionable[1]["name"], "tier3_harness")
            self.assertEqual(actionable[1]["category"], "prerequisite_skip")
            self.assertEqual(
                actionable[1]["log_path"],
                str(run_dir / "validation-evidence" / "steps" / "tier3_harness.log"),
            )
            self.assertIn("tier1 failed; skipped tier3-harness", actionable[1]["excerpt"])
            self.assertIn("tier1", result["output"]["summary"])
            self.assertIn("tier1.log", result["output"]["summary"])
            self.assertIn("CMake Error:", result["output"]["summary"])

    def test_validate_summarizes_test_failure_from_step_log(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                steps_dir = evidence_dir / "steps"
                steps_dir.mkdir(parents=True, exist_ok=True)
                unit_log = steps_dir / "unit.log"
                unit_log.write_text(
                    "\\n".join(
                        [
                            "warning: cached test environment reused",
                            "FAIL: test_login (tests.test_auth.AuthTest)",
                            "AssertionError: expected 200 != 500",
                            "",
                            "Ran 12 tests in 0.123s",
                            "FAILED (failures=1)",
                        ]
                    ),
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "fail",
                            "steps": [
                                {
                                    "name": "unit",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "command exited with status 1",
                                    "command": "python3 -m unittest tests.test_auth",
                                    "exitCode": 1,
                                    "log": str(unit_log),
                                }
                            ],
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(actionable[0]["category"], "test")
            self.assertEqual(actionable[0]["command"], "python3 -m unittest tests.test_auth")
            self.assertEqual(actionable[0]["exit_code"], 1)
            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "steps" / "unit.log"),
            )
            self.assertIn("FAIL: test_login", actionable[0]["excerpt"])
            self.assertIn("AssertionError: expected 200 != 500", actionable[0]["excerpt"])
            self.assertIn("unit.log", result["output"]["summary"])
            self.assertIn("FAIL: test_login", result["output"]["summary"])

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

            self.assertEqual(summary["status"], "succeeded")
            self.assertEqual(result["status"], "succeeded")
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

    def test_validate_sanitizes_read_only_worker_result_evidence(self):
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

                token = "readonly-token-" + "secret"
                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                result_path.write_text(
                    json.dumps(
                        {
                            "profile": "preflight",
                            "status": "pass",
                            "token": token,
                            "steps": [],
                        }
                    ),
                    encoding="utf-8",
                )
                result_path.chmod(0o400)
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
            artifact_text = run_dir_text(run_dir)

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(evidence_result["token"], "[REDACTED]")
            self.assertNotIn("readonly-token-secret", artifact_text)

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
                    assert request["project"] == "bump-eqemu", request
                    assert request["repo"] == str(Path.cwd()), request
                    assert request["ref"] == "main", request
                    assert request["profile"] == "tier3-harness", request
                    assert request["run_id"], request
                    assert request["timeout_seconds"] == 120, request
                    assert request["lock_wait_seconds"] == 30, request
                    assert "timeoutSeconds" not in request, request
                    assert not isinstance(request["repo"], dict), request
                    evidence_dir = Path(request["evidence_dir"])
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    (evidence_dir / "result.json").write_text(
                        json.dumps(
                            {
                                "profile": request["profile"],
                                "status": "pass",
                                "repo": request["repo"],
                                "checkout": {
                                    "requestedRef": request["ref"],
                                    "requestedCommit": request["commit"],
                                    "resolvedCommit": request["commit"],
                                },
                                "steps": [],
                            }
                        ),
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
            worker_request = json.loads((run_dir / "worker-request.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            evidence_result = run_dir / "validation-evidence" / "result.json"
            worker_output = run_dir / "validation-evidence" / "worker-output.json"

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(worker_request["project"], "bump-eqemu")
            self.assertEqual(worker_request["repo"], str(checkout))
            self.assertEqual(worker_request["ref"], "main")
            self.assertEqual(worker_request["commit"], start_commit)
            self.assertEqual(worker_request["run_id"], summary["run_id"])
            self.assertEqual(worker_request["timeout_seconds"], 120)
            self.assertEqual(worker_request["lock_wait_seconds"], 30)
            self.assertNotIn("timeoutSeconds", worker_request)
            self.assertNotIsInstance(worker_request["repo"], dict)
            self.assertEqual(worker_result["result"]["raw"]["profile"], "tier3-harness")
            self.assertTrue(worker_output.is_file())
            self.assertEqual(evidence_result.read_text(encoding="utf-8"), worker_output.read_text(encoding="utf-8"))
            self.assertIn(
                "default validation worker tier3-harness",
                (run_dir / "stdout.log").read_text(encoding="utf-8"),
            )

    def test_validate_default_bump_eqemu_worker_inherits_validation_timeout(self):
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
                    sleep 2
                    """
                ),
                encoding="utf-8",
            )
            worker_script.chmod(0o755)
            git(checkout, "add", "scripts/validation-worker.sh")
            git(checkout, "commit", "-m", "add sleeping validation worker")
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
                        },
                        "validation": {"timeout_seconds": 1},
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
            self.assertTrue(worker_result["result"]["normalized"]["adapter"]["timed_out"])

    def test_validate_default_bump_eqemu_worker_passes_external_worker_home_and_stack_without_dirtying_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            worker_home = temp_path / "validation-worker-home"
            stack_dir = temp_path / "bump-akk-stack-validation"
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
                    import os
                    import sys
                    from pathlib import Path

                    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
                    assert request["worker_home"] == os.environ["VALIDATION_WORKER_HOME"], request
                    assert request["stack"]["path"] == os.environ["AKKSTACK_DIR"], request
                    assert request["stack"]["role"] == "validation", request
                    worker_home = Path(request["worker_home"])
                    stack_dir = Path(request["stack"]["path"])
                    worker_home.mkdir(parents=True, exist_ok=True)
                    stack_dir.mkdir(parents=True, exist_ok=True)
                    (worker_home / "worker-touch.txt").write_text("outside checkout\\n", encoding="utf-8")
                    (stack_dir / "stack-touch.txt").write_text("outside checkout\\n", encoding="utf-8")
                    evidence_dir = Path(request["evidence_dir"])
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    result = {
                        "profile": request["profile"],
                        "status": "fail",
                        "failureCount": 1,
                        "summary": "tier3 harness failed with external worker home",
                        "metadata": {
                            "workerHome": os.environ["VALIDATION_WORKER_HOME"],
                            "stackDir": os.environ["AKKSTACK_DIR"],
                        },
                        "steps": [
                            {
                                "name": "tier3_harness",
                                "status": "fail",
                                "category": "validation_failed",
                                "reason": "intentional failure",
                                "command": "scripts/validate.sh --stack validation",
                                "exitCode": 1,
                                "log": str(evidence_dir / "steps" / "tier3_harness.log"),
                            }
                        ],
                    }
                    (evidence_dir / "steps").mkdir(parents=True, exist_ok=True)
                    (evidence_dir / "steps" / "tier3_harness.log").write_text(
                        "intentional failure\\n", encoding="utf-8"
                    )
                    (evidence_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
                    print("worker_home=" + os.environ["VALIDATION_WORKER_HOME"])
                    print("akkstack_dir=" + os.environ["AKKSTACK_DIR"])
                    PY
                    """
                ),
                encoding="utf-8",
            )
            worker_script.chmod(0o755)
            git(checkout, "add", "scripts/validation-worker.sh")
            git(checkout, "commit", "-m", "add configurable validation worker")
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
                        },
                        "validation": {
                            "worker_home": str(worker_home),
                            "stack": {"role": "validation", "path": str(stack_dir)},
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

            self.assertEqual(result["output"]["status"], "failed_validation")
            self.assertEqual(worker_request["worker_home"], str(worker_home))
            self.assertEqual(
                worker_request["stack"],
                {"role": "validation", "path": str(stack_dir)},
            )
            self.assertEqual(
                worker_result["result"]["raw"]["metadata"],
                {"workerHome": str(worker_home), "stackDir": str(stack_dir)},
            )
            self.assertEqual((worker_home / "worker-touch.txt").read_text(encoding="utf-8"), "outside checkout\n")
            self.assertEqual((stack_dir / "stack-touch.txt").read_text(encoding="utf-8"), "outside checkout\n")
            self.assertEqual(git(checkout, "status", "--short"), "")
            self.assertIn(f"worker_home={worker_home}", (run_dir / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn(f"akkstack_dir={stack_dir}", (run_dir / "stdout.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
