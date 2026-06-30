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
    def test_implement_provides_default_git_identity_for_commit_producing_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            git(checkout, "config", "--unset", "user.name")
            git(checkout, "config", "--unset", "user.email")
            ledger = temp_path / "ledger"
            agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("implemented\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement with default identity"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "committed with default identity"}),
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
                        "guardrails": ["stay within checkout"],
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
                    "GIT_AUTHOR_NAME": "",
                    "GIT_AUTHOR_EMAIL": "",
                    "GIT_COMMITTER_NAME": "",
                    "GIT_COMMITTER_EMAIL": "",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "committed with default identity")
            self.assertEqual(git(checkout, "log", "-1", "--format=%an <%ae>"), "AFK Pipeline <afk-pipeline@example.invalid>")

    def test_implement_preserves_repo_git_identity_for_commit_producing_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            git(checkout, "config", "user.name", "Repo Local Agent")
            git(checkout, "config", "user.email", "repo-local-agent@example.test")
            ledger = temp_path / "ledger"
            agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("implemented\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement with repo identity"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "committed with repo identity"}),
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
                        "guardrails": ["stay within checkout"],
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
                    "GIT_AUTHOR_NAME": "",
                    "GIT_AUTHOR_EMAIL": "",
                    "GIT_COMMITTER_NAME": "",
                    "GIT_COMMITTER_EMAIL": "",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "committed with repo identity")
            self.assertEqual(
                git(checkout, "log", "-1", "--format=%an <%ae>"),
                "Repo Local Agent <repo-local-agent@example.test>",
            )

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
            self.assertEqual(
                job_capsule["capsule"]["completion_contract"],
                {
                    "result_path": "agent-result.json",
                    "result_path_env": "AFK_AGENT_RESULT_PATH",
                    "write_result_file_before_exit": True,
                    "commit_required_for_success": False,
                    "instructions": [
                        "Write a JSON object matching expected_result_schema to AFK_AGENT_RESULT_PATH before exiting.",
                        "Use status=completed only when acceptance criteria are satisfied.",
                        "Use status=target_failed with failures when the requested work cannot be completed.",
                    ],
                },
            )
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

    def test_implement_runs_real_agent_command_with_explicit_auth_config_and_normalizes_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            agent_observation = temp_path / "agent-observation.json"
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                result_path = Path(os.environ["AFK_AGENT_RESULT_PATH"])
                observation = {{
                    "cwd": os.getcwd(),
                    "capsule_external_id": capsule["work_item"]["external_id"],
                    "codex_home": os.environ.get("CODEX_HOME"),
                    "pi_config_home": os.environ.get("PI_CONFIG_HOME"),
                    "home": os.environ.get("HOME"),
                    "xdg_config_home": os.environ.get("XDG_CONFIG_HOME"),
                    "home_exists": Path(os.environ["HOME"]).is_dir(),
                    "xdg_config_home_exists": Path(os.environ["XDG_CONFIG_HOME"]).is_dir(),
                    "ambient_pi_token": os.environ.get("PI_TOKEN", "missing"),
                    "ambient_openai_key": os.environ.get("OPENAI_API_KEY", "missing"),
                }}
                Path({str(agent_observation)!r}).write_text(json.dumps(observation), encoding="utf-8")
                Path("implemented.txt").write_text("real adapter smoke\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "real adapter smoke"], check=True)
                result_path.write_text(
                    json.dumps({{"status": "completed", "summary": "real adapter smoke implemented"}}),
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
                        "guardrails": ["stay within checkout"],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
                        },
                    }
                ),
                "--ledger",
                str(ledger),
                env_overrides={
                    "PI_TOKEN": "ambient-pi-secret",
                    "OPENAI_API_KEY": "ambient-openai-secret",
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))
            observation = json.loads(agent_observation.read_text(encoding="utf-8"))
            job_capsule = json.loads((run_dir / "job-capsule.json").read_text(encoding="utf-8"))
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(result["output"]["summary"], "real adapter smoke implemented")
            self.assertEqual(agent_result["result"]["adapter"]["type"], "real-agent-command")
            self.assertEqual(job_capsule["capsule"]["completion_contract"]["commit_required_for_success"], True)
            self.assertIn(
                "Commit successful code changes on the review branch before exiting.",
                job_capsule["capsule"]["completion_contract"]["instructions"],
            )
            self.assertEqual(result["output"]["git"]["changed_files"], ["implemented.txt"])
            self.assertEqual(result["output"]["git"]["dirty"], False)
            self.assertEqual(observation["cwd"], str(checkout))
            self.assertEqual(observation["capsule_external_id"], "central-lve.5")
            self.assertEqual(observation["codex_home"], str(codex_home))
            self.assertEqual(observation["pi_config_home"], str(pi_config_home))
            self.assertEqual(observation["xdg_config_home"], str(config_home))
            self.assertTrue(observation["home_exists"])
            self.assertTrue(observation["xdg_config_home_exists"])
            self.assertNotEqual(observation["home"], os.environ.get("HOME"))
            self.assertEqual(observation["ambient_pi_token"], "missing")
            self.assertEqual(observation["ambient_openai_key"], "missing")
            self.assertEqual(
                job_capsule["capsule"]["agent_mounts"],
                {
                    "codex_home": str(codex_home),
                    "config_home": str(config_home),
                    "pi_config_home": str(pi_config_home),
                },
            )
            self.assertIn(str(codex_home), artifact_text)
            self.assertIn(str(config_home), artifact_text)
            self.assertIn(str(pi_config_home), artifact_text)
            self.assertNotIn("ambient-pi-secret", artifact_text)
            self.assertNotIn("ambient-openai-secret", artifact_text)

    def test_implement_substitutes_job_prompt_for_real_agent_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            agent_code = temp_path / "real_agent.py"
            agent_code.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import subprocess
                    import sys
                    from pathlib import Path

                    prompt = sys.argv[1]
                    if prompt == "{prompt}":
                        raise SystemExit("real prompt was not rendered")
                    request = json.loads(prompt)
                    if request["work_item"]["external_id"] != "central-lve.5":
                        raise SystemExit("unexpected work item in rendered prompt")
                    Path("agent-prompt.json").write_text(prompt, encoding="utf-8")
                    Path("implemented.txt").write_text("real adapter with prompt", encoding="utf-8")
                    subprocess.run(["git", "add", "implemented.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "real adapter prompt smoke"], check=True)
                    Path(os.environ["AFK_AGENT_RESULT_PATH"]).write_text(
                        json.dumps({"status": "completed", "summary": "real adapter rendered prompt"}),
                        encoding="utf-8",
                    )
                    """
                ).strip(),
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
                            "type": "real-agent-command",
                            "command": [sys.executable, str(agent_code), "{prompt}"],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))
            rendered_prompt = json.loads((checkout / "agent-prompt.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "real adapter rendered prompt")
            self.assertEqual(agent_result["result"]["adapter"]["type"], "real-agent-command")
            self.assertEqual(rendered_prompt["work_item"]["external_id"], "central-lve.5")
            self.assertNotIn("{prompt}", json.dumps(rendered_prompt))

    def test_implement_preserves_secret_redaction_for_rendered_real_agent_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            secret = "ghp_secret_prompt_1234567890"
            agent_code = temp_path / "real_agent_redaction.py"
            agent_code.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import subprocess
                    import sys
                    from pathlib import Path

                    prompt = sys.argv[1]
                    request = json.loads(prompt)
                    print(request["work_item"]["acceptance_criteria"][0])
                    Path("implemented.txt").write_text("real adapter redaction smoke", encoding="utf-8")
                    subprocess.run(["git", "add", "implemented.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "real adapter redaction smoke"], check=True)
                    Path(os.environ["AFK_AGENT_RESULT_PATH"]).write_text(
                        json.dumps({"status": "completed", "summary": "real adapter redaction smoke"}),
                        encoding="utf-8",
                    )
                    """
                ).strip(),
                encoding="utf-8",
            )

            work_item = selected_work()
            work_item["acceptance_criteria"] = [f"validate token={secret} handling"]

            completed = run_afk(
                "run-step",
                "implement",
                "--input",
                json.dumps(
                    {
                        "work_selection": {"schema_version": 1, "selected_work": [work_item]},
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
                            "type": "real-agent-command",
                            "command": [sys.executable, str(agent_code), "{prompt}"],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", stdout_log)
            self.assertNotIn(secret, stdout_log)

    def test_implement_runs_real_agent_wrapper_with_runner_local_secret_file_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            wrapper_secret_dir = temp_path / "runner-secrets"
            wrapper_secret_file = wrapper_secret_dir / "openai-api-key.txt"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            wrapper_secret_dir.mkdir()
            wrapper_secret = "ghp_wrapper_contract_secret_1234567890"
            wrapper_secret_file.write_text(wrapper_secret + "\n", encoding="utf-8")
            agent_observation = temp_path / "agent-observation.json"
            wrapper_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                secret_path = Path(capsule["agent_mounts"]["wrapper_secret_files"]["primary"])
                secret_value = secret_path.read_text(encoding="utf-8").strip()
                observation = {{
                    "secret_path": str(secret_path),
                    "secret_value_length": len(secret_value),
                    "ambient_openai_key": os.environ.get("OPENAI_API_KEY", "missing"),
                }}
                Path({str(agent_observation)!r}).write_text(json.dumps(observation), encoding="utf-8")
                Path("implemented.txt").write_text("wrapper secret contract\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "wrapper secret contract"], check=True)
                Path(os.environ["AFK_AGENT_RESULT_PATH"]).write_text(
                    json.dumps({{"status": "completed", "summary": "wrapper secret contract implemented"}}),
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
                        "guardrails": ["stay within checkout"],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", wrapper_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
                            "wrapper_secret_files": {"primary": str(wrapper_secret_file)},
                        },
                    }
                ),
                "--ledger",
                str(ledger),
                env_overrides={
                    "OPENAI_API_KEY": "ambient-openai-secret",
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
            observation = json.loads(agent_observation.read_text(encoding="utf-8"))
            job_capsule = json.loads((run_dir / "job-capsule.json").read_text(encoding="utf-8"))
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "wrapper secret contract implemented")
            self.assertEqual(observation["secret_path"], str(wrapper_secret_file))
            self.assertEqual(observation["secret_value_length"], len(wrapper_secret))
            self.assertEqual(observation["ambient_openai_key"], "missing")
            self.assertEqual(
                job_capsule["capsule"]["agent_mounts"]["wrapper_secret_files"],
                {"primary": str(wrapper_secret_file)},
            )
            self.assertIn(str(wrapper_secret_file), artifact_text)
            self.assertNotIn(wrapper_secret, artifact_text)
            self.assertNotIn("ambient-openai-secret", artifact_text)

    def test_implement_runs_real_agent_with_secret_refs_in_job_capsule_without_resolving_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            secret_refs = {
                "primary": {
                    "secretRef": {
                        "provider": "runner-local-files",
                        "name": "codex-auth",
                        "key": "openai_api_key",
                    }
                },
                "secondary": {
                    "secretRef": {
                        "provider": "runner-local-files",
                        "name": "pi-session",
                        "key": "refresh_token",
                    }
                },
            }
            agent_observation = temp_path / "agent-observation.json"
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                observation = {{
                    "secret_refs": capsule["agent_mounts"]["secret_refs"],
                    "ambient_openai_key": os.environ.get("OPENAI_API_KEY", "missing"),
                }}
                Path({str(agent_observation)!r}).write_text(json.dumps(observation), encoding="utf-8")
                Path("implemented.txt").write_text("secret refs contract\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "secret refs contract"], check=True)
                Path(os.environ["AFK_AGENT_RESULT_PATH"]).write_text(
                    json.dumps({{"status": "completed", "summary": "secret refs contract implemented"}}),
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
                        "guardrails": ["stay within checkout"],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
                            "secret_refs": secret_refs,
                        },
                    }
                ),
                "--ledger",
                str(ledger),
                env_overrides={
                    "OPENAI_API_KEY": "ambient-openai-secret",
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
            observation = json.loads(agent_observation.read_text(encoding="utf-8"))
            job_capsule = json.loads((run_dir / "job-capsule.json").read_text(encoding="utf-8"))
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "secret refs contract implemented")
            self.assertEqual(observation["secret_refs"], secret_refs)
            self.assertEqual(observation["ambient_openai_key"], "missing")
            self.assertEqual(job_capsule["capsule"]["agent_mounts"]["secret_refs"], secret_refs)
            self.assertNotIn("ambient-openai-secret", artifact_text)

    def test_implement_rejects_invalid_secret_ref_shapes_and_plaintext_fields(self):
        cases = [
            (
                "secret_refs_not_object",
                [],
                "agent.secret_refs must be an object",
            ),
            (
                "logical_name_secret_like",
                {"openai_token": {"secretRef": {"provider": "vault", "name": "codex-auth", "key": "primary"}}},
                "agent.secret_refs.openai_token must use a non-secret logical name",
            ),
            (
                "entry_not_object",
                {"primary": "vault://codex-auth/primary"},
                "agent.secret_refs.primary must be an object containing only secretRef",
            ),
            (
                "plaintext_value_field",
                {
                    "primary": {
                        "secretRef": {"provider": "vault", "name": "codex-auth", "key": "primary"},
                        "value": "ghp_secret_value_1234567890",
                    }
                },
                "agent.secret_refs.primary must not include plaintext secret fields",
            ),
            (
                "secret_ref_not_object",
                {"primary": {"secretRef": "vault://codex-auth/primary"}},
                "agent.secret_refs.primary.secretRef must be an object",
            ),
            (
                "secret_ref_missing_key",
                {"primary": {"secretRef": {"provider": "vault", "name": "codex-auth"}}},
                "agent.secret_refs.primary.secretRef.key is required",
            ),
            (
                "secret_ref_non_string_field",
                {"primary": {"secretRef": {"provider": "vault", "name": "codex-auth", "key": 7}}},
                "agent.secret_refs.primary.secretRef.key must be a non-empty string",
            ),
            (
                "secret_ref_extra_field",
                {
                    "primary": {
                        "secretRef": {
                            "provider": "vault",
                            "name": "codex-auth",
                            "key": "primary",
                            "value": "ghp_secret_value_1234567890",
                        }
                    }
                },
                "agent.secret_refs.primary.secretRef must only contain provider, name, and key",
            ),
            (
                "secret_ref_secret_like_provider",
                {"primary": {"secretRef": {"provider": "ghp_secret_provider_1234567890", "name": "codex-auth", "key": "primary"}}},
                "agent.secret_refs.primary.secretRef.provider must not include a secret-looking value",
            ),
            (
                "secret_ref_secret_like_name",
                {"primary": {"secretRef": {"provider": "vault", "name": "github_pat_secret_name_1234567890", "key": "primary"}}},
                "agent.secret_refs.primary.secretRef.name must not include a secret-looking value",
            ),
            (
                "secret_ref_secret_like_key",
                {"primary": {"secretRef": {"provider": "vault", "name": "codex-auth", "key": "ghp_secret_key_1234567890"}}},
                "agent.secret_refs.primary.secretRef.key must not include a secret-looking value",
            ),
        ]
        for case_name, secret_refs, expected_message in cases:
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    checkout = temp_path / "checkout"
                    start_commit = init_checkout(checkout)
                    ledger = temp_path / "ledger"
                    codex_home = temp_path / "codex-home"
                    config_home = temp_path / "xdg-config"
                    pi_config_home = temp_path / "pi-config"
                    codex_home.mkdir()
                    config_home.mkdir()
                    pi_config_home.mkdir()

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
                                    "type": "real-agent-command",
                                    "command": [sys.executable, "-c", "print('should not run')"],
                                    "result_path": "agent-result.json",
                                    "codex_home": str(codex_home),
                                    "config_home": str(config_home),
                                    "env": {"PI_CONFIG_HOME": str(pi_config_home)},
                                    "secret_refs": secret_refs,
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
                    self.assertEqual(result["output"]["message"], expected_message)
                    self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))
                    self.assertNotIn("ghp_secret_value_1234567890", artifact_text)

    def test_implement_redacts_runtime_wrapper_secret_leaks_from_logs_and_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            wrapper_secret_dir = temp_path / "runner-secrets"
            wrapper_secret_file = wrapper_secret_dir / "plain-secret.txt"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            wrapper_secret_dir.mkdir()
            wrapper_secret = "plain-runtime-wrapper-secret"
            wrapper_secret_file.write_text(wrapper_secret + "\n", encoding="utf-8")
            wrapper_code = textwrap.dedent(
                """
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                secret_path = Path(capsule["agent_mounts"]["wrapper_secret_files"]["primary"])
                secret_value = secret_path.read_text(encoding="utf-8").strip()
                Path("implemented.txt").write_text("wrapper secret redaction\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "wrapper secret redaction"], check=True)
                Path(os.environ["AFK_AGENT_RESULT_PATH"]).write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "summary": secret_value,
                            "notes": [f"stdout copy {secret_value}"],
                            "details": {"artifact_text": f"artifact {secret_value} leak"},
                        }
                    ),
                    encoding="utf-8",
                )
                print(f"stdout {secret_value}")
                print(f"stderr {secret_value}", file=sys.stderr)
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
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "real-agent-command",
                            "command": [
                                sys.executable,
                                "-c",
                                "import sys\n" + wrapper_code,
                            ],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
                            "wrapper_secret_files": {"primary": str(wrapper_secret_file)},
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
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "[REDACTED]")
            self.assertEqual(result["output"]["agent_result"]["summary"], "[REDACTED]")
            self.assertIn("[REDACTED]", result["output"]["agent_result"]["notes"][0])
            self.assertEqual(agent_result["result"]["summary"], "[REDACTED]")
            self.assertEqual(
                job_capsule["capsule"]["agent_mounts"]["wrapper_secret_files"],
                {"primary": str(wrapper_secret_file)},
            )
            self.assertIn("[REDACTED]", (run_dir / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("[REDACTED]", (run_dir / "stderr.log").read_text(encoding="utf-8"))
            self.assertNotIn(wrapper_secret, artifact_text)

    def test_implement_fails_protocol_when_wrapper_secret_file_cannot_be_read_at_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            wrapper_secret_dir = temp_path / "runner-secrets"
            wrapper_secret_file = wrapper_secret_dir / "plain-secret.txt"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            wrapper_secret_dir.mkdir()
            wrapper_secret_file.write_text("plain-runtime-wrapper-secret\n", encoding="utf-8")
            wrapper_secret_file.chmod(0)

            try:
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
                            "validation": {"profile": "tier1", "commands": []},
                            "agent": {
                                "type": "real-agent-command",
                                "command": [
                                    sys.executable,
                                    "-c",
                                    "print('should not run')",
                                ],
                                "result_path": "agent-result.json",
                                "timeout_seconds": 10,
                                "codex_home": str(codex_home),
                                "config_home": str(config_home),
                                "env": {"PI_CONFIG_HOME": str(pi_config_home)},
                                "wrapper_secret_files": {"primary": str(wrapper_secret_file)},
                            },
                        }
                    ),
                    "--ledger",
                    str(ledger),
                )
            finally:
                wrapper_secret_file.chmod(0o600)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertIn("agent.wrapper_secret_files.primary", result["output"]["summary"])
            self.assertIn("could not be read", result["output"]["summary"])
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_fails_protocol_when_wrapper_secret_file_is_not_utf8_at_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            pi_config_home = temp_path / "pi-config"
            config_home = temp_path / "xdg-config-explicit"
            wrapper_secret_dir = temp_path / "runner-secrets"
            wrapper_secret_file = wrapper_secret_dir / "plain-secret.txt"
            codex_home.mkdir()
            pi_config_home.mkdir()
            config_home.mkdir()
            wrapper_secret_dir.mkdir()
            wrapper_secret_file.write_bytes(b"\xff\xfe\x00\x81")

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
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "real-agent-command",
                            "command": [
                                sys.executable,
                                "-c",
                                "print('should not run')",
                            ],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
                            "wrapper_secret_files": {"primary": str(wrapper_secret_file)},
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
            self.assertEqual(result["output"]["classification"], "protocol_failure")
            self.assertIn("agent.wrapper_secret_files.primary", result["output"]["summary"])
            self.assertIn("could not be read", result["output"]["summary"])
            self.assertIn(str(wrapper_secret_file), result["output"]["summary"])
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_command_success_without_new_commit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import json
                from pathlib import Path

                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "reported success without commit"}),
                    encoding="utf-8",
                )
                print("agent reported success")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            self.assertEqual(
                result["output"]["summary"],
                "agent reported success but produced no new commit",
            )
            self.assertEqual(agent_result["result"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["git"]["before_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["after_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["commits"], [])
            self.assertIn("agent reported success", agent_result["result"]["evidence"]["stdout_excerpt"])

    def test_implement_rejects_real_agent_command_success_when_post_run_git_metadata_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import json
                from pathlib import Path

                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "reported success before metadata failed"}),
                    encoding="utf-8",
                )
                Path(".git").rename(".git-broken")
                print("agent reported success before metadata failed")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            self.assertEqual(
                result["output"]["summary"],
                "agent reported success but post-run git metadata could not be verified",
            )
            self.assertEqual(agent_result["result"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["git"]["metadata_status"], "failed")
            self.assertEqual(result["output"]["git"]["before_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["after_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["commits"], [])
            self.assertIn(
                "agent reported success before metadata failed",
                agent_result["result"]["evidence"]["stdout_excerpt"],
            )

    def test_implement_rejects_real_agent_command_auth_config_paths_inside_checkout_or_relative(self):
        cases = [
            ("codex_home", "codex-cache", "agent.codex_home must be absolute"),
            ("config_home", "xdg-config", "agent.config_home must be absolute"),
            ("codex_home", "checkout", "agent.codex_home must be outside checkout"),
            ("config_home", "checkout/config", "agent.config_home must be outside checkout"),
            ("codex_home", "missing-codex-home", "agent.codex_home must be an existing directory"),
            ("config_home", "missing-xdg-config", "agent.config_home must be an existing directory"),
        ]
        for field, path_kind, expected_message in cases:
            with self.subTest(field=field, path_kind=path_kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    checkout = temp_path / "checkout"
                    start_commit = init_checkout(checkout)
                    ledger = temp_path / "ledger"
                    outside_codex_home = temp_path / "codex-home"
                    outside_config_home = temp_path / "xdg-config"
                    outside_codex_home.mkdir()
                    outside_config_home.mkdir()
                    checkout_config = checkout / "config"
                    checkout_config.mkdir()
                    path_value = {
                        "codex-cache": "codex-cache",
                        "xdg-config": "xdg-config",
                        "checkout": str(checkout),
                        "checkout/config": str(checkout_config),
                        "missing-codex-home": str(temp_path / "missing-codex-home"),
                        "missing-xdg-config": str(temp_path / "missing-xdg-config"),
                    }[path_kind]
                    agent = {
                        "type": "real-agent-command",
                        "command": [sys.executable, "-c", "print('should not run')"],
                        "result_path": "agent-result.json",
                        "codex_home": str(outside_codex_home),
                        "config_home": str(outside_config_home),
                        "env": {"PI_CONFIG_HOME": str(outside_config_home)},
                    }
                    agent[field] = path_value

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
                                "agent": agent,
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
                    self.assertEqual(result["output"]["message"], expected_message)
                    self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_wrapper_secret_file_paths_inside_checkout_or_relative(self):
        cases = [
            ("relative-secret.txt", "agent.wrapper_secret_files.primary must be an absolute file path outside checkout"),
            ("checkout/secret.txt", "agent.wrapper_secret_files.primary must be outside checkout"),
            ("missing-secret.txt", "agent.wrapper_secret_files.primary must be an existing file"),
        ]
        for path_kind, expected_message in cases:
            with self.subTest(path_kind=path_kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    checkout = temp_path / "checkout"
                    start_commit = init_checkout(checkout)
                    ledger = temp_path / "ledger"
                    outside_codex_home = temp_path / "codex-home"
                    outside_config_home = temp_path / "xdg-config"
                    outside_pi_config = temp_path / "pi-config"
                    outside_wrapper_secret = temp_path / "runner-secrets" / "openai.txt"
                    outside_codex_home.mkdir()
                    outside_config_home.mkdir()
                    outside_pi_config.mkdir()
                    outside_wrapper_secret.parent.mkdir()
                    outside_wrapper_secret.write_text("secret\n", encoding="utf-8")
                    checkout_secret = checkout / "secret.txt"
                    checkout_secret.write_text("secret\n", encoding="utf-8")
                    path_value = {
                        "relative-secret.txt": "relative-secret.txt",
                        "checkout/secret.txt": str(checkout_secret),
                        "missing-secret.txt": str(temp_path / "missing-secret.txt"),
                    }[path_kind]

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
                                    "type": "real-agent-command",
                                    "command": [sys.executable, "-c", "print('should not run')"],
                                    "result_path": "agent-result.json",
                                    "codex_home": str(outside_codex_home),
                                    "config_home": str(outside_config_home),
                                    "env": {"PI_CONFIG_HOME": str(outside_pi_config)},
                                    "wrapper_secret_files": {"primary": path_value},
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
                    self.assertEqual(result["output"]["message"], expected_message)
                    self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_wrapper_secret_file_secret_logical_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            outside_codex_home = temp_path / "codex-home"
            outside_config_home = temp_path / "xdg-config"
            outside_pi_config = temp_path / "pi-config"
            outside_wrapper_secret = temp_path / "runner-secrets" / "openai.txt"
            outside_codex_home.mkdir()
            outside_config_home.mkdir()
            outside_pi_config.mkdir()
            outside_wrapper_secret.parent.mkdir()
            outside_wrapper_secret.write_text("secret\n", encoding="utf-8")

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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", "print('should not run')"],
                            "result_path": "agent-result.json",
                            "codex_home": str(outside_codex_home),
                            "config_home": str(outside_config_home),
                            "env": {"PI_CONFIG_HOME": str(outside_pi_config)},
                            "wrapper_secret_files": {"openai_token": str(outside_wrapper_secret)},
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
                "agent.wrapper_secret_files.openai_token must use a non-secret logical name",
            )
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_command_missing_remote_auth_mount_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            outside_codex_home = temp_path / "codex-home"
            outside_config_home = temp_path / "xdg-config"
            outside_codex_home.mkdir()
            outside_config_home.mkdir()
            outside_pi_config = temp_path / "pi-config-missing"
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
                                "type": "real-agent-command",
                                "command": [sys.executable, "-c", "print('should not run')"],
                                "result_path": "agent-result.json",
                                "codex_home": str(outside_codex_home),
                                "config_home": str(outside_config_home),
                                "env": {"PI_CONFIG_HOME": str(outside_pi_config)},
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
                "agent.env.PI_CONFIG_HOME must be an existing directory",
            )
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_command_missing_required_auth_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            base_agent = {
                "type": "real-agent-command",
                "command": [sys.executable, "-c", "print('should not run')"],
                "result_path": "agent-result.json",
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {"PI_CONFIG_HOME": str(pi_config_home)},
            }
            missing_cases = [
                ("codex_home", "agent.codex_home is required", {"codex_home": None}),
                ("config_home", "agent.config_home is required", {"config_home": None}),
                ("PI_CONFIG_HOME", "agent.env must include PI_CONFIG_HOME", {"env": {}}),
            ]
            for field, expected_message, mutation in missing_cases:
                with self.subTest(field=field):
                    agent = dict(base_agent)
                    if "codex_home" in mutation and mutation["codex_home"] is None:
                        agent.pop("codex_home")
                    if "config_home" in mutation and mutation["config_home"] is None:
                        agent.pop("config_home")
                    if "env" in mutation:
                        agent["env"] = mutation["env"]

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
                                "agent": agent,
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
                    self.assertEqual(result["output"]["message"], expected_message)
                    self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_command_checkout_internal_config_env_paths(self):
        cases = [
            ("PI_CONFIG_HOME", "checkout/.pi-config", "agent.env.PI_CONFIG_HOME must be outside checkout"),
            ("PI_CACHE_DIR", "checkout/.pi-cache", "agent.env.PI_CACHE_DIR must be outside checkout"),
            ("PI_SESSION_PATH", "checkout/session.json", "agent.env.PI_SESSION_PATH must be outside checkout"),
            ("PI_CONFIG_HOME", "relative/.pi-config", "agent.env.PI_CONFIG_HOME must be an absolute path outside checkout"),
        ]
        for key, path_kind, expected_message in cases:
            with self.subTest(key=key, path_kind=path_kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    checkout = temp_path / "checkout"
                    start_commit = init_checkout(checkout)
                    ledger = temp_path / "ledger"
                    required_pi_config_home = temp_path / "required-pi-config"
                    required_codex_home = temp_path / "codex-home"
                    required_config_home = temp_path / "xdg-config"
                    checkout_config = checkout / ".pi-config"
                    checkout_cache = checkout / ".pi-cache"
                    checkout_config.mkdir()
                    checkout_cache.mkdir()
                    required_pi_config_home.mkdir()
                    required_codex_home.mkdir()
                    required_config_home.mkdir()
                    path_value = {
                        "checkout/.pi-config": str(checkout_config),
                        "checkout/.pi-cache": str(checkout_cache),
                        "checkout/session.json": str(checkout / "session.json"),
                        "relative/.pi-config": ".pi-config",
                    }[path_kind]

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
                                    "type": "real-agent-command",
                                    "command": [sys.executable, "-c", "print('should not run')"],
                                    "result_path": "agent-result.json",
                                    "codex_home": str(required_codex_home),
                                    "config_home": str(required_config_home),
                                    "env": {
                                        "PI_CONFIG_HOME": str(required_pi_config_home),
                                        key: path_value,
                                    },
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
                    self.assertEqual(result["output"]["message"], expected_message)
                    self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_validates_pi_coding_agent_dir_as_external_existing_directory(self):
        invalid_cases = [
            ("relative", ".pi-coding-agent", "agent.env.PI_CODING_AGENT_DIR must be an absolute path outside checkout"),
            ("missing", "missing-pi-coding-agent", "agent.env.PI_CODING_AGENT_DIR must be an existing directory"),
            ("inside_checkout", "checkout/.pi-coding-agent", "agent.env.PI_CODING_AGENT_DIR must be outside checkout"),
        ]
        for case_name, path_kind, expected_message in invalid_cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    checkout = temp_path / "checkout"
                    start_commit = init_checkout(checkout)
                    ledger = temp_path / "ledger"
                    codex_home = temp_path / "codex-home"
                    config_home = temp_path / "xdg-config"
                    pi_config_home = temp_path / "pi-config"
                    checkout_pi_coding_agent_dir = checkout / ".pi-coding-agent"
                    codex_home.mkdir()
                    config_home.mkdir()
                    pi_config_home.mkdir()
                    checkout_pi_coding_agent_dir.mkdir()
                    pi_coding_agent_dir = {
                        "relative": ".pi-coding-agent",
                        "missing": str(temp_path / "missing-pi-coding-agent"),
                        "inside_checkout": str(checkout_pi_coding_agent_dir),
                    }[case_name]

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
                                    "type": "real-agent-command",
                                    "command": [sys.executable, "-c", "print('should not run')"],
                                    "result_path": "agent-result.json",
                                    "codex_home": str(codex_home),
                                    "config_home": str(config_home),
                                    "env": {
                                        "PI_CONFIG_HOME": str(pi_config_home),
                                        "PI_CODING_AGENT_DIR": pi_coding_agent_dir,
                                    },
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
                    self.assertEqual(result["output"]["message"], expected_message)
                    self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            observation_path = temp_path / "agent-observation.json"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                Path({str(observation_path)!r}).write_text(
                    json.dumps({{"pi_coding_agent_dir": os.environ.get("PI_CODING_AGENT_DIR")}}),
                    encoding="utf-8",
                )
                Path("pi-coding-agent-dir.txt").write_text("accepted\\n", encoding="utf-8")
                subprocess.run(["git", "add", "pi-coding-agent-dir.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "accept pi coding agent dir"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({{"status": "completed", "summary": "accepted pi coding agent dir"}}),
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {
                                "PI_CONFIG_HOME": str(pi_config_home),
                                "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                            },
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
            observation = json.loads(observation_path.read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(observation["pi_coding_agent_dir"], str(pi_coding_agent_dir))

    def test_implement_rejects_real_agent_command_required_auth_mount_url_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            codex_home.mkdir()
            config_home.mkdir()

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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", "print('should not run')"],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": "https://example.invalid/pi-config"},
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
                "agent.env.PI_CONFIG_HOME must be an absolute path outside checkout",
            )
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_allows_non_required_config_like_env_path_without_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            required_pi_config = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            required_pi_config.mkdir()
            missing_optional_config = temp_path / "missing-config-like"
            agent_code = textwrap.dedent(
                """
                import json
                from pathlib import Path

                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "noop"}),
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {
                                "PI_CONFIG_HOME": str(required_pi_config),
                                "FOO_CONFIG_HOME": str(missing_optional_config),
                            },
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
                "agent reported success but produced no new commit",
            )
            self.assertNotIn(
                "agent.env.FOO_CONFIG_HOME must be an existing directory",
                json.dumps(result),
            )
            self.assertNotIn(
                "agent.env must include PI_CONFIG_HOME",
                json.dumps(result),
            )
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_command_secret_env_without_exposing_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            secret = "recipe-embedded-secret"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", "print('should not run')"],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {
                                "PI_CONFIG_HOME": str(pi_config_home),
                                "OPENAI_API_KEY": secret,
                            },
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
            self.assertEqual(
                result["output"]["message"],
                "agent.env must not include secret variable OPENAI_API_KEY",
            )
            self.assertNotIn(secret, artifact_text)
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_rejects_real_agent_command_secret_shaped_env_value_without_exposing_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            secret = "ghp_secretshapedvalue1234567890"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", "print('should not run')"],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {
                                "PI_CONFIG_HOME": str(pi_config_home),
                                "PIPELINE_LABEL": secret,
                            },
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
            self.assertEqual(
                result["output"]["message"],
                "agent.env.PIPELINE_LABEL must not include a secret-looking value",
            )
            self.assertNotIn(secret, artifact_text)
            self.assertNotIn("should not run", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_implement_allows_real_agent_command_safe_url_env_value_with_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            service_url = "https://example.invalid/api?mode=test"
            observation_path = temp_path / "agent-observation.json"
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                Path({str(observation_path)!r}).write_text(
                    json.dumps({{"service_url": os.environ.get("SERVICE_URL")}}),
                    encoding="utf-8",
                )
                Path("safe-url-env.txt").write_text(os.environ.get("SERVICE_URL", "") + "\\n", encoding="utf-8")
                subprocess.run(["git", "add", "safe-url-env.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "safe url env accepted"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({{"status": "completed", "summary": "safe url env accepted"}}),
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {
                                "PI_CONFIG_HOME": str(pi_config_home),
                                "SERVICE_URL": service_url,
                            },
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
            self.assertEqual(result["output"]["summary"], "safe url env accepted")
            self.assertEqual(result["output"]["git"]["changed_files"], ["safe-url-env.txt"])
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            self.assertEqual(observation["service_url"], service_url)

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

    def test_implement_records_pi_auth_failure_as_runtime_evidence_for_real_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import sys
                print("Replaying remote auth state", file=sys.stderr)
                print("OAuth refresh failed: expired OAuth credential for openai-codex", file=sys.stderr)
                print("Error: No API key for provider: openai-codex", file=sys.stderr)
                sys.exit(1)
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            self.assertEqual(
                result["output"]["summary"],
                "OAuth refresh failed: expired OAuth credential for openai-codex",
            )
            self.assertEqual(
                agent_result["result"]["summary"],
                "OAuth refresh failed: expired OAuth credential for openai-codex",
            )
            self.assertEqual(agent_result["result"]["adapter"]["type"], "real-agent-command")
            self.assertEqual(agent_result["result"]["adapter"]["returncode"], 1)
            self.assertIn(
                "OAuth refresh failed: expired OAuth credential for openai-codex",
                agent_result["result"]["evidence"]["stderr_excerpt"],
            )
            self.assertIn(
                "Error: No API key for provider: openai-codex",
                agent_result["result"]["evidence"]["stderr_excerpt"],
            )
            self.assertNotIn("access_token=", artifact_text)
            self.assertNotIn("refresh_token=", artifact_text)
            self.assertEqual(result["output"]["git"]["before_commit"], start_commit)
            self.assertEqual(result["output"]["git"]["after_commit"], start_commit)

    def test_implement_prefers_auth_error_line_over_remote_auth_preamble(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import sys
                print("Replaying remote auth state", file=sys.stderr)
                print("loading auth mount", file=sys.stderr)
                print("No API key for provider: openai-codex", file=sys.stderr)
                sys.exit(1)
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["summary"], "No API key for provider: openai-codex")

    def test_implement_records_configured_timeout_and_elapsed_time_for_adapter_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            timeout_seconds = 0.2
            agent_code = textwrap.dedent(
                """
                import time
                print("starting adapter", flush=True)
                time.sleep(5)
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
                            "timeout_seconds": timeout_seconds,
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

            self.assertEqual(result["output"]["status"], "failed_runtime")
            self.assertEqual(result["output"]["classification"], "runtime_failure")
            self.assertEqual(agent_result["result"]["summary"], "agent command timed out")
            self.assertEqual(agent_result["result"]["adapter"]["configured_timeout_seconds"], timeout_seconds)
            self.assertGreaterEqual(agent_result["result"]["adapter"]["elapsed_seconds"], timeout_seconds)
            self.assertTrue(agent_result["result"]["adapter"]["timed_out"])
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

    def test_implement_recovers_missing_real_agent_result_when_stdout_and_clean_commit_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("implemented from stdout\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement from stdout"], check=True)
                print("Implemented in commit.")
                print("Validation: smoke passed.")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["classification"], "success")
            self.assertEqual(result["output"]["summary"], "Implemented in commit.")
            self.assertEqual(result["output"]["git"]["dirty"], False)
            self.assertEqual(result["output"]["git"]["changed_files"], ["implemented.txt"])
            self.assertEqual(agent_result["result"]["status"], "implemented")
            self.assertIn("agent-result.json missing", agent_result["result"]["notes"][0])
            self.assertIn("Validation: smoke passed.", agent_result["result"]["evidence"]["stdout_excerpt"])

    def test_implement_does_not_recover_missing_real_agent_result_with_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = "from pathlib import Path; Path('dirty.txt').write_text('dirty\\n', encoding='utf-8'); print('Implemented')"

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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            self.assertEqual(result["output"]["summary"], "agent result file was not produced")
            self.assertEqual(result["output"]["git"]["dirty"], True)
            self.assertEqual(result["output"]["git"]["commits"], [])
            self.assertEqual(agent_result["result"]["failures"][0]["message"], "agent result file was not produced")

    def test_implement_does_not_recover_missing_real_agent_result_without_success_signal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("ambiguous output\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "ambiguous missing result"], check=True)
                print("ERROR: something went wrong")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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
            agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["summary"], "agent result file was not produced")
            self.assertEqual(result["output"]["git"]["dirty"], False)
            self.assertEqual(result["output"]["git"]["changed_files"], ["implemented.txt"])
            self.assertIn("ERROR: something went wrong", agent_result["result"]["evidence"]["stdout_excerpt"])

    def test_implement_recovered_stdout_summary_preserves_bracketed_human_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("bracketed summary\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "bracketed summary"], check=True)
                print("[done] all good")
                print("Implemented.")
                print("Validation: smoke passed.")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "[done] all good")

    def test_implement_does_not_recover_missing_real_agent_result_with_negated_success_word(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("negated output\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "negated output"], check=True)
                print("not implemented yet")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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

            self.assertEqual(result["output"]["status"], "failed_protocol")
            self.assertEqual(result["output"]["summary"], "agent result file was not produced")

    def test_implement_recovers_missing_real_agent_result_with_successful_test_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("test summary\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "test summary"], check=True)
                print("Implemented.")
                print("12 passed, 0 failed")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "Implemented.")

    def test_implement_recovers_missing_real_agent_result_with_common_done_signal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("done signal\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "done signal"], check=True)
                print("Done.")
                print("All changes committed.")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "Done.")

    def test_implement_recovered_stdout_summary_skips_git_status_chatter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "config-home"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            agent_code = textwrap.dedent(
                """
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("git chatter\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "git chatter"], check=True)
                print("On branch afk/test-work")
                print("Your branch is up to date with 'origin/main'.")
                print("Implemented.")
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
                            "type": "real-agent-command",
                            "command": [sys.executable, "-c", agent_code],
                            "result_path": "agent-result.json",
                            "timeout_seconds": 10,
                            "codex_home": str(codex_home),
                            "config_home": str(config_home),
                            "env": {"PI_CONFIG_HOME": str(pi_config_home)},
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

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["summary"], "Implemented.")

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

    def test_implement_rejects_wrapper_secret_files_for_fake_agent_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            wrapper_secret = temp_path / "wrapper-secret.txt"
            wrapper_secret.write_text("secret\n", encoding="utf-8")

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
                            "wrapper_secret_files": {"primary": str(wrapper_secret)},
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
            self.assertEqual(result["output"]["message"], "agent.wrapper_secret_files is not supported")
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

    def test_implement_selection_scope_carries_all_selected_items_in_job_capsule_and_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            start_commit = init_checkout(checkout)
            ledger = temp_path / "ledger"
            agent_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                assert [item["external_id"] for item in capsule["work_selection"]["selected_work"]] == [
                    "central-lve.5",
                    "central-lve.6",
                ]
                Path("implemented.txt").write_text(capsule["work_item"]["external_id"], encoding="utf-8")
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "combined work"}),
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
                        "work_scope": "selection",
                        "work_selection": {
                            "schema_version": 1,
                            "selected_work": [
                                selected_work("fixture", "central-lve.5"),
                                selected_work("fixture", "central-lve.6"),
                            ],
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
            result = json.loads((ledger / "runs" / summary["run_id"] / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["status"], "implemented")
            self.assertEqual(result["output"]["work_item"]["external_id"], "central-lve.5,central-lve.6")
            self.assertEqual(
                [item["external_id"] for item in result["output"]["work_selection"]["selected_work"]],
                ["central-lve.5", "central-lve.6"],
            )

    def test_implement_rejects_unknown_work_scope(self):
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
                        "work_scope": "selected",
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
                            "command": [sys.executable, "-c", "raise SystemExit('agent should not run')"],
                            "result_path": "agent-result.json",
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / "runs" / summary["run_id"] / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(result["output"]["status"], "failed_invalid_payload")
            self.assertEqual(result["output"]["message"], "work_scope must be item or selection")

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
