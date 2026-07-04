import json
import os
import time
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

from afk import validation


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
    COMPILER_LOG_FIXTURE = (ROOT / "tests" / "fixtures" / "validation-compiler-error.log").read_text(encoding="utf-8")

    def test_validate_default_project_worker_uses_absolute_artifact_paths_with_relative_ledger(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            scripts_dir = checkout / "scripts"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            relative_ledger = os.path.relpath(ledger, ROOT)
            worker_script = textwrap.dedent(
                f"""
                #!{sys.executable}
                import json
                import os
                import sys
                from pathlib import Path

                if sys.argv[1:] != ["run", "--request", sys.argv[3]]:
                    raise SystemExit("unexpected argv")
                request_path = Path(sys.argv[3])
                if not request_path.is_absolute():
                    raise SystemExit("request path must be absolute")
                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                evidence_dir = Path(os.environ["AFK_WORKER_EVIDENCE_DIR"])
                env_request_path = Path(os.environ["AFK_WORKER_REQUEST"])
                if not result_path.is_absolute():
                    raise SystemExit("result path must be absolute")
                if not evidence_dir.is_absolute():
                    raise SystemExit("evidence dir must be absolute")
                if not env_request_path.is_absolute():
                    raise SystemExit("env request path must be absolute")
                request = json.loads(request_path.read_text(encoding="utf-8"))
                if not Path(request["evidence_dir"]).is_absolute():
                    raise SystemExit("request evidence_dir must be absolute")
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(
                    json.dumps(
                        {{
                            "profile": request["profile"],
                            "status": "pass",
                            "failureCount": 0,
                            "repo": request["repo"],
                            "checkout": {{
                                "source": "local",
                                "path": request["repo"],
                                "requestedRef": request["ref"],
                                "requestedCommit": request["commit"],
                                "resolvedCommit": request["commit"],
                            }},
                            "metadata": {{
                                "request_path": str(request_path),
                                "env_request_path_absolute": env_request_path.is_absolute(),
                                "result_path_absolute": result_path.is_absolute(),
                                "evidence_dir_absolute": evidence_dir.is_absolute(),
                                "request_evidence_dir": request["evidence_dir"],
                            }},
                            "steps": [],
                        }}
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()

            scripts_dir.mkdir(parents=True)
            worker_path = scripts_dir / "validation-worker.sh"
            worker_path.write_text(worker_script, encoding="utf-8")
            worker_path.chmod(0o755)

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
                    }
                ),
                "--ledger",
                relative_ledger,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_request = json.loads((run_dir / "worker-request.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            metadata = worker_result["result"]["raw"]["metadata"]

            self.assertEqual(result["output"]["status"], "validated")
            self.assertTrue(Path(worker_request["evidence_dir"]).is_absolute())
            self.assertEqual(worker_request["repo"], str(checkout))
            self.assertEqual(worker_request["ref"], "main")
            self.assertEqual(worker_request["commit"], start_commit)
            self.assertTrue(Path(metadata["request_path"]).is_absolute())
            self.assertTrue(metadata["env_request_path_absolute"])
            self.assertTrue(metadata["result_path_absolute"])
            self.assertTrue(metadata["evidence_dir_absolute"])
            self.assertEqual(metadata["request_evidence_dir"], worker_request["evidence_dir"])

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

    def test_run_command_adapter_allows_repeated_runs_with_same_evidence_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            checkout.mkdir()
            evidence_dir = temp_path / "validation-evidence"
            request_path = temp_path / "worker-request.json"
            result_path = temp_path / "worker-result.json"
            request_path.write_text(json.dumps({"profile": "tier3-harness"}), encoding="utf-8")
            worker = {
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        import json
                        import os
                        from pathlib import Path

                        Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                            json.dumps({"status": "pass", "steps": []}),
                            encoding="utf-8",
                        )
                        """
                    ).strip(),
                ],
                "timeout_seconds": 5,
                "env": {},
            }

            first = validation.run_command_adapter(
                worker,
                checkout_path=checkout,
                request_path=request_path,
                result_path=result_path,
                evidence_dir=evidence_dir,
                profile="tier3-harness",
            )
            second = validation.run_command_adapter(
                worker,
                checkout_path=checkout,
                request_path=request_path,
                result_path=result_path,
                evidence_dir=evidence_dir,
                profile="tier3-harness",
            )

        self.assertEqual(first["returncode"], 0)
        self.assertEqual(second["returncode"], 0)

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
                "validation.worker_home and validation.stack are only supported for the default project worker "
                "or explicit local-command validation workers",
            )

    def test_validate_reports_invalid_worker_type_before_external_config_gate(self):
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
                            "worker_home": str(temp_path / "validation-worker-home"),
                            "stack": {"role": "validation", "path": str(temp_path / "bump-akk-stack-validation")},
                        },
                        "worker": {
                            "type": "bogus-worker",
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
            self.assertEqual(result["output"]["message"], "worker.type must be local-command or remote-command")

    def test_validate_custom_local_worker_receives_external_worker_home_and_stack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            worker_home = temp_path / "validation-worker-home"
            stack_dir = temp_path / "bump-akk-stack-validation"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text(encoding="utf-8"))
                assert request["worker_home"] == os.environ["VALIDATION_WORKER_HOME"], request
                assert request["stack"] == {
                    "role": "validation",
                    "path": os.environ["AKKSTACK_DIR"],
                }, request
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": request["profile"],
                            "status": "pass",
                            "failureCount": 0,
                            "metadata": {
                                "workerHome": request["worker_home"],
                                "stack": request["stack"],
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
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(worker_request["worker_home"], str(worker_home))
            self.assertEqual(worker_request["stack"], {"role": "validation", "path": str(stack_dir)})
            self.assertEqual(
                worker_result["result"]["raw"]["metadata"],
                {
                    "workerHome": str(worker_home),
                    "stack": {"role": "validation", "path": str(stack_dir)},
                },
            )

    def test_validate_rejects_checkout_internal_external_stack_for_custom_local_worker(self):
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
                            "stack": {
                                "role": "validation",
                                "path": str(checkout / "bump-akk-stack-validation"),
                            }
                        },
                        "worker": {
                            "type": "local-command",
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
                "validation.stack.path must be outside checkout",
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

    @unittest.skipUnless(sys.platform == "linux", "stopped-process detection requires Linux /proc")
    def test_validate_reports_stopped_worker_process_before_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import os
                import signal
                import time

                os.kill(os.getpid(), signal.SIGSTOP)
                time.sleep(5)
                """
            ).strip()

            started_at = time.monotonic()
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
                            "timeout_seconds": 5,
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )
            elapsed = time.monotonic() - started_at

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertLess(elapsed, 2, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["classification"], "runtime_failure")
            self.assertEqual(worker_result["result"]["normalized"]["classification"], "runtime_failure")
            self.assertEqual(worker_result["result"]["normalized"]["actionable_failures"][0]["category"], "runtime")
            self.assertEqual(evidence_result["status"], "failed_runtime")
            self.assertEqual(evidence_result["classification"], "runtime_failure")
            self.assertIn("stopped", worker_result["result"]["normalized"]["summary"])
            self.assertIn("SIGCONT", worker_result["result"]["normalized"]["summary"])
            self.assertIn("State:", evidence_result["summary"])

    @unittest.skipUnless(sys.platform == "linux", "stopped-process detection requires Linux /proc")
    def test_validate_reports_stopped_worker_with_invalid_json_before_timeout_as_runtime_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import os
                import signal
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text('{"status":"pass"', encoding="utf-8")
                os.kill(os.getpid(), signal.SIGSTOP)
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
                            "timeout_seconds": 5,
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
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(worker_result["result"]["normalized"]["status"], "failed_runtime")
            self.assertEqual(worker_result["result"]["normalized"]["classification"], "runtime_failure")
            self.assertEqual(worker_result["result"]["normalized"]["actionable_failures"][0]["category"], "runtime")
            self.assertIn("stopped", worker_result["result"]["normalized"]["summary"])
            self.assertIn("SIGCONT", worker_result["result"]["normalized"]["summary"])
            self.assertEqual(evidence_result["status"], "failed_runtime")
            self.assertEqual(evidence_result["classification"], "runtime_failure")
            self.assertEqual(evidence_result["process_state"], "stopped")
            self.assertEqual(evidence_result["remediation"], worker_result["result"]["normalized"]["adapter"]["remediation"])
            self.assertGreaterEqual(len(worker_result["result"]["normalized"]["adapter"]["stopped_processes"]), 1)

    @unittest.skipUnless(sys.platform == "linux", "stopped-process detection requires Linux /proc")
    def test_validate_overwrites_pass_evidence_when_worker_stops_after_reporting_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import json
                import os
                import signal
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "pass",
                            "summary": "validation passed before stop",
                            "steps": [],
                        }
                    ),
                    encoding="utf-8",
                )
                os.kill(os.getpid(), signal.SIGSTOP)
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
                            "timeout_seconds": 5,
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
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )
            worker_output = json.loads(
                (run_dir / "validation-evidence" / "worker-output.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(worker_result["result"]["raw"], None)
            self.assertEqual(worker_result["result"]["normalized"]["status"], "failed_runtime")
            self.assertEqual(evidence_result["status"], "failed_runtime")
            self.assertEqual(evidence_result["classification"], "runtime_failure")
            self.assertEqual(worker_output["status"], "failed_runtime")
            self.assertEqual(worker_output["classification"], "runtime_failure")
            self.assertEqual(evidence_result["process_state"], "stopped")
            self.assertEqual(worker_output["process_state"], "stopped")

    @unittest.skipUnless(sys.platform == "linux", "stopped-process detection requires Linux /proc")
    def test_validate_reports_stopped_worker_descendant_before_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import os
                import signal
                import subprocess
                import sys
                import time

                child = subprocess.Popen(
                    [sys.executable, "-c", "import os, signal, time; os.kill(os.getpid(), signal.SIGSTOP); time.sleep(5)"]
                )
                while child.poll() is None:
                    time.sleep(0.05)
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
                            "timeout_seconds": 5,
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
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["classification"], "runtime_failure")
            self.assertEqual(worker_result["result"]["normalized"]["adapter"]["timed_out"], False)
            self.assertIn("State: T", worker_result["result"]["normalized"]["summary"])
            self.assertGreaterEqual(
                len(worker_result["result"]["normalized"]["adapter"]["stopped_processes"]),
                1,
            )
            self.assertEqual(evidence_result["process_state"], "stopped")
            self.assertEqual(evidence_result["remediation"].count("SIGCONT"), 1)

    def test_validate_waits_for_inherited_stdio_descendants_after_worker_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            child_done_path = temp_path / "child-done.txt"
            child_code = textwrap.dedent(
                f"""
                import sys
                import time
                from pathlib import Path

                time.sleep(2.4)
                print("late child stdout")
                print("late child stderr", file=sys.stderr)
                Path({str(child_done_path)!r}).write_text("done\\n", encoding="utf-8")
                """
            ).strip()
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                request_path = Path(os.environ["AFK_WORKER_REQUEST"])
                request = json.loads(request_path.read_text(encoding="utf-8"))
                result_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([sys.executable, "-c", {child_code!r}])
                result_path.write_text(
                    json.dumps(
                        {{
                            "profile": request["profile"],
                            "status": "pass",
                            "failureCount": 0,
                            "repo": request["repo"]["path"],
                            "checkout": {{
                                "source": "local",
                                "path": request["repo"]["path"],
                                "requestedCommit": request["repo"]["commit"],
                                "resolvedCommit": request["repo"]["commit"],
                            }},
                            "steps": [],
                            "summary": "worker completed before descendant output closed",
                        }}
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()

            started_at = time.monotonic()
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
                            "timeout_seconds": 5,
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )
            elapsed = time.monotonic() - started_at

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertGreaterEqual(elapsed, 2.3, completed.stderr)
            self.assertTrue(child_done_path.exists())
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "validated")
            self.assertIn("late child stdout", (run_dir / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("late child stderr", (run_dir / "stderr.log").read_text(encoding="utf-8"))

    def test_validate_preserves_utf8_split_across_stream_read_boundaries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            payload = ("a" * 4095) + "€" + "Z"
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                import sys
                from pathlib import Path

                payload = {payload!r}.encode("utf-8")
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()
                sys.stderr.buffer.write(payload)
                sys.stderr.buffer.flush()
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {{
                            "profile": "tier3-harness",
                            "status": "pass",
                            "summary": "wrote utf-8 payload across chunk boundary",
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
            stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")
            stderr_log = (run_dir / "stderr.log").read_text(encoding="utf-8")

            self.assertEqual(result["output"]["status"], "validated")
            self.assertEqual(stdout_log, payload)
            self.assertEqual(stderr_log, payload)
            self.assertTrue(worker_result["result"]["normalized"]["evidence"]["stdout_excerpt"].endswith("€Z"))
            self.assertTrue(worker_result["result"]["normalized"]["evidence"]["stderr_excerpt"].endswith("€Z"))
            self.assertNotIn("\ufffd", stdout_log)
            self.assertNotIn("\ufffd", stderr_log)

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
            worker_output = json.loads(
                (run_dir / "validation-evidence" / "worker-output.json").read_text(encoding="utf-8")
            )
            artifact_text = run_dir_text(run_dir)

            self.assertEqual(result["output"]["status"], "failed_timeout")
            self.assertEqual(result["output"]["classification"], "timeout")
            self.assertEqual(evidence_result["status"], "failed_timeout")
            self.assertEqual(evidence_result["classification"], "timeout")
            self.assertEqual(worker_output["status"], "failed_timeout")
            self.assertEqual(worker_output["classification"], "timeout")
            self.assertNotIn("token", evidence_result)
            self.assertNotIn("timeout-token-secret", artifact_text)

    def test_validate_classifies_post_exit_open_stdio_past_deadline_as_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            child_code = textwrap.dedent(
                """
                import time

                time.sleep(1.0)
                """
            ).strip()
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([sys.executable, "-c", {child_code!r}])
                result_path.write_text(
                    json.dumps(
                        {{
                            "profile": "tier3-harness",
                            "status": "pass",
                            "summary": "worker exited before descendant pipes closed",
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
                            "timeout_seconds": 0.2,
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
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_timeout")
            self.assertEqual(result["output"]["classification"], "timeout")
            self.assertEqual(worker_result["result"]["normalized"]["status"], "failed_timeout")
            self.assertTrue(worker_result["result"]["normalized"]["adapter"]["timed_out"])
            self.assertEqual(evidence_result["status"], "failed_timeout")
            self.assertEqual(evidence_result["classification"], "timeout")

    @unittest.skipUnless(sys.platform == "linux", "stopped-process detection requires Linux /proc")
    def test_validate_reports_stopped_descendant_after_parent_exit_before_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            child_code = textwrap.dedent(
                """
                import os
                import signal
                import time

                os.kill(os.getpid(), signal.SIGSTOP)
                time.sleep(5)
                """
            ).strip()
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([sys.executable, "-c", {child_code!r}])
                result_path.write_text(
                    json.dumps(
                        {{
                            "profile": "tier3-harness",
                            "status": "pass",
                            "summary": "parent exited immediately after spawning stopped child",
                            "steps": [],
                        }}
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()

            started_at = time.monotonic()
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
                            "timeout_seconds": 5,
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )
            elapsed = time.monotonic() - started_at

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertLess(elapsed, 2, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["classification"], "runtime_failure")
            self.assertIn("SIGCONT", worker_result["result"]["normalized"]["summary"])
            self.assertIn("State: T", worker_result["result"]["normalized"]["summary"])
            self.assertEqual(evidence_result["status"], "failed_runtime")
            self.assertEqual(evidence_result["process_state"], "stopped")
            self.assertIn("SIGCONT", worker_result["result"]["normalized"]["adapter"]["remediation"])

    @unittest.skipUnless(sys.platform == "linux", "stopped-process detection requires Linux /proc")
    def test_validate_scans_owned_process_group_after_pipes_drain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            child_code = textwrap.dedent(
                """
                import os
                import signal
                import time

                os.close(1)
                os.close(2)
                os.kill(os.getpid(), signal.SIGSTOP)
                time.sleep(5)
                """
            ).strip()
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.Popen([sys.executable, "-c", {child_code!r}])
                result_path.write_text(
                    json.dumps(
                        {{
                            "profile": "tier3-harness",
                            "status": "pass",
                            "summary": "parent exited after spawning stopped child with closed stdio",
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
                            "timeout_seconds": 5,
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
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["classification"], "runtime_failure")
            self.assertEqual(worker_result["result"]["normalized"]["status"], "failed_runtime")
            self.assertEqual(evidence_result["status"], "failed_runtime")
            self.assertEqual(evidence_result["process_state"], "stopped")
            self.assertIn("SIGCONT", worker_result["result"]["normalized"]["summary"])

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

    def test_validate_uses_validation_log_when_worker_result_is_sparse(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "validation.log").write_text(
                    "AkkStack code path points at /tmp/central-lhy6 instead of worker checkout\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"status": "failed", "summary": "failed_validation"}),
                    encoding="utf-8",
                )
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(result["output"]["status"], "failed_validation")
            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "logs" / "validation.log"),
            )
            self.assertIn("AkkStack code path points", actionable[0]["excerpt"])
            self.assertIn("validation.log", result["output"]["summary"])
            self.assertIn("AkkStack code path points", result["output"]["summary"])

    def test_validate_prefers_validation_log_over_generic_adapter_stderr(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "validation.log").write_text(
                    "AkkStack code path points at /tmp/central-lhy6 instead of worker checkout\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"status": "failed", "summary": "failed_validation"}),
                    encoding="utf-8",
                )
                print("failed", file=sys.stderr)
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
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "logs" / "validation.log"),
            )
            self.assertIn("AkkStack code path points", actionable[0]["excerpt"])

    def test_validate_uses_validation_log_when_step_failure_log_is_missing(self):
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
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "validation.log").write_text(
                    "AkkStack code path points at /tmp/central-lhy6 instead of worker checkout\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "fail",
                            "summary": "failed_validation",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "failed_validation",
                                    "command": "scripts/validate.sh tier3-harness",
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
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "logs" / "validation.log"),
            )
            self.assertEqual(actionable[0]["log_path_status"], "fallback")
            self.assertIn("AkkStack code path points", actionable[0]["excerpt"])
            self.assertIn("validation.log", result["output"]["summary"])
            self.assertIn("AkkStack code path points", result["output"]["summary"])

    def test_validate_uses_validation_log_for_generic_step_exit_reason(self):
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
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "validation.log").write_text(
                    "error: migrations table is missing required column\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "profile": "tier3-harness",
                            "status": "fail",
                            "summary": "failed_validation",
                            "steps": [
                                {
                                    "name": "tier3_harness",
                                    "status": "fail",
                                    "category": "validation_failed",
                                    "reason": "command exited with status 1",
                                    "command": "scripts/validate.sh tier3-harness",
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
                str(run_dir / "validation-evidence" / "logs" / "validation.log"),
            )
            self.assertIn("migrations table", actionable[0]["excerpt"])

    def test_validate_prefers_specific_evidence_log_over_generic_validation_log(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "validation.log").write_text("failed_validation\\n", encoding="utf-8")
                (log_dir / "stack.log").write_text(
                    "permission denied while rebinding AkkStack code symlink\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"status": "failed", "summary": "failed_validation"}),
                    encoding="utf-8",
                )
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
            worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "logs" / "stack.log"),
            )
            self.assertIn("permission denied", actionable[0]["excerpt"])

    def test_validate_prefers_compiler_failure_in_validation_log_over_setup_binding_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                import sys
                from pathlib import Path

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "stack.log").write_text(
                    "2026-07-01T00:46:50Z binding validation stack /tmp/stack code to /tmp/checkout\\n",
                    encoding="utf-8",
                )
                (log_dir / "validation.log").write_text(
                    "* boost-throw-exception:x64-linux@1.89.0\\n"
                    "/home/eqemu/code/world/../common/repositories/actor_action_queue_repository.h:355:5: error: call to consteval function 'fmt::fstring<std::basic_string<char>, const long &, std::basic_string<char>, const long &, const long &, unsigned long &>::fstring<383UL>' is not a constant expression\\n"
                    "/home/eqemu/code/build/vcpkg_installed/x64-linux/include/fmt/base.h:894:5: note: in call to '&fmt::fstring<...>::checker(...).context_->do_check_arg_id(6)'\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({{"status": "failed", "summary": "failed_validation"}}),
                    encoding="utf-8",
                )
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "logs" / "validation.log"),
            )
            self.assertIn("actor_action_queue_repository.h:355:5: error:", actionable[0]["excerpt"])
            self.assertIn("validation.log", result["output"]["summary"])
            self.assertIn("actor_action_queue_repository.h:355:5: error:", result["output"]["summary"])

    def test_validate_prefers_runtime_exception_in_validation_log_over_setup_binding_log(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "stack.log").write_text(
                    "2026-07-01T00:46:50Z binding validation stack /tmp/stack code to /tmp/checkout\\n",
                    encoding="utf-8",
                )
                (log_dir / "validation.log").write_text(
                    "NullPointerException: zone boot exploded\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"status": "failed", "summary": "failed_validation"}),
                    encoding="utf-8",
                )
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "logs" / "validation.log"),
            )
            self.assertIn("NullPointerException: zone boot exploded", actionable[0]["excerpt"])
            self.assertIn("validation.log", result["output"]["summary"])

    def test_validate_prefers_generic_runtime_failure_in_validation_log_over_setup_binding_log(self):
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

                evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                log_dir = evidence_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "stack.log").write_text(
                    "2026-07-01T00:46:50Z binding validation stack /tmp/stack code to /tmp/checkout\\n",
                    encoding="utf-8",
                )
                (log_dir / "validation.log").write_text(
                    "permission denied while starting zone harness\\n",
                    encoding="utf-8",
                )
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"status": "failed", "summary": "failed_validation"}),
                    encoding="utf-8",
                )
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
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "logs" / "validation.log"),
            )
            self.assertIn("permission denied while starting zone harness", actionable[0]["excerpt"])
            self.assertIn("validation.log", result["output"]["summary"])

    def test_validate_uses_protocol_evidence_artifact_when_worker_result_is_missing(self):
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
                            "command": [sys.executable, "-c", "raise SystemExit(0)"],
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
            evidence_result = json.loads(
                (run_dir / "validation-evidence" / "result.json").read_text(encoding="utf-8")
            )
            actionable = worker_result["result"]["normalized"]["actionable_failures"]

            self.assertEqual(worker_result["result"]["normalized"]["status"], "failed_missing_result")
            self.assertEqual(evidence_result["status"], "failed_missing_result")
            self.assertEqual(evidence_result["classification"], "missing_worker_result")
            self.assertEqual(
                actionable[0]["log_path"],
                str(run_dir / "validation-evidence" / "result.json"),
            )
            self.assertIn("worker result file was not produced", actionable[0]["excerpt"])

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
                    __COMPILER_LOG__,
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
            ).strip().replace("__COMPILER_LOG__", repr(self.COMPILER_LOG_FIXTURE))

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

    def test_validate_captures_invalid_utf8_stdout_and_stderr_with_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            worker_code = textwrap.dedent(
                """
                import sys

                sys.stdout.buffer.write(b"prefix\\xffsuffix\\n")
                sys.stdout.flush()
                sys.stderr.buffer.write(b"err\\xfeor\\n")
                sys.stderr.flush()
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
            stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")
            stderr_log = (run_dir / "stderr.log").read_text(encoding="utf-8")

            self.assertEqual(result["output"]["status"], "failed_missing_result")
            self.assertIn("prefix\ufffdsuffix", stdout_log)
            self.assertIn("err\ufffdor", stderr_log)
            self.assertIn("prefix\ufffdsuffix", worker_result["result"]["normalized"]["evidence"]["stdout_excerpt"])
            self.assertIn("err\ufffdor", worker_result["result"]["normalized"]["evidence"]["stderr_excerpt"])

    def test_run_local_command_adapter_marks_cleanup_as_best_effort_without_process_groups(self):
        from afk import validation

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            child_code = textwrap.dedent(
                """
                import time

                time.sleep(2)
                """
            ).strip()
            worker_code = textwrap.dedent(
                f"""
                import subprocess
                import sys

                subprocess.Popen([sys.executable, "-c", {child_code!r}])
                """
            ).strip()

            with mock.patch.object(validation, "supports_process_groups", return_value=False):
                with self.assertRaises(validation.WorkerRuntimeError) as raised:
                    validation.run_local_command_adapter(
                        [sys.executable, "-c", worker_code],
                        cwd=temp_path,
                        env=os.environ.copy(),
                        timeout_seconds=0.2,
                    )

        error = raised.exception
        self.assertTrue(error.timed_out)
        self.assertEqual(error.failure_artifact["process_state"], "stdout_stderr_open_after_exit")
        self.assertIn("best-effort", error.failure_artifact["remediation"])

    def test_stopped_process_message_limits_cleanup_scope_to_worker_process_group(self):
        from afk import validation

        message = validation.format_stopped_process_message(
            1234,
            [{"pid": 1234, "name": "python", "state": "T (stopped)"}],
        )

        self.assertIn("worker process group", message)
        self.assertIn("setsid/setpgid", message)
        self.assertIn("outside AFK's cleanup guarantee", message)

    def test_descendant_stdio_failure_artifact_scopes_detached_cleanup_to_best_effort(self):
        from afk import validation

        artifact = validation.descendant_stdio_failure_artifact(process_groups_available=True)

        self.assertEqual(artifact["process_state"], "stdout_stderr_open_after_exit")
        self.assertIn("worker process group", artifact["remediation"])
        self.assertIn("setsid/setpgid", artifact["remediation"])
        self.assertIn("outside AFK's cleanup guarantee", artifact["remediation"])


if __name__ == "__main__":
    unittest.main()
