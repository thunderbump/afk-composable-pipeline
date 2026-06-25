import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.workstream import WorkstreamLedger, pr_body_markdown, select_work_proves_different_item  # noqa: E402


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


def selected_fixture_item(external_id="central-lve.9", title=None):
    resolved_title = title
    if resolved_title is None:
        resolved_title = (
            "Compose workstream recipe and terminal PR publisher"
            if external_id == "central-lve.9"
            else f"Work item {external_id}"
        )
    return {
        "external_id": external_id,
        "url": f"https://tracker.example/{external_id}",
        "title": resolved_title,
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
    def test_select_work_proves_different_item_with_fixture_enumerated_candidates(self):
        state = {"selected_work": [selected_fixture_item("central-lve.9")]}
        input_data = {
            "sources": [
                {
                    "type": "fixture",
                    "id": "fixture",
                    "items": [selected_fixture_item("central-lve.10")],
                }
            ]
        }

        self.assertTrue(select_work_proves_different_item(input_data, state))

    def test_select_work_does_not_prove_different_item_with_non_fixture_enumerated_candidates(self):
        state = {"selected_work": [selected_fixture_item("central-lve.9")]}
        input_data = {
            "sources": [
                {
                    "type": "github",
                    "id": "issues",
                    "items": [selected_fixture_item("central-lve.10")],
                }
            ]
        }

        self.assertFalse(select_work_proves_different_item(input_data, state))

    def test_select_work_target_ids_prove_different_item_regardless_of_source_shape(self):
        state = {"selected_work": [selected_fixture_item("central-lve.9")]}
        input_data = {
            "target_ids": ["central-lve.10"],
            "sources": [
                {
                    "type": "github",
                    "id": "issues",
                    "items": [selected_fixture_item("central-lve.9")],
                }
            ],
        }

        self.assertTrue(select_work_proves_different_item(input_data, state))

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
            "gh_token": os.environ.get("GH_TOKEN", ""),
            "github_token": os.environ.get("GITHUB_TOKEN", ""),
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

record = {{
    "tool": "gh",
    "argv": sys.argv[1:],
    "publisher_secret": os.environ.get("AFK_PUBLISHER_SECRET", ""),
    "gh_token": os.environ.get("GH_TOKEN", ""),
    "github_token": os.environ.get("GITHUB_TOKEN", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
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
                    "GH_TOKEN": "ghp_ambient_gh_token_secret_1234567890",
                    "GITHUB_TOKEN": "github_pat_ambient_token_secret_12345678901234567890",
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
            self.assertEqual(result["publication"]["status"], "published", result["publication"])
            self.assertEqual(result["publication"]["mode"], "create")
            self.assertEqual(result["publication"]["url"], "https://github.example/pr/123")

            git_calls = [call for call in calls if call["tool"] == "git"]
            gh_calls = [call for call in calls if call["tool"] == "gh"]
            self.assertEqual(len(git_calls), 1)
            self.assertEqual(len(gh_calls), 2)
            self.assertEqual(git_calls[0]["publisher_secret"], "")
            for gh_call in gh_calls:
                self.assertEqual(gh_call["publisher_secret"], "")
            self.assertEqual(git_calls[0]["gh_token"], "")
            self.assertEqual(git_calls[0]["github_token"], "")
            for gh_call in gh_calls:
                self.assertEqual(gh_call["gh_token"], "")
                self.assertEqual(gh_call["github_token"], "")
            self.assertEqual(git_calls[0]["cwd"], str(checkout))
            self.assertEqual(
                git_calls[0]["argv"],
                ["push", "origin", "HEAD:refs/heads/afk/workstream-terminal-pr"],
            )
            self.assertEqual(gh_calls[0]["argv"][0:3], ["auth", "status", "--hostname"])
            self.assertEqual(gh_calls[1]["argv"][0:3], ["pr", "create", "--repo"])
            body = gh_calls[1]["body"]
            self.assertIn("Workstream: central-lve.9", body)
            self.assertIn("Parent: central-lve", body)
            self.assertIn("central-lve.9 - Compose workstream recipe and terminal PR publisher", body)
            self.assertIn("Changed files", body)
            self.assertIn("implemented.txt", body)
            self.assertIn("Validation", body)
            self.assertIn("tier1: validated", body)
            self.assertIn("unit=pass", body)
            self.assertIn("command:", body)
            self.assertIn("summary: validated", body)
            self.assertIn("evidence: runs/", body)
            self.assertNotRegex(body, r"(?m)^-\s*:\s")
            self.assertIn("Review: passed", body)
            self.assertIn("Artifacts", body)
            self.assertIn(result["steps"][-1]["result_path"], body)

    def test_workstream_publisher_uses_explicit_gh_config_mount_without_inheriting_ambient_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
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
            "argv": sys.argv[1:],
            "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
            "gh_token": os.environ.get("GH_TOKEN", ""),
            "github_token": os.environ.get("GITHUB_TOKEN", ""),
            "home": os.environ.get("HOME", ""),
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

record = {{
    "tool": "gh",
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
    "gh_token": os.environ.get("GH_TOKEN", ""),
    "github_token": os.environ.get("GITHUB_TOKEN", ""),
    "home": os.environ.get("HOME", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("https://github.example/pr/456")
sys.exit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"]["gh"]["auth"] = {"config_dir": str(mounted_gh_config)}

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
                    "GH_TOKEN": "ghp_ambient_gh_token_secret_1234567890",
                    "GITHUB_TOKEN": "github_pat_ambient_token_secret_12345678901234567890",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            publication = json.loads((result_path.parent / "publication-result.json").read_text(encoding="utf-8"))
            command_artifact = (result_path.parent / "command.json").read_text(encoding="utf-8")
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(result["publication"]["auth"]["source"], "gh_config_dir")
            self.assertEqual(result["publication"]["auth"]["path"], "[REDACTED]")
            self.assertEqual(publication["auth"]["source"], "gh_config_dir")
            self.assertEqual(publication["auth"]["path"], "[REDACTED]")
            self.assertNotIn(str(mounted_gh_config), command_artifact)
            self.assertNotIn(str(mounted_gh_config), result_path.read_text(encoding="utf-8"))
            self.assertEqual([call["argv"][0] for call in calls], ["auth", "push", "pr"])
            for call in calls:
                self.assertEqual(call["gh_config_dir"], str(mounted_gh_config))
                self.assertEqual(call["gh_token"], "")
                self.assertEqual(call["github_token"], "")
                self.assertNotEqual(call["home"], os.environ.get("HOME", ""))

    def test_workstream_records_actionable_terminal_result_for_invalid_publisher_auth_config(self):
        cases = [
            (
                "raw_token_rejected",
                lambda publisher, _temp_path, _checkout: publisher["gh"].__setitem__(
                    "token", "ghp_recipe_secret_1234567890"
                ),
                "publisher.gh.token is not supported; mount gh auth config instead",
            ),
            (
                "secret_like_top_level_key_rejected",
                lambda publisher, _temp_path, _checkout: publisher["gh"].__setitem__(
                    "access_token", "ghp_recipe_secret_1234567890"
                ),
                "publisher.gh.access_token is not supported; mount gh auth config instead",
            ),
            (
                "relative_config_dir",
                lambda publisher, _temp_path, _checkout: publisher["gh"].__setitem__(
                    "auth", {"config_dir": "relative-gh-config"}
                ),
                "publisher.gh.auth.config_dir must be absolute",
            ),
            (
                "missing_config_dir",
                lambda publisher, temp_path, _checkout: publisher["gh"].__setitem__(
                    "auth", {"config_dir": str(temp_path / "missing-gh-config")}
                ),
                "publisher.gh.auth.config_dir must be an existing directory",
            ),
            (
                "checkout_config_dir",
                lambda publisher, _temp_path, checkout: publisher["gh"].__setitem__(
                    "auth", {"config_dir": str(checkout)}
                ),
                "publisher.gh.auth.config_dir must be outside checkout",
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
                    mutate_publisher(recipe["publisher"], temp_path, checkout)

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

                    self.assertEqual(summary["status"], "failed-needs-human")
                    self.assertEqual(result["publication"]["status"], "failed-needs-human")
                    self.assertEqual(publication["status"], "failed-needs-human")
                    self.assertIn(expected_reason, result["publication"]["reason"])
                    self.assertIn("publisher.gh.auth.config_dir", result["publication"]["retry"])
                    self.assertIn("publisher.gh.auth.config_dir", publication["retry"])
                    self.assertFalse(fake_calls.exists())

    def test_workstream_records_actionable_terminal_result_for_gh_auth_status_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            stderr_secret = "ghp_auth_status_failure_secret_1234567890"
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
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "argv": sys.argv[1:],
            "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
            "gh_token": os.environ.get("GH_TOKEN", ""),
            "github_token": os.environ.get("GITHUB_TOKEN", ""),
        }}
    )
    + "\\n"
)
print("auth failed {stderr_secret}", file=sys.stderr)
sys.exit(1)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"]["gh"]["auth"] = {"config_dir": str(mounted_gh_config)}

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
                    "GH_TOKEN": "ghp_ambient_gh_token_secret_1234567890",
                    "GITHUB_TOKEN": "github_pat_ambient_token_secret_12345678901234567890",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            publication_text = (result_path.parent / "publication-result.json").read_text(encoding="utf-8")
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertIn("gh auth status failed", result["publication"]["reason"])
            self.assertIn("publisher.gh.auth.config_dir", result["publication"]["retry"])
            self.assertEqual(result["publication"]["command"][1:3], ["auth", "status"])
            self.assertNotIn(stderr_secret, publication_text)
            self.assertIn("[REDACTED]", publication_text)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["argv"][0:3], ["auth", "status", "--hostname"])
            self.assertEqual(calls[0]["gh_config_dir"], str(mounted_gh_config))
            self.assertEqual(calls[0]["gh_token"], "")
            self.assertEqual(calls[0]["github_token"], "")

    def test_workstream_default_publisher_path_preflights_gh_auth_before_push_and_blocks_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            stderr_secret = "ghp_default_auth_status_failure_secret_1234567890"
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
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "argv": sys.argv[1:],
            "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
            "gh_token": os.environ.get("GH_TOKEN", ""),
            "github_token": os.environ.get("GITHUB_TOKEN", ""),
        }}
    )
    + "\\n"
)
print("auth failed {stderr_secret}", file=sys.stderr)
sys.exit(1)
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
                    "GH_TOKEN": "ghp_ambient_gh_token_secret_1234567890",
                    "GITHUB_TOKEN": "github_pat_ambient_token_secret_12345678901234567890",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            publication_text = (result_path.parent / "publication-result.json").read_text(encoding="utf-8")
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["auth"]["source"], "minimal_env")
            self.assertIn("gh auth status failed", result["publication"]["reason"])
            self.assertIn("publisher.gh.auth.config_dir", result["publication"]["retry"])
            self.assertEqual(result["publication"]["command"][1:3], ["auth", "status"])
            self.assertNotIn(stderr_secret, publication_text)
            self.assertIn("[REDACTED]", publication_text)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["argv"][0:3], ["auth", "status", "--hostname"])
            self.assertEqual(calls[0]["gh_config_dir"], "")
            self.assertEqual(calls[0]["gh_token"], "")
            self.assertEqual(calls[0]["github_token"], "")

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

record = {{"tool": "gh", "argv": sys.argv[1:]}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
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

            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(result["status"], "validated-unpublished")
            self.assertEqual(result["publication"]["status"], "validated-unpublished")
            self.assertIn("validated terminal state", result["publication"]["reason"])
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertEqual(result["selected_work"][0]["result"], "validated")
            self.assertFalse(reviewer_invoked.exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_stops_before_fresh_select_cycle_after_successful_validation(self):
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
            recipe["steps"] = recipe["steps"][:4] + [
                recipe["steps"][0],
                recipe["steps"][1],
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

            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(result["status"], "validated-unpublished")
            self.assertEqual(result["publication"]["status"], "validated-unpublished")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertIn("validated terminal state", result["terminal_reason"])
            self.assertEqual(
                result["next_allowed_command"],
                "afk run-workstream --workstream-id central-lve.9 --ledger <ledger> --input <recipe>",
            )
            self.assertFalse(fake_calls.exists())

    def test_workstream_stops_before_non_fixture_enumerated_follow_up_selection_after_validation(self):
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
            recipe["steps"] = recipe["steps"][:4] + [
                {
                    "name": "select-work",
                    "input": {
                        "required_labels": ["afk:ready"],
                        "sources": [
                            {
                                "type": "github",
                                "id": "issues",
                                "items": [selected_fixture_item("central-lve.10")],
                            }
                        ],
                    },
                },
                recipe["steps"][1],
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

            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(result["status"], "validated-unpublished")
            self.assertEqual(result["publication"]["status"], "validated-unpublished")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertIn("validated terminal state", result["terminal_reason"])
            self.assertEqual(
                result["next_allowed_command"],
                "afk run-workstream --workstream-id central-lve.9 --ledger <ledger> --input <recipe>",
            )
            self.assertFalse(fake_calls.exists())

    def test_workstream_allows_proven_different_item_after_successful_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            second_checkout = temp_path / "checkout-two"
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
            second_agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("implemented-second.txt").write_text("central-lve.10\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented-second.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement central-lve.10"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "implemented second work item"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"] = recipe["steps"][:4] + [
                {
                    "name": "select-work",
                    "input": {
                        "target_ids": ["central-lve.10"],
                        "required_labels": ["afk:ready"],
                        "sources": [
                            {
                                "type": "fixture",
                                "id": "fixture",
                                "items": [selected_fixture_item("central-lve.10")],
                            }
                        ],
                    },
                },
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(second_checkout),
                    },
                },
                {
                    "name": "implement",
                    "input": {
                        "guardrails": ["stay within checkout"],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", second_agent_code],
                            "result_path": "agent-result.json",
                        },
                    },
                },
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
            self.assertEqual(result["publication"]["reason"], "required final validation evidence is missing")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                [
                    "select-work",
                    "prepare-checkout",
                    "implement",
                    "validate",
                    "select-work",
                    "prepare-checkout",
                    "implement",
                ],
            )
            self.assertEqual(result["selected_work"][0]["external_id"], "central-lve.10")
            self.assertEqual(result["selected_work"][0]["result"], "implemented")
            self.assertTrue((second_checkout / "implemented-second.txt").exists())
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

record = {{"tool": "gh", "argv": sys.argv[1:]}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
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

    def test_workstream_blocks_retry_when_retry_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            retry_checkout = temp_path / "retry-checkout"
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
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 0}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["steps"] = recipe["steps"][:4] + [
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(retry_checkout),
                    },
                }
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
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("retry budget exhausted", result["publication"]["reason"])
            self.assertEqual(
                result["retry_budget"],
                {
                    "max_retries": 0,
                    "attempted_retries": 0,
                    "remaining_retries": 0,
                },
            )
            self.assertEqual(result["retry_attempts"], [])
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertFalse(retry_checkout.exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_blocks_new_retry_checkout_when_prior_retry_is_dirty_and_summarizes_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            retry_checkout = temp_path / "retry-checkout"
            blocked_checkout = temp_path / "retry-checkout-2"
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
            retry_agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("retry-implementation.txt").write_text("retry implementation\\n", encoding="utf-8")
                subprocess.run(["git", "add", "retry-implementation.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "retry implementation"], check=True)
                Path("retry-dirty.txt").write_text("left dirty on purpose\\n", encoding="utf-8")
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "retry implementation left dirty evidence"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 2}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["steps"] = recipe["steps"][:4] + [
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(retry_checkout),
                    },
                },
                {
                    "name": "implement",
                    "input": {
                        "guardrails": ["stay within checkout"],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", retry_agent_code],
                            "result_path": "agent-result.json",
                        },
                    },
                },
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(blocked_checkout),
                    },
                },
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
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("prior retry checkout is dirty", result["publication"]["reason"])
            self.assertEqual(
                result["retry_budget"],
                {
                    "max_retries": 2,
                    "attempted_retries": 1,
                    "remaining_retries": 1,
                },
            )
            self.assertEqual(len(result["retry_attempts"]), 1)
            retry_attempt = result["retry_attempts"][0]
            self.assertEqual(retry_attempt["repairing_failure_class"], "failed_validation")
            self.assertEqual(retry_attempt["checkout_path"], str(retry_checkout))
            self.assertEqual(retry_attempt["review_branch"], "afk/workstream-terminal-pr")
            self.assertEqual(retry_attempt["status"], "dirty")
            self.assertTrue(retry_attempt["commit"])
            self.assertEqual(result["cleanup"]["status"], "dirty_retry_checkouts")
            self.assertEqual(
                result["cleanup"]["resources"],
                [
                    {
                        "kind": "retry_checkout",
                        "path": str(retry_checkout),
                        "branch": "afk/workstream-terminal-pr",
                        "commit": retry_attempt["commit"],
                        "status": "dirty",
                    }
                ],
            )
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "prepare-checkout", "implement"],
            )
            self.assertFalse(blocked_checkout.exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_blocks_new_retry_checkout_when_prior_retry_is_awaiting_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            retry_checkout = temp_path / "retry-checkout"
            blocked_checkout = temp_path / "retry-checkout-2"
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
            retry_agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("retry-implementation.txt").write_text("retry implementation\\n", encoding="utf-8")
                subprocess.run(["git", "add", "retry-implementation.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "retry implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "retry implementation"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 2}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["steps"] = recipe["steps"][:4] + [
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(retry_checkout),
                    },
                },
                {
                    "name": "implement",
                    "input": {
                        "guardrails": ["stay within checkout"],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", retry_agent_code],
                            "result_path": "agent-result.json",
                        },
                    },
                },
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(blocked_checkout),
                    },
                },
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
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("still running validation", result["publication"]["reason"])
            self.assertEqual(
                result["retry_budget"],
                {
                    "max_retries": 2,
                    "attempted_retries": 1,
                    "remaining_retries": 1,
                },
            )
            self.assertEqual(len(result["retry_attempts"]), 1)
            retry_attempt = result["retry_attempts"][0]
            self.assertEqual(retry_attempt["repairing_failure_class"], "failed_validation")
            self.assertEqual(retry_attempt["checkout_path"], str(retry_checkout))
            self.assertEqual(retry_attempt["review_branch"], "afk/workstream-terminal-pr")
            self.assertEqual(retry_attempt["status"], "awaiting_validation")
            self.assertTrue(retry_attempt["commit"])
            self.assertEqual(result["cleanup"]["status"], "unknown")
            self.assertEqual(result["cleanup"]["resources"], [])
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "prepare-checkout", "implement"],
            )
            self.assertFalse(blocked_checkout.exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_keeps_dirty_retry_evidence_after_validation_failure_and_blocks_further_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            retry_checkout = temp_path / "retry-checkout"
            blocked_checkout = temp_path / "retry-checkout-2"
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
            retry_agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("retry-implementation.txt").write_text("retry implementation\\n", encoding="utf-8")
                subprocess.run(["git", "add", "retry-implementation.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "retry implementation"], check=True)
                Path("retry-dirty.txt").write_text("left dirty on purpose\\n", encoding="utf-8")
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "retry implementation left dirty evidence"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 2}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["steps"] = recipe["steps"][:4] + [
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(retry_checkout),
                    },
                },
                {
                    "name": "implement",
                    "input": {
                        "guardrails": ["stay within checkout"],
                        "validation": {"profile": "tier1", "commands": []},
                        "agent": {
                            "type": "fake-pi-command",
                            "command": [sys.executable, "-c", retry_agent_code],
                            "result_path": "agent-result.json",
                        },
                    },
                },
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {"dry_run": False},
                        "worker": {"type": "local-command", "command": [sys.executable, "-c", failing_worker_code]},
                    },
                },
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(blocked_checkout),
                    },
                },
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
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("prior retry checkout is dirty", result["publication"]["reason"])
            self.assertEqual(
                result["retry_budget"],
                {
                    "max_retries": 2,
                    "attempted_retries": 1,
                    "remaining_retries": 1,
                },
            )
            self.assertEqual(len(result["retry_attempts"]), 1)
            retry_attempt = result["retry_attempts"][0]
            self.assertEqual(retry_attempt["repairing_failure_class"], "failed_validation")
            self.assertEqual(retry_attempt["checkout_path"], str(retry_checkout))
            self.assertEqual(retry_attempt["review_branch"], "afk/workstream-terminal-pr")
            self.assertEqual(retry_attempt["status"], "dirty")
            self.assertTrue(retry_attempt["commit"])
            self.assertEqual(result["cleanup"]["status"], "dirty_retry_checkouts")
            self.assertEqual(
                result["cleanup"]["resources"],
                [
                    {
                        "kind": "retry_checkout",
                        "path": str(retry_checkout),
                        "branch": "afk/workstream-terminal-pr",
                        "commit": retry_attempt["commit"],
                        "status": "dirty",
                    }
                ],
            )
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                [
                    "select-work",
                    "prepare-checkout",
                    "implement",
                    "validate",
                    "prepare-checkout",
                    "implement",
                    "validate",
                ],
            )
            self.assertFalse(blocked_checkout.exists())
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

record = {{"tool": "gh", "argv": sys.argv[1:]}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
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
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["tool"], "gh")
            self.assertEqual(calls[0]["argv"][0:3], ["auth", "status", "--hostname"])
            self.assertEqual(calls[1]["tool"], "gh")
            self.assertEqual(calls[1]["argv"][0:4], ["pr", "edit", "123", "--repo"])
            self.assertIn("central-lve.9 - Compose workstream recipe", calls[1]["body"])

    def test_workstream_update_mode_falls_back_to_rest_when_pr_edit_hits_projects_classic_failure(self):
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

record = {{"tool": "gh", "argv": sys.argv[1:]}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
if "--input" in sys.argv:
    input_file = sys.argv[sys.argv.index("--input") + 1]
    record["input"] = json.loads(Path(input_file).read_text(encoding="utf-8"))
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:4] == ["pr", "edit", "afk/workstream-terminal-pr"]:
    print("GraphQL: Projects (classic) is being deprecated in favor of the new Projects experience", file=sys.stderr)
    sys.exit(1)
if sys.argv[1:4] == ["pr", "view", "afk/workstream-terminal-pr"]:
    print("123")
    sys.exit(0)
if sys.argv[1:4] == ["api", "--method", "PATCH"]:
    print("https://github.example/pr/123")
    sys.exit(0)
sys.exit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"]["mode"] = "update"
            recipe["publisher"]["pr"] = "afk/workstream-terminal-pr"
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

            self.assertEqual(result["publication"]["status"], "published", result["publication"])
            self.assertEqual(result["publication"]["mode"], "update")
            self.assertEqual(
                [call["argv"][0:2] for call in calls],
                [["auth", "status"], ["pr", "edit"], ["pr", "view"], ["api", "--method"]],
            )
            self.assertEqual(
                calls[3]["argv"],
                [
                    "api",
                    "--method",
                    "PATCH",
                    "repos/thunderbump/afk-composable-pipeline/pulls/123",
                    "--input",
                    calls[3]["argv"][5],
                    "--jq",
                    ".html_url",
                ],
            )
            self.assertEqual(
                calls[3]["input"]["title"],
                "central-lve.9: Compose workstream recipe and terminal PR publisher",
            )
            body = calls[3]["input"]["body"]
            self.assertIn("central-lve.9 - Compose workstream recipe", body)
            self.assertIn("Validation", body)
            self.assertIn("tier1: validated", body)
            self.assertIn("unit=pass", body)
            self.assertIn("command:", body)
            self.assertIn("summary: validated", body)
            self.assertIn("evidence: runs/", body)
            self.assertNotRegex(body, r"(?m)^-\s*:\s")

    def test_workstream_update_mode_resolves_non_numeric_pr_refs_before_rest_fallback(self):
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

record = {{"tool": "gh", "argv": sys.argv[1:]}}
if "--input" in sys.argv:
    input_file = sys.argv[sys.argv.index("--input") + 1]
    record["input"] = json.loads(Path(input_file).read_text(encoding="utf-8"))
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:4] == ["pr", "edit", "feature/pull/123"]:
    print("GraphQL: Projects (classic) is being deprecated in favor of the new Projects experience", file=sys.stderr)
    sys.exit(1)
if sys.argv[1:4] == ["pr", "view", "feature/pull/123"]:
    print("987")
    sys.exit(0)
if sys.argv[1:4] == ["api", "--method", "PATCH"]:
    print("https://github.example/pr/987")
    sys.exit(0)
sys.exit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"]["mode"] = "update"
            recipe["publisher"]["pr"] = "feature/pull/123"
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
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [call["argv"][0:3] for call in calls],
                [
                    ["auth", "status", "--hostname"],
                    ["pr", "edit", "feature/pull/123"],
                    ["pr", "view", "feature/pull/123"],
                    ["api", "--method", "PATCH"],
                ],
            )
            self.assertEqual(calls[3]["argv"][3], "repos/thunderbump/afk-composable-pipeline/pulls/987")

    def test_workstream_pr_body_redacts_validation_worker_command_secret_args(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            body = pr_body_markdown(
                {
                    "workstream_id": "central-lve.9",
                    "parent": "central-lve",
                    "review_branch": "afk/workstream-terminal-pr",
                },
                {
                    "implementation": {"git": {"changed_files": [], "commits": []}},
                    "validations": [
                        {
                            "step_result_path": str(temp_path / "ledger" / "runs" / "validate" / "step-result.json"),
                            "worker_result_path": str(temp_path / "ledger" / "runs" / "validate" / "worker-result.json"),
                            "output": {
                                "status": "validated",
                                "summary": "validated",
                                "validation": {"requested_profile": "tier1"},
                                "worker_result": {
                                    "raw": {"steps": [{"name": "unit", "status": "pass"}]},
                                    "normalized": {
                                        "adapter": {
                                            "command": [
                                                sys.executable,
                                                "worker.py",
                                                "--token",
                                                "plain-secret-value",
                                                "--api-key=plain-api-secret",
                                            ]
                                        }
                                    },
                                },
                            },
                        }
                    ],
                    "review": {"status": "passed"},
                    "cleanup": {"status": "clean", "resources": []},
                },
                [{"name": "validate", "result_path": "runs/validate/step-result.json"}],
                [
                    {
                        "external_id": "central-lve.9",
                        "title": "Compose workstream recipe",
                        "result": "passed",
                    }
                ],
                WorkstreamLedger(temp_path / "ledger", "run-1"),
            )

            self.assertIn("command:", body)
            self.assertIn("--token [REDACTED]", body)
            self.assertIn("--api-key=[REDACTED]", body)
            self.assertNotIn("plain-secret-value", body)
            self.assertNotIn("plain-api-secret", body)

    def test_workstream_pr_body_preserves_validation_contract_when_worker_evidence_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            body = pr_body_markdown(
                {
                    "workstream_id": "central-lve.9",
                    "parent": "central-lve",
                    "review_branch": "afk/workstream-terminal-pr",
                },
                {
                    "implementation": {"git": {"changed_files": [], "commits": []}},
                    "validations": [
                        {
                            "step_result_path": str(temp_path / "ledger" / "runs" / "validate" / "step-result.json"),
                            "worker_result_path": "",
                            "output": {
                                "status": "validated",
                                "validation": {"requested_profile": "tier1"},
                            },
                        }
                    ],
                    "review": {"status": "passed"},
                    "cleanup": {"status": "clean", "resources": []},
                },
                [{"name": "validate", "result_path": "runs/validate/step-result.json"}],
                [
                    {
                        "external_id": "central-lve.9",
                        "title": "Compose workstream recipe",
                        "result": "passed",
                    }
                ],
                WorkstreamLedger(temp_path / "ledger", "run-1"),
            )

            self.assertIn(
                "- tier1: validated - result: missing - command: missing - summary: missing - evidence: runs/validate/step-result.json",
                body,
            )

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
import json
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "gh", "argv": sys.argv[1:]}}) + "\\n"
)
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
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

            self.assertEqual(summary["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["returncode"], 9)
            self.assertIn("git command failed", result["publication"]["reason"])
            self.assertIn("push rejected", result["publication"]["stderr_excerpt"])
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertIn("afk run-workstream", result["retry"])
            self.assertEqual([call["tool"] for call in calls], ["gh", "git"])
            self.assertEqual(calls[0]["argv"][0:3], ["auth", "status", "--hostname"])

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

            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(result["publication"]["status"], "validated-unpublished")
            self.assertEqual(
                result["next_allowed_command"],
                "afk run-workstream --workstream-id central-lve.9 --ledger <ledger> --input <recipe>",
            )
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

record = {{"tool": "gh", "argv": sys.argv[1:]}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
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
            gh_body = next(call["body"] for call in calls if call["tool"] == "gh" and "body" in call)

            for body in (workstream_result_text, pr_body, gh_body):
                self.assertNotIn(issue_secret, body)
                self.assertNotIn(commit_secret, body)
                self.assertNotIn(review_secret, body)
                self.assertIn("[REDACTED]", body)

    def test_workstream_redacts_secret_shaped_successful_publisher_stdout_from_artifacts(self):
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
            stdout_secret = "ghp_success_stdout_secret_1234567890"
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print({stdout_secret!r})
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
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_path = ledger / summary["result_path"]
            workstream_result_text = result_path.read_text(encoding="utf-8")
            publication_result_text = result_path.parent.joinpath("publication-result.json").read_text(
                encoding="utf-8"
            )
            result = json.loads(workstream_result_text)
            publication = json.loads(publication_result_text)

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["publication"]["url"], "[REDACTED]")
            self.assertEqual(publication["url"], "[REDACTED]")
            self.assertNotIn(stdout_secret, workstream_result_text)
            self.assertNotIn(stdout_secret, publication_result_text)

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

            self.assertEqual(summary["status"], "failed-needs-human")
            self.assertEqual(result["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertEqual(publication["status"], "failed-needs-human")
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

                    self.assertEqual(summary["status"], "failed-needs-human")
                    self.assertEqual(result["status"], "failed-needs-human")
                    self.assertEqual(result["publication"]["status"], "failed-needs-human")
                    self.assertEqual(publication["status"], "failed-needs-human")
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

            self.assertEqual(summary["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertIn("publisher.head must match review_branch", result["publication"]["reason"])
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertFalse(fake_calls.exists())
