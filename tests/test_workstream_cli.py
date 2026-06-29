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

from afk.registry import StepResult  # noqa: E402
from afk.workstream import (  # noqa: E402
    WorkstreamLedger,
    composed_step_input,
    current_selected_work_selection_identity,
    pr_body_markdown,
    publish_terminal_pr,
    selected_work_records,
    select_work_proves_different_item,
    update_state_from_step,
)


def run_afk(*args, env_overrides=None, cwd=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "afk", *args],
        cwd=cwd or ROOT,
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


def merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh):
    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
    recipe["tracker"] = {
        "terminal_decision": {
            "status": "merged",
            "merge_commit": "deadbeef",
            "pr_url": "https://github.example/pr/123",
        }
    }
    recipe["retrospective"] = {
        "summary": "Merged after validating token=ghp_secret_merge_retrospective_1234567890 cleanup.",
        "validation": [
            "Manual check kept password=super-secret-value out of the ledger.",
        ],
    }
    return recipe


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

    def test_select_work_target_ids_do_not_prove_different_item_when_any_current_item_overlaps(self):
        state = {
            "selected_work": [
                selected_fixture_item("central-lve.9"),
                selected_fixture_item("central-lve.10", "Follow-up terminal publisher hardening"),
            ]
        }
        input_data = {"target_ids": ["central-lve.10"]}

        self.assertFalse(select_work_proves_different_item(input_data, state))

    def test_select_work_source_qualified_target_ids_can_prove_same_external_id_is_different(self):
        state = {
            "selected_work": [
                {
                    **selected_fixture_item("123"),
                    "source_id": "beads",
                    "source_type": "beads",
                    "url": "",
                }
            ]
        }
        input_data = {"target_ids": ["github_issues:github:123"]}

        self.assertTrue(select_work_proves_different_item(input_data, state))

    def test_multi_item_selection_identity_is_order_independent(self):
        first = selected_fixture_item("central-lve.9")
        second = selected_fixture_item("central-lve.10", "Follow-up terminal publisher hardening")

        self.assertEqual(
            current_selected_work_selection_identity({"selected_work": [first, second]}),
            current_selected_work_selection_identity({"selected_work": [second, first]}),
        )

    def test_review_step_input_carries_combined_selected_work_for_multi_item_review(self):
        second_item = selected_fixture_item(
            "central-lve.10",
            "Follow-up terminal publisher hardening",
        )
        state = {
            "selected_work": [selected_fixture_item("central-lve.9"), second_item],
            "checkout": {
                "status": "prepared",
                "checkout_path": "/tmp/checkout",
                "start_commit": "abc123",
            },
            "implementation": {
                "status": "implemented",
                "git": {"after_commit": "def456"},
            },
            "validations": [],
        }

        review_input = composed_step_input(
            {"name": "review", "input": {"reviewer": {"type": "fake-reviewer-command"}}},
            {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": "afk/workstream-terminal-pr",
            },
            state,
            Path("/tmp/ledger"),
        )

        self.assertEqual(review_input["work_item"]["external_id"], "central-lve.9")
        self.assertEqual(
            [item["external_id"] for item in review_input["work_selection"]["selected_work"]],
            ["central-lve.9", "central-lve.10"],
        )

    def test_selected_work_records_mark_unreviewed_items_not_processed_in_partial_multi_item_result(self):
        selected_work = [
            selected_fixture_item("central-lve.9"),
            selected_fixture_item("central-lve.10", "Follow-up terminal publisher hardening"),
        ]
        state = {
            "selected_work": selected_work,
            "implementation": {"status": "implemented", "git": {"after_commit": "def456"}},
            "implementation_selection": selected_work,
            "implementation_result_path": "runs/implement/step-result.json",
            "validations": [
                {
                    "output": {
                        "status": "validated",
                        "checkout": {"start_commit": "def456"},
                    },
                    "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                    "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
                }
            ],
            "review": {"status": "passed", "checkout": {"start_commit": "def456"}},
            "review_selection": [selected_work[0]],
            "review_result_path": "runs/review/step-result.json",
        }

        self.assertEqual(
            [item["result"] for item in selected_work_records(state)],
            ["passed", "not_processed"],
        )

    def test_selected_work_records_do_not_match_evidence_across_sources_with_same_external_id(self):
        beads_item = {
            **selected_fixture_item("123"),
            "source_id": "beads",
            "source_type": "beads",
            "url": "",
        }
        github_item = {
            **selected_fixture_item("123"),
            "source_id": "github",
            "source_type": "github_issues",
            "url": "",
        }
        state = {
            "selected_work": [beads_item, github_item],
            "implementation": {"status": "implemented", "git": {"after_commit": "def456"}},
            "implementation_selection": [github_item],
            "implementation_result_path": "runs/implement/step-result.json",
            "validations": [
                {
                    "output": {"status": "validated", "checkout": {"start_commit": "def456"}},
                    "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                    "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
                }
            ],
            "review": {"status": "passed", "checkout": {"start_commit": "def456"}},
            "review_selection": [github_item],
            "review_result_path": "runs/review/step-result.json",
        }

        self.assertEqual(
            [item["result"] for item in selected_work_records(state)],
            ["not_processed", "passed"],
        )

    def test_review_step_input_does_not_expand_legacy_implementation_to_full_selection(self):
        first = selected_fixture_item("central-lve.9")
        second = selected_fixture_item("central-lve.10", "Follow-up terminal publisher hardening")
        state = {
            "selected_work": [first, second],
            "checkout": {
                "status": "prepared",
                "checkout_path": "/tmp/checkout",
                "start_commit": "abc123",
            },
            "implementation": {
                "status": "implemented",
                "work_item": first,
                "git": {"after_commit": "def456"},
            },
            "validations": [],
        }

        review_input = composed_step_input(
            {"name": "review", "input": {"reviewer": {"type": "fake-reviewer-command"}}},
            {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": "afk/workstream-terminal-pr",
            },
            state,
            Path("/tmp/ledger"),
        )

        self.assertEqual(
            [item["external_id"] for item in review_input["work_selection"]["selected_work"]],
            ["central-lve.9"],
        )

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
            self.assertEqual(result["tracker"]["status"], "awaiting-review")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["pr_url"], "https://github.example/pr/123")
            self.assertIn("keep the source Beads item open", result["tracker"]["comment"])
            self.assertEqual(result["artifacts"]["tracker"], "tracker-result.json")
            self.assertEqual(result["artifacts"]["pipeline_retrospective"], "pipeline-retrospective.json")
            self.assertEqual(
                result["pipeline_retrospective"],
                {
                    "schema_version": 1,
                    "status": "published",
                    "health": "healthy",
                    "publication_status": "published",
                    "tracker_status": "awaiting-review",
                    "signals": [],
                    "recommended_follow_up": [],
                    "follow_up": {
                        "recommended": [],
                        "created": [],
                        "creation": {"enabled": False, "status": "recommendation-only"},
                    },
                    "judge": {"enabled": False, "status": "disabled"},
                },
            )
            self.assertEqual(
                json.loads(
                    (ledger / "workstreams" / summary["run_id"] / "pipeline-retrospective.json").read_text(
                        encoding="utf-8"
                    )
                ),
                result["pipeline_retrospective"],
            )

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

    def test_workstream_reaches_validated_unpublished_with_real_reviewer_stdout_json(self):
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
            reviewer_code = textwrap.dedent(
                """
                import json

                print(json.dumps({"status": "pass", "summary": "stdout review passed", "findings": []}))
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][4]["input"]["reviewer"] = {
                "type": "real-reviewer-command",
                "command": [sys.executable, "-c", reviewer_code],
                "timeout_seconds": 10,
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
            review_step = next(step for step in result["steps"] if step["name"] == "review")
            review_result = json.loads(
                (ledger / "runs" / review_step["run_id"] / "reviewer-result.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(result["status"], "validated-unpublished")
            self.assertEqual(result["publication"]["status"], "validated-unpublished")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "review"],
            )
            self.assertEqual(result["selected_work"][0]["result"], "passed")
            self.assertEqual(review_result["result"]["status"], "passed")
            self.assertEqual(review_result["result"]["adapter"]["type"], "real-reviewer-command")
            self.assertEqual(review_result["result"]["evidence"]["result_source"], "stdout_fallback")
            self.assertEqual(review_result["result"]["evidence"]["result_file_present"], False)
            self.assertFalse(fake_calls.exists())

    def test_workstream_terminal_merge_decision_closes_tracker_without_republishing(self):
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
Path({str(fake_calls)!r}).write_text("publisher git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("publisher gh should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                }
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

            self.assertFalse(fake_calls.exists())
            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertIn("terminal tracker decision", result["publication"]["reason"])
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["merge_commit"], "deadbeef")
            self.assertEqual(result["tracker"]["pr_url"], "https://github.example/pr/123")
            self.assertEqual([item["result"] for item in result["selected_work"]], ["passed"])
            tracker_result = json.loads((ledger / summary["result_path"]).parent.joinpath("tracker-result.json").read_text(encoding="utf-8"))
            self.assertEqual(tracker_result["merge_commit"], "deadbeef")
            self.assertEqual(tracker_result["pr_url"], "https://github.example/pr/123")

    def test_workstream_includes_redacted_retrospective_for_terminal_merge_decision(self):
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
Path({str(fake_calls)!r}).write_text("publisher git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("publisher gh should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                }
            }
            recipe["retrospective"] = {
                "summary": "Merged after validating token=ghp_secret_merge_retrospective_1234567890 cleanup.",
                "changes": [
                    "Added top-level retrospective evidence to workstream outputs.",
                    "Recorded tracker close guidance for merged workstreams.",
                ],
                "validation": [
                    "tier1 validation passed for commit deadbeef.",
                    "Manual check kept password=super-secret-value out of the ledger.",
                ],
                "review": [
                    "Correctness and bug-risk reviews passed with no open findings.",
                ],
                "unresolved_risks": [
                    "No further merge blockers remain.",
                ],
                "process_findings": [
                    "Retrospective evidence should be added after the terminal merge decision lands.",
                ],
                "follow_up": {
                    "recommended": [
                        {
                            "id": "central-3x6.5",
                            "summary": "Track follow-up automation for retrospective prompts.",
                            "labels": ["project:afk-composable-pipeline", "afk:ready"],
                        }
                    ],
                    "created": [
                        {
                            "id": "central-3x6.6",
                            "summary": "Document retrospective note locations.",
                            "labels": ["project:afk-composable-pipeline"],
                        }
                    ],
                },
                "notes": {
                    "personal_work": [
                        "~/Documents/rmd/Ceremonies/Personal Work/work/2026-06-27-personal.md",
                    ],
                    "spikes": [
                        "~/Documents/rmd/Ceremonies/Personal Work/spikes/2026-06-27-retrospective.md",
                    ],
                },
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
            result = json.loads(result_path.read_text(encoding="utf-8"))
            tracker = json.loads((result_path.parent / "tracker-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["retrospective"], tracker["retrospective"])
            self.assertEqual(result["tracker"]["retrospective"], tracker["retrospective"])
            self.assertEqual(result["artifacts"]["retrospective"], "retrospective.json")
            self.assertEqual(result["artifacts"]["pipeline_retrospective"], "pipeline-retrospective.json")
            self.assertIn("[REDACTED]", result["retrospective"]["summary"])
            self.assertIn("[REDACTED]", result["retrospective"]["validation"][1])
            self.assertEqual(
                result["retrospective"]["follow_up"]["recommended"][0]["labels"],
                ["project:afk-composable-pipeline", "afk:ready"],
            )
            self.assertEqual(
                result["retrospective"]["notes"]["personal_work"],
                ["~/Documents/rmd/Ceremonies/Personal Work/work/2026-06-27-personal.md"],
            )
            self.assertEqual(
                result["retrospective"]["notes"]["spikes"],
                ["~/Documents/rmd/Ceremonies/Personal Work/spikes/2026-06-27-retrospective.md"],
            )
            self.assertEqual(result["pipeline_retrospective"]["status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["health"], "healthy")
            self.assertEqual(result["pipeline_retrospective"]["publication_status"], "tracker-closed")
            self.assertEqual(result["pipeline_retrospective"]["tracker_status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["signals"], [])
            self.assertEqual(
                json.loads((result_path.parent / "retrospective.json").read_text(encoding="utf-8")),
                result["retrospective"],
            )
            self.assertEqual(
                json.loads((result_path.parent / "pipeline-retrospective.json").read_text(encoding="utf-8")),
                result["pipeline_retrospective"],
            )

    def test_workstream_records_disabled_retrospective_judge_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
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
            self.assertEqual(result["pipeline_retrospective"]["judge"], {"enabled": False, "status": "disabled"})

    def test_workstream_runs_retrospective_judge_with_redacted_evidence_pack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            judge_requests = temp_path / "judge-requests.json"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            judge_src = temp_path / "judge-src"
            judge_src.mkdir()
            judge_module = judge_src / "fake_retrospective_judge.py"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            judge_module.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    from pathlib import Path

                    def contains_key(value, target):
                        if isinstance(value, dict):
                            return target in value or any(contains_key(item, target) for item in value.values())
                        if isinstance(value, list):
                            return any(contains_key(item, target) for item in value)
                        return False

                    request = json.loads(Path(os.environ["AFK_RETROSPECTIVE_JUDGE_REQUEST"]).read_text(encoding="utf-8"))
                    evidence_pack = request["evidence_pack"]
                    assert "[REDACTED]" in evidence_pack["retrospective"]["summary"]
                    assert "[REDACTED]" in evidence_pack["retrospective"]["validation"][0]
                    assert "ghp_secret_merge_retrospective_1234567890" not in json.dumps(evidence_pack)
                    assert "super-secret-value" not in json.dumps(evidence_pack)
                    assert not contains_key(evidence_pack, "stdout_excerpt")
                    assert not contains_key(evidence_pack, "stderr_excerpt")
                    Path({str(judge_requests)!r}).write_text(json.dumps(request), encoding="utf-8")
                    Path(os.environ["AFK_RETROSPECTIVE_JUDGE_RESULT"]).write_text(
                        json.dumps({{"status": "pass", "summary": "judge accepted token=ghp_judge_secret_1234567890"}}),
                        encoding="utf-8",
                    )
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "fake-judge-command",
                "command": [sys.executable, "-m", "fake_retrospective_judge"],
                "timeout_seconds": 10,
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
                    "PYTHONPATH": f"{judge_src}{os.pathsep}{ROOT / 'src'}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            self.assertTrue(judge_requests.exists())
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["signals"], [])
            self.assertEqual(result["pipeline_retrospective"]["judge"]["enabled"], True)
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "passed")
            self.assertIn("[REDACTED]", result["pipeline_retrospective"]["judge"]["summary"])
            self.assertEqual(result["artifacts"]["retrospective_judge_evidence"], "retrospective-judge-evidence.json")
            self.assertEqual(result["artifacts"]["retrospective_judge_request"], "retrospective-judge-request.json")
            self.assertEqual(result["artifacts"]["retrospective_judge_result"], "retrospective-judge-result.json")
            self.assertEqual(result["artifacts"]["retrospective_judge_stdout"], "retrospective-judge-stdout.log")
            self.assertEqual(result["artifacts"]["retrospective_judge_stderr"], "retrospective-judge-stderr.log")
            run_dir = ledger / "workstreams" / summary["run_id"]
            evidence = json.loads((run_dir / "retrospective-judge-evidence.json").read_text(
                encoding="utf-8"
            ))
            judge_result = json.loads((run_dir / "retrospective-judge-result.json").read_text(
                encoding="utf-8"
            ))
            self.assertEqual(evidence["redaction"]["raw_logs_included"], False)
            self.assertEqual(judge_result["result"], result["pipeline_retrospective"]["judge"])
            for path_name in result["pipeline_retrospective"]["judge"]["evidence"].values():
                self.assertTrue((run_dir / path_name).is_file(), path_name)

    def test_workstream_passes_pi_auth_mounts_through_to_openai_codex_pi_retrospective_judge_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            pi_bin = temp_path / "pi"
            write_executable(
                pi_bin,
                f"""#!{sys.executable}
import json
import os
from pathlib import Path

assert os.environ["CODEX_HOME"] == {str(codex_home)!r}
assert os.environ["XDG_CONFIG_HOME"] == {str(config_home)!r}
assert os.environ["PI_CONFIG_HOME"] == {str(pi_config_home)!r}
assert os.environ["PI_CODING_AGENT_DIR"] == {str(pi_coding_agent_dir)!r}
Path(os.environ["AFK_RETROSPECTIVE_JUDGE_RESULT"]).write_text(
    json.dumps({{"status": "pass", "summary": "judge auth mounts available", "findings": []}}),
    encoding="utf-8",
)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
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
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "passed")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["summary"], "judge auth mounts available")

    def test_workstream_blocks_before_steps_when_pi_openai_codex_auth_preflight_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout_root = temp_path / "checkouts"
            checkout = checkout_root / "checkout"
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            fake_calls = temp_path / "pi-calls.jsonl"
            judge_marker = temp_path / "judge-ran.txt"
            leaked_secret = "ghp_preflight_secret_1234567890"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            fake_pi = temp_path / "pi"
            write_executable(
                fake_pi,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"argv": sys.argv[1:]}}) + "\\n"
)
print("No API key for provider: openai-codex {leaked_secret}", file=sys.stderr)
sys.exit(1)
""",
            )
            recipe = {
                "schema_version": 1,
                "workstream_id": "central-cknp",
                "parent": "central",
                "steps": [
                    {
                        "name": "select-work",
                        "input": {
                            "required_labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                            "sources": [
                                {
                                    "type": "fixture",
                                    "id": "fixture",
                                    "items": [selected_fixture_item("central-cknp")],
                                }
                            ],
                        },
                    },
                    {
                        "name": "prepare-checkout",
                        "input": {
                            "repo_url": str(repo),
                            "base_ref": "main",
                            "checkout_root": str(checkout_root),
                            "checkout_path": str(checkout),
                        },
                    },
                    {
                        "name": "implement",
                        "input": {
                            "guardrails": ["do not write secrets"],
                            "validation": {"profile": "tier1", "commands": []},
                            "agent": {
                                "type": "real-agent-command",
                                "command": [
                                    str(fake_pi),
                                    "-p",
                                    "{prompt}",
                                    "--provider",
                                    "openai-codex",
                                    "--model",
                                    "gpt-5.4-mini",
                                ],
                                "result_path": "agent-result.json",
                                "timeout_seconds": 10,
                                "codex_home": str(codex_home),
                                "config_home": str(config_home),
                                "env": {
                                    "PI_CONFIG_HOME": str(pi_config_home),
                                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                                },
                            },
                        },
                    },
                ],
                "retrospective_judge": {
                    "enabled": True,
                    "type": "local-command",
                    "command": [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(judge_marker)!r}).write_text('judge ran\\n', encoding='utf-8')",
                    ],
                    "timeout_seconds": 10,
                },
                "publisher": {"enabled": False},
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-cknp",
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
            preflight_path = result_path.parent / "pi-auth-preflight.json"
            preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
            artifact_text = "\n".join(
                [
                    result_path.read_text(encoding="utf-8"),
                    preflight_path.read_text(encoding="utf-8"),
                ]
            )
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["steps"], [])
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertIn("Pi auth preflight failed for implement.agent", result["publication"]["reason"])
            self.assertIn("No API key for provider: openai-codex", result["publication"]["reason"])
            self.assertEqual(result["artifacts"]["pi_auth_preflight"], "pi-auth-preflight.json")
            self.assertEqual(preflight["status"], "failed")
            self.assertEqual(preflight["results"][0]["target"], "implement.agent")
            self.assertIn("No API key for provider: openai-codex", preflight["results"][0]["summary"])
            self.assertEqual(result["pipeline_retrospective"]["judge"]["enabled"], True)
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "skipped")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["classification"], "auth_preflight_failed")
            self.assertIn("Pi auth preflight failed for implement.agent", result["pipeline_retrospective"]["judge"]["summary"])
            self.assertNotIn(leaked_secret, artifact_text)
            self.assertIn("[REDACTED]", artifact_text)
            self.assertEqual(len(calls), 1)
            self.assertFalse(checkout.exists())
            self.assertFalse(judge_marker.exists())

    def test_workstream_defers_checkout_local_shell_wrapped_pi_auth_preflight_until_checkout_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            fake_calls = temp_path / "checkout-local-pi-calls.jsonl"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            repo_bin = repo / "bin"
            repo_bin.mkdir()
            write_executable(
                repo_bin / "pi",
                f"""#!{sys.executable}
import json
import os
import subprocess
import sys
from pathlib import Path

calls_path = Path({str(fake_calls)!r})
calls_path.open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "preflight": os.environ.get("AFK_PI_AUTH_PREFLIGHT") == "1",
            "wrapper_mode": os.environ.get("PI_WRAPPER_MODE"),
        }}
    )
    + "\\n"
)
if os.environ.get("PI_WRAPPER_MODE") != "wrapped":
    raise SystemExit("missing wrapper mode")
if os.environ.get("AFK_PI_AUTH_PREFLIGHT") == "1":
    raise SystemExit(0)
Path("implemented.txt").write_text("central-lve.9\\n", encoding="utf-8")
subprocess.run(["git", "add", "implemented.txt"], check=True)
subprocess.run(["git", "commit", "-m", "implement central-lve.9"], check=True)
Path("agent-result.json").write_text(
    json.dumps({{"status": "completed", "summary": "checkout-local wrapped pi succeeded"}}),
    encoding="utf-8",
)
""",
            )
            git(repo, "add", "bin/pi")
            git(repo, "commit", "-m", "add checkout-local pi adapter")
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][2]["input"]["agent"] = {
                "type": "real-agent-command",
                "command": [
                    "bash",
                    "-lc",
                    "PI_WRAPPER_MODE=wrapped exec ./bin/pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini",
                ],
                "result_path": "agent-result.json",
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
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
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            run_dir = ledger / "workstreams" / summary["run_id"]
            preflight = json.loads((run_dir / "pi-auth-preflight.json").read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["steps"][2]["name"], "implement")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "disabled")
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0]["preflight"])
            self.assertFalse(calls[1]["preflight"])
            self.assertEqual(calls[0]["wrapper_mode"], "wrapped")
            self.assertEqual(calls[1]["wrapper_mode"], "wrapped")
            self.assertEqual(calls[0]["cwd"], str(checkout))
            self.assertEqual(calls[1]["cwd"], str(checkout))
            self.assertEqual(preflight["status"], "passed")
            self.assertEqual([item["status"] for item in preflight["results"]], ["deferred", "passed"])

    def test_workstream_does_not_retry_deferred_retrospective_judge_preflight_without_prepared_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"]["sources"][0]["items"] = []
            recipe["publisher"] = {"enabled": False}
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    "bash",
                    "-lc",
                    "exec ./bin/pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini",
                ],
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
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
            result = json.loads(result_path.read_text(encoding="utf-8"))
            preflight = json.loads((result_path.parent / "pi-auth-preflight.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["reason"], "select-work selected no work items")
            self.assertEqual(preflight["status"], "deferred")
            self.assertTrue(all(item["status"] != "failed" for item in preflight["results"]))
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "skipped")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["classification"], "checkout_unavailable")
            self.assertIn("no prepared checkout", result["pipeline_retrospective"]["judge"]["summary"])
            self.assertFalse(checkout.exists())

    def test_workstream_pi_auth_preflight_preserves_implement_wrapper_secret_metadata_in_job_capsule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            wrapper_secret_dir = temp_path / "runner-secrets"
            wrapper_secret_file = wrapper_secret_dir / "openai-api-key.txt"
            preflight_observation = temp_path / "preflight-observation.json"
            secret_refs = {
                "primary": {
                    "secretRef": {
                        "provider": "runner-local-files",
                        "name": "codex-auth",
                        "key": "openai_api_key",
                    }
                }
            }
            wrapper_secret = "ghp_wrapper_preflight_secret_1234567890"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            wrapper_secret_dir.mkdir()
            wrapper_secret_file.write_text(wrapper_secret + "\n", encoding="utf-8")
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            pi_bin = temp_path / "pi"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                pi_bin,
                f"""#!{sys.executable}
import json
import os
import subprocess
from pathlib import Path

capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
if os.environ.get("AFK_PI_AUTH_PREFLIGHT") == "1":
    Path({str(preflight_observation)!r}).write_text(
        json.dumps(
            {{
                "wrapper_secret_files": capsule["agent_mounts"]["wrapper_secret_files"],
                "secret_refs": capsule["agent_mounts"]["secret_refs"],
            }}
        ),
        encoding="utf-8",
    )
    raise SystemExit(0)
Path("implemented.txt").write_text("wrapper metadata preserved\\n", encoding="utf-8")
subprocess.run(["git", "add", "implemented.txt"], check=True)
subprocess.run(["git", "commit", "-m", "preserve preflight wrapper metadata"], check=True)
Path(os.environ["AFK_AGENT_RESULT_PATH"]).write_text(
    json.dumps({{"status": "completed", "summary": "wrapper metadata preserved"}}),
    encoding="utf-8",
)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][2]["input"]["agent"] = {
                "type": "real-agent-command",
                "command": [str(pi_bin), "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                "result_path": "agent-result.json",
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
                "wrapper_secret_files": {"primary": str(wrapper_secret_file)},
                "secret_refs": secret_refs,
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
            run_dir = ledger / "workstreams" / summary["run_id"]
            observation = json.loads(preflight_observation.read_text(encoding="utf-8"))
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in run_dir.iterdir()
                if path.is_file()
            )

            self.assertEqual(observation["wrapper_secret_files"], {"primary": str(wrapper_secret_file)})
            self.assertEqual(observation["secret_refs"], secret_refs)
            self.assertNotIn(wrapper_secret, artifact_text)

    def test_workstream_pi_auth_preflight_renders_reviewer_and_judge_path_placeholders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            pi_calls = temp_path / "pi-calls.jsonl"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            pi_bin = temp_path / "pi"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                pi_bin,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

if "AFK_REVIEWER_REQUEST" in os.environ:
    target = "reviewer"
    request_env = os.environ["AFK_REVIEWER_REQUEST"]
    result_env = os.environ["AFK_REVIEWER_RESULT"]
elif "AFK_RETROSPECTIVE_JUDGE_REQUEST" in os.environ:
    target = "retrospective_judge"
    request_env = os.environ["AFK_RETROSPECTIVE_JUDGE_REQUEST"]
    result_env = os.environ["AFK_RETROSPECTIVE_JUDGE_RESULT"]
else:
    raise SystemExit("unexpected pi target")

record = {{
    "target": target,
    "preflight": os.environ.get("AFK_PI_AUTH_PREFLIGHT") == "1",
    "request_in_argv": request_env in sys.argv[1:],
    "result_in_argv": result_env in sys.argv[1:],
    "placeholder_seen": any(
        "{{request_path}}" in arg or "{{result_path}}" in arg
        for arg in sys.argv[1:]
    ),
}}
Path({str(pi_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if record["placeholder_seen"] or not record["request_in_argv"] or not record["result_in_argv"]:
    raise SystemExit("preflight path placeholders were not rendered")
if record["preflight"]:
    raise SystemExit(0)
if target == "reviewer":
    Path(result_env).write_text(
        json.dumps({{"status": "pass", "summary": "review placeholders rendered", "findings": []}}),
        encoding="utf-8",
    )
else:
    Path(result_env).write_text(
        json.dumps({{"status": "pass", "summary": "judge placeholders rendered", "findings": []}}),
        encoding="utf-8",
    )
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][4]["input"]["reviewer"] = {
                "type": "fake-reviewer-command",
                "command": [
                    str(pi_bin),
                    "-p",
                    "{prompt}",
                    "--provider",
                    "openai-codex",
                    "--model",
                    "gpt-5.4-mini",
                    "{request_path}",
                    "{result_path}",
                ],
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            }
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    str(pi_bin),
                    "-p",
                    "{prompt}",
                    "--provider",
                    "openai-codex",
                    "--model",
                    "gpt-5.4-mini",
                    "{request_path}",
                    "{result_path}",
                ],
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
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
            calls = [json.loads(line) for line in pi_calls.read_text(encoding="utf-8").splitlines()]
            preflight_calls = [call for call in calls if call["preflight"]]

            self.assertEqual(result["status"], "closed")
            self.assertEqual([step["name"] for step in result["steps"]], ["select-work", "prepare-checkout", "implement", "validate", "review"])
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "passed")
            self.assertEqual({call["target"] for call in preflight_calls}, {"reviewer", "retrospective_judge"})
            self.assertEqual(sorted(call["target"] for call in calls), ["retrospective_judge", "retrospective_judge", "reviewer", "reviewer"])
            self.assertTrue(all(not call["placeholder_seen"] for call in calls))
            self.assertTrue(all(call["request_in_argv"] for call in calls))
            self.assertTrue(all(call["result_in_argv"] for call in calls))

    def test_workstream_rejects_openai_codex_pi_retrospective_judge_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": ["pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                "timeout_seconds": 10,
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

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("retrospective_judge.codex_home", completed.stderr)
            self.assertIn("pi --provider openai-codex", completed.stderr)

    def test_workstream_rejects_wrapped_openai_codex_pi_retrospective_judge_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            commands = [
                ["/usr/bin/env", "pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
                ["python3", "-m", "pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4-mini"],
            ]
            for command in commands:
                with self.subTest(command=command):
                    ledger = temp_path / f"ledger-{Path(command[0]).name}-{command[1].replace('/', '-')}"
                    recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
                    recipe["retrospective_judge"] = {
                        "enabled": True,
                        "type": "local-command",
                        "command": command,
                        "timeout_seconds": 10,
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

                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn("retrospective_judge.codex_home", completed.stderr)
                    self.assertIn("pi --provider openai-codex", completed.stderr)

    def test_workstream_rejects_shell_wrapped_openai_codex_pi_retrospective_judge_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": ["bash", "-lc", "pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini"],
                "timeout_seconds": 10,
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(temp_path / "ledger"),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("retrospective_judge.codex_home", completed.stderr)
            self.assertIn("pi --provider openai-codex", completed.stderr)

    def test_workstream_rejects_assignment_prefixed_shell_wrapped_openai_codex_pi_retrospective_judge_without_required_mounts(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    "bash",
                    "-lc",
                    "FOO=bar pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini",
                ],
                "timeout_seconds": 10,
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(temp_path / "ledger"),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("retrospective_judge.codex_home", completed.stderr)
            self.assertIn("pi --provider openai-codex", completed.stderr)

    def test_workstream_rejects_exec_and_split_string_wrapped_openai_codex_pi_retrospective_judge_without_required_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            commands = [
                ["bash", "-lc", "exec pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini"],
                ["/usr/bin/env", "--split-string=pi -p '{prompt}' --provider openai-codex --model gpt-5.4-mini"],
            ]
            for command in commands:
                with self.subTest(command=command):
                    ledger = temp_path / f"ledger-{Path(command[0]).name}"
                    recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
                    recipe["retrospective_judge"] = {
                        "enabled": True,
                        "type": "local-command",
                        "command": command,
                        "timeout_seconds": 10,
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

                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn("retrospective_judge.codex_home", completed.stderr)
                    self.assertIn("pi --provider openai-codex", completed.stderr)

    def test_workstream_rejects_non_openai_pi_retrospective_judge_mounts_for_direct_entry_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            commands = [
                ["/usr/bin/env", "pi", "-p", "{prompt}", "--provider", "anthropic", "--model", "gpt-5.4-mini"],
                ["python3", "-m", "pi", "-p", "{prompt}", "--provider", "anthropic", "--model", "gpt-5.4-mini"],
            ]
            for command in commands:
                with self.subTest(command=command):
                    ledger = temp_path / f"ledger-non-openai-{Path(command[0]).name}-{command[1].replace('/', '-')}"
                    recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
                    recipe["retrospective_judge"] = {
                        "enabled": True,
                        "type": "local-command",
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

                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn("retrospective_judge.codex_home", completed.stderr)
                    self.assertIn(
                        "only supported when retrospective_judge.command uses pi --provider openai-codex",
                        completed.stderr,
                    )

    def test_workstream_rejects_unknown_provider_pi_retrospective_judge_mounts_for_direct_entry_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
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
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(temp_path / "ledger"),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("retrospective_judge.codex_home", completed.stderr)
            self.assertIn("provider could not be determined", completed.stderr)

    def test_workstream_rejects_non_pi_retrospective_judge_mounts_for_direct_entry_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [sys.executable, "-c", "raise SystemExit('judge should not run')"],
                "timeout_seconds": 10,
                "codex_home": str(codex_home),
                "config_home": str(config_home),
                "env": {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(temp_path / "ledger"),
                env_overrides={
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("retrospective_judge.codex_home", completed.stderr)
            self.assertIn(
                "only supported when retrospective_judge.command uses pi --provider openai-codex",
                completed.stderr,
            )

    def test_workstream_substitutes_prompt_for_retrospective_judge_local_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            judge_secret = "ghp_secret_judge_1234567890"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective"]["summary"] = f"Merged after validating token={judge_secret} cleanup."
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        f"""
                        import json
                        import os
                        import sys
                        from pathlib import Path

                        prompt = sys.argv[1]
                        request_path_arg = sys.argv[2]
                        result_path_arg = sys.argv[3]
                        request = json.loads(prompt)
                        if request["artifact_type"] != "retrospective-judge-request":
                            raise SystemExit("unexpected judge request payload")
                        Path(request_path_arg).write_text(json.dumps(request), encoding="utf-8")
                        print(request["evidence_pack"]["retrospective"]["summary"])
                        print(request_path_arg)
                        print(result_path_arg)
                        Path(os.environ["AFK_RETROSPECTIVE_JUDGE_RESULT"]).write_text(
                            json.dumps({{"status": "pass", "summary": "judge accepted review prompt"}}),
                            encoding="utf-8",
                        )
                        """
                    ).strip(),
                    "{prompt}",
                    "{request_path}",
                    "{result_path}",
                ],
                "timeout_seconds": 10,
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
            run_dir = ledger / "workstreams" / summary["run_id"]
            judge_request = json.loads((run_dir / "retrospective-judge-request.json").read_text(encoding="utf-8"))

            self.assertEqual(judge_request["artifact_type"], "retrospective-judge-request")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "passed")
            stdout_log = (run_dir / "retrospective-judge-stdout.log").read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", stdout_log)
            self.assertNotIn("ghp_secret_merge_retrospective_1234567890", stdout_log)
            self.assertNotIn("ghp_secret_judge_1234567890", stdout_log)

    def test_workstream_accepts_retrospective_judge_json_from_stdout_when_result_file_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        import json
                        print(json.dumps({
                            "status": "fail",
                            "summary": "implementation timed out before publication",
                            "findings": [
                                {
                                    "scope": "pipeline_retrospective",
                                    "severity": "error",
                                    "summary": "implement did not reach implemented",
                                    "evidence": ["implement failed_runtime"],
                                    "next_action": "rerun with a longer implementation timeout",
                                }
                            ],
                        }))
                        """
                    ),
                ],
                "timeout_seconds": 10,
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
            run_dir = ledger / "workstreams" / summary["run_id"]

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "failed")
            self.assertEqual(
                result["pipeline_retrospective"]["judge"]["summary"],
                "implementation timed out before publication",
            )
            self.assertEqual(
                result["pipeline_retrospective"]["judge"]["findings"][0]["next_action"],
                "rerun with a longer implementation timeout",
            )
            judge_result = json.loads((run_dir / "retrospective-judge-result.json").read_text(encoding="utf-8"))
            self.assertEqual(judge_result["result"], result["pipeline_retrospective"]["judge"])
            self.assertIn(
                "implementation timed out before publication",
                (run_dir / "retrospective-judge-stdout.log").read_text(encoding="utf-8"),
            )

    def test_workstream_reports_invalid_retrospective_judge_stdout_as_protocol_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [sys.executable, "-c", "print('progress log before result')"],
                "timeout_seconds": 10,
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
            run_dir = ledger / "workstreams" / summary["run_id"]

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "failed_protocol")
            self.assertEqual(
                result["pipeline_retrospective"]["judge"]["summary"],
                "retrospective judge result stdout is not valid JSON",
            )
            self.assertIn(
                "progress log before result",
                (run_dir / "retrospective-judge-stdout.log").read_text(encoding="utf-8"),
            )

    def test_workstream_prefers_retrospective_judge_result_file_over_stdout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        import json
                        import os
                        from pathlib import Path
                        Path(os.environ["AFK_RETROSPECTIVE_JUDGE_RESULT"]).write_text(
                            json.dumps({"status": "pass", "summary": "file result wins"}),
                            encoding="utf-8",
                        )
                        print("stdout should not be parsed")
                        """
                    ),
                ],
                "timeout_seconds": 10,
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
            run_dir = ledger / "workstreams" / summary["run_id"]

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "passed")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["summary"], "file result wins")
            self.assertIn(
                "stdout should not be parsed",
                (run_dir / "retrospective-judge-stdout.log").read_text(encoding="utf-8"),
            )

    def test_workstream_preserves_retrospective_judge_prompt_placeholders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective"]["summary"] = "Keep marker {request_path} and {result_path} literal."
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        import json
                        import os
                        import sys
                        from pathlib import Path

                        request = json.loads(sys.argv[1])
                        summary = request["evidence_pack"]["retrospective"]["summary"]
                        request_path_marker = "{" + "request_path}"
                        result_path_marker = "{" + "result_path}"
                        if request_path_marker not in summary or result_path_marker not in summary:
                            raise SystemExit("retrospective prompt markers were rewritten")
                        if request["artifact_type"] != "retrospective-judge-request":
                            raise SystemExit("unexpected judge request payload")
                        Path(os.environ["AFK_RETROSPECTIVE_JUDGE_RESULT"]).write_text(
                            json.dumps({"status": "pass", "summary": "prompt placeholders preserved"}),
                            encoding="utf-8",
                        )
                        """
                    ).strip(),
                    "{prompt}",
                    "{request_path}",
                    "{result_path}",
                ],
                "timeout_seconds": 10,
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

            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "passed")

    def test_workstream_uses_strict_selected_work_redaction_for_retrospective_judge_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            selected_work_secret = "selected-work-non-detectable-12345"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"]["sources"][0]["items"][0]["secret_note"] = selected_work_secret
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        import json
                        import os
                        import sys
                        from pathlib import Path

                        print(sys.argv[1])
                        Path(os.environ["AFK_RETROSPECTIVE_JUDGE_RESULT"]).write_text(
                            json.dumps({"status": "pass", "summary": "judge summary from prompt"}),
                            encoding="utf-8",
                        )
                        """
                    ).strip(),
                    "{prompt}",
                    "{request_path}",
                    "{result_path}",
                ],
                "timeout_seconds": 10,
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
            run_dir = ledger / "workstreams" / summary["run_id"]
            stdout_log = (run_dir / "retrospective-judge-stdout.log").read_text(encoding="utf-8")

            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "passed")
            self.assertNotIn(selected_work_secret, stdout_log)
            self.assertIn("central-lve.9", stdout_log)

    def test_workstream_records_retrospective_judge_failure_without_changing_publication_or_tracker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [sys.executable, "-c", "import sys; print('judge failed'); raise SystemExit(7)"],
                "timeout_seconds": 10,
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

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(result["tracker"]["status"], "awaiting-review")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["enabled"], True)
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "failed")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["adapter"]["returncode"], 7)
            self.assertEqual(result["pipeline_retrospective"]["signals"][0]["kind"], "retrospective-judge")
            self.assertEqual(result["pipeline_retrospective"]["signals"][0]["severity"], "error")
            self.assertEqual(result["artifacts"]["retrospective_judge_result"], "retrospective-judge-result.json")
            self.assertEqual(result["artifacts"]["retrospective_judge_stdout"], "retrospective-judge-stdout.log")
            self.assertEqual(result["artifacts"]["retrospective_judge_stderr"], "retrospective-judge-stderr.log")
            run_dir = ledger / "workstreams" / summary["run_id"]
            for path_name in result["pipeline_retrospective"]["signals"][0]["evidence_paths"]:
                self.assertTrue((run_dir / path_name).is_file(), path_name)

    def test_workstream_records_non_utf8_retrospective_judge_output_as_protocol_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(temp_path / "fake-git-calls.jsonl")!r}).write_text(json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            judge_code = (
                "import os, sys; "
                "from pathlib import Path; "
                "sys.stdout.buffer.write(bytes([255])); "
                "Path(os.environ['AFK_RETROSPECTIVE_JUDGE_RESULT']).write_bytes(bytes([255]))"
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [sys.executable, "-c", judge_code],
                "timeout_seconds": 10,
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

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "failed_protocol")
            self.assertEqual(
                result["pipeline_retrospective"]["judge"]["summary"],
                "retrospective judge result file is not valid JSON",
            )
            self.assertEqual(result["artifacts"]["retrospective_judge_stdout"], "retrospective-judge-stdout.log")
            self.assertEqual(result["artifacts"]["retrospective_judge_stderr"], "retrospective-judge-stderr.log")

    def test_workstream_runs_retrospective_follow_up_creator_with_redacted_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            follow_up_requests = temp_path / "follow-up-requests.json"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            creator_src = temp_path / "creator-src"
            creator_src.mkdir()
            creator_module = creator_src / "fake_retrospective_follow_up.py"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective"]["follow_up"] = {
                "recommended": [
                    {
                        "summary": "Capture token=ghp_follow_up_secret_1234567890 remediation notes.",
                        "labels": ["area:retro"],
                    },
                    {
                        "summary": "Capture token=ghp_follow_up_secret_9999999999 remediation notes.",
                        "labels": ["area:retro"],
                    },
                ],
                "created": [{"id": "central-4x9.44"}],
            }
            creator_module.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    from pathlib import Path

                    request = json.loads(Path(os.environ["AFK_RETROSPECTIVE_FOLLOW_UP_REQUEST"]).read_text(encoding="utf-8"))
                    recommended = request["follow_up"]["recommended"]
                    assert len(recommended) == 1
                    assert "[REDACTED]" in recommended[0]["summary"]
                    assert "ghp_follow_up_secret_1234567890" not in json.dumps(request)
                    Path({str(follow_up_requests)!r}).write_text(json.dumps(request), encoding="utf-8")
                    Path(os.environ["AFK_RETROSPECTIVE_FOLLOW_UP_RESULT"]).write_text(
                        json.dumps(
                            {{
                                "status": "created",
                                "summary": "created token=ghp_creator_secret_1234567890",
                                "created": [
                                    {{
                                        "id": "central-4x9.44",
                                        "kind": recommended[0]["kind"],
                                        "fingerprint": "retro-follow-up:stale",
                                        "summary": recommended[0]["summary"],
                                    }}
                                ],
                            }}
                        ),
                        encoding="utf-8",
                    )
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "fake-follow-up-command",
                "command": [sys.executable, "-m", "fake_retrospective_follow_up"],
                "timeout_seconds": 10,
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
                    "PYTHONPATH": f"{creator_src}{os.pathsep}{ROOT / 'src'}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            self.assertTrue(follow_up_requests.exists())
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["enabled"], True)
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["status"], "created")
            self.assertIn("[REDACTED]", result["pipeline_retrospective"]["follow_up"]["creation"]["summary"])
            self.assertEqual(
                result["pipeline_retrospective"]["follow_up"]["created"][0]["id"],
                "central-4x9.44",
            )
            self.assertEqual(len(result["pipeline_retrospective"]["follow_up"]["created"]), 1)
            self.assertEqual(result["artifacts"]["retrospective_follow_up_request"], "retrospective-follow-up-request.json")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_result"], "retrospective-follow-up-result.json")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_stdout"], "retrospective-follow-up-stdout.log")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_stderr"], "retrospective-follow-up-stderr.log")
            run_dir = ledger / "workstreams" / summary["run_id"]
            request = json.loads((run_dir / "retrospective-follow-up-request.json").read_text(encoding="utf-8"))
            creation_result = json.loads((run_dir / "retrospective-follow-up-result.json").read_text(encoding="utf-8"))
            self.assertEqual(len(request["follow_up"]["recommended"]), 1)
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["recommended"], [])
            self.assertEqual(
                result["pipeline_retrospective"]["follow_up"]["created"][0]["fingerprint"],
                request["follow_up"]["recommended"][0]["fingerprint"],
            )
            self.assertEqual(
                creation_result["result"]["created"],
                result["pipeline_retrospective"]["follow_up"]["created"],
            )
            self.assertEqual(
                {key: value for key, value in creation_result["result"].items() if key != "created"},
                result["pipeline_retrospective"]["follow_up"]["creation"],
            )

    def test_workstream_preserves_follow_up_prompt_placeholder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective"]["follow_up"] = {
                "recommended": [
                    {
                        "summary": "Track follow-up placeholder handling.",
                        "labels": ["area:retro"],
                    }
                ]
            }
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        import json
                        import os
                        import sys
                        from pathlib import Path

                        if sys.argv[1] != "{prompt}":
                            raise SystemExit("follow-up prompt placeholder was not preserved")
                        Path(os.environ["AFK_RETROSPECTIVE_FOLLOW_UP_RESULT"]).write_text(
                            json.dumps(
                                {
                                    "status": "created",
                                    "summary": "follow-up command accepted prompt placeholder",
                                    "created": [],
                                }
                            ),
                            encoding="utf-8",
                        )
                        """
                    ).strip(),
                    "{prompt}",
                    "{request_path}",
                    "{result_path}",
                ],
                "timeout_seconds": 10,
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

            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["status"], "created")

    def test_workstream_records_retrospective_follow_up_creation_failure_without_changing_functional_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective"]["follow_up"] = {
                "recommended": [
                    {
                        "summary": "Document retrospective follow-up capture.",
                        "labels": ["area:retro"],
                    }
                ]
            }
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [sys.executable, "-c", "import sys; print('create failed'); raise SystemExit(7)"],
                "timeout_seconds": 10,
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

            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["enabled"], True)
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["status"], "failed")
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["adapter"]["returncode"], 7)
            self.assertEqual(result["artifacts"]["retrospective_follow_up_result"], "retrospective-follow-up-result.json")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_stdout"], "retrospective-follow-up-stdout.log")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_stderr"], "retrospective-follow-up-stderr.log")

    def test_workstream_records_retrospective_follow_up_timeout_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            timeout_code = "import sys, time; sys.stdout.buffer.write(b'before-timeout-\\xff'); sys.stdout.flush(); time.sleep(2)"
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective"]["follow_up"] = {
                "recommended": [
                    {
                        "summary": "Document retrospective follow-up capture.",
                        "labels": ["area:retro"],
                    }
                ]
            }
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [sys.executable, "-c", timeout_code],
                "timeout_seconds": 0.1,
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
            run_dir = ledger / "workstreams" / summary["run_id"]

            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["status"], "failed")
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["creation"]["adapter"]["timed_out"], True)
            self.assertIn(
                "before-timeout-",
                (run_dir / "retrospective-follow-up-stdout.log").read_text(encoding="utf-8"),
            )

    def test_workstream_records_enabled_retrospective_follow_up_skip_artifacts_without_recommendations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            creator_ran = temp_path / "creator-ran.txt"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(creator_ran)!r}).write_text('ran', encoding='utf-8')",
                ],
                "timeout_seconds": 10,
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
            self.assertFalse(creator_ran.exists())
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            creation = result["pipeline_retrospective"]["follow_up"]["creation"]

            self.assertEqual(result["status"], "closed")
            self.assertEqual(creation["enabled"], True)
            self.assertEqual(creation["status"], "skipped")
            self.assertEqual(creation["classification"], "no_recommendations")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_request"], "retrospective-follow-up-request.json")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_result"], "retrospective-follow-up-result.json")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_stdout"], "retrospective-follow-up-stdout.log")
            self.assertEqual(result["artifacts"]["retrospective_follow_up_stderr"], "retrospective-follow-up-stderr.log")

    def test_workstream_terminal_no_merge_decision_closes_tracker_without_republishing(self):
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
Path({str(fake_calls)!r}).write_text("publisher git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("publisher gh should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "no-merge",
                    "reason": "Superseded by follow-up PR",
                    "pr_url": "https://github.example/pr/123",
                }
            }
            recipe["retrospective"] = {
                "summary": "No-merge after reviewing token=ghp_no_merge_cli_secret_1234567890 supersession.",
                "changes": ["Recorded why this PR will not merge."],
                "validation": ["Validation stayed green before the no-merge decision."],
                "review": ["Review passed; superseding work made the branch unnecessary."],
                "unresolved_risks": ["Superseding work still needs follow-up tracking."],
                "process_findings": ["No-merge decisions should still leave retrospective evidence."],
                "follow_up": {
                    "recommended": [
                        {
                            "id": "central-3x6.8",
                            "summary": "Track the superseding PR.",
                            "labels": ["project:afk-composable-pipeline"],
                        }
                    ],
                },
                "notes": {
                    "personal_work": [
                        "~/Documents/rmd/Ceremonies/Personal Work/work/2026-06-27-personal.md",
                    ],
                    "spikes": [
                        "~/Documents/rmd/Ceremonies/Personal Work/spikes/2026-06-27-no-merge.md",
                    ],
                },
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
            result = json.loads(result_path.read_text(encoding="utf-8"))
            tracker = json.loads((result_path.parent / "tracker-result.json").read_text(encoding="utf-8"))

            self.assertFalse(fake_calls.exists())
            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(
                result["tracker"]["close_reason"],
                "Superseded by follow-up PR",
            )
            self.assertEqual(result["tracker"]["pr_url"], "https://github.example/pr/123")
            self.assertEqual([item["result"] for item in result["selected_work"]], ["passed"])
            self.assertEqual(result["retrospective"], tracker["retrospective"])
            self.assertEqual(result["tracker"]["retrospective"], tracker["retrospective"])
            self.assertEqual(result["artifacts"]["retrospective"], "retrospective.json")
            self.assertEqual(result["artifacts"]["pipeline_retrospective"], "pipeline-retrospective.json")
            self.assertIn("[REDACTED]", result["retrospective"]["summary"])
            self.assertEqual(
                result["retrospective"]["follow_up"]["recommended"][0]["labels"],
                ["project:afk-composable-pipeline"],
            )
            self.assertEqual(
                result["retrospective"]["notes"]["spikes"],
                ["~/Documents/rmd/Ceremonies/Personal Work/spikes/2026-06-27-no-merge.md"],
            )
            self.assertEqual(result["pipeline_retrospective"]["status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["health"], "healthy")
            self.assertEqual(result["pipeline_retrospective"]["publication_status"], "tracker-closed")
            self.assertEqual(result["pipeline_retrospective"]["tracker_status"], "closed")
            self.assertEqual(result["pipeline_retrospective"]["signals"], [])
            self.assertEqual(
                json.loads((result_path.parent / "retrospective.json").read_text(encoding="utf-8")),
                result["retrospective"],
            )
            self.assertEqual(
                json.loads((result_path.parent / "pipeline-retrospective.json").read_text(encoding="utf-8")),
                result["pipeline_retrospective"],
            )

    def test_workstream_terminal_merge_decision_stays_open_when_review_feedback_is_unresolved(self):
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
Path({str(fake_calls)!r}).write_text("publisher git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("publisher gh should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["review_cycles"] = [
                {
                    "status": "request-changes",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "request-changes",
                            "summary": "Please address the review findings before merge.",
                            "requires_response": True,
                        }
                    ],
                }
            ]
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                }
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

            self.assertFalse(fake_calls.exists())
            self.assertEqual(summary["status"], "review-findings-open")
            self.assertEqual(result["publication"]["status"], "tracker-close-blocked")
            self.assertIn("review_feedback_status", result["publication"]["reason"])
            self.assertEqual(result["tracker"]["status"], "review-findings-open")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["pr_url"], "https://github.example/pr/123")

    def test_workstream_terminal_merge_decision_closes_when_review_feedback_is_explicitly_resolved(self):
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
Path({str(fake_calls)!r}).write_text("publisher git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("publisher gh should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["review_cycles"] = [
                {
                    "status": "request-changes",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "request-changes",
                            "summary": "Please address the review findings before merge.",
                            "requires_response": True,
                        }
                    ],
                }
            ]
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                    "review_feedback_status": "resolved",
                }
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

            self.assertFalse(fake_calls.exists())
            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(
                result["tracker"]["close_reason"],
                "merged via deadbeef",
            )
            self.assertIn("resolved before closure", result["tracker"]["comment"])

    def test_workstream_terminal_merge_decision_ignores_feedback_resolution_text_without_response_required_cycles(self):
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
Path({str(fake_calls)!r}).write_text("publisher git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("publisher gh should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                    "review_feedback_status": "resolved",
                }
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

            self.assertFalse(fake_calls.exists())
            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["close_reason"], "merged via deadbeef")
            self.assertEqual(
                result["tracker"]["comment"],
                "PR merged; close the source Beads item with the recorded merge commit.",
            )

    def test_workstream_accepts_empty_tracker_terminal_decision_as_unset(self):
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
import sys
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "git", "argv": sys.argv[1:]}}) + "\\n")
sys.exit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "gh", "argv": sys.argv[1:]}}) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("https://github.example/pr/123")
sys.exit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {"terminal_decision": {}}

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

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["tracker"]["status"], "awaiting-review")
            self.assertFalse(result["tracker"]["close_source_item"])

    def test_workstream_rejects_terminal_decision_without_pr_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(0)
""",
            )

            for status, extra_fields, expected_error in (
                (
                    "merged",
                    {"merge_commit": "deadbeef"},
                    "tracker.terminal_decision.pr_url is required for merged",
                ),
                (
                    "no-merge",
                    {"reason": "Superseded by follow-up PR", "pr_url": "   "},
                    "tracker.terminal_decision.pr_url is required for no-merge",
                ),
            ):
                with self.subTest(status=status):
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    recipe["tracker"] = {"terminal_decision": {"status": status, **extra_fields}}

                    completed = run_afk(
                        "run-workstream",
                        "--workstream-id",
                        "central-lve.9",
                        "--input",
                        json.dumps(recipe),
                        "--ledger",
                        str(ledger),
                    )

                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(expected_error, completed.stderr)

    def test_workstream_rejects_invalid_terminal_review_feedback_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                    "review_feedback_status": "done",
                }
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "tracker.terminal_decision.review_feedback_status must be one of: resolved, waived",
                completed.stderr,
            )

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

    def test_workstream_publisher_uses_absolute_pr_body_path_when_relative_ledger_runs_from_different_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            checkout = temp_path / "checkout"
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            checkout.mkdir()
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "gh",
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body_path"] = body_file
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("https://github.example/pr/456")
sys.exit(0)
""",
            )
            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": "afk/workstream-terminal-pr",
            }
            state = {
                "checkout": {"status": "prepared", "checkout_path": str(checkout)},
                "implementation": {
                    "git": {
                        "changed_files": ["implemented.txt"],
                        "commits": [{"commit": "abc1234", "subject": "implement central-lve.9"}],
                    }
                },
                "validations": [
                    {
                        "output": {
                            "status": "validated",
                            "summary": "validated",
                            "worker_result": {
                                "raw": {"status": "pass", "steps": [{"name": "unit", "status": "pass"}]},
                                "normalized": {
                                    "summary": "validated",
                                    "adapter": {"command": [sys.executable, "-c", "print('ok')"]},
                                },
                            },
                        },
                        "step_result_path": "runs/validate/step-result.json",
                        "worker_result_path": "runs/validate/worker-result.json",
                    }
                ],
                "review": {"status": "passed", "summary": "ready for PR"},
                "cleanup": {"status": "clean", "resources": []},
                "implementation_selection": [],
                "review_selection": [],
            }
            steps = [{"name": "validate", "result_path": "runs/validate/step-result.json"}]
            selected_work = [{**selected_fixture_item(), "result": "passed"}]
            old_cwd = Path.cwd()
            try:
                os.chdir(runner)
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-success")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": "afk/workstream-terminal-pr",
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "gh": {
                            "path": str(fake_gh),
                            "auth": {"config_dir": str(mounted_gh_config)},
                        },
                    },
                    normalized=normalized,
                    state=state,
                    steps=steps,
                    selected_work=selected_work,
                    ledger=ledger,
                )
            finally:
                os.chdir(old_cwd)

            publication_path = runner / ledger_arg / "workstreams" / ledger.run_id / "publication-result.json"
            publication_text = json.dumps(result)
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            expected_body_path = (runner / ledger_arg / "workstreams" / ledger.run_id / "pr-body.md").resolve()
            pr_call = next(call for call in calls if call["tool"] == "gh" and call["argv"][0:2] == ["pr", "create"])
            command = result["commands"]["gh"]

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["auth"]["path"], "[REDACTED]")
            self.assertEqual(pr_call["cwd"], str(checkout))
            self.assertEqual(pr_call["gh_config_dir"], str(mounted_gh_config))
            self.assertEqual(Path(pr_call["body_path"]), expected_body_path)
            self.assertTrue(Path(pr_call["body_path"]).is_absolute())
            self.assertEqual(command[command.index("--body-file") + 1], str(expected_body_path))
            self.assertIn(str(expected_body_path), publication_text)
            self.assertNotIn(str(mounted_gh_config), publication_text)
            self.assertTrue(publication_path.parent.joinpath("pr-body.md").is_file())

    def test_workstream_failed_publication_keeps_retry_guidance_neutral_in_pr_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            checkout = temp_path / "checkout"
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            checkout.mkdir()
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "gh",
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body_path"] = body_file
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("publisher create failed", file=sys.stderr)
sys.exit(11)
""",
            )
            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": "afk/workstream-terminal-pr",
            }
            state = {
                "checkout": {"status": "prepared", "checkout_path": str(checkout)},
                "implementation": {
                    "git": {
                        "changed_files": ["implemented.txt"],
                        "commits": [{"commit": "abc1234", "subject": "implement central-lve.9"}],
                    }
                },
                "validations": [
                    {
                        "output": {
                            "status": "validated",
                            "summary": "validated",
                            "worker_result": {
                                "raw": {"status": "pass", "steps": [{"name": "unit", "status": "pass"}]},
                                "normalized": {
                                    "summary": "validated",
                                    "adapter": {"command": [sys.executable, "-c", "print('ok')"]},
                                },
                            },
                        },
                        "step_result_path": "runs/validate/step-result.json",
                        "worker_result_path": "runs/validate/worker-result.json",
                    }
                ],
                "review": {"status": "passed", "summary": "ready for PR"},
                "cleanup": {"status": "clean", "resources": []},
                "implementation_selection": [],
                "review_selection": [],
            }
            steps = [{"name": "validate", "result_path": "runs/validate/step-result.json"}]
            selected_work = [{**selected_fixture_item(), "result": "passed"}]
            old_cwd = Path.cwd()
            try:
                os.chdir(runner)
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-failure")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": "afk/workstream-terminal-pr",
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "gh": {
                            "path": str(fake_gh),
                            "auth": {"config_dir": str(mounted_gh_config)},
                        },
                    },
                    normalized=normalized,
                    state=state,
                    steps=steps,
                    selected_work=selected_work,
                    ledger=ledger,
                )
            finally:
                os.chdir(old_cwd)

            workstream_dir = runner / ledger_arg / "workstreams" / ledger.run_id
            publication_text = json.dumps(result)
            pr_body = workstream_dir.joinpath("pr-body.md").read_text(encoding="utf-8")
            calls = [
                json.loads(line)
                for line in fake_calls.read_text(encoding="utf-8").splitlines()
            ]

            expected_body_path = workstream_dir.joinpath("pr-body.md").resolve()
            pr_call = next(call for call in calls if call["tool"] == "gh" and call["argv"][0:2] == ["pr", "create"])
            command = result["command"]

            self.assertEqual(result["status"], "failed-needs-human")
            self.assertIn("afk run-workstream", result["retry"])
            self.assertEqual(Path(pr_call["body_path"]), expected_body_path)
            self.assertTrue(Path(pr_call["body_path"]).is_absolute())
            self.assertEqual(command[command.index("--body-file") + 1], str(expected_body_path))
            self.assertIn(str(expected_body_path), publication_text)
            self.assertNotIn(str(mounted_gh_config), publication_text)
            self.assertNotIn("Retry: not required after successful publication", pr_body)
            self.assertIn("Retry: rerun the workstream if terminal publication fails", pr_body)

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
            self.assertEqual(result["selected_work"][0]["result"], "blocked")
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
            self.assertEqual(result["selected_work"][0]["result"], "blocked")
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
                import sys

                print("python3.13: command not found")
                sys.exit(127)
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

    def test_workstream_keeps_tracker_open_when_validation_blocks_publication_despite_terminal_decision(self):
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
                import sys

                print("python3.13: command not found")
                sys.exit(127)
                """
            ).strip()
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                }
            }

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
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(result["tracker"]["status"], "implemented")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["close_reason"], "")
            self.assertEqual(result["tracker"]["pr_url"], "https://github.example/pr/123")
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

    def test_workstream_retry_reuses_implemented_review_branch_after_validation_failure(self):
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
            passing_worker_code = textwrap.dedent(
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
                            "summary": "unit tests passed",
                            "steps": [{"name": "unit", "status": "pass"}],
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
                    json.dumps({"status": "completed", "summary": "retry implementation left cleanup evidence"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["steps"] = recipe["steps"][:4] + [
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
                            "command": [sys.executable, "-c", retry_agent_code],
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
                            "command": [sys.executable, "-c", passing_worker_code],
                            "timeout_seconds": 10,
                        },
                    },
                },
                recipe["steps"][4],
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

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["status"], "published")
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
                    "review",
                ],
            )
            first_validate = json.loads(
                (ledger / result["steps"][3]["result_path"]).read_text(encoding="utf-8")
            )["output"]
            retried_checkout = json.loads(
                (ledger / result["steps"][4]["result_path"]).read_text(encoding="utf-8")
            )["output"]
            retry_implementation = json.loads(
                (ledger / result["steps"][5]["result_path"]).read_text(encoding="utf-8")
            )["output"]
            second_validate = json.loads(
                (ledger / result["steps"][6]["result_path"]).read_text(encoding="utf-8")
            )["output"]
            review_result = json.loads(
                (ledger / result["steps"][7]["result_path"]).read_text(encoding="utf-8")
            )["output"]

            self.assertEqual(first_validate["status"], "failed_validation")
            self.assertEqual(retried_checkout["status"], "prepared")
            self.assertEqual(retry_implementation["status"], "implemented")
            self.assertTrue(retry_implementation["git"]["dirty"])
            self.assertEqual(second_validate["status"], "validated")
            self.assertEqual(review_result["status"], "passed")
            self.assertEqual(
                retry_implementation["git"]["after_commit"],
                second_validate["checkout"]["start_commit"],
            )
            self.assertEqual(
                retry_implementation["git"]["after_commit"],
                second_validate["checkout"]["requested_ref"],
            )
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(result["publication"]["url"], "https://github.example/pr/123")
            self.assertEqual(result["tracker"]["status"], "awaiting-review")
            self.assertEqual(result["cleanup"]["status"], "dirty_retry_checkouts")
            self.assertEqual(result["pipeline_retrospective"]["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["health"], "warning")
            self.assertEqual(result["pipeline_retrospective"]["publication_status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["tracker_status"], "awaiting-review")
            self.assertEqual(
                result["pipeline_retrospective"]["signals"][0]["kind"],
                "dirty-cleanup",
            )
            self.assertEqual(result["pipeline_retrospective"]["signals"][0]["severity"], "warning")
            self.assertEqual(
                json.loads((ledger / "workstreams" / summary["run_id"] / "pipeline-retrospective.json").read_text(
                    encoding="utf-8"
                )),
                result["pipeline_retrospective"],
            )

    def test_workstream_retry_respects_explicit_prepare_requested_ref_for_same_checkout_path(self):
        state = {
            "selected_work": [],
            "checkout": {
                "status": "prepared",
                "checkout_path": "/work/checkout",
                "start_commit": "1111111111111111111111111111111111111111",
                "requested_ref": "1111111111111111111111111111111111111111",
            },
            "checkout_attempts": [],
            "implementation": {
                "status": "implemented",
                "git": {"after_commit": "2222222222222222222222222222222222222222"},
            },
            "validations": [],
            "review": None,
        }
        step_input = composed_step_input(
            {
                "name": "prepare-checkout",
                "input": {
                    "checkout_path": "/work/checkout",
                    "requested_ref": "main",
                },
            },
            {"review_branch": "afk/test"},
            state,
            Path("/ledger"),
        )

        self.assertEqual(step_input["requested_ref"], "main")

    def test_workstream_failed_retry_prepare_preserves_current_validation_and_review_state(self):
        state = {
            "selected_work": [],
            "checkout": {
                "status": "prepared",
                "checkout_path": "/work/checkout",
                "start_commit": "1111111111111111111111111111111111111111",
            },
            "checkout_attempts": [],
            "implementation": {
                "status": "implemented",
                "git": {"after_commit": "1111111111111111111111111111111111111111"},
            },
            "validations": [{"output": {"status": "validated"}}],
            "review": {"status": "request_revision"},
            "cleanup": {"status": "unknown", "resources": []},
        }
        failed_prepare = {
            "status": "failed_existing_branch",
            "checkout_path": "/work/checkout",
            "review_branch": "afk/test",
            "dirty": False,
        }

        update_state_from_step(
            state,
            "prepare-checkout",
            StepResult(
                run_id="retry",
                step="prepare-checkout",
                status="succeeded",
                output=failed_prepare,
                stdout="",
                stderr="",
                result_sha256="",
            ),
            Path("/ledger"),
        )

        self.assertEqual(state["checkout"]["status"], "prepared")
        self.assertEqual(state["validations"], [{"output": {"status": "validated"}}])
        self.assertEqual(state["review"], {"status": "request_revision"})
        self.assertEqual(state["checkout_attempts"], [])

    def test_workstream_retry_reuses_implemented_review_branch_after_review_failure(self):
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
            failing_reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({"status": "request_revision", "summary": "needs fixes", "findings": []}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retry_policy"] = {"max_retries": 1}
            first_review = recipe["steps"][4]
            first_review["input"]["reviewer"]["command"] = [sys.executable, "-c", failing_reviewer_code]
            recipe["steps"] = recipe["steps"] + [
                {
                    "name": "prepare-checkout",
                    "input": {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_root": str(temp_path),
                        "checkout_path": str(checkout),
                    },
                },
                recipe["steps"][3],
                successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)["steps"][4],
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
            self.assertEqual(summary["status"], "published")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                [
                    "select-work",
                    "prepare-checkout",
                    "implement",
                    "validate",
                    "review",
                    "prepare-checkout",
                    "validate",
                    "review",
                ],
            )
            first_review_output = json.loads(
                (ledger / result["steps"][4]["result_path"]).read_text(encoding="utf-8")
            )["output"]
            retried_checkout = json.loads(
                (ledger / result["steps"][5]["result_path"]).read_text(encoding="utf-8")
            )["output"]
            second_review_output = json.loads(
                (ledger / result["steps"][7]["result_path"]).read_text(encoding="utf-8")
            )["output"]

            self.assertEqual(first_review_output["status"], "request_revision")
            self.assertEqual(retried_checkout["status"], "prepared")
            self.assertEqual(second_review_output["status"], "passed")
            self.assertEqual(result["retry_attempts"][0]["repairing_failure_class"], "request_revision")
            self.assertEqual(result["publication"]["status"], "published")

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

    def test_workstream_supports_multi_item_selection_with_combined_review_evidence(self):
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
print("https://github.example/pr/789")
sys.exit(0)
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
            combined_agent_code = textwrap.dedent(
                """
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                selected = [item["external_id"] for item in capsule["work_selection"]["selected_work"]]
                assert selected == ["central-lve.9", "central-lve.10"]
                Path("implemented.txt").write_text("\\n".join(selected) + "\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement selected work"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "implemented selected work"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", combined_agent_code]
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
            pr_call = next(call for call in calls if call["tool"] == "gh" and call["argv"][0:2] == ["pr", "create"])
            body = pr_call["body"]

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "review"],
            )
            self.assertEqual(
                [item["external_id"] for item in result["selected_work"]],
                ["central-lve.9", "central-lve.10"],
            )
            self.assertEqual([item["result"] for item in result["selected_work"]], ["passed", "passed"])
            self.assertIn("central-lve.9 - Compose workstream recipe and terminal PR publisher (passed)", body)
            self.assertIn("central-lve.10 - Follow-up terminal publisher hardening (passed)", body)
            self.assertIn("implementation: runs/", body)
            self.assertIn("validation: runs/", body)
            self.assertIn("review: runs/", body)

    def test_workstream_does_not_reuse_multi_item_validation_after_new_selection_and_implementation(self):
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
            third_item = selected_fixture_item("central-lve.11", "Tracker comments for terminal PR handoff")
            fourth_item = selected_fixture_item("central-lve.12", "Review artifact links for combined work")
            second_agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("implemented-second.txt").write_text("central-lve.11\\ncentral-lve.12\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented-second.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement central-lve.11 and central-lve.12"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "implemented second multi-item selection"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][0]["input"]["sources"][0]["items"] = [
                selected_fixture_item(),
                selected_fixture_item("central-lve.10", "Follow-up terminal publisher hardening"),
            ]
            recipe["steps"] = recipe["steps"][:4] + [
                {
                    "name": "select-work",
                    "input": {
                        "target_ids": ["central-lve.11", "central-lve.12"],
                        "required_labels": ["afk:ready"],
                        "sources": [
                            {
                                "type": "fixture",
                                "id": "fixture",
                                "items": [third_item, fourth_item],
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
            self.assertEqual(
                [item["external_id"] for item in result["selected_work"]],
                ["central-lve.11", "central-lve.12"],
            )
            self.assertEqual([item["result"] for item in result["selected_work"]], ["blocked", "blocked"])
            self.assertNotIn("passed", [item["result"] for item in result["selected_work"]])
            self.assertTrue((second_checkout / "implemented-second.txt").exists())
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

    def test_workstream_keeps_tracker_open_when_review_blocks_publication_despite_terminal_decision(self):
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
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "no-merge",
                    "reason": "Superseded by follow-up PR",
                    "pr_url": "https://github.example/pr/123",
                }
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

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(result["tracker"]["status"], "validated")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["close_reason"], "")
            self.assertEqual(result["tracker"]["pr_url"], "https://github.example/pr/123")
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
            self.assertEqual(result["pipeline_retrospective"]["status"], "failed-needs-human")
            self.assertEqual(result["pipeline_retrospective"]["health"], "failing")
            self.assertEqual(result["pipeline_retrospective"]["publication_status"], "failed-needs-human")
            self.assertEqual(result["pipeline_retrospective"]["tracker_status"], "validated")
            self.assertEqual(result["pipeline_retrospective"]["signals"][0]["kind"], "publisher-failure")
            self.assertEqual(
                result["pipeline_retrospective"]["recommended_follow_up"],
                [
                    {
                        "summary": "Address the blocked publication or retry evidence before rerunning the workstream.",
                        "labels": ["afk:follow-up", "area:workstream", "project:afk-composable-pipeline"],
                    }
                ],
            )
            self.assertEqual(result["tracker"]["status"], "validated")
            self.assertEqual(result["cleanup"], {"status": "clean", "resources": []})
            self.assertIn("afk run-workstream", result["retry"])
            self.assertEqual(
                json.loads(
                    (ledger / "workstreams" / summary["run_id"] / "pipeline-retrospective.json").read_text(
                        encoding="utf-8"
                    )
                ),
                result["pipeline_retrospective"],
            )
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

    def test_workstream_includes_normalized_review_cycles_in_result_and_tracker(self):
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
import sys
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "git", "argv": sys.argv[1:]}}) + "\\n")
sys.exit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "gh", "argv": sys.argv[1:]}}) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("https://github.example/pr/123")
sys.exit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["review_cycles"] = [
                {
                    "status": "passed",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "passed",
                            "summary": "Matches the bead acceptance criteria.",
                            "pr_comment_url": "https://github.example/pr/123#issuecomment-1",
                        },
                        {
                            "role": "bug-risk",
                            "status": "passed",
                            "summary": "No regression risk found.",
                            "pr_comment_url": "https://github.example/pr/123#issuecomment-2",
                        },
                    ],
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
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            tracker = json.loads((result_path.parent / "tracker-result.json").read_text(encoding="utf-8"))

            expected_cycles = [
                {
                    "cycle": 1,
                    "status": "passed",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "passed",
                            "summary": "Matches the bead acceptance criteria.",
                            "pr_comment_url": "https://github.example/pr/123#issuecomment-1",
                            "requires_response": False,
                        },
                        {
                            "role": "bug-risk",
                            "status": "passed",
                            "summary": "No regression risk found.",
                            "pr_comment_url": "https://github.example/pr/123#issuecomment-2",
                            "requires_response": False,
                        },
                    ],
                }
            ]

            self.assertEqual(result["review_cycles"], expected_cycles)
            self.assertEqual(result["tracker"]["review_cycles"], expected_cycles)
            self.assertEqual(tracker["review_cycles"], expected_cycles)
            self.assertEqual(result["tracker"]["status"], "review-feedback-addressed")
            self.assertEqual(tracker["status"], "review-feedback-addressed")

    def test_workstream_surfaces_open_and_addressed_review_cycles_without_overwriting_prior_cycles(self):
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
import sys
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "git", "argv": sys.argv[1:]}}) + "\\n")
sys.exit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps({{"tool": "gh", "argv": sys.argv[1:]}}) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("https://github.example/pr/123")
sys.exit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["review_cycles"] = [
                {
                    "status": "findings-open",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "findings-open",
                            "summary": "Needs follow-up before merge ghp_open_review_secret_1234567890",
                            "pr_comment_url": "https://github.example/pr/123?token=ghp_open_review_secret_1234567890#issuecomment-10",
                            "requires_response": True,
                        }
                    ],
                },
                {
                    "cycle": 2,
                    "status": "findings-addressed",
                    "reviews": [
                        {
                            "role": "bug-risk",
                            "status": "findings-addressed",
                            "summary": "Addressed in follow-up.",
                            "pr_comment_url": "https://github.example/pr/123?token=ghp_addressed_review_secret_1234567890#issuecomment-11",
                            "requires_response": True,
                            "response": {
                                "status": "addressed",
                                "summary": "Patched via github_pat_response_secret_12345678901234567890",
                            },
                        }
                    ],
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
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            tracker = json.loads((result_path.parent / "tracker-result.json").read_text(encoding="utf-8"))

            self.assertEqual([cycle["cycle"] for cycle in result["review_cycles"]], [1, 2])
            self.assertEqual(result["review_cycles"][0]["status"], "findings-open")
            self.assertEqual(result["review_cycles"][1]["status"], "findings-addressed")
            self.assertEqual(result["review_cycles"][0]["reviews"][0]["requires_response"], True)
            self.assertNotIn("response", result["review_cycles"][0]["reviews"][0])
            self.assertEqual(
                result["review_cycles"][1]["reviews"][0]["response"],
                {
                    "status": "addressed",
                    "summary": "Patched via [REDACTED]",
                },
            )
            self.assertEqual(
                result["review_cycles"][0]["reviews"][0]["pr_comment_url"],
                "https://github.example/pr/123#issuecomment-10",
            )
            self.assertEqual(
                result["review_cycles"][1]["reviews"][0]["pr_comment_url"],
                "https://github.example/pr/123#issuecomment-11",
            )
            self.assertIn("[REDACTED]", result["review_cycles"][0]["reviews"][0]["summary"])
            self.assertEqual(result["tracker"]["review_cycles"], result["review_cycles"])
            self.assertEqual(tracker["review_cycles"], result["review_cycles"])
            self.assertEqual(result["tracker"]["status"], "review-findings-open")
            self.assertEqual(tracker["status"], "review-findings-open")
            self.assertIn("response-required review findings", result["tracker"]["comment"])

    def test_workstream_rejects_malformed_review_cycles(self):
        cases = [
            (
                "top_level_not_list",
                {"cycle": 1},
                "review_cycles must be a list",
            ),
            (
                "reviews_not_list",
                [{"status": "passed", "reviews": "wrong"}],
                "review_cycles[0].reviews must be a list",
            ),
            (
                "review_role_missing",
                [{"status": "passed", "reviews": [{"status": "passed", "summary": "Reviewed"}]}],
                "review_cycles[0].reviews[0].role is required",
            ),
            (
                "cycle_status_invalid",
                [{"status": "wip", "reviews": [{"role": "correctness", "status": "passed", "summary": "Reviewed"}]}],
                "review_cycles[0].status must be one of: findings-addressed, findings-open, passed, request-changes",
            ),
            (
                "review_status_invalid",
                [{"status": "passed", "reviews": [{"role": "correctness", "status": "wip", "summary": "Reviewed"}]}],
                "review_cycles[0].reviews[0].status must be one of: findings-addressed, findings-open, passed, request-changes",
            ),
            (
                "response_status_invalid",
                [
                    {
                        "status": "request-changes",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "request-changes",
                                "summary": "Needs a follow-up.",
                                "response": {"status": "wip", "summary": "Investigating"},
                            }
                        ],
                    }
                ],
                "review_cycles[0].reviews[0].response.status must be one of: addressed, findings-addressed",
            ),
        ]
        for case_name, review_cycles, expected_message in cases:
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    repo = temp_path / "repo-src"
                    checkout = temp_path / "checkout"
                    ledger = temp_path / "ledger"
                    fake_git = temp_path / "publisher-git"
                    fake_gh = temp_path / "publisher-gh"
                    init_repo(repo)
                    write_executable(
                        fake_git,
                        f"""#!{sys.executable}
raise SystemExit(0)
""",
                    )
                    write_executable(
                        fake_gh,
                        f"""#!{sys.executable}
raise SystemExit(0)
""",
                    )
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    recipe["review_cycles"] = review_cycles

                    completed = run_afk(
                        "run-workstream",
                        "--workstream-id",
                        "central-lve.9",
                        "--input",
                        json.dumps(recipe),
                        "--ledger",
                        str(ledger),
                    )

                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(expected_message, completed.stderr)

    def test_workstream_rejects_retrospective_without_terminal_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            init_repo(repo)
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
raise SystemExit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective"] = {
                "summary": "Draft retrospective before the PR closes.",
            }

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "retrospective requires tracker.terminal_decision.status to be merged or no-merge",
                completed.stderr,
            )
