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
            "GIT_ALLOW_PROTOCOL": "file",
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


def init_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "--initial-branch", "main")
    git(path, "config", "user.name", "AFK Test")
    git(path, "config", "user.email", "afk-test@example.test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "seed")


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def selected_fixture_item():
    return {
        "external_id": "central-lve.9",
        "url": "https://tracker.example/central-lve.9",
        "title": "Compose workstream recipe and terminal PR publisher",
        "status": "open",
        "labels": ["project:afk-composable-pipeline", "afk:ready"],
        "parent": "central-lve",
        "workstream": "central-lve",
        "acceptance_criteria": ["One terminal PR is created after validation and review pass."],
        "dependencies": [{"id": "central-lve.8", "status": "closed"}],
        "blockers": [],
        "afk": {"ready": True},
    }


def successful_recipe(temp_path, repo, checkout, fake_git, fake_gh):
    review_branch = "afk/workstream-terminal-pr"
    agent_code = textwrap.dedent(
        """
        import json
        import subprocess
        from pathlib import Path

        Path("implemented.txt").write_text("central-lve.9\\n", encoding="utf-8")
        subprocess.run(["git", "add", "implemented.txt"], check=True)
        subprocess.run(["git", "commit", "-m", "implement central-lve.9"], check=True)
        Path("agent-result.json").write_text(
            json.dumps({"status": "completed", "summary": "implemented workstream publisher"}),
            encoding="utf-8",
        )
        """
    ).strip()
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
                    "steps": [{"name": "unit", "status": "pass"}],
                }
            ),
            encoding="utf-8",
        )
        """
    ).strip()
    reviewer_code = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path

        request = json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
        assert request["evidence_pack"]["validation"]["required"][0]["status"] == "validated"
        Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
            json.dumps({"status": "pass", "summary": "ready for PR", "findings": []}),
            encoding="utf-8",
        )
        """
    ).strip()
    return {
        "schema_version": 1,
        "workstream_id": "central-lve.9",
        "parent": "central-lve",
        "review_branch": review_branch,
        "steps": [
            {
                "name": "select-work",
                "input": {
                    "required_labels": ["afk:ready"],
                    "sources": [{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}],
                },
            },
            {
                "name": "prepare-checkout",
                "input": {
                    "repo_url": str(repo),
                    "base_ref": "main",
                    "checkout_root": str(temp_path),
                    "checkout_path": str(checkout),
                },
            },
            {
                "name": "implement",
                "input": {
                    "guardrails": ["stay within checkout"],
                    "validation": {"profile": "tier1", "commands": []},
                    "agent": {
                        "type": "fake-pi-command",
                        "command": [sys.executable, "-c", agent_code],
                        "result_path": "agent-result.json",
                    },
                },
            },
            {
                "name": "validate",
                "profile": "tier1",
                "input": {
                    "validation": {"dry_run": True, "timeout_seconds": 30},
                    "worker": {
                        "type": "local-command",
                        "command": [sys.executable, "-c", worker_code],
                        "timeout_seconds": 10,
                    },
                },
            },
            {
                "name": "review",
                "input": {
                    "guardrails": [{"name": "no secrets", "status": "pass"}],
                    "cleanup": {"status": "clean", "resources": []},
                    "reviewer": {
                        "type": "fake-reviewer-command",
                        "command": [sys.executable, "-c", reviewer_code],
                        "timeout_seconds": 10,
                    },
                },
            },
        ],
        "publisher": {
            "enabled": True,
            "mode": "create",
            "git": {"path": str(fake_git), "push": True, "remote": "origin"},
            "gh": {"path": str(fake_gh)},
            "repo": "thunderbump/afk-composable-pipeline",
            "base": "afk/central-lve-8-final-review",
            "head": review_branch,
            "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
        },
    }


class WorkstreamCliTest(unittest.TestCase):
    def test_workstream_composes_steps_and_creates_one_terminal_pr_from_ledger_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "git",
            "cwd": os.getcwd(),
            "argv": sys.argv[1:],
            "publisher_secret": os.environ.get("AFK_PUBLISHER_SECRET", ""),
        }}
    )
    + "\\n"
)
sys.exit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

body_file = sys.argv[sys.argv.index("--body-file") + 1]
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "gh",
            "argv": sys.argv[1:],
            "body": Path(body_file).read_text(encoding="utf-8"),
            "publisher_secret": os.environ.get("AFK_PUBLISHER_SECRET", ""),
        }}
    )
    + "\\n"
)
print("https://github.example/pr/123")
sys.exit(0)
""",
            )

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                    "AFK_PUBLISHER_SECRET": "publisher-secret-should-not-leak",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["command"], "run-workstream")
            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["status"], "published")
            self.assertEqual(result["workstream_id"], "central-lve.9")
            self.assertEqual(result["parent"], "central-lve")
            self.assertEqual(result["review_branch"], "afk/workstream-terminal-pr")
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "review"],
            )
            implemented_head = git(checkout, "rev-parse", "HEAD")
            self.assertNotEqual(implemented_head, git(repo, "rev-parse", "HEAD"))
            validate_step = next(step for step in result["steps"] if step["name"] == "validate")
            worker_request = json.loads(
                (ledger / "runs" / validate_step["run_id"] / "worker-request.json").read_text(encoding="utf-8")
            )
            validate_result = json.loads(
                (ledger / "runs" / validate_step["run_id"] / "step-result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(worker_request["repo"]["commit"], implemented_head)
            self.assertEqual(validate_result["output"]["checkout"]["requested_ref"], implemented_head)
            self.assertTrue(
                all(step["equivalent_command"][0:3] == ["afk", "run-step", step["name"]] for step in result["steps"])
            )
            self.assertEqual(
                result["selected_work"],
                [
                    {
                        "external_id": "central-lve.9",
                        "title": "Compose workstream recipe and terminal PR publisher",
                        "source_id": "fixture",
                        "source_type": "fixture",
                        "result": "passed",
                    }
                ],
            )
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(result["publication"]["mode"], "create")
            self.assertEqual(result["publication"]["url"], "https://github.example/pr/123")

            git_calls = [call for call in calls if call["tool"] == "git"]
            gh_calls = [call for call in calls if call["tool"] == "gh"]
            self.assertEqual(len(git_calls), 1)
            self.assertEqual(len(gh_calls), 1)
            self.assertEqual(git_calls[0]["publisher_secret"], "")
            self.assertEqual(gh_calls[0]["publisher_secret"], "")
            self.assertEqual(git_calls[0]["cwd"], str(checkout))
            self.assertEqual(
                git_calls[0]["argv"],
                ["push", "origin", "HEAD:refs/heads/afk/workstream-terminal-pr"],
            )
            self.assertEqual(gh_calls[0]["argv"][0:3], ["pr", "create", "--repo"])
            body = gh_calls[0]["body"]
            self.assertIn("Workstream: central-lve.9", body)
            self.assertIn("Parent: central-lve", body)
            self.assertIn("central-lve.9 - Compose workstream recipe and terminal PR publisher", body)
            self.assertIn("Changed files", body)
            self.assertIn("implemented.txt", body)
            self.assertIn("Validation", body)
            self.assertIn("tier1: validated", body)
            self.assertIn("Review: passed", body)
            self.assertIn("Artifacts", body)
            self.assertIn(result["steps"][-1]["result_path"], body)

    def test_workstream_review_uses_final_validation_artifacts_over_recipe_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
sys.exit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

body_file = sys.argv[sys.argv.index("--body-file") + 1]
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "gh",
            "argv": sys.argv[1:],
            "body": Path(body_file).read_text(encoding="utf-8"),
        }}
    )
    + "\\n"
)
print("https://github.example/pr/123")
sys.exit(0)
""",
            )
            stale_step_path = temp_path / "stale-validation-sentinel" / "step-result.json"
            stale_worker_path = temp_path / "stale-validation-sentinel" / "worker-result.json"
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][4]["input"]["validation"] = {
                "required_artifacts": [
                    {
                        "name": "stale-validation-sentinel",
                        "step_result_path": str(stale_step_path),
                        "worker_result_path": str(stale_worker_path),
                    }
                ]
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            result_text = result_path.read_text(encoding="utf-8")
            result = json.loads(result_text)
            validate_step = next(step for step in result["steps"] if step["name"] == "validate")
            review_step = next(step for step in result["steps"] if step["name"] == "review")
            reviewer_request = json.loads(
                (ledger / "runs" / review_step["run_id"] / "reviewer-request.json").read_text(encoding="utf-8")
            )
            required = reviewer_request["evidence_pack"]["validation"]["required"]

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["publication"]["status"], "published")
            self.assertNotIn("stale-validation-sentinel", result_text)
            self.assertEqual(len(required), 1)
            self.assertEqual(
                required[0]["step_result_path"],
                str((ledger / "runs" / validate_step["run_id"] / "step-result.json").resolve(strict=False)),
            )
            self.assertEqual(
                required[0]["worker_result_path"],
                str((ledger / "runs" / validate_step["run_id"] / "worker-result.json").resolve(strict=False)),
            )

    def test_workstream_later_implementation_requires_fresh_validation_before_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            reviewer_invoked = temp_path / "reviewer-invoked"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            second_agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("second-implementation.txt").write_text("later implementation\\n", encoding="utf-8")
                subprocess.run(["git", "add", "second-implementation.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "later implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "later implementation"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            reviewer_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                Path({str(reviewer_invoked)!r}).write_text("reviewer ran\\n", encoding="utf-8")
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({{"status": "pass", "summary": "stale validation accepted", "findings": []}}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"].insert(
                4,
                {
                    "name": "implement",
                    "input": {
                        "guardrails": ["stay within checkout"],
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", second_agent_code],
                            "result_path": "agent-result.json",
                        },
                    },
                },
            )
            recipe["steps"][5]["input"]["reviewer"]["command"] = [sys.executable, "-c", reviewer_code]

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("validation evidence", result["publication"]["reason"])
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "implement"],
            )
            self.assertEqual(result["selected_work"][0]["result"], "implemented")
            self.assertFalse(reviewer_invoked.exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_implement_uses_selected_work_and_prepared_checkout_over_recipe_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            alternate_checkout = temp_path / "alternate-checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            git(temp_path, "clone", str(repo), str(alternate_checkout))
            git(alternate_checkout, "checkout", "-b", "afk/foreign-work")
            alternate_start = git(alternate_checkout, "rev-parse", "HEAD")
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import os
import subprocess
import sys
from pathlib import Path

head = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=os.getcwd(),
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()
files = subprocess.run(
    ["git", "ls-tree", "-r", "--name-only", "HEAD"],
    cwd=os.getcwd(),
    text=True,
    capture_output=True,
    check=True,
).stdout.splitlines()
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "git",
            "cwd": os.getcwd(),
            "argv": sys.argv[1:],
            "head": head,
            "files": files,
        }}
    )
    + "\\n"
)
sys.exit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

body_file = sys.argv[sys.argv.index("--body-file") + 1]
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "gh",
            "argv": sys.argv[1:],
            "body": Path(body_file).read_text(encoding="utf-8"),
        }}
    )
    + "\\n"
)
print("https://github.example/pr/123")
sys.exit(0)
""",
            )
            foreign_item = {
                **selected_fixture_item(),
                "source_id": "foreign-fixture",
                "source_type": "fixture",
                "external_id": "foreign-work.1",
                "title": "Foreign work item",
            }
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][2]["input"]["work_selection"] = {
                "schema_version": 1,
                "selected_work": [foreign_item],
            }
            recipe["steps"][2]["input"]["checkout"] = {
                "status": "prepared",
                "checkout_path": str(alternate_checkout),
                "review_branch": "afk/foreign-work",
                "requested_ref": alternate_start,
                "start_commit": alternate_start,
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            implement_step = next(step for step in result["steps"] if step["name"] == "implement")
            job_capsule = json.loads(
                (ledger / "runs" / implement_step["run_id"] / "job-capsule.json").read_text(encoding="utf-8")
            )["capsule"]
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]
            git_call = next(call for call in calls if call["tool"] == "git")

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(job_capsule["work_item"]["external_id"], "central-lve.9")
            self.assertEqual(job_capsule["checkout"]["path"], str(checkout))
            self.assertNotEqual(job_capsule["checkout"]["path"], str(alternate_checkout))
            self.assertTrue((checkout / "implemented.txt").exists())
            self.assertFalse((alternate_checkout / "implemented.txt").exists())
            self.assertEqual(git_call["cwd"], str(checkout))
            self.assertIn("implemented.txt", git_call["files"])

    def test_workstream_equivalent_command_redacts_nested_command_flag_values(self):
        cases = [
            (
                "agent",
                lambda recipe: recipe["steps"][2]["input"]["agent"].__setitem__(
                    "command",
                    [sys.executable, "-c", "print('agent should not run')", "--token", "plain-secret-value"],
                ),
            ),
            (
                "worker",
                lambda recipe: recipe["steps"][3]["input"]["worker"].__setitem__(
                    "command",
                    [sys.executable, "-c", "print('worker should not run')", "--token", "plain-secret-value"],
                ),
            ),
            (
                "reviewer",
                lambda recipe: recipe["steps"][4]["input"]["reviewer"].__setitem__(
                    "command",
                    [sys.executable, "-c", "print('reviewer should not run')", "--token", "plain-secret-value"],
                ),
            ),
        ]
        for case_name, mutate_recipe in cases:
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    repo = temp_path / "repo-src"
                    checkout = temp_path / "checkout"
                    ledger = temp_path / "ledger"
                    fake_calls = temp_path / "fake-calls.jsonl"
                    init_repo(repo)
                    fake_git = temp_path / "publisher-git"
                    fake_gh = temp_path / "publisher-gh"
                    write_executable(
                        fake_git,
                        f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
                    )
                    write_executable(
                        fake_gh,
                        f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
                    )
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    mutate_recipe(recipe)

                    completed = run_afk(
                        "run-workstream",
                        "--workstream-id",
                        "central-lve.9",
                        "--input",
                        json.dumps(recipe),
                        "--ledger",
                        str(ledger),
                        env_overrides={
                            "GIT_ALLOW_PROTOCOL": "file",
                            "GIT_AUTHOR_NAME": "AFK Test",
                            "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                            "GIT_COMMITTER_NAME": "AFK Test",
                            "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                        },
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    summary = json.loads(completed.stdout)
                    result_text = (ledger / summary["result_path"]).read_text(encoding="utf-8")

                    self.assertEqual(summary["status"], "blocked")
                    self.assertNotIn("plain-secret-value", result_text)
                    self.assertIn("[REDACTED]", result_text)
                    self.assertFalse(fake_calls.exists())

    def test_workstream_blocks_publication_when_final_validation_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            failing_worker_code = textwrap.dedent(
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
                            "summary": "unit tests failed",
                            "steps": [{"name": "unit", "status": "fail"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]

            completed = run_afk(
                "run-workstream",
                "--parent",
                "central-lve",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("validate did not reach validated", result["publication"]["reason"])
            self.assertEqual(result["cleanup"], {"status": "unknown", "resources": []})
            self.assertIn("afk run-workstream", result["retry"])
            self.assertNotIn("pr_body", result["artifacts"])
            self.assertFalse((ledger / summary["result_path"]).parent.joinpath("pr-body.md").exists())
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertFalse(fake_calls.exists())

    def test_workstream_blocks_validation_before_implementation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][2], recipe["steps"][3] = recipe["steps"][3], recipe["steps"][2]

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("validate requires implementation", result["publication"]["reason"])
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout"],
            )
            self.assertFalse(fake_calls.exists())

    def test_workstream_blocks_multi_item_selection_before_implementation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            second_item = {
                **selected_fixture_item(),
                "external_id": "central-lve.10",
                "url": "https://tracker.example/central-lve.10",
                "title": "Follow-up terminal publisher hardening",
            }
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"]["sources"][0]["items"] = [
                selected_fixture_item(),
                second_item,
            ]

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("single selected work item", result["publication"]["reason"])
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout"],
            )
            self.assertEqual(
                [item["external_id"] for item in result["selected_work"]],
                ["central-lve.9", "central-lve.10"],
            )
            self.assertEqual([item["result"] for item in result["selected_work"]], ["selected", "selected"])
            self.assertNotIn("passed", [item["result"] for item in result["selected_work"]])
            self.assertFalse(fake_calls.exists())

    def test_workstream_update_mode_edits_existing_terminal_pr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

body_file = sys.argv[sys.argv.index("--body-file") + 1]
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "gh",
            "argv": sys.argv[1:],
            "body": Path(body_file).read_text(encoding="utf-8"),
        }}
    )
    + "\\n"
)
print("https://github.example/pr/123")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"]["mode"] = "update"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(result["publication"]["mode"], "update")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["tool"], "gh")
            self.assertEqual(calls[0]["argv"][0:4], ["pr", "edit", "123", "--repo"])
            self.assertIn("central-lve.9 - Compose workstream recipe", calls[0]["body"])

    def test_workstream_blocks_publication_when_final_review_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            failing_reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "status": "fail",
                            "summary": "review found missing evidence",
                            "findings": [{"status": "fail", "title": "Missing terminal summary"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe["steps"][4]["input"]["reviewer"]["command"] = [sys.executable, "-c", failing_reviewer_code]

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("review did not reach passed", result["publication"]["reason"])
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertEqual(result["selected_work"][0]["result"], "failed")
            self.assertFalse(fake_calls.exists())

    def test_workstream_records_cleanup_and_retry_when_publication_fails_before_pr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
print("push rejected", file=sys.stderr)
sys.exit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write("gh should not run\\n")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["status"], "failed_publication")
            self.assertEqual(result["publication"]["status"], "failed")
            self.assertEqual(result["publication"]["returncode"], 9)
            self.assertIn("git command failed", result["publication"]["reason"])
            self.assertIn("push rejected", result["publication"]["stderr_excerpt"])
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertIn("afk run-workstream", result["retry"])
            self.assertEqual([call["tool"] for call in calls], ["git"])

    def test_workstream_disabled_publisher_does_not_advertise_absent_pr_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "completed")
            self.assertEqual(result["publication"]["status"], "skipped_disabled")
            self.assertNotIn("pr_body", result["artifacts"])
            self.assertFalse(result_path.parent.joinpath("pr-body.md").exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_redacts_secret_shaped_values_from_pr_body_and_published_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
sys.exit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

body_file = sys.argv[sys.argv.index("--body-file") + 1]
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "gh",
            "argv": sys.argv[1:],
            "body": Path(body_file).read_text(encoding="utf-8"),
        }}
    )
    + "\\n"
)
print("https://github.example/pr/123")
sys.exit(0)
""",
            )
            issue_secret = "ghp_issue_title_secret_1234567890"
            commit_secret = "ghp_commit_subject_secret_1234567890"
            review_secret = "ghp_review_summary_secret_1234567890"
            agent_code = textwrap.dedent(
                f"""
                import json
                import subprocess
                from pathlib import Path

                Path("implemented.txt").write_text("central-lve.9\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "{commit_secret}"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({{"status": "completed", "summary": "implemented workstream publisher"}}),
                    encoding="utf-8",
                )
                """
            ).strip()
            reviewer_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({{"status": "pass", "summary": "{review_secret}", "findings": []}}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"]["sources"][0]["items"][0]["title"] = issue_secret
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", agent_code]
            recipe["steps"][4]["input"]["reviewer"]["command"] = [sys.executable, "-c", reviewer_code]

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            workstream_result_text = result_path.read_text(encoding="utf-8")
            pr_body = result_path.parent.joinpath("pr-body.md").read_text(encoding="utf-8")
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]
            gh_body = next(call["body"] for call in calls if call["tool"] == "gh")

            for body in (workstream_result_text, pr_body, gh_body):
                self.assertNotIn(issue_secret, body)
                self.assertNotIn(commit_secret, body)
                self.assertNotIn(review_secret, body)
                self.assertIn("[REDACTED]", body)

    def test_workstream_records_terminal_result_for_non_object_publisher_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = "not-a-dict"

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            publication = json.loads(
                (result_path.parent / "publication-result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["status"], "failed_publication")
            self.assertEqual(result["status"], "failed_publication")
            self.assertEqual(result["publication"]["status"], "failed")
            self.assertEqual(publication["status"], "failed")
            self.assertIn("publisher must be an object", result["publication"]["reason"])
            self.assertIn("publisher must be an object", publication["reason"])
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertTrue(result_path.is_file())
            self.assertFalse(fake_calls.exists())

    def test_workstream_records_terminal_result_for_invalid_publisher_config(self):
        cases = [
            ("missing_repo", lambda publisher: publisher.pop("repo"), "publisher.repo is required"),
            (
                "invalid_mode",
                lambda publisher: publisher.__setitem__("mode", "delete"),
                "publisher.mode must be create or update",
            ),
        ]
        for case_name, mutate_publisher, expected_reason in cases:
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    repo = temp_path / "repo-src"
                    checkout = temp_path / "checkout"
                    ledger = temp_path / "ledger"
                    fake_calls = temp_path / "fake-calls.jsonl"
                    init_repo(repo)
                    fake_git = temp_path / "publisher-git"
                    fake_gh = temp_path / "publisher-gh"
                    write_executable(
                        fake_git,
                        f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
                    )
                    write_executable(
                        fake_gh,
                        f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
                    )
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    mutate_publisher(recipe["publisher"])

                    completed = run_afk(
                        "run-workstream",
                        "--workstream-id",
                        "central-lve.9",
                        "--input",
                        json.dumps(recipe),
                        "--ledger",
                        str(ledger),
                        env_overrides={
                            "GIT_ALLOW_PROTOCOL": "file",
                            "GIT_AUTHOR_NAME": "AFK Test",
                            "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                            "GIT_COMMITTER_NAME": "AFK Test",
                            "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                        },
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    summary = json.loads(completed.stdout)
                    result_path = ledger / summary["result_path"]
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    publication = json.loads(
                        (result_path.parent / "publication-result.json").read_text(encoding="utf-8")
                    )

                    self.assertEqual(summary["status"], "failed_publication")
                    self.assertEqual(result["status"], "failed_publication")
                    self.assertEqual(result["publication"]["status"], "failed")
                    self.assertEqual(publication["status"], "failed")
                    self.assertIn(expected_reason, result["publication"]["reason"])
                    self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
                    self.assertIn("afk run-workstream", result["retry"])
                    self.assertIn("afk run-workstream", publication["retry"])
                    self.assertFalse(fake_calls.exists())

    def test_workstream_rejects_publisher_head_that_differs_from_review_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"]["head"] = "afk/different-terminal-pr"

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed_publication")
            self.assertEqual(result["publication"]["status"], "failed")
            self.assertIn("publisher.head must match review_branch", result["publication"]["reason"])
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertFalse(fake_calls.exists())
