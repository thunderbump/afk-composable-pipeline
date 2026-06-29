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
    git(path, "checkout", "-b", "afk/review")
    start_commit = git(path, "rev-parse", "HEAD")
    (path / "reviewed.txt").write_text("implemented\n", encoding="utf-8")
    git(path, "add", "reviewed.txt")
    git(path, "commit", "-m", "implement reviewed work")
    return start_commit


def selected_work():
    return {
        "source_id": "fixture",
        "source_type": "fixture",
        "external_id": "central-lve.8",
        "url": "https://tracker.example/central-lve.8",
        "title": "Implement ledger-backed final review step",
        "status": "open",
        "labels": ["project:afk-composable-pipeline", "afk:ready"],
        "parent": "central-lve",
        "workstream": "central-lve",
        "acceptance_criteria": [
            "afk run-step review produces a final evidence pack",
            "A fake reviewer adapter can pass/fail/request-revision using only the evidence pack",
        ],
        "dependencies": [{"id": "central-lve.7", "status": "closed"}],
        "blockers": [],
        "dependency_status": "clear",
        "afk": {"ready": True},
    }


def write_validation_artifacts(path, *, status="validated", classification="success", summary="tier1 passed"):
    path.mkdir(parents=True)
    step_result = {
        "schema_version": 1,
        "run_id": "validation-run",
        "step": "validate",
        "status": validation_step_result_status(status),
        "output": {
            "schema_version": 1,
            "status": status,
            "classification": classification,
            "summary": summary,
            "validation": {"requested_profile": "tier1", "worker_profile": "tier1"},
            "artifacts": {
                "worker_request": "worker-request.json",
                "worker_result": "worker-result.json",
            },
        },
        "result_sha256": "0" * 64,
    }
    worker_result = {
        "schema_version": 1,
        "run_id": "validation-run",
        "step": "validate",
        "artifact_type": "worker-result",
        "result": {
            "raw": {"status": "pass", "steps": [{"name": "unit", "status": "pass"}]},
            "normalized": {
                "schema_version": 1,
                "status": status,
                "classification": classification,
                "summary": summary,
                "failures": [],
            },
        },
    }
    (path / "step-result.json").write_text(json.dumps(step_result), encoding="utf-8")
    (path / "worker-result.json").write_text(json.dumps(worker_result), encoding="utf-8")
    return path / "step-result.json", path / "worker-result.json"


def validation_step_result_status(status):
    if status == "validated":
        return "succeeded"
    if status == "skipped_profile":
        return "skipped"
    return "failed"


def run_dir_text(run_dir):
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    )


def review_input(
    *,
    checkout,
    start_commit,
    head_commit,
    validation_step,
    validation_worker,
    reviewer_code,
    work_item=None,
    guardrails=None,
    cleanup=None,
):
    return {
        "work_item": work_item or selected_work(),
        "checkout": {
            "status": "prepared",
            "checkout_path": str(checkout),
            "review_branch": "afk/review",
            "requested_ref": "main",
            "start_commit": start_commit,
        },
        "implementation": {
            "status": "implemented",
            "summary": "implemented reviewed work",
            "git": {
                "before_commit": start_commit,
                "after_commit": head_commit,
                "changed_files": ["reviewed.txt"],
                "commits": [{"commit": head_commit, "subject": "implement reviewed work"}],
                "dirty": False,
                "dirty_status": [],
            },
        },
        "validation": {
            "required_artifacts": [
                {
                    "name": "tier1",
                    "step_result_path": str(validation_step),
                    "worker_result_path": str(validation_worker),
                }
            ]
        },
        "guardrails": guardrails if guardrails is not None else [],
        "cleanup": cleanup if cleanup is not None else {"status": "clean", "resources": []},
        "reviewer": {
            "type": "fake-reviewer-command",
            "command": [sys.executable, "-c", reviewer_code],
            "timeout_seconds": 10,
        },
    }


class ReviewCliTest(unittest.TestCase):
    def test_review_passes_with_fake_reviewer_and_writes_evidence_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                request = json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
                pack = request["evidence_pack"]
                assert pack["work_item"]["external_id"] == "central-lve.8"
                assert pack["acceptance_criteria"][0].startswith("afk run-step review")
                assert pack["implementation"]["git"]["changed_files"] == ["reviewed.txt"]
                assert pack["validation"]["required"][0]["status"] == "validated"
                assert pack["guardrails"][0]["status"] == "pass"
                assert pack["cleanup"]["status"] == "clean"
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "status": "pass",
                            "summary": "final evidence is complete",
                            "findings": [
                                {
                                    "status": "pass",
                                    "title": "Evidence pack is sufficient",
                                    "evidence": ["tier1 validated", "changed file recorded"],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                print("review saw evidence pack")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    {
                        "work_item": selected_work(),
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(checkout),
                            "review_branch": "afk/review",
                            "requested_ref": "main",
                            "start_commit": start_commit,
                        },
                        "implementation": {
                            "status": "implemented",
                            "summary": "implemented reviewed work",
                            "git": {
                                "before_commit": start_commit,
                                "after_commit": head_commit,
                                "changed_files": ["reviewed.txt"],
                                "commits": [
                                    {"commit": head_commit, "subject": "implement reviewed work"}
                                ],
                                "dirty": False,
                                "dirty_status": [],
                            },
                        },
                        "validation": {
                            "required_artifacts": [
                                {
                                    "name": "tier1",
                                    "step_result_path": str(validation_step),
                                    "worker_result_path": str(validation_worker),
                                }
                            ]
                        },
                        "guardrails": [
                            {"name": "no secrets", "status": "pass", "summary": "redaction applied"}
                        ],
                        "cleanup": {"status": "clean", "resources": []},
                        "reviewer": {
                            "type": "fake-reviewer-command",
                            "command": [sys.executable, "-c", reviewer_code],
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
            evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
            reviewer_request = json.loads((run_dir / "reviewer-request.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
            review_summary = (run_dir / "review-summary.md").read_text(encoding="utf-8")

            self.assertEqual(summary["step"], "review")
            self.assertEqual(summary["status"], "succeeded")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["output"]["status"], "passed")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(
                result["output"]["artifacts"],
                {
                    "evidence_pack": "evidence-pack.json",
                    "reviewer_request": "reviewer-request.json",
                    "reviewer_result": "reviewer-result.json",
                    "review_summary": "review-summary.md",
                },
            )
            self.assertEqual(evidence_pack["artifact_type"], "evidence-pack")
            self.assertEqual(evidence_pack["evidence_pack"]["work_item"]["external_id"], "central-lve.8")
            self.assertEqual(evidence_pack["evidence_pack"]["acceptance_criteria"], selected_work()["acceptance_criteria"])
            self.assertEqual(evidence_pack["evidence_pack"]["implementation"]["git"]["changed_files"], ["reviewed.txt"])
            self.assertEqual(evidence_pack["evidence_pack"]["validation"]["required"][0]["status"], "validated")
            self.assertEqual(evidence_pack["evidence_pack"]["redaction"]["applied"], True)
            self.assertEqual(reviewer_request["evidence_pack"], evidence_pack["evidence_pack"])
            self.assertEqual(reviewer_result["artifact_type"], "reviewer-result")
            self.assertEqual(reviewer_result["result"]["status"], "passed")
            self.assertEqual(reviewer_result["result"]["findings"][0]["status"], "pass")
            self.assertIn("final evidence is complete", review_summary)
            self.assertIn("Evidence pack is sufficient", review_summary)
            self.assertIn("review saw evidence pack", (run_dir / "stdout.log").read_text(encoding="utf-8"))

            events = [
                json.loads(line)
                for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[2]["artifacts"]["evidence_pack"], "evidence-pack.json")
            self.assertEqual(events[2]["artifacts"]["review_summary"], "review-summary.md")

    def test_review_passes_pi_auth_mounts_through_to_openai_codex_pi_reviewer_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            pi_bin = temp_path / "pi"
            pi_bin.write_text(
                f"""#!{sys.executable}
import json
import os
from pathlib import Path

assert os.environ["CODEX_HOME"] == {str(codex_home)!r}
assert os.environ["XDG_CONFIG_HOME"] == {str(config_home)!r}
assert os.environ["PI_CONFIG_HOME"] == {str(pi_config_home)!r}
assert os.environ["PI_CODING_AGENT_DIR"] == {str(pi_coding_agent_dir)!r}
Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
    json.dumps({{"status": "pass", "summary": "reviewer auth mounts available", "findings": []}}),
    encoding="utf-8",
)
""",
            )
            pi_bin.chmod(0o755)

            input_payload = review_input(
                checkout=checkout,
                start_commit=start_commit,
                head_commit=head_commit,
                validation_step=validation_step,
                validation_worker=validation_worker,
                reviewer_code="raise SystemExit('reviewer should not run')",
            )
            input_payload["reviewer"] = {
                "type": "fake-reviewer-command",
                "command": [str(pi_bin), "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            }

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(input_payload),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / "runs" / summary["run_id"] / "step-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["output"]["status"], "passed")
            self.assertEqual(result["output"]["summary"], "reviewer auth mounts available")

    def test_review_rejects_openai_codex_pi_reviewer_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"

            input_payload = review_input(
                checkout=checkout,
                start_commit=start_commit,
                head_commit=head_commit,
                validation_step=validation_step,
                validation_worker=validation_worker,
                reviewer_code="raise SystemExit('reviewer should not run')",
            )
            input_payload["reviewer"] = {
                "type": "fake-reviewer-command",
                "command": ["pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                "timeout_seconds": 10,
            }

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(input_payload),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertIn("reviewer.codex_home", result["output"]["message"])
            self.assertIn("pi --provider openai-codex", result["output"]["message"])

    def test_review_rejects_wrapped_openai_codex_pi_reviewer_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"

            commands = [
                ["/usr/bin/env", "pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                ["python3", "-m", "pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
            ]
            for command in commands:
                with self.subTest(command=command):
                    input_payload = review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code="raise SystemExit('reviewer should not run')",
                    )
                    input_payload["reviewer"] = {
                        "type": "fake-reviewer-command",
                        "command": command,
                        "timeout_seconds": 10,
                    }

                    completed = run_afk(
                        "run-step",
                        "review",
                        "--input",
                        json.dumps(input_payload),
                        "--ledger",
                        str(ledger),
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    summary = json.loads(completed.stdout)
                    run_dir = ledger / "runs" / summary["run_id"]
                    result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

                    self.assertEqual(summary["status"], "failed")
                    self.assertEqual(result["output"]["status"], "failed_invalid_payload")
                    self.assertIn("reviewer.codex_home", result["output"]["message"])
                    self.assertIn("pi --provider openai-codex", result["output"]["message"])

    def test_review_rejects_shell_wrapped_openai_codex_pi_reviewer_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"

            input_payload = review_input(
                checkout=checkout,
                start_commit=start_commit,
                head_commit=head_commit,
                validation_step=validation_step,
                validation_worker=validation_worker,
                reviewer_code="raise SystemExit('reviewer should not run')",
            )
            input_payload["reviewer"] = {
                "type": "fake-reviewer-command",
                "command": ["bash", "-lc", "pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini"],
                "timeout_seconds": 10,
            }

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(input_payload),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertIn("reviewer.codex_home", result["output"]["message"])
            self.assertIn("pi --provider openai-codex", result["output"]["message"])

    def test_review_rejects_assignment_prefixed_shell_wrapped_openai_codex_pi_reviewer_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"

            input_payload = review_input(
                checkout=checkout,
                start_commit=start_commit,
                head_commit=head_commit,
                validation_step=validation_step,
                validation_worker=validation_worker,
                reviewer_code="raise SystemExit('reviewer should not run')",
            )
            input_payload["reviewer"] = {
                "type": "fake-reviewer-command",
                "command": [
                    "bash",
                    "-lc",
                    "FOO=bar pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini",
                ],
                "timeout_seconds": 10,
            }

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(input_payload),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertIn("reviewer.codex_home", result["output"]["message"])
            self.assertIn("pi --provider openai-codex", result["output"]["message"])

    def test_review_rejects_exec_and_split_string_wrapped_openai_codex_pi_reviewer_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"

            commands = [
                ["bash", "-lc", "exec pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini"],
                ["/usr/bin/env", "--split-string=pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini"],
            ]
            for command in commands:
                with self.subTest(command=command):
                    input_payload = review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code="raise SystemExit('reviewer should not run')",
                    )
                    input_payload["reviewer"] = {
                        "type": "fake-reviewer-command",
                        "command": command,
                        "timeout_seconds": 10,
                    }

                    completed = run_afk(
                        "run-step",
                        "review",
                        "--input",
                        json.dumps(input_payload),
                        "--ledger",
                        str(ledger),
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    summary = json.loads(completed.stdout)
                    run_dir = ledger / "runs" / summary["run_id"]
                    result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

                    self.assertEqual(summary["status"], "failed")
                    self.assertEqual(result["output"]["status"], "failed_invalid_payload")
                    self.assertIn("reviewer.codex_home", result["output"]["message"])
                    self.assertIn("pi --provider openai-codex", result["output"]["message"])

    def test_review_rejects_non_openai_pi_reviewer_mounts_for_direct_entry_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()

            commands = [
                ["/usr/bin/env", "pi", "-p", "{prompt}", "--provider", "anthropic", "--model", "gpt-5.4-mini"],
                ["python3", "-m", "pi", "-p", "{prompt}", "--provider", "anthropic", "--model", "gpt-5.4-mini"],
            ]
            for command in commands:
                with self.subTest(command=command):
                    input_payload = review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code="raise SystemExit('reviewer should not run')",
                    )
                    input_payload["reviewer"] = {
                        "type": "fake-reviewer-command",
                        "command": command,
                        "timeout_seconds": 10,
                        "codex_home": str(codex_home),
                        "config_home": str(config_home),
                        "env": {
                            "PI_CONFIG_HOME": str(pi_config_home),
                            "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                        },
                    }

                    completed = run_afk(
                        "run-step",
                        "review",
                        "--input",
                        json.dumps(input_payload),
                        "--ledger",
                        str(ledger),
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    summary = json.loads(completed.stdout)
                    run_dir = ledger / "runs" / summary["run_id"]
                    result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

                    self.assertEqual(summary["status"], "failed")
                    self.assertEqual(result["output"]["status"], "failed_invalid_payload")
                    self.assertIn("reviewer.codex_home", result["output"]["message"])
                    self.assertIn("only supported when reviewer.command uses pi --provider openai-codex", result["output"]["message"])

    def test_review_rejects_unknown_provider_pi_reviewer_mounts_for_direct_entry_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()

            input_payload = review_input(
                checkout=checkout,
                start_commit=start_commit,
                head_commit=head_commit,
                validation_step=validation_step,
                validation_worker=validation_worker,
                reviewer_code="raise SystemExit('reviewer should not run')",
            )
            input_payload["reviewer"] = {
                "type": "fake-reviewer-command",
                "command": ["pi", "-p", "{prompt}", "--model", "gpt-5.4-mini"],
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            }

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(input_payload),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertIn("reviewer.codex_home", result["output"]["message"])
            self.assertIn("provider could not be determined", result["output"]["message"])

    def test_review_rejects_non_pi_reviewer_command_mounts_for_direct_entry_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()

            input_payload = review_input(
                checkout=checkout,
                start_commit=start_commit,
                head_commit=head_commit,
                validation_step=validation_step,
                validation_worker=validation_worker,
                reviewer_code="raise SystemExit('reviewer should not run')",
            )
            input_payload["reviewer"] = {
                "type": "fake-reviewer-command",
                "command": [sys.executable, "-c", "raise SystemExit('reviewer should not run')"],
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            }

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(input_payload),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertIn("reviewer.codex_home", result["output"]["message"])
            self.assertIn("only supported when reviewer.command uses pi --provider openai-codex", result["output"]["message"])

    def test_review_substitutes_prompt_for_reviewer_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            work_item = selected_work()
            work_item["acceptance_criteria"] = [
                "Keep {request_path} literal",
                "Keep {result_path} literal",
            ]
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                request = json.loads(sys.argv[1])
                if request["artifact_type"] != "reviewer-request":
                    raise SystemExit("missing reviewer request prompt")
                if request["evidence_pack"]["work_item"]["external_id"] != "central-lve.8":
                    raise SystemExit("missing evidence pack")
                criteria = request["evidence_pack"]["acceptance_criteria"]
                expected = [
                    "Keep {" + "request_path} literal",
                    "Keep {" + "result_path} literal",
                ]
                if criteria != expected:
                    raise SystemExit("prompt placeholders were rewritten")
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({"status": "pass", "summary": "review prompt accepted", "findings": []}),
                    encoding="utf-8",
                )
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                        work_item=work_item,
                    )
                    | {
                        "reviewer": {
                            "type": "fake-reviewer-command",
                            "command": [sys.executable, "-c", reviewer_code, "{prompt}"],
                            "timeout_seconds": 10,
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

            self.assertEqual(result["output"]["status"], "passed")
            self.assertEqual(result["output"]["summary"], "review prompt accepted")

    def test_review_refuses_pass_when_required_validation_artifact_is_not_validated(self):
        cases = [
            ("missing", None, None, "required validation artifact missing is not validated"),
            ("failed", "failed_validation", "worker_failure", "tests failed"),
            ("skipped", "skipped_profile", "profile_skipped", "profile skipped"),
            ("protocol", "failed_protocol", "protocol_failure", "protocol failed"),
        ]
        for name, validation_status, validation_classification, validation_summary in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                checkout = temp_path / "checkout"
                start_commit = init_checkout(checkout)
                head_commit = git(checkout, "rev-parse", "HEAD")
                if validation_status is None:
                    validation_step = temp_path / "missing-validation" / "step-result.json"
                    validation_worker = temp_path / "missing-validation" / "worker-result.json"
                else:
                    validation_step, validation_worker = write_validation_artifacts(
                        temp_path / "validation-run",
                        status=validation_status,
                        classification=validation_classification,
                        summary=validation_summary,
                    )
                ledger = temp_path / "ledger"
                reviewer_code = textwrap.dedent(
                    """
                    import json
                    import os
                    from pathlib import Path

                    Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                        json.dumps({"status": "pass", "summary": "reviewer should not decide"}),
                        encoding="utf-8",
                    )
                    print("reviewer should not run")
                    """
                ).strip()

                completed = run_afk(
                    "run-step",
                    "review",
                    "--input",
                    json.dumps(
                        {
                            "work_item": selected_work(),
                            "checkout": {
                                "status": "prepared",
                                "checkout_path": str(checkout),
                                "review_branch": "afk/review",
                                "requested_ref": "main",
                                "start_commit": start_commit,
                            },
                            "implementation": {
                                "status": "implemented",
                                "summary": "implemented reviewed work",
                                "git": {
                                    "before_commit": start_commit,
                                    "after_commit": head_commit,
                                    "changed_files": ["reviewed.txt"],
                                    "commits": [
                                        {"commit": head_commit, "subject": "implement reviewed work"}
                                    ],
                                    "dirty": False,
                                    "dirty_status": [],
                                },
                            },
                            "validation": {
                                "required_artifacts": [
                                    {
                                        "name": "tier1",
                                        "step_result_path": str(validation_step),
                                        "worker_result_path": str(validation_worker),
                                    }
                                ]
                            },
                            "guardrails": [],
                            "cleanup": {"status": "clean", "resources": []},
                            "reviewer": {
                                "type": "fake-reviewer-command",
                                "command": [sys.executable, "-c", reviewer_code],
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
                reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
                review_summary = (run_dir / "review-summary.md").read_text(encoding="utf-8")

                self.assertEqual(result["output"]["status"], "failed_validation_evidence")
                self.assertEqual(result["output"]["classification"], "validation_evidence_incomplete")
                self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
                self.assertIn("tier1", reviewer_result["result"]["summary"])
                self.assertIn("tier1", review_summary)
                self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_invalid_payload_marks_top_level_step_status_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps({"work_item": None}),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")

    def test_review_protocol_failure_marks_top_level_step_status_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = "print('reviewer returned no payload')"

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
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
            self.assertEqual(result["output"]["status"], "failed_protocol")

    def test_review_accepts_reviewer_json_from_stdout_when_result_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json

                print(
                    json.dumps(
                        {
                            "artifact_type": "reviewer-result",
                            "status": "request_revision",
                            "summary": "stdout requested changes",
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "title": "Need another validation pass",
                                    "evidence": ["stdout fallback"],
                                }
                            ],
                        }
                    )
                )
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "succeeded")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["output"]["status"], "request_revision")
            self.assertEqual(result["output"]["classification"], "review_revision_requested")
            self.assertEqual(reviewer_result["result"]["status"], "request_revision")
            self.assertEqual(reviewer_result["result"]["findings"][0]["title"], "Need another validation pass")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], False)

    def test_review_rejects_unmarked_review_shaped_stdout_json_when_result_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json

                print(
                    json.dumps(
                        {
                            "status": "request_revision",
                            "summary": "stdout requested changes",
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "title": "Need another validation pass",
                                    "evidence": ["stdout fallback"],
                                }
                            ],
                        }
                    )
                )
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(result["output"]["summary"], "reviewer stdout JSON must match the reviewer result schema")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], False)

    def test_review_rejects_status_only_stdout_json_when_result_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json

                print(json.dumps({"status": "pass"}))
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(result["output"]["summary"], "reviewer stdout JSON must match the reviewer result schema")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], False)

    def test_review_rejects_stdout_json_when_findings_is_not_a_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json

                print(json.dumps({"status": "pass", "summary": "looks good", "findings": "none"}))
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(result["output"]["summary"], "reviewer stdout JSON must match the reviewer result schema")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], False)

    def test_review_rejects_generic_stdout_json_when_result_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json

                print(json.dumps({"status": "success"}))
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(
                result["output"]["summary"],
                "reviewer stdout JSON must match the reviewer result schema",
            )
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], False)

    def test_review_reports_malformed_stdout_when_result_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                print("reviewer: starting up")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(result["output"]["summary"], "reviewer stdout is not valid JSON")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], False)

    def test_review_reports_missing_result_file_when_reviewer_exits_zero_with_silent_stdout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                pass
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(result["output"]["summary"], "reviewer result file was not produced")
            self.assertEqual(reviewer_result["result"]["summary"], "reviewer result file was not produced")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], False)

    def test_review_prefers_valid_result_file_over_noisy_stdout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "status": "pass",
                            "summary": "file result wins",
                            "findings": [],
                        }
                    ),
                    encoding="utf-8",
                )
                print("reviewer: debug noise before exit")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "succeeded")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["output"]["status"], "passed")
            self.assertEqual(result["output"]["summary"], "file result wins")
            self.assertEqual(reviewer_result["result"]["summary"], "file result wins")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "reviewer_result_file")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], True)

    def test_review_does_not_fallback_to_stdout_when_result_file_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text("{not json", encoding="utf-8")
                print(json.dumps({"status": "pass", "summary": "stdout should not rescue invalid file"}))
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertEqual(result["output"]["summary"], "reviewer result file is not valid JSON")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_source"], "reviewer_result_file")
            self.assertEqual(reviewer_result["result"]["evidence"]["result_file_present"], True)

    def test_review_refuses_missing_required_validation_artifact_paths_as_validation_evidence(self):
        cases = [
            (
                "step_result_path",
                "step-result.json path is required",
                "step_result",
                "step_result_path",
            ),
            (
                "worker_result_path",
                "worker-result.json path is required",
                "worker_result",
                "worker_result_path",
            ),
        ]
        for missing_path, expected_error, result_key, path_key in cases:
            with self.subTest(missing_path=missing_path), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                checkout = temp_path / "checkout"
                start_commit = init_checkout(checkout)
                head_commit = git(checkout, "rev-parse", "HEAD")
                validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
                ledger = temp_path / "ledger"
                reviewer_code = textwrap.dedent(
                    """
                    import json
                    import os
                    from pathlib import Path

                    Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                        json.dumps({"status": "pass", "summary": "reviewer should not decide"}),
                        encoding="utf-8",
                    )
                    print("reviewer should not run")
                    """
                ).strip()
                request = review_input(
                    checkout=checkout,
                    start_commit=start_commit,
                    head_commit=head_commit,
                    validation_step=validation_step,
                    validation_worker=validation_worker,
                    reviewer_code=reviewer_code,
                )
                del request["validation"]["required_artifacts"][0][missing_path]

                completed = run_afk(
                    "run-step",
                    "review",
                    "--input",
                    json.dumps(request),
                    "--ledger",
                    str(ledger),
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                summary = json.loads(completed.stdout)
                run_dir = ledger / "runs" / summary["run_id"]
                result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
                reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
                evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
                required = evidence_pack["evidence_pack"]["validation"]["required"][0]
                finding = reviewer_result["result"]["findings"][0]

                self.assertEqual(result["output"]["status"], "failed_validation_evidence")
                self.assertEqual(result["output"]["classification"], "validation_evidence_incomplete")
                self.assertEqual(reviewer_result["artifact_type"], "reviewer-result")
                self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
                self.assertEqual(reviewer_result["result"]["classification"], "validation_evidence_incomplete")
                self.assertEqual(required["evidence_status"], "invalid")
                self.assertEqual(required[path_key], "")
                self.assertEqual(required[result_key]["status"], "invalid_path")
                self.assertIn(expected_error, required["evidence_errors"])
                self.assertIn(expected_error, finding["summary"])
                self.assertEqual(finding["validation"]["name"], "tier1")
                self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_refuses_pass_when_required_worker_result_artifact_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            validation_worker.unlink()
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({"status": "pass", "summary": "reviewer should not decide"}),
                    encoding="utf-8",
                )
                print("reviewer should not run")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
            evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_validation_evidence")
            self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
            self.assertIn("tier1", reviewer_result["result"]["summary"])
            self.assertEqual(
                evidence_pack["evidence_pack"]["validation"]["required"][0]["worker_status"],
                "missing",
            )
            self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_refuses_forged_validation_artifacts_with_validated_nested_statuses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            step_payload = json.loads(validation_step.read_text(encoding="utf-8"))
            step_payload["step"] = "review"
            step_payload["status"] = "failed"
            validation_step.write_text(json.dumps(step_payload), encoding="utf-8")
            worker_payload = json.loads(validation_worker.read_text(encoding="utf-8"))
            worker_payload["run_id"] = "forged-worker-run"
            validation_worker.write_text(json.dumps(worker_payload), encoding="utf-8")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({"status": "pass", "summary": "reviewer should not decide"}),
                    encoding="utf-8",
                )
                print("reviewer should not run")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
            evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_validation_evidence")
            self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
            self.assertIn("tier1", reviewer_result["result"]["summary"])
            self.assertEqual(evidence_pack["evidence_pack"]["validation"]["required"][0]["status"], "validated")
            self.assertEqual(
                evidence_pack["evidence_pack"]["validation"]["required"][0]["evidence_status"],
                "invalid",
            )
            self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_refuses_validation_artifacts_missing_run_id_values(self):
        cases = [
            ("step", "step_result run_id is required"),
            ("worker", "worker_result run_id is required"),
        ]
        for missing_artifact, expected_error in cases:
            with self.subTest(missing_artifact=missing_artifact), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                checkout = temp_path / "checkout"
                start_commit = init_checkout(checkout)
                head_commit = git(checkout, "rev-parse", "HEAD")
                validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
                if missing_artifact == "step":
                    payload = json.loads(validation_step.read_text(encoding="utf-8"))
                    del payload["run_id"]
                    validation_step.write_text(json.dumps(payload), encoding="utf-8")
                else:
                    payload = json.loads(validation_worker.read_text(encoding="utf-8"))
                    del payload["run_id"]
                    validation_worker.write_text(json.dumps(payload), encoding="utf-8")
                ledger = temp_path / "ledger"
                reviewer_code = textwrap.dedent(
                    """
                    import json
                    import os
                    from pathlib import Path

                    Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                        json.dumps({"status": "pass", "summary": "reviewer should not decide"}),
                        encoding="utf-8",
                    )
                    print("reviewer should not run")
                    """
                ).strip()

                completed = run_afk(
                    "run-step",
                    "review",
                    "--input",
                    json.dumps(
                        review_input(
                            checkout=checkout,
                            start_commit=start_commit,
                            head_commit=head_commit,
                            validation_step=validation_step,
                            validation_worker=validation_worker,
                            reviewer_code=reviewer_code,
                        )
                    ),
                    "--ledger",
                    str(ledger),
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                summary = json.loads(completed.stdout)
                run_dir = ledger / "runs" / summary["run_id"]
                result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
                reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
                evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
                required = evidence_pack["evidence_pack"]["validation"]["required"][0]

                self.assertEqual(result["output"]["status"], "failed_validation_evidence")
                self.assertEqual(result["output"]["classification"], "validation_evidence_incomplete")
                self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
                self.assertEqual(required["evidence_status"], "invalid")
                self.assertIn(expected_error, required["evidence_errors"])
                self.assertIn(expected_error, reviewer_result["result"]["findings"][0]["summary"])
                self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_rejects_arbitrary_validation_artifact_filenames_without_reading_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            arbitrary_artifact = validation_step.with_name("secrets.json")
            step_payload = json.loads(validation_step.read_text(encoding="utf-8"))
            step_payload["leak"] = "arbitrary-json-secret"
            arbitrary_artifact.write_text(json.dumps(step_payload), encoding="utf-8")
            opposite_worker_secret = "sk_live_worker_pair_sentinel_123"
            worker_payload = json.loads(validation_worker.read_text(encoding="utf-8"))
            worker_payload["result"]["raw"]["note"] = opposite_worker_secret
            validation_worker.write_text(json.dumps(worker_payload), encoding="utf-8")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({"status": "pass", "summary": "reviewer should not decide"}),
                    encoding="utf-8",
                )
                print("reviewer should not run")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=arbitrary_artifact,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
            evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
            artifact_text = run_dir_text(run_dir)

            self.assertEqual(result["output"]["status"], "failed_validation_evidence")
            self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
            self.assertEqual(
                evidence_pack["evidence_pack"]["validation"]["required"][0]["step_result"]["status"],
                "invalid_path",
            )
            self.assertNotIn("arbitrary-json-secret", artifact_text)
            self.assertNotIn(opposite_worker_secret, artifact_text)
            self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_rejects_arbitrary_worker_artifact_filename_without_reading_step_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            opposite_step_secret = "ghp_step_pair_sentinel_123"
            step_payload = json.loads(validation_step.read_text(encoding="utf-8"))
            step_payload["evidence_marker"] = opposite_step_secret
            validation_step.write_text(json.dumps(step_payload), encoding="utf-8")
            arbitrary_worker_artifact = validation_worker.with_name("secrets.json")
            worker_payload = json.loads(validation_worker.read_text(encoding="utf-8"))
            worker_payload["result"]["raw"]["leak"] = "arbitrary-worker-json-secret"
            arbitrary_worker_artifact.write_text(json.dumps(worker_payload), encoding="utf-8")
            ledger = temp_path / "ledger"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({"status": "pass", "summary": "reviewer should not decide"}),
                    encoding="utf-8",
                )
                print("reviewer should not run")
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=arbitrary_worker_artifact,
                        reviewer_code=reviewer_code,
                    )
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
            evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
            artifact_text = run_dir_text(run_dir)

            self.assertEqual(result["output"]["status"], "failed_validation_evidence")
            self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
            self.assertEqual(
                evidence_pack["evidence_pack"]["validation"]["required"][0]["worker_result"]["status"],
                "invalid_path",
            )
            self.assertNotIn(opposite_step_secret, artifact_text)
            self.assertNotIn("arbitrary-worker-json-secret", artifact_text)
            self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_rejects_non_sibling_validation_artifacts_without_reading_them(self):
        cases = ["step", "worker"]
        for secret_artifact in cases:
            with self.subTest(secret_artifact=secret_artifact), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                checkout = temp_path / "checkout"
                start_commit = init_checkout(checkout)
                head_commit = git(checkout, "rev-parse", "HEAD")
                trusted_step, trusted_worker = write_validation_artifacts(temp_path / "validation-run")
                outside_step, outside_worker = write_validation_artifacts(temp_path / "outside" / "validation-run")
                non_sibling_secret = f"non-sibling-{secret_artifact}-artifact-secret"
                if secret_artifact == "step":
                    step_payload = json.loads(outside_step.read_text(encoding="utf-8"))
                    step_payload["leak"] = non_sibling_secret
                    outside_step.write_text(json.dumps(step_payload), encoding="utf-8")
                    validation_step = outside_step
                    validation_worker = trusted_worker
                else:
                    worker_payload = json.loads(outside_worker.read_text(encoding="utf-8"))
                    worker_payload["result"]["raw"]["note"] = non_sibling_secret
                    outside_worker.write_text(json.dumps(worker_payload), encoding="utf-8")
                    validation_step = trusted_step
                    validation_worker = outside_worker
                ledger = temp_path / "ledger"
                reviewer_code = textwrap.dedent(
                    """
                    import json
                    import os
                    from pathlib import Path

                    request = json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
                    Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                        json.dumps(
                            {
                                "status": "pass",
                                "summary": "reviewer should not decide",
                                "findings": [{"status": "pass", "details": repr(request)}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    print("reviewer should not run")
                    """
                ).strip()

                completed = run_afk(
                    "run-step",
                    "review",
                    "--input",
                    json.dumps(
                        review_input(
                            checkout=checkout,
                            start_commit=start_commit,
                            head_commit=head_commit,
                            validation_step=validation_step,
                            validation_worker=validation_worker,
                            reviewer_code=reviewer_code,
                        )
                    ),
                    "--ledger",
                    str(ledger),
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                summary = json.loads(completed.stdout)
                run_dir = ledger / "runs" / summary["run_id"]
                result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
                reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
                evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
                artifact_text = run_dir_text(run_dir)

                self.assertEqual(result["output"]["status"], "failed_validation_evidence")
                self.assertEqual(result["output"]["classification"], "validation_evidence_incomplete")
                self.assertEqual(reviewer_result["result"]["status"], "failed_validation_evidence")
                self.assertIn(
                    "validation artifacts must be in the same or sibling ledger run directory",
                    evidence_pack["evidence_pack"]["validation"]["required"][0]["evidence_errors"],
                )
                self.assertNotIn(non_sibling_secret, artifact_text)
                self.assertNotIn("reviewer should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_review_records_reviewer_fail_and_request_revision_statuses(self):
        cases = [
            ("fail", "failed", "review_failure", "failed"),
            ("request_revision", "request_revision", "review_revision_requested", "succeeded"),
        ]
        for raw_status, expected_status, expected_classification, expected_step_status in cases:
            with self.subTest(raw_status=raw_status), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                checkout = temp_path / "checkout"
                start_commit = init_checkout(checkout)
                head_commit = git(checkout, "rev-parse", "HEAD")
                validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
                ledger = temp_path / "ledger"
                reviewer_code = textwrap.dedent(
                    f"""
                    import json
                    import os
                    from pathlib import Path

                    request = json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
                    assert request["evidence_pack"]["validation"]["required"][0]["status"] == "validated"
                    Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                        json.dumps(
                            {{
                                "status": "{raw_status}",
                                "summary": "review status {raw_status}",
                                "findings": [
                                    {{
                                        "status": "{raw_status}",
                                        "title": "Reviewer finding for {raw_status}",
                                        "evidence": ["used evidence pack only"],
                                    }}
                                ],
                            }}
                        ),
                        encoding="utf-8",
                    )
                    """
                ).strip()

                completed = run_afk(
                    "run-step",
                    "review",
                    "--input",
                    json.dumps(
                        review_input(
                            checkout=checkout,
                            start_commit=start_commit,
                            head_commit=head_commit,
                            validation_step=validation_step,
                            validation_worker=validation_worker,
                            reviewer_code=reviewer_code,
                        )
                    ),
                    "--ledger",
                    str(ledger),
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                summary = json.loads(completed.stdout)
                run_dir = ledger / "runs" / summary["run_id"]
                result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
                reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
                review_summary = (run_dir / "review-summary.md").read_text(encoding="utf-8")

                self.assertEqual(summary["status"], expected_step_status)
                self.assertEqual(result["status"], expected_step_status)
                self.assertEqual(result["output"]["status"], expected_status)
                self.assertEqual(result["output"]["classification"], expected_classification)
                self.assertEqual(reviewer_result["result"]["status"], expected_status)
                self.assertEqual(reviewer_result["result"]["classification"], expected_classification)
                self.assertEqual(reviewer_result["result"]["findings"][0]["status"], raw_status)
                self.assertIn(f"Reviewer finding for {raw_status}", review_summary)

    def test_review_redacts_nested_evidence_and_reviewer_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            head_commit = git(checkout, "rev-parse", "HEAD")
            validation_step, validation_worker = write_validation_artifacts(temp_path / "validation-run")
            validation_secret = "validation-token-secret"
            validation_payload = json.loads(validation_worker.read_text(encoding="utf-8"))
            validation_payload["result"]["raw"]["token"] = validation_secret
            validation_payload["result"]["raw"]["steps"].append(
                {"name": "secret_check", "status": "pass", "message": "API_TOKEN=" + validation_secret}
            )
            validation_worker.write_text(json.dumps(validation_payload), encoding="utf-8")
            ledger = temp_path / "ledger"
            ambient_secret = "ambient-reviewer-secret"
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                import sys
                from pathlib import Path

                saw_secret = "REVIEW_TOKEN" in os.environ or "GITHUB_TOKEN" in os.environ
                raw_secret = "raw-reviewer-" + "secret"
                json_stdout_secret = "json-stdout-" + "secret"
                json_stderr_secret = "json-stderr-" + "secret"
                json_api_secret = "json-api-" + "secret"
                print("REVIEW_TOKEN=" + os.environ.get("REVIEW_TOKEN", "missing"))
                print(json.dumps({"token": json_stdout_secret, "api_key": json_api_secret}))
                print("GITHUB_TOKEN=" + os.environ.get("GITHUB_TOKEN", "missing"), file=sys.stderr)
                print(json.dumps({"password": json_stderr_secret}), file=sys.stderr)
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "status": "pass",
                            "summary": "API_TOKEN=" + raw_secret,
                            "findings": [
                                {
                                    "status": "pass",
                                    "title": "No visible secrets" if not saw_secret else "Ambient secret visible",
                                    "details": "PASSWORD=" + raw_secret,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            work_item = {
                **selected_work(),
                "raw": {"token": "work-item-token-secret"},
            }

            completed = run_afk(
                "run-step",
                "review",
                "--input",
                json.dumps(
                    review_input(
                        checkout=checkout,
                        start_commit=start_commit,
                        head_commit=head_commit,
                        validation_step=validation_step,
                        validation_worker=validation_worker,
                        reviewer_code=reviewer_code,
                        work_item=work_item,
                        guardrails=[
                            {
                                "name": "secret scan",
                                "status": "pass",
                                "details": "API_TOKEN=guardrail-token-secret",
                            }
                        ],
                        cleanup={
                            "status": "clean",
                            "resources": [{"name": "temp", "password": "cleanup-password-secret"}],
                        },
                    )
                ),
                "--ledger",
                str(ledger),
                env_overrides={
                    "REVIEW_TOKEN": ambient_secret,
                    "GITHUB_TOKEN": ambient_secret,
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
            reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
            stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")
            stderr_log = (run_dir / "stderr.log").read_text(encoding="utf-8")
            artifact_text = run_dir_text(run_dir)

            self.assertEqual(result["output"]["status"], "passed")
            self.assertEqual(evidence_pack["evidence_pack"]["redaction"]["applied"], True)
            self.assertEqual(
                evidence_pack["evidence_pack"]["validation"]["required"][0]["worker_result"]["result"]["raw"]["token"],
                "[REDACTED]",
            )
            self.assertEqual(reviewer_result["result"]["summary"], "API_TOKEN=[REDACTED]")
            self.assertEqual(reviewer_result["result"]["findings"][0]["details"], "PASSWORD=[REDACTED]")
            self.assertIn("REVIEW_TOKEN=[REDACTED]", artifact_text)
            self.assertIn('"token": "[REDACTED]"', stdout_log)
            self.assertIn('"api_key": "[REDACTED]"', stdout_log)
            self.assertIn('"password": "[REDACTED]"', stderr_log)
            self.assertIn('"token": "[REDACTED]"', reviewer_result["result"]["evidence"]["stdout_excerpt"])
            self.assertIn('"password": "[REDACTED]"', reviewer_result["result"]["evidence"]["stderr_excerpt"])
            self.assertIn("GITHUB_TOKEN=[REDACTED]", artifact_text)
            self.assertNotIn(validation_secret, artifact_text)
            self.assertNotIn("work-item-token-secret", artifact_text)
            self.assertNotIn("guardrail-token-secret", artifact_text)
            self.assertNotIn("cleanup-password-secret", artifact_text)
            self.assertNotIn("raw-reviewer-secret", artifact_text)
            self.assertNotIn(ambient_secret, artifact_text)
            self.assertNotIn("json-stdout-secret", artifact_text)
            self.assertNotIn("json-stderr-secret", artifact_text)
            self.assertNotIn("json-api-secret", artifact_text)


if __name__ == "__main__":
    unittest.main()
