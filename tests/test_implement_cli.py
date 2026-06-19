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
    git(path, "checkout", "-b", "afk/test-work")
    return git(path, "rev-parse", "HEAD")


def selected_work(source_type="fixture", external_id="central-lve.5"):
    return {
        "source_id": source_type,
        "source_type": source_type,
        "external_id": external_id,
        "url": "https://tracker.example/central-lve.5",
        "title": "Implement Pi-backed agent task step",
        "status": "open",
        "labels": ["project:afk-composable-pipeline", "afk:ready"],
        "parent": "central-lve",
        "workstream": "central-lve",
        "acceptance_criteria": ["Fake Pi adapter implementation step writes normalized result artifacts."],
        "dependencies": [{"id": "central-lve.4", "status": "closed"}],
        "blockers": [],
        "dependency_status": "clear",
        "afk": {"ready": True},
        "raw": {source_type: {"id": external_id}},
    }


class ImplementCliTest(unittest.TestCase):
    def test_implement_runs_fake_adapter_and_writes_normalized_agent_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(__import__("os").environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                Path("implemented.txt").write_text(capsule["work_item"]["external_id"] + "\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement central-lve.5"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "implemented fixture work", "notes": ["fake adapter"]}),
                    encoding="utf-8",
                )
                print("agent completed")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": ["stay within checkout"],
                        "validation": {
                            "profile": "tier1",
                            "commands": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
                        },
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [
                                sys.executable,
                                "-c",
                                agent_code,
                                "--author",
                                "pipeline-bot",
                                "--tokenize",
                                "work-item",
                                "--secretary",
                                "notes",
                            ],
                            "result_path": "agent-result.json",
                        },
                    }
                ),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            job_capsule = json.loads((run_dir / "job-capsule.json").read_text(encoding="utf-8"))
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["step"], "implement")
            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(result["output"]["summary"], "implemented fixture work")
            self.assertEqual(
                result["output"]["artifacts"],
                {"job_capsule": "job-capsule.json", "agent_result": "agent-result.json"},
            )
            self.assertEqual(job_capsule["artifact_type"], "job-capsule")
            self.assertEqual(job_capsule["capsule"]["work_item"]["external_id"], "central-lve.5")
            self.assertEqual(job_capsule["capsule"]["work_item"]["source_type"], "fixture")
            self.assertEqual(job_capsule["capsule"]["guardrails"], ["stay within checkout"])
            self.assertEqual(job_capsule["capsule"]["validation"]["profile"], "tier1")
            self.assertEqual(job_capsule["capsule"]["checkout"]["path"], str(checkout))
            self.assertEqual(job_capsule["capsule"]["checkout"]["start_commit"], start_commit)
            self.assertEqual(agent_result["artifact_type"], "agent-result")
            self.assertEqual(agent_result["result"]["status"], "implemented")
            self.assertEqual(agent_result["result"]["classification"], "success")
            self.assertEqual(agent_result["result"]["notes"], ["fake adapter"])
            self.assertEqual(result["output"]["git"]["before_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["after_commit"], git(checkout, "rev-parse", "HEAD"))
            self.assertEqual(result["output"]["git"]["changed_files"], ["implemented.txt"])
            self.assertEqual(result["output"]["git"]["dirty"], False)
            stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")
            self.assertIn("implement central-lve.5", stdout_log)
            self.assertIn("agent completed", stdout_log)
            self.assertEqual((run_dir / "stderr.log").read_text(encoding="utf-8"), "")

            events = [
                json.loads(line)
                for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[2]["artifacts"]["job_capsule"], "job-capsule.json")
            self.assertEqual(events[2]["artifacts"]["agent_result"], "agent-result.json")

    def test_implement_classifies_adapter_nonzero_as_runtime_failure_with_redacted_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            secret_url = "https://user:runtime-secret@example.invalid/repo.git?token=query-secret"
            agent_code = textwrap.dedent(
                f"""
                import sys
                print("starting adapter")
                print("{secret_url}", file=sys.stderr)
                sys.exit(7)
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in [
                    run_dir / "command.json",
                    run_dir / "step-result.json",
                    run_dir / "agent-result.json",
                    run_dir / "stdout.log",
                    run_dir / "stderr.log",
                ]
            )

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["classification"], "runtime_failure")
            self.assertEqual(agent_result["result"]["adapter"]["returncode"], 7)
            self.assertEqual(agent_result["result"]["evidence"]["stdout_excerpt"], "starting adapter\n")
            self.assertIn("https://example.invalid/repo.git", agent_result["result"]["evidence"]["stderr_excerpt"])
            self.assertNotIn("runtime-secret", artifact_text)
            self.assertNotIn("query-secret", artifact_text)
            self.assertEqual(result["output"]["git"]["before_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["after_commit"], start_commit)

    def test_implement_classifies_agent_reported_target_failure_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            agent_code = textwrap.dedent(
                """
                import json
                from pathlib import Path

                Path("agent-result.json").write_text(
                    json.dumps(
                        {
                            "status": "target_failed",
                            "summary": "validation failed",
                            "notes": ["tests need attention"],
                            "failures": [{"type": "test", "message": "unit failure"}],
                        }
                    ),
                    encoding="utf-8",
                )
                print("target validation failed")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_target")
            self.assertEqual(result["output"]["classification"], "target_failure")
            self.assertEqual(agent_result["result"]["summary"], "validation failed")
            self.assertEqual(agent_result["result"]["notes"], ["tests need attention"])
            self.assertEqual(
                agent_result["result"]["failures"],
                [{"type": "test", "message": "unit failure"}],
            )
            self.assertEqual(agent_result["result"]["adapter"]["returncode"], 0)
            self.assertEqual(result["output"]["git"]["before_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["after_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["dirty"], False)
            self.assertIn("target validation failed", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_classifies_missing_agent_result_as_protocol_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", "print('no result produced')"],
                            "result_path": "agent-result.json",
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(agent_result["result"]["summary"], "agent result file was not produced")
            self.assertEqual(agent_result["result"]["adapter"]["returncode"], 0)
            self.assertIn("no result produced", agent_result["result"]["evidence"]["stdout_excerpt"])

    def test_implement_does_not_expose_ambient_secrets_to_adapter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            fake_home = temp_path / "home-with-secrets"
            fake_home.mkdir()
            fake_config = temp_path / "config-with-secrets"
            fake_config.mkdir()
            secret = "ambient-pi-secret"
            agent_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                capsule_dir = Path(os.environ["AFK_JOB_CAPSULE"]).parent
                home = os.environ.get("HOME")
                xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
                isolated_home = (
                    home
                    and Path(home).is_dir()
                    and Path(home).is_relative_to(capsule_dir)
                    and xdg_config_home
                    and Path(xdg_config_home).is_dir()
                    and Path(xdg_config_home).is_relative_to(capsule_dir)
                )
                saw_ambient_secret = "PI_TOKEN" in os.environ or "GITHUB_TOKEN" in os.environ
                print("PI_TOKEN=" + os.environ.get("PI_TOKEN", "missing"))
                print("GITHUB_TOKEN=" + os.environ.get("GITHUB_TOKEN", "missing"))
                print("HOME=" + os.environ.get("HOME", "missing"))
                print("XDG_CONFIG_HOME=" + os.environ.get("XDG_CONFIG_HOME", "missing"))
                Path("agent-result.json").write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "summary": "env isolated" if isolated_home and not saw_ambient_secret else "ambient secret visible",
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                        },
                    }
                ),
                "--ledger",
                str(ledger),
                env_overrides={
                    "PI_TOKEN": secret,
                    "GITHUB_TOKEN": secret,
                    "HOME": str(fake_home),
                    "XDG_CONFIG_HOME": str(fake_config),
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )
            self.assertEqual(result["output"]["summary"], "env isolated")
            self.assertNotIn(secret, artifact_text)
            self.assertNotIn(str(fake_home), artifact_text)
            self.assertNotIn(str(fake_config), artifact_text)
            self.assertIn("PI_TOKEN=[REDACTED]", artifact_text)
            self.assertIn("GITHUB_TOKEN=[REDACTED]", artifact_text)
            self.assertIn("HOME=", artifact_text)
            self.assertIn("XDG_CONFIG_HOME=", artifact_text)
            self.assertNotIn("HOME=missing", artifact_text)
            self.assertNotIn("XDG_CONFIG_HOME=missing", artifact_text)

    def test_implement_ignores_stale_agent_result_and_requires_fresh_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            (checkout / "agent-result.json").write_text(
                json.dumps({"status": "completed", "summary": "stale success"}),
                encoding="utf-8",
            )

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", "print('no fresh result')"],
                            "result_path": "agent-result.json",
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(agent_result["result"]["summary"], "agent result file was not produced")
            self.assertNotIn("stale success", json.dumps(result))
            self.assertFalse((checkout / "agent-result.json").exists())

    def test_implement_rejects_preexisting_agent_result_symlink_without_removing_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            readme_before = (checkout / "README.md").read_text(encoding="utf-8")
            (checkout / "agent-result.json").symlink_to("README.md")

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", "print('should not run')"],
                            "result_path": "agent-result.json",
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(
                agent_result["result"]["summary"],
                "agent result_path exists but is a symlink",
            )
            self.assertTrue((checkout / "agent-result.json").is_symlink())
            self.assertEqual(os.readlink(checkout / "agent-result.json"), "README.md")
            self.assertEqual((checkout / "README.md").read_text(encoding="utf-8"), readme_before)
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_agent_result_symlink_without_deleting_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            readme_before = (checkout / "README.md").read_text(encoding="utf-8")
            agent_code = textwrap.dedent(
                """
                from pathlib import Path

                Path("agent-result.json").symlink_to("README.md")
                print("symlink result produced")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(agent_result["result"]["summary"], "agent result_path is a symlink")
            self.assertEqual((checkout / "README.md").read_text(encoding="utf-8"), readme_before)
            self.assertIn("symlink result produced", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_non_reserved_agent_result_path_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            readme_before = (checkout / "README.md").read_text(encoding="utf-8")

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", "print('should not run')"],
                            "result_path": "README.md",
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
            self.assertEqual(result["output"]["message"], "agent.result_path must be agent-result.json")
            self.assertEqual((checkout / "README.md").read_text(encoding="utf-8"), readme_before)
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_agent_command_credential_arguments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            secret = "pi-command-secret"

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [
                                sys.executable,
                                "-c",
                                "print('should not run')",
                                "--auth-file",
                                f"/tmp/{secret}",
                                "--auth.file",
                                f"/tmp/{secret}-dotted-auth",
                                "--credential-file",
                                f"/tmp/{secret}-credential",
                                f"--credential.file=/tmp/{secret}-dotted-credential",
                                "--access-token",
                                f"{secret}-access",
                                "--api.key",
                                f"{secret}-api-dot",
                                f"--api.key={secret}-api-dot-equals",
                                f"--token={secret}",
                                f"--github-token={secret}-github",
                            ],
                            "result_path": "agent-result.json",
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertIn("agent.command must not include credential flag", result["output"]["message"])
            self.assertNotIn(secret, artifact_text)
            self.assertNotIn(f"{secret}-dotted-auth", artifact_text)
            self.assertNotIn(f"{secret}-dotted-credential", artifact_text)
            self.assertNotIn(f"{secret}-api-dot", artifact_text)
            self.assertNotIn(f"{secret}-api-dot-equals", artifact_text)
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_refuses_checkout_with_mismatched_start_commit_before_adapter_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            commit_file = checkout / "later.txt"
            commit_file.write_text("later\n", encoding="utf-8")
            git(checkout, "add", "later.txt")
            git(checkout, "commit", "-m", "later commit")
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [
                                sys.executable,
                                "-c",
                                "raise SystemExit('adapter should not run')",
                            ],
                            "result_path": "agent-result.json",
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
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(
                result["output"]["summary"],
                "checkout HEAD does not match checkout.start_commit",
            )
            self.assertNotIn("adapter should not run", (run_dir / "stderr.log").read_text(encoding="utf-8"))

    def test_implement_refuses_dirty_checkout_before_adapter_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            (checkout / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [
                                sys.executable,
                                "-c",
                                "raise SystemExit('adapter should not run')",
                            ],
                            "result_path": "agent-result.json",
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
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(
                result["output"]["summary"],
                "checkout has uncommitted changes before agent execution",
            )
            self.assertEqual(result["output"]["git"]["changed_files"], ["dirty.txt"])
            self.assertNotIn("adapter should not run", (run_dir / "stderr.log").read_text(encoding="utf-8"))

    def test_implement_preserves_agent_classification_when_post_metadata_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            agent_code = textwrap.dedent(
                """
                import json
                import shutil
                from pathlib import Path

                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "metadata damaged"}),
                    encoding="utf-8",
                )
                shutil.rmtree(".git")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
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
            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(result["output"]["git"]["metadata_status"], "failed")
            self.assertEqual(result["output"]["git"]["before_commit"], start_commit)

    def test_implement_consumes_normalized_work_items_without_source_specific_structures(self):
        for source_type, external_id in (
            ("fixture", "central-lve.5"),
            ("github_issues", "thunderbump/afk-composable-pipeline#5"),
            ("beads", "central-lve.5"),
        ):
            with self.subTest(source_type=source_type):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    checkout = temp_path / "checkout"
                    start_commit = init_checkout(checkout)
                    ledger = temp_path / "ledger"
                    completed = run_afk(
                        "run-step",
                        "implement",
                        "--input",
                        json.dumps(
                            {
                                "work_selection": {
                                    "schema_version": 1,
                                    "selected_work": [selected_work(source_type, external_id)],
                                },
                                "checkout": {
                                    "status": "prepared",
                                    "checkout_path": str(checkout),
                                    "review_branch": "afk/test-work",
                                    "requested_ref": "main",
                                    "start_commit": start_commit,
                                },
                                "guardrails": [],
                                "validation": {"profile": "tier1", "commands": []},
                                "agent": {
                                    "type": "fake-pi-command",
                                    "command": [
                                        sys.executable,
                                        "-c",
                                        "from pathlib import Path; Path('agent-result.json').write_text('{\"status\":\"completed\",\"summary\":\"done\"}', encoding='utf-8')",
                                    ],
                                    "result_path": "agent-result.json",
                                },
                            }
                        ),
                        "--ledger",
                        str(ledger),
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    summary = json.loads(completed.stdout)
                    run_dir = ledger / "runs" / summary["run_id"]
                    job_capsule = json.loads((run_dir / "job-capsule.json").read_text(encoding="utf-8"))
                    work_item = job_capsule["capsule"]["work_item"]

                    self.assertEqual(work_item["source_type"], source_type)
                    self.assertEqual(work_item["external_id"], external_id)
                    self.assertNotIn("raw", work_item)

    def test_implement_does_not_persist_pi_credentials_from_rejected_agent_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [selected_work()]},
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/test-work",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "guardrails": [],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", "print('should not run')"],
                            "result_path": "agent-result.json",
                            "credentials_path": "/tmp/pi-auth-secret-token",
                            "token": "pi-secret-token",
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
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )

            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertIn("credentials_path", result["output"]["message"])
            self.assertNotIn("pi-auth-secret-token", artifact_text)
            self.assertNotIn("pi-secret-token", artifact_text)
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))
