import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.contracts import ProjectContract, ProjectContractIdentity  # noqa: E402
from afk.recipes import generate_workstream_recipe  # noqa: E402
from afk.registry import StepResult  # noqa: E402
from afk.implement import normalize_validation as normalize_implement_validation  # noqa: E402
from afk.workstream import (  # noqa: E402
    WorkstreamLedger,
    _retrospective_follow_up_bead_description,
    _retrospective_follow_up_bead_labels,
    _retrospective_follow_up_fingerprint,
    composed_step_input,
    current_selected_work_selection_identity,
    merged_implement_validation_input,
    normalize_recipe,
    pr_body_markdown,
    publish_terminal_pr,
    review_implementation_input,
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


def clone_repo(src, dest, *args):
    completed = subprocess.run(
        ["git", "clone", *args, str(src), str(dest)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git clone {' '.join(args)} failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def commit_file(repo, relative_path, content, message):
    (repo / relative_path).write_text(content, encoding="utf-8")
    git(repo, "add", relative_path)
    git(repo, "commit", "-m", message)


def init_remote_checkout(temp_path):
    repo = temp_path / "repo-src"
    remote = temp_path / "origin.git"
    checkout = temp_path / "checkout"
    init_repo(repo)
    clone_repo(repo, remote, "--bare")
    clone_repo(remote, checkout)
    start_commit = git(checkout, "rev-parse", "HEAD")
    return repo, remote, checkout, start_commit


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
            "review_feedback_status": "waived",
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
    @staticmethod
    def _minimal_workstream_recipe() -> dict[str, object]:
        return {
            "schema_version": 1,
            "workstream_id": "central-lve.9",
            "steps": [
                {
                    "name": "select-work",
                    "input": {
                        "required_labels": ["afk:ready"],
                        "sources": [
                            {
                                "type": "fixture",
                                "id": "fixture",
                                "items": [selected_fixture_item("central-lve.9")],
                            }
                        ],
                    },
                }
            ],
        }

    def test_run_workstream_defaults_ledger_to_ledgers_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(self._minimal_workstream_recipe()),
                cwd=temp_path,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue((temp_path / "ledgers" / summary["result_path"]).is_file())

    def test_run_workstream_uses_afk_ledger_dir_when_flag_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ledger = temp_path / "env-ledgers"

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(self._minimal_workstream_recipe()),
                cwd=temp_path,
                env_overrides={"AFK_LEDGER_DIR": str(ledger)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue((ledger / summary["result_path"]).is_file())
            self.assertFalse((temp_path / "ledgers").exists())

    def test_run_workstream_ledger_flag_overrides_afk_ledger_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            env_ledger = temp_path / "env-ledgers"
            explicit_ledger = temp_path / "explicit-ledgers"

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(self._minimal_workstream_recipe()),
                "--ledger",
                str(explicit_ledger),
                cwd=temp_path,
                env_overrides={"AFK_LEDGER_DIR": str(env_ledger)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue((explicit_ledger / summary["result_path"]).is_file())
            self.assertFalse(env_ledger.exists())

    def test_review_implementation_input_preserves_existing_git_when_cumulative_metadata_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "not-a-git-checkout"
            checkout.mkdir()
            state = {
                "checkout": {
                    "checkout_path": str(checkout),
                    "base_commit": "abc123",
                    "start_commit": "abc123",
                },
                "implementation": {
                    "status": "implemented",
                    "summary": "repair implementation complete",
                    "git": {
                        "before_commit": "def456",
                        "after_commit": "fedcba",
                        "changed_files": ["file-b.txt"],
                        "commits": [{"commit": "fedcba", "subject": "repair implementation"}],
                        "dirty": False,
                        "dirty_status": [],
                    },
                },
            }

            result = review_implementation_input(state)

            self.assertEqual(result, state["implementation"])

    def test_review_implementation_input_preserves_existing_cumulative_git_when_retry_base_commit_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "checkout"
            init_repo(repo)
            start_commit = git(repo, "rev-parse", "HEAD")
            (repo / "file-a.txt").write_text("initial\n", encoding="utf-8")
            git(repo, "add", "file-a.txt")
            git(repo, "commit", "-m", "initial implementation")
            (repo / "file-b.txt").write_text("repair\n", encoding="utf-8")
            git(repo, "add", "file-b.txt")
            git(repo, "commit", "-m", "repair implementation")
            repair_head = git(repo, "rev-parse", "HEAD")
            state = {
                "checkout": {
                    "checkout_path": str(repo),
                    "start_commit": repair_head,
                },
                "implementation": {
                    "status": "implemented",
                    "summary": "repair implementation complete",
                    "git": {
                        "before_commit": start_commit,
                        "after_commit": repair_head,
                        "changed_files": ["file-a.txt", "file-b.txt"],
                        "commits": [
                            {"commit": repair_head, "subject": "repair implementation"},
                            {"commit": "initial", "subject": "initial implementation"},
                        ],
                        "dirty": False,
                        "dirty_status": [],
                    },
                    "latest_repair": {
                        "before_commit": git(repo, "rev-parse", "HEAD^"),
                        "after_commit": repair_head,
                        "changed_files": ["file-b.txt"],
                        "commits": [{"commit": repair_head, "subject": "repair implementation"}],
                        "dirty": False,
                        "dirty_status": [],
                    },
                },
            }

            result = review_implementation_input(state)

            self.assertEqual(result, state["implementation"])

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

    def test_select_work_step_input_does_not_auto_exclude_explicit_targets(self):
        state = {
            "attempted_work_aliases": [
                "central-lve.10",
                "central-lve.12",
                "fixture:central-lve.12",
                "fixture:fixture:central-lve.12",
            ]
        }

        step_input = composed_step_input(
            {
                "name": "select-work",
                "input": {
                    "target_ids": ["central-lve.12"],
                    "exclude_ids": ["central-lve.10"],
                },
            },
            {"review_branch": "afk/test"},
            state,
            Path("/ledger"),
        )

        self.assertEqual(step_input["exclude_ids"], ["central-lve.10"])

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

    def test_workstream_retrospective_warns_when_dry_run_validation_uses_generated_smoke_adapter(self):
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
if sys.argv[1] == "push":
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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "gh", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    sys.exit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][3]["input"]["worker"]["command"] = [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """\
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
                                "steps": [{"name": "generated-recipe-smoke", "status": "pass"}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    """
                ).strip(),
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
            validate_step = next(step for step in result["steps"] if step["name"] == "validate")

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["health"], "warning")
            self.assertEqual(
                result["pipeline_retrospective"]["signals"],
                [
                    {
                        "kind": "validation-smoke",
                        "scope": "pipeline-process",
                        "severity": "warning",
                        "summary": "Validation used dry-run generated smoke coverage instead of project worker evidence.",
                        "step": "tier1",
                        "classification": "dry-run-smoke-validation",
                        "excerpt": "Validation used dry-run generated smoke coverage instead of project worker evidence.",
                        "evidence_paths": [
                            str((ledger / "runs" / validate_step["run_id"] / "step-result.json").resolve()),
                            str((ledger / "runs" / validate_step["run_id"] / "worker-result.json").resolve()),
                        ],
                    }
                ],
            )
            self.assertEqual(
                result["pipeline_retrospective"]["recommended_follow_up"],
                [
                    {
                        "summary": "Switch validation to project-worker or another non-dry-run adapter before treating the run as honest dogfood evidence.",
                        "labels": ["afk:follow-up", "area:validation", "project:afk-composable-pipeline"],
                    }
                ],
            )

    def test_workstream_retrospective_skips_warning_when_generated_smoke_is_explicitly_expected(self):
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
if sys.argv[1] == "push":
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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "gh", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/123")
    sys.exit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["validation_expectations"] = {"generated_smoke_dry_run_expected": True}
            recipe["steps"][3]["input"]["worker"]["command"] = [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """\
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
                                "steps": [{"name": "generated-recipe-smoke", "status": "pass"}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    """
                ).strip(),
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
            self.assertEqual(result["pipeline_retrospective"]["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["health"], "healthy")
            self.assertEqual(result["pipeline_retrospective"]["signals"], [])
            self.assertEqual(result["pipeline_retrospective"]["recommended_follow_up"], [])
            self.assertFalse((ledger / "workstreams" / summary["run_id"] / "retrospective-follow-up-request.json").exists())
            self.assertFalse((ledger / "workstreams" / summary["run_id"] / "retrospective-follow-up-result.json").exists())

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
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps({"status": "pass", "summary": "stdout review passed", "findings": []}),
                    encoding="utf-8",
                )
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
            self.assertEqual(review_result["result"]["evidence"]["result_source"], "reviewer_result_file")
            self.assertEqual(review_result["result"]["evidence"]["result_file_present"], True)
            self.assertFalse(fake_calls.exists())

    def test_workstream_terminal_merge_decision_does_not_skip_minimal_pr_publication(self):
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
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write("git\\n")
raise SystemExit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import sys
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
print("https://github.example/pr/123")
raise SystemExit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                    "review_feedback_status": "waived",
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

            self.assertTrue(fake_calls.exists())
            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(result["tracker"]["status"], "awaiting-review")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["pr_url"], "https://github.example/pr/123")
            self.assertEqual([item["result"] for item in result["selected_work"]], ["passed"])
            tracker_result = json.loads((ledger / summary["result_path"]).parent.joinpath("tracker-result.json").read_text(encoding="utf-8"))
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
raise SystemExit(0)
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
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                    "review_feedback_status": "waived",
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
            self.assertEqual(result["pipeline_retrospective"]["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["health"], "healthy")
            self.assertEqual(result["pipeline_retrospective"]["publication_status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["tracker_status"], "awaiting-review")
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

    def test_workstream_ignores_retrospective_judge_and_follow_up_runtime_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            judge_ran = temp_path / "judge-ran.txt"
            creator_ran = temp_path / "creator-ran.txt"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
raise SystemExit(0)
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
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(judge_ran)!r}).write_text('ran\\n', encoding='utf-8')",
                ],
                "timeout_seconds": 10,
            }
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(creator_ran)!r}).write_text('ran\\n', encoding='utf-8')",
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
            self.assertFalse(judge_ran.exists())
            self.assertFalse(creator_ran.exists())
            self.assertEqual(result["status"], "published")
            self.assertEqual(result["pipeline_retrospective"]["signals"], [])
            self.assertEqual(result["pipeline_retrospective"]["judge"], {"enabled": False, "status": "disabled"})
            self.assertEqual(
                result["pipeline_retrospective"]["follow_up"]["creation"],
                {"enabled": False, "status": "recommendation-only"},
            )
            self.assertNotIn("retrospective_judge_evidence", result["artifacts"])
            self.assertNotIn("retrospective_judge_request", result["artifacts"])
            self.assertNotIn("retrospective_judge_result", result["artifacts"])
            self.assertNotIn("retrospective_judge_stdout", result["artifacts"])
            self.assertNotIn("retrospective_judge_stderr", result["artifacts"])
            self.assertNotIn("retrospective_follow_up_request", result["artifacts"])
            self.assertNotIn("retrospective_follow_up_result", result["artifacts"])
            self.assertNotIn("retrospective_follow_up_stdout", result["artifacts"])
            self.assertNotIn("retrospective_follow_up_stderr", result["artifacts"])

    def test_workstream_tolerates_legacy_enabled_retrospective_runtime_config(self):
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
raise SystemExit(0)
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
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["retrospective_judge"] = {"enabled": True}
            recipe["retrospective_follow_up"] = {"enabled": True, "creator": "beads"}

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
            self.assertEqual(result["pipeline_retrospective"]["judge"], {"enabled": False, "status": "disabled"})
            self.assertEqual(
                result["pipeline_retrospective"]["follow_up"]["creation"],
                {"enabled": False, "status": "recommendation-only"},
            )

    def test_workstream_runs_checkout_local_shell_wrapped_pi_command_once_without_preflight(self):
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

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["steps"][2]["name"], "implement")
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "disabled")
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0]["preflight"])
            self.assertEqual(calls[0]["wrapper_mode"], "wrapped")
            self.assertEqual(calls[0]["cwd"], str(checkout))
            self.assertFalse((run_dir / "pi-auth-preflight.json").exists())
            self.assertNotIn("pi_auth_preflight", result["artifacts"])

    def test_workstream_reviewer_runtime_renders_path_placeholders_without_preflight(self):
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
Path(result_env).write_text(
    json.dumps({{"status": "pass", "summary": "review placeholders rendered", "findings": []}}),
    encoding="utf-8",
)
""",
            )
            recipe = merged_recipe_with_retrospective(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][4]["input"]["reviewer"] = {
                "type": "fake-reviewer-command",
                "provider": "openai-codex",
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
            run_dir = ledger / "workstreams" / summary["run_id"]

            self.assertEqual(result["status"], "published")
            self.assertEqual([step["name"] for step in result["steps"]], ["select-work", "prepare-checkout", "implement", "validate", "review"])
            self.assertEqual(result["pipeline_retrospective"]["judge"]["status"], "disabled")
            self.assertEqual([call["target"] for call in calls], ["reviewer", "reviewer"])
            self.assertTrue(all(not call["preflight"] for call in calls))
            self.assertTrue(all(not call["placeholder_seen"] for call in calls))
            self.assertTrue(all(call["request_in_argv"] for call in calls))
            self.assertTrue(all(call["result_in_argv"] for call in calls))
            self.assertFalse((run_dir / "pi-auth-preflight.json").exists())
            self.assertNotIn("pi_auth_preflight", result["artifacts"])

    def test_retrospective_follow_up_bead_labels_include_project_fallback(self):
        labels = _retrospective_follow_up_bead_labels(
            {"summary": "Configured follow-up", "labels": ["area:retro"]},
            {"labels": ["ready-for-agent"]},
        )

        self.assertEqual(
            labels,
            ["area:retro", "project:afk-composable-pipeline", "ready-for-agent"],
        )

    def test_retrospective_follow_up_fingerprint_includes_kind(self):
        summary = "Same follow-up summary"
        labels = ["afk:follow-up", "project:afk-composable-pipeline"]

        self.assertNotEqual(
            _retrospective_follow_up_fingerprint("retrospective-judge", summary, labels),
            _retrospective_follow_up_fingerprint("publisher-failure", summary, labels),
        )

    def test_retrospective_follow_up_bead_description_includes_specific_failure_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ledger = WorkstreamLedger(temp_path, "test-run")
            ledger.prepare()
            description = _retrospective_follow_up_bead_description(
                normalized={
                    "workstream_id": "central-ymjv",
                    "parent": "central",
                },
                pipeline_retrospective={
                    "signals": [
                        {
                            "kind": "validation-failure",
                            "severity": "error",
                            "step": "tier1",
                            "classification": "compiler",
                            "excerpt": "zone/harness/zone_harness_runtime.cpp:98:9 error: SetBotID is a private member of Bot",
                            "evidence_paths": [
                                "validation-evidence/logs/validation.log",
                                "step-result.json",
                                "worker-result.json",
                            ],
                        }
                    ]
                },
                recommendation={
                    "kind": "validation-failure",
                    "summary": "Fix tier1 [compiler]: zone/harness/zone_harness_runtime.cpp:98:9 error: SetBotID is a private member of Bot",
                    "fingerprint": "retro-follow-up:test",
                },
                ledger=ledger,
                request_path=ledger.path / "retrospective-follow-up-request.json",
                result_path=ledger.path / "retrospective-follow-up-result.json",
            )

            self.assertIn("Step: tier1", description)
            self.assertIn("Classification: compiler", description)
            self.assertIn("SetBotID is a private member of Bot", description)
            self.assertIn("validation-evidence/logs/validation.log", description)

    @unittest.skip("terminal closure moved out of the minimal run-workstream path")
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
                    "review_feedback_status": "waived",
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
            self.assertEqual(result["tracker"]["pr_url"], "")
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

    @unittest.skip("terminal closure moved out of the minimal run-workstream path")
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
            self.assertEqual(result["tracker"]["pr_url"], "")

    @unittest.skip("terminal closure moved out of the minimal run-workstream path")
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

    @unittest.skip("terminal closure moved out of the minimal run-workstream path")
    def test_workstream_terminal_merge_decision_requires_recorded_review_cycles_or_waiver(self):
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
            self.assertEqual(summary["status"], "validated")
            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["publication"]["status"], "tracker-close-blocked")
            self.assertIn("review cycle evidence", result["publication"]["reason"])
            self.assertEqual(result["tracker"]["status"], "validated")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertIn("review cycle evidence", result["tracker"]["comment"])

    @unittest.skip("terminal closure moved out of the minimal run-workstream path")
    def test_workstream_terminal_merge_decision_closes_when_review_cycles_are_recorded_and_addressed(self):
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
                    "status": "findings-addressed",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "request-changes",
                            "summary": "Please address the review findings before merge.",
                            "requires_response": True,
                            "response": {"status": "addressed", "summary": "Addressed on the PR."},
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
            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["close_reason"], "merged via deadbeef")

    @unittest.skip("terminal closure moved out of the minimal run-workstream path")
    def test_workstream_terminal_merge_decision_uses_runtime_review_cycles_after_review_feedback_repair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            review_count = temp_path / "review-count.txt"
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
            agent_code = textwrap.dedent(
                """
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                repair = capsule.get("repair_context")
                if repair:
                    Path("repair.txt").write_text("repair\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "repair.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "repair review feedback"], check=True)
                else:
                    Path("implemented.txt").write_text("initial\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "implemented.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "initial implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "implementation complete"}),
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
                            "summary": "tests passed",
                            "steps": [{"name": "unit", "status": "pass"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            reviewer_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                count_path = Path({str(review_count)!r})
                prior = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
                count_path.write_text(str(prior + 1), encoding="utf-8")
                if prior == 0:
                    payload = {{
                        "status": "request_revision",
                        "summary": "review requested changes",
                        "findings": [
                            {{
                                "status": "request_revision",
                                "severity": "high",
                                "file": "src/demo.py",
                                "line": 41,
                                "required_fix": "Handle the empty review cycle before publishing.",
                                "summary": "Tracker close path still misses the empty review cycle case.",
                            }}
                        ],
                    }}
                else:
                    payload = {{
                        "status": "pass",
                        "summary": "review passed after repair",
                        "findings": [],
                    }}
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(json.dumps(payload), encoding="utf-8")
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "pr_url": "https://github.example/pr/123",
                }
            }
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["review_feedback"] = {"enabled": True}
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", agent_code]
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]
            recipe["steps"][4]["input"]["role"] = "correctness"
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
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertFalse(fake_calls.exists())
            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(len(result["review_cycles"]), 2)
            self.assertEqual(result["review_cycles"][0]["status"], "findings-addressed")
            self.assertEqual(result["review_cycles"][1]["status"], "passed")

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

    def test_workstream_create_mode_preserves_configured_terminal_decision_metadata_without_closing(self):
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
            recipe["tracker"] = {
                "terminal_decision": {
                    "status": "no-merge",
                    "reason": "Hand off to external closer",
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
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            tracker = json.loads((result_path.parent / "tracker-result.json").read_text(encoding="utf-8"))
            expected_decision = {
                "status": "no-merge",
                "merge_commit": "",
                "reason": "Hand off to external closer",
                "pr_url": "https://github.example/pr/123",
                "review_feedback_status": "",
            }

            self.assertEqual(summary["status"], "published")
            self.assertEqual(result["publication"]["status"], "published")
            self.assertEqual(result["tracker"]["terminal_decision"], expected_decision)
            self.assertEqual(tracker["terminal_decision"], expected_decision)
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertFalse(tracker["close_source_item"])
            self.assertEqual(result["tracker"]["close_reason"], "")
            self.assertEqual(tracker["close_reason"], "")

            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(call["argv"][:2] == ["pr", "merge"] for call in calls if call["tool"] == "gh"))
            self.assertFalse(any(call["argv"][:2] == ["issue", "close"] for call in calls if call["tool"] == "gh"))

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

    def test_workstream_update_fallback_uses_absolute_pr_update_path_when_relative_ledger_runs_from_different_cwd(self):
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
if "--input" in sys.argv:
    input_file = sys.argv[sys.argv.index("--input") + 1]
    record["input_path"] = input_file
    record["input"] = json.loads(Path(input_file).read_text(encoding="utf-8"))
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:3] == ["pr", "edit"]:
    print("GraphQL: Projects (classic) is deprecated", file=sys.stderr)
    sys.exit(1)
if sys.argv[1:4] == ["api", "--method", "PATCH"]:
    print("https://github.example/pr/789")
    sys.exit(0)
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-update-fallback")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "update",
                        "pr": "123",
                        "repo": "thunderbump/afk-composable-pipeline",
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

            expected_input_path = (runner / ledger_arg / "workstreams" / ledger.run_id / "pr-update.json").resolve()
            api_call = next(call for call in calls if call["tool"] == "gh" and call["argv"][0:3] == ["api", "--method", "PATCH"])
            command = result["commands"]["gh"]

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["auth"]["path"], "[REDACTED]")
            self.assertEqual(api_call["cwd"], str(checkout))
            self.assertEqual(api_call["gh_config_dir"], str(mounted_gh_config))
            self.assertEqual(Path(api_call["input_path"]), expected_input_path)
            self.assertTrue(Path(api_call["input_path"]).is_absolute())
            self.assertEqual(
                api_call["input"],
                {
                    "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                    "body": publication_path.parent.joinpath("pr-body.md").read_text(encoding="utf-8"),
                },
            )
            self.assertEqual(command[command.index("--input") + 1], str(expected_input_path))
            self.assertIn(str(expected_input_path), publication_text)
            self.assertNotIn(str(mounted_gh_config), publication_text)
            self.assertTrue(publication_path.parent.joinpath("pr-update.json").is_file())

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

    def test_publish_terminal_pr_retries_non_fast_forward_afk_review_branch_with_force_with_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, _, checkout, start_commit = init_remote_checkout(temp_path)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "afk/central-lve-9"
            commit_file(checkout, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "push", "origin", f"HEAD:refs/heads/{review_branch}")
            git(checkout, "reset", "--hard", start_commit)
            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")
            replacement_commit = git(checkout, "rev-parse", "HEAD")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body_path"] = body_file
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/456")
    sys.exit(0)
sys.exit(9)
""",
            )

            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": review_branch,
            }
            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": start_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": replacement_commit, "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-non-fast-forward-retry")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": review_branch,
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "git": {"path": str(fake_git), "push": True, "remote": "origin"},
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

            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            remote_commit = git(checkout, "rev-parse", f"refs/remotes/origin/{review_branch}")

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["url"], "https://github.example/pr/456")
            self.assertEqual(result["commands"]["git_push"], [str(fake_git), "push", "origin", f"HEAD:refs/heads/{review_branch}"])
            self.assertEqual(
                result["commands"]["git_push_retry"],
                [
                    str(fake_git),
                    "push",
                    f"--force-with-lease=refs/heads/{review_branch}:{prior_remote_commit}",
                    "origin",
                    f"HEAD:refs/heads/{review_branch}",
                ],
            )
            self.assertEqual(result["git_push"]["retry_handling"], "force-with-lease-replaced")
            self.assertEqual(result["git_push"]["base_commit"], start_commit)
            self.assertEqual(result["git_push"]["lease_expected"], prior_remote_commit)
            self.assertEqual(
                [attempt["outcome"] for attempt in result["git_push"]["attempts"]],
                ["non-fast-forward", "pushed"],
            )
            self.assertEqual(remote_commit, replacement_commit)
            push_commands = [call["argv"] for call in calls if call["tool"] == "git" and call["argv"][0] == "push"]
            self.assertEqual(
                push_commands,
                [
                    ["push", "origin", f"HEAD:refs/heads/{review_branch}"],
                    [
                        "push",
                        f"--force-with-lease=refs/heads/{review_branch}:{prior_remote_commit}",
                        "origin",
                        f"HEAD:refs/heads/{review_branch}",
                    ],
                ],
            )

    def test_publish_terminal_pr_retries_non_fast_forward_afk_review_branch_from_preserved_checkout_base(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, _, checkout, start_commit = init_remote_checkout(temp_path)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "afk/central-lve-9"
            commit_file(checkout, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "push", "origin", f"HEAD:refs/heads/{review_branch}")
            git(checkout, "reset", "--hard", start_commit)
            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")
            replacement_commit = git(checkout, "rev-parse", "HEAD")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body_path"] = body_file
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/456")
    sys.exit(0)
sys.exit(9)
""",
            )

            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": review_branch,
            }
            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": replacement_commit,
                    "base_commit": start_commit,
                    "requested_ref": replacement_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": replacement_commit, "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-non-fast-forward-preserved-base")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": review_branch,
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "git": {"path": str(fake_git), "push": True, "remote": "origin"},
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

            remote_commit = git(checkout, "rev-parse", f"refs/remotes/origin/{review_branch}")

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["git_push"]["retry_handling"], "force-with-lease-replaced")
            self.assertEqual(result["git_push"]["base_commit"], start_commit)
            self.assertEqual(result["git_push"]["remote_tip"], prior_remote_commit)
            self.assertEqual(result["git_push"]["local_head"], replacement_commit)
            self.assertEqual(result["git_push"]["merge_base"], start_commit)
            self.assertEqual(result["git_push"]["lease_expected"], prior_remote_commit)
            self.assertEqual(
                result["git_push"]["retry_reason"],
                "remote and local heads descend from the checkout base commit",
            )
            self.assertEqual(remote_commit, replacement_commit)

    def test_publish_terminal_pr_retries_non_fast_forward_when_remote_tip_exists_only_on_remote(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, remote, checkout, start_commit = init_remote_checkout(temp_path)
            remote_writer = temp_path / "remote-writer"
            clone_repo(remote, remote_writer)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "afk/central-lve-9"
            commit_file(remote_writer, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(remote_writer, "rev-parse", "HEAD")
            git(remote_writer, "push", "origin", f"HEAD:refs/heads/{review_branch}")

            remote_tip_probe = subprocess.run(
                ["git", "cat-file", "-e", f"{prior_remote_commit}^{{commit}}"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(remote_tip_probe.returncode, 0)

            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")
            replacement_commit = git(checkout, "rev-parse", "HEAD")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body_path"] = body_file
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/456")
    sys.exit(0)
sys.exit(9)
""",
            )

            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": review_branch,
            }
            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": replacement_commit,
                    "base_commit": start_commit,
                    "requested_ref": replacement_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": replacement_commit, "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-non-fast-forward-remote-only-tip")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": review_branch,
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "git": {"path": str(fake_git), "push": True, "remote": "origin"},
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

            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            git(checkout, "fetch", "origin", review_branch)
            remote_commit = git(checkout, "rev-parse", "FETCH_HEAD")

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["git_push"]["retry_handling"], "force-with-lease-replaced")
            self.assertEqual(result["git_push"]["base_commit"], start_commit)
            self.assertEqual(result["git_push"]["remote_tip"], prior_remote_commit)
            self.assertEqual(result["git_push"]["local_head"], replacement_commit)
            self.assertEqual(result["git_push"]["merge_base"], start_commit)
            self.assertEqual(result["git_push"]["lease_expected"], prior_remote_commit)
            self.assertEqual(
                result["git_push"]["retry_reason"],
                "remote and local heads descend from the checkout base commit",
            )
            self.assertEqual(remote_commit, replacement_commit)
            self.assertIn(
                ["fetch", "origin", f"refs/heads/{review_branch}"],
                [call["argv"] for call in calls if call["tool"] == "git" and call["argv"][0] == "fetch"],
            )

    def test_publish_terminal_pr_does_not_force_afk_review_branch_when_remote_tip_is_outside_checkout_base(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, _, checkout, start_commit = init_remote_checkout(temp_path)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "afk/central-lve-9"
            commit_file(checkout, "base.txt", "checkout base\n", "checkout base")
            base_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "reset", "--hard", start_commit)
            commit_file(checkout, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "push", "origin", f"HEAD:refs/heads/{review_branch}")
            git(checkout, "reset", "--hard", base_commit)
            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")
            replacement_commit = git(checkout, "rev-parse", "HEAD")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("gh should not create PR", file=sys.stderr)
sys.exit(9)
""",
            )

            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": review_branch,
            }
            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": replacement_commit,
                    "base_commit": base_commit,
                    "requested_ref": replacement_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": replacement_commit, "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-non-fast-forward-unrelated-remote")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": review_branch,
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "git": {"path": str(fake_git), "push": True, "remote": "origin"},
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

            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            push_commands = [call["argv"] for call in calls if call["tool"] == "git" and call["argv"][0] == "push"]
            remote_commit = git(checkout, "rev-parse", f"refs/remotes/origin/{review_branch}")

            self.assertEqual(result["status"], "failed-needs-human")
            self.assertIn("does not descend from the checkout base commit", result["reason"])
            self.assertEqual(result["git_push"]["retry_handling"], "not-eligible")
            self.assertEqual(result["git_push"]["base_commit"], base_commit)
            self.assertEqual(result["git_push"]["remote_tip"], prior_remote_commit)
            self.assertEqual(result["git_push"]["local_head"], replacement_commit)
            self.assertEqual(result["git_push"]["merge_base"], start_commit)
            self.assertEqual(result["git_push"]["lease_expected"], prior_remote_commit)
            self.assertEqual(
                result["git_push"]["retry_reason"],
                "remote review branch does not descend from the checkout base commit",
            )
            self.assertEqual([attempt["outcome"] for attempt in result["git_push"]["attempts"]], ["non-fast-forward"])
            self.assertEqual(push_commands, [["push", "origin", f"HEAD:refs/heads/{review_branch}"]])
            self.assertEqual(remote_commit, prior_remote_commit)

    def test_publish_terminal_pr_retries_non_fast_forward_for_default_slugged_review_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, _, checkout, start_commit = init_remote_checkout(temp_path)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "afk/central-lve-9"
            commit_file(checkout, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "push", "origin", f"HEAD:refs/heads/{review_branch}")
            git(checkout, "reset", "--hard", start_commit)
            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")
            replacement_commit = git(checkout, "rev-parse", "HEAD")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body_path"] = body_file
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.example/pr/456")
    sys.exit(0)
sys.exit(9)
""",
            )

            recipe = {
                "schema_version": 1,
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "steps": [{"name": "validate", "input": {}}],
                "publisher": {
                    "enabled": True,
                    "mode": "create",
                    "repo": "thunderbump/afk-composable-pipeline",
                    "base": "main",
                    "git": {"path": str(fake_git), "push": True, "remote": "origin"},
                    "gh": {
                        "path": str(fake_gh),
                        "auth": {"config_dir": str(mounted_gh_config)},
                    },
                },
            }
            normalized = normalize_recipe(recipe, parent=None, workstream_id=None)
            self.assertEqual(normalized["review_branch"], review_branch)

            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": start_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": replacement_commit, "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-default-slugged-review-branch")
                ledger.prepare()
                result = publish_terminal_pr(
                    recipe["publisher"],
                    normalized=normalized,
                    state=state,
                    steps=steps,
                    selected_work=selected_work,
                    ledger=ledger,
                )
            finally:
                os.chdir(old_cwd)

            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            remote_commit = git(checkout, "rev-parse", f"refs/remotes/origin/{review_branch}")

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["url"], "https://github.example/pr/456")
            self.assertEqual(result["commands"]["git_push"], [str(fake_git), "push", "origin", f"HEAD:refs/heads/{review_branch}"])
            self.assertEqual(
                result["commands"]["git_push_retry"],
                [
                    str(fake_git),
                    "push",
                    f"--force-with-lease=refs/heads/{review_branch}:{prior_remote_commit}",
                    "origin",
                    f"HEAD:refs/heads/{review_branch}",
                ],
            )
            self.assertEqual(result["git_push"]["retry_handling"], "force-with-lease-replaced")
            self.assertEqual(result["git_push"]["owned_branch"], review_branch)
            self.assertEqual(remote_commit, replacement_commit)
            push_commands = [call["argv"] for call in calls if call["tool"] == "git" and call["argv"][0] == "push"]
            self.assertEqual(
                push_commands,
                [
                    ["push", "origin", f"HEAD:refs/heads/{review_branch}"],
                    [
                        "push",
                        f"--force-with-lease=refs/heads/{review_branch}:{prior_remote_commit}",
                        "origin",
                        f"HEAD:refs/heads/{review_branch}",
                    ],
                ],
            )

    def test_publish_terminal_pr_keeps_successful_git_push_retry_metadata_when_pr_create_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, _, checkout, start_commit = init_remote_checkout(temp_path)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "afk/central-lve-9"
            commit_file(checkout, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "push", "origin", f"HEAD:refs/heads/{review_branch}")
            git(checkout, "reset", "--hard", start_commit)
            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")
            replacement_commit = git(checkout, "rev-parse", "HEAD")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body_path"] = body_file
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("publisher create failed after git retry", file=sys.stderr)
sys.exit(11)
""",
            )

            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": review_branch,
            }
            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": start_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": replacement_commit, "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-non-fast-forward-gh-failure")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": review_branch,
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "git": {"path": str(fake_git), "push": True, "remote": "origin"},
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

            remote_commit = git(checkout, "rev-parse", f"refs/remotes/origin/{review_branch}")

            self.assertEqual(result["status"], "failed-needs-human")
            self.assertEqual(result["git_push"]["retry_handling"], "force-with-lease-replaced")
            self.assertEqual(result["git_push"]["lease_expected"], prior_remote_commit)
            self.assertEqual(
                [attempt["outcome"] for attempt in result["git_push"]["attempts"]],
                ["non-fast-forward", "pushed"],
            )
            self.assertEqual(remote_commit, replacement_commit)

    def test_publish_terminal_pr_does_not_force_non_afk_review_branch_on_non_fast_forward(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, _, checkout, start_commit = init_remote_checkout(temp_path)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "review/workstream-terminal-pr"
            commit_file(checkout, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "push", "origin", f"HEAD:refs/heads/{review_branch}")
            git(checkout, "reset", "--hard", start_commit)
            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    json.dumps({{"tool": "gh", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("gh should not create PR", file=sys.stderr)
sys.exit(9)
""",
            )

            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": review_branch,
            }
            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": start_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": "abc1234", "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-non-fast-forward-blocked")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": review_branch,
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "git": {"path": str(fake_git), "push": True, "remote": "origin"},
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

            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            push_commands = [call["argv"] for call in calls if call["tool"] == "git" and call["argv"][0] == "push"]

            self.assertEqual(result["status"], "failed-needs-human")
            self.assertIn("non-fast-forward", result["reason"])
            self.assertIn("afk/", result["reason"])
            self.assertEqual(result["git_push"]["retry_handling"], "not-eligible")
            self.assertEqual(result["git_push"]["lease_expected"], prior_remote_commit)
            self.assertEqual([attempt["outcome"] for attempt in result["git_push"]["attempts"]], ["non-fast-forward"])
            self.assertEqual(push_commands, [["push", "origin", f"HEAD:refs/heads/{review_branch}"]])

    def test_publish_terminal_pr_does_not_force_foreign_afk_review_branch_on_non_fast_forward(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            _, _, checkout, start_commit = init_remote_checkout(temp_path)
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)

            review_branch = "afk/foreign-work"
            commit_file(checkout, "prior.txt", "prior retry branch\n", "prior publication")
            prior_remote_commit = git(checkout, "rev-parse", "HEAD")
            git(checkout, "push", "origin", f"HEAD:refs/heads/{review_branch}")
            git(checkout, "reset", "--hard", start_commit)
            commit_file(checkout, "replacement.txt", "replacement retry branch\n", "replacement publication")

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

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
completed = subprocess.run([{real_git!r}, *sys.argv[1:]], check=False)
sys.exit(completed.returncode)
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
    json.dumps({{"tool": "gh", "cwd": os.getcwd(), "argv": sys.argv[1:]}}) + "\\n"
)
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
print("gh should not create PR", file=sys.stderr)
sys.exit(9)
""",
            )

            normalized = {
                "workstream_id": "central-lve.9",
                "parent": "central-lve",
                "review_branch": review_branch,
            }
            state = {
                "checkout": {
                    "status": "prepared",
                    "checkout_path": str(checkout),
                    "start_commit": start_commit,
                },
                "implementation": {
                    "git": {
                        "changed_files": ["replacement.txt"],
                        "commits": [{"commit": "abc1234", "subject": "replacement publication"}],
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
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-foreign-afk-branch-blocked")
                ledger.prepare()
                result = publish_terminal_pr(
                    {
                        "enabled": True,
                        "mode": "create",
                        "repo": "thunderbump/afk-composable-pipeline",
                        "base": "main",
                        "head": review_branch,
                        "title": "central-lve.9: Compose workstream recipe and terminal PR publisher",
                        "git": {"path": str(fake_git), "push": True, "remote": "origin"},
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

            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            push_commands = [call["argv"] for call in calls if call["tool"] == "git" and call["argv"][0] == "push"]
            remote_commit = git(checkout, "rev-parse", f"refs/remotes/origin/{review_branch}")

            self.assertEqual(result["status"], "failed-needs-human")
            self.assertIn("workstream-owned AFK branch", result["reason"])
            self.assertEqual(result["git_push"]["retry_handling"], "not-eligible")
            self.assertEqual(result["git_push"]["lease_expected"], prior_remote_commit)
            self.assertEqual([attempt["outcome"] for attempt in result["git_push"]["attempts"]], ["non-fast-forward"])
            self.assertEqual(push_commands, [["push", "origin", f"HEAD:refs/heads/{review_branch}"]])
            self.assertEqual(remote_commit, prior_remote_commit)

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
                f"afk run-workstream --workstream-id central-lve.9 --ledger {ledger} --input <recipe>",
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
                f"afk run-workstream --workstream-id central-lve.9 --ledger {ledger} --input <recipe>",
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

    def test_workstream_implement_job_capsule_inherits_validation_stack_context_from_validate_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            worker_home = temp_path / "worker-home"
            validation_stack_path = temp_path / "mounts" / "bump-akk-stack-validation"
            init_repo(repo)
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["validation"] = {"profile": "tier1", "commands": []}
            recipe["steps"][3]["input"]["validation"] = {
                "profile": "tier1",
                "dry_run": False,
                "timeout_seconds": 3600,
                "worker_home": str(worker_home),
                "stack": {
                    "role": "validation",
                    "path": str(validation_stack_path),
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
            implement_step = next(step for step in result["steps"] if step["name"] == "implement")
            job_capsule = json.loads(
                (ledger / "runs" / implement_step["run_id"] / "job-capsule.json").read_text(encoding="utf-8")
            )["capsule"]

            self.assertEqual(
                job_capsule["validation"],
                {
                    "profile": "tier1",
                    "commands": [],
                    "available_profiles": [],
                    "worker_home": str(worker_home),
                    "stack": {
                        "role": "validation",
                        "path": str(validation_stack_path),
                    },
                    "run_commands_during_implementation": False,
                    "pipeline_validate_step_runs_stack": True,
                    "implementation_instructions": [
                        "No implementation-time validation commands were provided.",
                        "Leave stack validation to the pipeline validate step; do not guess alternate validation stack paths.",
                    ],
                },
            )

    def test_workstream_implement_job_capsule_backfills_validation_aliases_from_validate_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            worker_home = temp_path / "worker-home"
            validation_stack_path = temp_path / "mounts" / "bump-akk-stack-validation"
            init_repo(repo)
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["validation"] = {"profile": "tier1", "commands": []}
            recipe["steps"][3]["input"]["validation"] = {
                "profile": "tier1",
                "dry_run": False,
                "timeout_seconds": 3600,
                "workerHome": str(worker_home),
                "stack": {
                    "path": str(validation_stack_path),
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
            implement_step = next(step for step in result["steps"] if step["name"] == "implement")
            job_capsule = json.loads(
                (ledger / "runs" / implement_step["run_id"] / "job-capsule.json").read_text(encoding="utf-8")
            )["capsule"]

            self.assertEqual(
                job_capsule["validation"],
                {
                    "profile": "tier1",
                    "commands": [],
                    "available_profiles": [],
                    "worker_home": str(worker_home),
                    "stack": {
                        "role": "validation",
                        "path": str(validation_stack_path),
                    },
                    "run_commands_during_implementation": False,
                    "pipeline_validate_step_runs_stack": True,
                    "implementation_instructions": [
                        "No implementation-time validation commands were provided.",
                        "Leave stack validation to the pipeline validate step; do not guess alternate validation stack paths.",
                    ],
                },
            )

    def test_workstream_implement_job_capsule_backfills_validation_commands_from_validate_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            worker_home = temp_path / "worker-home"
            validation_stack_path = temp_path / "mounts" / "bump-akk-stack-validation"
            init_repo(repo)
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["validation"] = {"profile": "tier1", "commands": []}
            recipe["steps"][3]["input"]["validation"] = {
                "profile": "tier1",
                "dry_run": False,
                "timeout_seconds": 3600,
                "commands": [["make", "test"], ["pytest", "-q"]],
                "worker_home": str(worker_home),
                "stack": {
                    "role": "validation",
                    "path": str(validation_stack_path),
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
            implement_step = next(step for step in result["steps"] if step["name"] == "implement")
            job_capsule = json.loads(
                (ledger / "runs" / implement_step["run_id"] / "job-capsule.json").read_text(encoding="utf-8")
            )["capsule"]

            self.assertEqual(
                job_capsule["validation"],
                {
                    "profile": "tier1",
                    "commands": [["make", "test"], ["pytest", "-q"]],
                    "available_profiles": [],
                    "worker_home": str(worker_home),
                    "stack": {
                        "role": "validation",
                        "path": str(validation_stack_path),
                    },
                    "run_commands_during_implementation": True,
                    "pipeline_validate_step_runs_stack": True,
                    "implementation_instructions": [
                        "Run validation.commands during implementation before finishing when your changes affect them.",
                        "Leave stack validation to the pipeline validate step; do not guess alternate validation stack paths.",
                    ],
                },
            )

    def test_workstream_later_implement_job_capsule_uses_following_validate_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            second_checkout = temp_path / "checkout-two"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            first_worker_home = temp_path / "worker-home-first"
            second_worker_home = temp_path / "worker-home-second"
            first_validation_stack_path = temp_path / "mounts" / "first-validation-stack"
            second_validation_stack_path = temp_path / "mounts" / "second-validation-stack"
            init_repo(repo)
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
            recipe["steps"][2]["input"]["validation"] = {"profile": "tier1", "commands": []}
            recipe["steps"][3]["input"]["validation"] = {
                "profile": "tier1",
                "dry_run": False,
                "timeout_seconds": 3600,
                "commands": [["make", "first-validate"]],
                "worker_home": str(first_worker_home),
                "stack": {
                    "role": "validation",
                    "path": str(first_validation_stack_path),
                },
            }
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
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "dry_run": False,
                            "timeout_seconds": 3600,
                            "commands": [["make", "second-validate"]],
                            "worker_home": str(second_worker_home),
                            "stack": {
                                "role": "validation",
                                "path": str(second_validation_stack_path),
                            },
                        },
                        "worker": dict(recipe["steps"][3]["input"]["worker"]),
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
            implement_steps = [step for step in result["steps"] if step["name"] == "implement"]
            second_implement_step = implement_steps[1]
            second_job_capsule = json.loads(
                (ledger / "runs" / second_implement_step["run_id"] / "job-capsule.json").read_text(encoding="utf-8")
            )["capsule"]

            self.assertEqual(
                second_job_capsule["validation"],
                {
                    "profile": "tier1",
                    "commands": [["make", "second-validate"]],
                    "available_profiles": [],
                    "worker_home": str(second_worker_home),
                    "stack": {
                        "role": "validation",
                        "path": str(second_validation_stack_path),
                    },
                    "run_commands_during_implementation": True,
                    "pipeline_validate_step_runs_stack": True,
                    "implementation_instructions": [
                        "Run validation.commands during implementation before finishing when your changes affect them.",
                        "Leave stack validation to the pipeline validate step; do not guess alternate validation stack paths.",
                    ],
                },
            )

    def test_workstream_implement_job_capsule_preserves_explicit_suppression_of_validation_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            worker_home = temp_path / "worker-home"
            validation_stack_path = temp_path / "mounts" / "bump-akk-stack-validation"
            init_repo(repo)
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["validation"] = {
                "profile": "tier1",
                "commands": [],
                "run_commands_during_implementation": False,
            }
            recipe["steps"][3]["input"]["validation"] = {
                "profile": "tier1",
                "dry_run": False,
                "timeout_seconds": 3600,
                "commands": [["make", "test"], ["pytest", "-q"]],
                "worker_home": str(worker_home),
                "stack": {
                    "role": "validation",
                    "path": str(validation_stack_path),
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
            implement_step = next(step for step in result["steps"] if step["name"] == "implement")
            job_capsule = json.loads(
                (ledger / "runs" / implement_step["run_id"] / "job-capsule.json").read_text(encoding="utf-8")
            )["capsule"]

            self.assertEqual(
                job_capsule["validation"],
                {
                    "profile": "tier1",
                    "commands": [],
                    "available_profiles": [],
                    "worker_home": str(worker_home),
                    "stack": {
                        "role": "validation",
                        "path": str(validation_stack_path),
                    },
                    "run_commands_during_implementation": False,
                    "pipeline_validate_step_runs_stack": True,
                    "implementation_instructions": [
                        "No implementation-time validation commands were provided.",
                        "Leave stack validation to the pipeline validate step; do not guess alternate validation stack paths.",
                    ],
                },
            )

    def test_merged_implement_validation_input_preserves_explicit_commands(self):
        merged = merged_implement_validation_input(
            {"profile": "tier1", "commands": [["bin", "explicit"]]},
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "test"]],
                        }
                    },
                }
            ],
        )

        self.assertEqual(merged["commands"], [["bin", "explicit"]])

    def test_merged_implement_validation_input_backfills_empty_commands_from_validate_step(self):
        merged = merged_implement_validation_input(
            {"profile": "tier1", "commands": []},
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "test"]],
                        }
                    },
                }
            ],
        )

        self.assertEqual(merged["commands"], [["make", "test"]])

    def test_merged_implement_validation_input_backfills_empty_commands_when_true_marker_matches_validate_step(self):
        merged = merged_implement_validation_input(
            {
                "profile": "tier1",
                "commands": [],
                "run_commands_during_implementation": True,
            },
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "test"]],
                        }
                    },
                }
            ],
        )

        self.assertEqual(merged["commands"], [["make", "test"]])

    def test_merged_implement_validation_input_preserves_explicit_suppression_of_empty_commands(self):
        merged = merged_implement_validation_input(
            {
                "profile": "tier1",
                "commands": [],
                "run_commands_during_implementation": False,
            },
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "test"]],
                        }
                    },
                }
            ],
        )

        self.assertEqual(merged["commands"], [])

    def test_merged_implement_validation_input_preserves_false_marker_without_commands(self):
        merged = merged_implement_validation_input(
            {
                "profile": "tier1",
                "run_commands_during_implementation": False,
            },
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "test"]],
                        }
                    },
                }
            ],
        )

        self.assertNotIn("commands", merged)

    def test_merged_implement_validation_input_backfills_missing_commands_when_true_marker_matches_validate_step(self):
        merged = merged_implement_validation_input(
            {
                "profile": "tier1",
                "run_commands_during_implementation": True,
            },
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "test"]],
                        }
                    },
                }
            ],
        )

        self.assertEqual(merged["commands"], [["make", "test"]])

    def test_merged_implement_validation_input_preserves_implement_worker_home_alias_over_validate_step(self):
        merged = merged_implement_validation_input(
            {
                "profile": "tier1",
                "commands": [],
                "workerHome": "/tmp/implement-worker-home",
            },
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "worker_home": "/tmp/validate-worker-home",
                        }
                    },
                }
            ],
        )

        normalized = normalize_implement_validation(merged, None, checkout_path=Path("/tmp/checkout"))

        self.assertEqual(normalized["status"], "valid")
        self.assertEqual(normalized["validation"]["worker_home"], "/tmp/implement-worker-home")

    def test_merged_implement_validation_input_uses_following_validate_step_for_later_implement(self):
        merged = merged_implement_validation_input(
            {"profile": "tier1", "commands": []},
            [
                {
                    "name": "implement",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [],
                        }
                    },
                },
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "first-validate"]],
                            "worker_home": "/tmp/first-worker-home",
                            "stack": {
                                "role": "validation",
                                "path": "/tmp/first-validation-stack",
                            },
                        }
                    },
                },
                {
                    "name": "implement",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [],
                        }
                    },
                },
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "second-validate"]],
                            "worker_home": "/tmp/second-worker-home",
                            "stack": {
                                "role": "validation",
                                "path": "/tmp/second-validation-stack",
                            },
                        }
                    },
                },
            ],
            step_index=2,
        )

        self.assertEqual(merged["commands"], [["make", "second-validate"]])
        self.assertEqual(merged["worker_home"], "/tmp/second-worker-home")
        self.assertEqual(
            merged["stack"],
            {
                "role": "validation",
                "path": "/tmp/second-validation-stack",
            },
        )

    def test_merged_implement_validation_input_prefers_following_validate_step_with_matching_profile(self):
        merged = merged_implement_validation_input(
            {"profile": "tier2", "commands": []},
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {
                        "validation": {
                            "profile": "tier1",
                            "commands": [["make", "tier1-validate"]],
                            "worker_home": "/tmp/tier1-worker-home",
                            "stack": {
                                "role": "validation",
                                "path": "/tmp/tier1-validation-stack",
                            },
                        }
                    },
                },
                {
                    "name": "validate",
                    "profile": "tier2",
                    "input": {
                        "validation": {
                            "profile": "tier2",
                            "commands": [["make", "tier2-validate"]],
                            "worker_home": "/tmp/tier2-worker-home",
                            "stack": {
                                "role": "validation",
                                "path": "/tmp/tier2-validation-stack",
                            },
                        }
                    },
                },
            ],
        )

        self.assertEqual(merged["profile"], "tier2")
        self.assertEqual(merged["commands"], [["make", "tier2-validate"]])
        self.assertEqual(merged["worker_home"], "/tmp/tier2-worker-home")
        self.assertEqual(
            merged["stack"],
            {
                "role": "validation",
                "path": "/tmp/tier2-validation-stack",
            },
        )

    def test_merged_implement_validation_input_backfills_profile_from_legacy_validate_step_profile(self):
        merged = merged_implement_validation_input(
            {"commands": []},
            [
                {
                    "name": "validate",
                    "profile": "tier1",
                    "input": {},
                }
            ],
        )

        self.assertEqual(merged["profile"], "tier1")

    def test_implement_normalize_validation_accepts_validation_worker_home_alias_and_default_stack_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            worker_home = temp_path / "worker-home"
            validation_stack_path = temp_path / "mounts" / "bump-akk-stack-validation"
            checkout.mkdir()

            normalized = normalize_implement_validation(
                {
                    "profile": "tier1",
                    "commands": [],
                    "workerHome": str(worker_home),
                    "stack": {"path": str(validation_stack_path)},
                },
                None,
                checkout_path=checkout,
            )

            self.assertEqual(
                normalized,
                {
                    "status": "valid",
                    "validation": {
                        "profile": "tier1",
                        "commands": [],
                        "available_profiles": [],
                        "worker_home": str(worker_home),
                        "stack": {
                            "role": "validation",
                            "path": str(validation_stack_path),
                        },
                        "run_commands_during_implementation": False,
                        "pipeline_validate_step_runs_stack": True,
                        "implementation_instructions": [
                            "No implementation-time validation commands were provided.",
                            "Leave stack validation to the pipeline validate step; do not guess alternate validation stack paths.",
                        ],
                    },
                },
            )

    def test_implement_normalize_validation_keeps_worker_home_without_implying_stack_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout = temp_path / "checkout"
            worker_home = temp_path / "worker-home"
            checkout.mkdir()

            normalized = normalize_implement_validation(
                {
                    "profile": "tier1",
                    "commands": [],
                    "worker_home": str(worker_home),
                },
                None,
                checkout_path=checkout,
            )

            self.assertEqual(
                normalized,
                {
                    "status": "valid",
                    "validation": {
                        "profile": "tier1",
                        "commands": [],
                        "available_profiles": [],
                        "worker_home": str(worker_home),
                        "run_commands_during_implementation": False,
                        "pipeline_validate_step_runs_stack": False,
                        "implementation_instructions": [
                            "No implementation-time validation commands were provided.",
                        ],
                    },
                },
            )

    def test_implement_normalize_validation_rejects_non_empty_commands_with_false_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir) / "checkout"
            checkout.mkdir()

            normalized = normalize_implement_validation(
                {
                    "profile": "tier1",
                    "commands": [["make", "test"]],
                    "run_commands_during_implementation": False,
                },
                None,
                checkout_path=checkout,
            )

            self.assertEqual(
                normalized,
                {
                    "status": "invalid",
                    "message": (
                        "validation.run_commands_during_implementation=false contradicts "
                        "non-empty validation.commands"
                    ),
                },
            )

    def test_implement_normalize_validation_rejects_empty_commands_with_true_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir) / "checkout"
            checkout.mkdir()

            normalized = normalize_implement_validation(
                {
                    "profile": "tier1",
                    "commands": [],
                    "run_commands_during_implementation": True,
                },
                None,
                checkout_path=checkout,
            )

            self.assertEqual(
                normalized,
                {
                    "status": "invalid",
                    "message": (
                        "validation.run_commands_during_implementation=true requires "
                        "non-empty validation.commands"
                    ),
                },
            )

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
            self.assertEqual(result["tracker"]["pr_url"], "")
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

    def test_workstream_validation_feedback_retries_target_failure_and_continues_to_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
            validate_count = temp_path / "validate-count.txt"
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                repair = capsule.get("repair_context")
                if repair:
                    assert repair["attempt"] == 1
                    assert repair["trigger"] == "validation_feedback"
                    assert repair["validation"]["status"] == "failed_validation"
                    assert repair["validation"]["classification"] == "compiler"
                    assert "SetBotID is a private member of Bot" in repair["validation"]["root_excerpt"]
                    assert any(path.endswith("compiler.log") for path in repair["validation"]["evidence_paths"])
                    assert any(path.endswith("step-result.json") for path in repair["validation"]["evidence_paths"])
                    assert any(path.endswith("worker-result.json") for path in repair["validation"]["evidence_paths"])
                    assert repair["previous_implementation"]["commit"]
                    assert "implemented.txt" in repair["previous_implementation"]["changed_files"]
                    assert repair["acceptance_criteria"] == capsule["acceptance_criteria"]
                    Path("repair.txt").write_text("repair\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "repair.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "repair validation failure"], check=True)
                else:
                    Path("implemented.txt").write_text("initial\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "implemented.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "initial implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({{"status": "completed", "summary": "implementation complete"}}),
                    encoding="utf-8",
                )
                """
            ).strip()
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                evidence_dir = result_path.parent
                count_path = Path({str(validate_count)!r})
                prior = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
                count_path.write_text(str(prior + 1), encoding="utf-8")
                request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text(encoding="utf-8"))
                if prior == 0:
                    (evidence_dir / "compiler.log").write_text(
                        "zone/harness/zone_harness_runtime.cpp:98:9 error: SetBotID is a private member of Bot\\n",
                        encoding="utf-8",
                    )
                    payload = {{
                        "profile": request["profile"],
                        "status": "fail",
                        "summary": "compile failed",
                        "failures": [
                            {{
                                "name": "build",
                                "status": "fail",
                                "category": "compiler",
                                "reason": "zone/harness/zone_harness_runtime.cpp:98:9 error: SetBotID is a private member of Bot",
                                "command": "ninja test",
                                "exitCode": 1,
                                "log": "compiler.log",
                            }}
                        ],
                    }}
                else:
                    payload = {{
                        "profile": request["profile"],
                        "status": "pass",
                        "summary": "tests passed",
                        "steps": [{{"name": "unit", "status": "pass"}}],
                    }}
                result_path.write_text(json.dumps(payload), encoding="utf-8")
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["validation_feedback"] = {"enabled": True}
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", agent_code]
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]

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
            repair_output = json.loads((ledger / result["steps"][5]["result_path"]).read_text(encoding="utf-8"))["output"]
            second_validate = json.loads((ledger / result["steps"][6]["result_path"]).read_text(encoding="utf-8"))["output"]
            review_output = json.loads((ledger / result["steps"][7]["result_path"]).read_text(encoding="utf-8"))["output"]
            self.assertEqual(repair_output["repair_context"]["attempt"], 1)
            self.assertEqual(repair_output["repair_context"]["validation"]["classification"], "compiler")
            self.assertEqual(second_validate["status"], "validated")
            self.assertEqual(review_output["status"], "passed")
            self.assertEqual(result["outcome"]["functional"]["status"], "validated-unpublished")
            self.assertEqual(result["outcome"]["process_retrospective"]["status"], "clear")
            self.assertEqual(result["pipeline_retrospective"]["follow_up"]["recommended"], [])

    def test_workstream_validation_feedback_does_not_retry_runtime_validation_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["validation_feedback"] = {"enabled": True}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", "import sys; sys.exit(7)"]

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
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertEqual(result["retry_attempts"], [])
            self.assertIn("validate did not reach validated", result["publication"]["reason"])

    def test_workstream_validation_feedback_does_not_retry_worker_setup_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
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
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["validation_feedback"] = {"enabled": True}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]

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
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertEqual(result["retry_attempts"], [])
            self.assertIn("validate did not reach validated", result["publication"]["reason"])

    def test_workstream_validation_feedback_does_not_retry_generic_path_setup_failures(self):
        excerpts = [
            "bash: /tmp/missing/script.sh: No such file or directory",
            "ninja: fatal: chdir to /tmp/build: No such file or directory",
            'CMake Error: The source directory "/tmp/missing" does not exist.',
        ]
        for excerpt in excerpts:
            with self.subTest(excerpt=excerpt):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_path = Path(temp_dir)
                    repo = temp_path / "repo-src"
                    checkout = temp_path / "checkout"
                    ledger = temp_path / "ledger"
                    init_repo(repo)
                    fake_git = temp_path / "publisher-git"
                    fake_gh = temp_path / "publisher-gh"
                    write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
                    write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
                    worker_code = textwrap.dedent(
                        f"""
                        import json
                        import os
                        import sys
                        from pathlib import Path

                        evidence_dir = Path(os.environ["AFK_WORKER_RESULT"]).parent
                        log_dir = evidence_dir / "logs"
                        log_dir.mkdir(parents=True, exist_ok=True)
                        (log_dir / "validation.log").write_text({excerpt!r} + "\\n", encoding="utf-8")
                        Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                            json.dumps({{"status": "failed", "summary": "failed_validation"}}),
                            encoding="utf-8",
                        )
                        sys.exit(1)
                        """
                    ).strip()
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    recipe["publisher"] = {"enabled": False}
                    recipe["retry_policy"] = {"max_retries": 1}
                    recipe["validation_feedback"] = {"enabled": True}
                    recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]

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
                    self.assertEqual(
                        [step["name"] for step in result["steps"]],
                        ["select-work", "prepare-checkout", "implement", "validate"],
                    )
                    self.assertEqual(result["retry_attempts"], [])
                    self.assertIn("validate did not reach validated", result["publication"]["reason"])

    def test_workstream_validation_feedback_retries_compiler_missing_header_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
            validate_count = temp_path / "validate-count.txt"
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                repair = capsule.get("repair_context")
                if repair:
                    assert repair["attempt"] == 1
                    assert repair["validation"]["classification"] == "compiler"
                    assert "missing_header.h: No such file or directory" in repair["validation"]["root_excerpt"]
                    Path("repair.txt").write_text("repair\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "repair.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "repair missing header"], check=True)
                else:
                    Path("implemented.txt").write_text("initial\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "implemented.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "initial implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({{"status": "completed", "summary": "implementation complete"}}),
                    encoding="utf-8",
                )
                """
            ).strip()
            worker_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                result_path = Path(os.environ["AFK_WORKER_RESULT"])
                count_path = Path({str(validate_count)!r})
                prior = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
                count_path.write_text(str(prior + 1), encoding="utf-8")
                request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text(encoding="utf-8"))
                if prior == 0:
                    payload = {{
                        "profile": request["profile"],
                        "status": "fail",
                        "summary": "compile failed",
                        "failures": [
                            {{
                                "name": "build",
                                "status": "fail",
                                "category": "compiler",
                                "reason": "src/generated/foo.cpp:10: fatal error: missing_header.h: No such file or directory",
                                "command": "ninja test",
                                "exitCode": 1,
                            }}
                        ],
                    }}
                else:
                    payload = {{
                        "profile": request["profile"],
                        "status": "pass",
                        "summary": "tests passed",
                        "steps": [{{"name": "unit", "status": "pass"}}],
                    }}
                result_path.write_text(json.dumps(payload), encoding="utf-8")
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["validation_feedback"] = {"enabled": True}
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", agent_code]
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]

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
            repair_output = json.loads((ledger / result["steps"][5]["result_path"]).read_text(encoding="utf-8"))["output"]
            self.assertIn("missing_header.h: No such file or directory", repair_output["repair_context"]["validation"]["root_excerpt"])

    def test_workstream_validation_feedback_reports_exhausted_retry_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
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
                            "summary": "compile failed",
                            "failures": [{"name": "build", "status": "fail", "category": "compiler", "reason": "compile failed"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 0}
            recipe["validation_feedback"] = {"enabled": True}
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]

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
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertIn("retry budget exhausted", result["publication"]["reason"])

    def test_workstream_review_feedback_repairs_request_changes_then_reruns_validation_and_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
            review_count = temp_path / "review-count.txt"
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                repair = capsule.get("repair_context")
                if repair:
                    assert repair["attempt"] == 1
                    assert repair["trigger"] == "review_feedback"
                    assert repair["review"]["role"] == "correctness"
                    assert repair["review"]["status"] == "request_revision"
                    assert repair["review"]["summary"] == "review requested changes"
                    assert len(repair["review"]["findings"]) == 1
                    finding = repair["review"]["findings"][0]
                    assert finding["severity"] == "high"
                    assert finding["file"] == "src/demo.py"
                    assert finding["line"] == 41
                    assert finding["required_fix"] == "Handle the empty review cycle before publishing."
                    assert repair["current_implementation"]["summary"] == "implementation complete"
                    assert repair["validation"]["status"] == "validated"
                    assert repair["validation"]["summary"] == "tests passed"
                    assert repair["validation"]["evidence_paths"][0].endswith("step-result.json")
                    assert repair["validation"]["evidence_paths"][1].endswith("worker-result.json")
                    Path("repair.txt").write_text("repair\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "repair.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "repair review feedback"], check=True)
                else:
                    Path("implemented.txt").write_text("initial\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "implemented.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "initial implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({{"status": "completed", "summary": "implementation complete"}}),
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
                            "summary": "tests passed",
                            "steps": [{"name": "unit", "status": "pass"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            reviewer_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                count_path = Path({str(review_count)!r})
                prior = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
                count_path.write_text(str(prior + 1), encoding="utf-8")
                request = json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
                if prior == 0:
                    payload = {{
                        "status": "request_revision",
                        "summary": "review requested changes",
                        "findings": [
                            {{
                                "status": "request_revision",
                                "severity": "high",
                                "file": "src/demo.py",
                                "line": 41,
                                "required_fix": "Handle the empty review cycle before publishing.",
                                "summary": "Tracker close path still misses the empty review cycle case.",
                            }},
                            {{
                                "status": "request_revision",
                                "classification": "pipeline_failure",
                                "severity": "medium",
                                "summary": "Reviewer adapter timed out once in CI; capture a pipeline follow-up.",
                            }},
                        ],
                    }}
                else:
                    payload = {{
                        "status": "pass",
                        "summary": "review passed after repair",
                        "findings": [],
                    }}
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(json.dumps(payload), encoding="utf-8")
                """
            ).strip()
            project_contract = ProjectContract(
                project_slug="test-project",
                repo_url=str(repo),
                base_branch="main",
                beads_labels=("project:test-project",),
                validation_profiles=("tier1",),
                validation_profile_requests={"tier1": {"profile": "tier1"}},
                artifact_retention={"ledger_days": 30, "log_days": 30},
                pr_target={"remote": "origin", "branch": "main"},
                identity=ProjectContractIdentity(path="test-project.json", sha256="deadbeef"),
            )
            recipe = generate_workstream_recipe(
                workstream_id="central-lve.9",
                project_contract=project_contract,
                beads_workspace=temp_path,
                checkout_root=temp_path,
                checkout_path=checkout,
                validation_profile="tier1",
                sources=[{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}],
                required_labels=["afk:ready"],
                enable_review_feedback=True,
            )
            recipe["steps"][0]["input"]["required_metadata"] = []
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", agent_code]
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]
            recipe["steps"][4]["input"]["role"] = "correctness"
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
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(recipe["review_feedback"], {"enabled": True})
            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                [
                    "select-work",
                    "prepare-checkout",
                    "implement",
                    "validate",
                    "review",
                    "prepare-checkout",
                    "implement",
                    "validate",
                    "review",
                ],
            )
            repair_output = json.loads((ledger / result["steps"][6]["result_path"]).read_text(encoding="utf-8"))["output"]
            self.assertEqual(repair_output["repair_context"]["trigger"], "review_feedback")
            self.assertEqual(repair_output["repair_context"]["review"]["role"], "correctness")
            self.assertEqual(len(result["review_cycles"]), 2)
            self.assertEqual(result["review_cycles"][0]["status"], "findings-addressed")
            self.assertEqual(result["review_cycles"][0]["reviews"][0]["status"], "request-changes")
            self.assertEqual(result["review_cycles"][0]["reviews"][0]["requires_response"], True)
            self.assertEqual(result["review_cycles"][0]["reviews"][0]["response"]["status"], "addressed")
            self.assertEqual(
                result["review_cycles"][0]["reviews"][0]["response"]["follow_up_review_status"],
                "passed",
            )
            self.assertEqual(
                result["review_cycles"][0]["reviews"][0]["response"]["pipeline_follow_up"][0]["classification"],
                "pipeline_failure",
            )
            self.assertEqual(result["review_cycles"][1]["status"], "passed")
            self.assertEqual(result["review_cycles"][1]["reviews"][0]["role"], "correctness")
            self.assertEqual(result["tracker"]["review_cycles"], result["review_cycles"])
            self.assertEqual(result["tracker"]["status"], "review-feedback-addressed")

    def test_workstream_review_feedback_retry_review_uses_cumulative_branch_diff_and_latest_repair_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
            review_count = temp_path / "review-count.txt"
            agent_code = textwrap.dedent(
                """
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                if capsule.get("repair_context"):
                    Path("file-b.txt").write_text("repair\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "file-b.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "repair implementation"], check=True)
                else:
                    Path("file-a.txt").write_text("initial\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "file-a.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "initial implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "implementation complete"}),
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
                            "summary": "tests passed",
                            "steps": [{"name": "unit", "status": "pass"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            reviewer_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                count_path = Path({str(review_count)!r})
                prior = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
                count_path.write_text(str(prior + 1), encoding="utf-8")
                request = json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
                implementation = request["evidence_pack"]["implementation"]
                if prior == 0:
                    payload = {{
                        "status": "request_revision",
                        "summary": "review requested changes",
                        "findings": [
                            {{
                                "status": "request_revision",
                                "severity": "high",
                                "file": "file-b.txt",
                                "line": 1,
                                "required_fix": "Add the repair change.",
                                "summary": "Repair commit is still missing.",
                            }}
                        ],
                    }}
                else:
                    assert implementation["git"]["changed_files"] == ["file-a.txt", "file-b.txt"], implementation
                    assert [commit["subject"] for commit in implementation["git"]["commits"]] == [
                        "repair implementation",
                        "initial implementation",
                    ], implementation
                    latest_repair = implementation["latest_repair"]
                    assert latest_repair["changed_files"] == ["file-b.txt"], latest_repair
                    assert [commit["subject"] for commit in latest_repair["commits"]] == [
                        "repair implementation"
                    ], latest_repair
                    payload = {{
                        "status": "pass",
                        "summary": "review passed after cumulative diff check",
                        "findings": [],
                    }}
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(json.dumps(payload), encoding="utf-8")
                """
            ).strip()
            project_contract = ProjectContract(
                project_slug="test-project",
                repo_url=str(repo),
                base_branch="main",
                beads_labels=("project:test-project",),
                validation_profiles=("tier1",),
                validation_profile_requests={"tier1": {"profile": "tier1"}},
                artifact_retention={"ledger_days": 30, "log_days": 30},
                pr_target={"remote": "origin", "branch": "main"},
                identity=ProjectContractIdentity(path="test-project.json", sha256="deadbeef"),
            )
            recipe = generate_workstream_recipe(
                workstream_id="central-lve.9",
                project_contract=project_contract,
                beads_workspace=temp_path,
                checkout_root=temp_path,
                checkout_path=checkout,
                validation_profile="tier1",
                sources=[{"type": "fixture", "id": "fixture", "items": [selected_fixture_item()]}],
                required_labels=["afk:ready"],
                enable_review_feedback=True,
            )
            recipe["steps"][0]["input"]["required_metadata"] = []
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", agent_code]
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]
            recipe["steps"][4]["input"]["role"] = "correctness"
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
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                [
                    "select-work",
                    "prepare-checkout",
                    "implement",
                    "validate",
                    "review",
                    "prepare-checkout",
                    "implement",
                    "validate",
                    "review",
                ],
            )

    def test_workstream_review_feedback_blocks_when_repair_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "status": "request_revision",
                            "summary": "review requested changes",
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "severity": "high",
                                    "file": "src/demo.py",
                                    "line": 41,
                                    "required_fix": "Handle the empty review cycle before publishing.",
                                    "summary": "Tracker close path still misses the empty review cycle case.",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 0}
            recipe["review_feedback"] = {"enabled": True}
            recipe["steps"][4]["input"]["role"] = "correctness"
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
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "review"],
            )
            self.assertEqual(result["review_cycles"][0]["status"], "request-changes")
            self.assertEqual(result["review_cycles"][0]["reviews"][0]["role"], "correctness")
            self.assertIn("review feedback retry budget exhausted", result["publication"]["reason"])
            self.assertIn("Handle the empty review cycle before publishing.", result["publication"]["reason"])

    def test_workstream_review_feedback_blocks_pipeline_only_request_revision_without_repair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            write_executable(fake_git, f"#!{sys.executable}\nraise SystemExit(9)\n")
            write_executable(fake_gh, f"#!{sys.executable}\nraise SystemExit(9)\n")
            reviewer_code = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
                    json.dumps(
                        {
                            "status": "request_revision",
                            "summary": "review requested pipeline follow-up",
                            "findings": [
                                {
                                    "status": "request_revision",
                                    "classification": "pipeline_failure",
                                    "severity": "medium",
                                    "summary": "Reviewer adapter timed out once in CI; capture a pipeline follow-up.",
                                },
                                {
                                    "status": "request_revision",
                                    "classification": "tool_failure",
                                    "severity": "low",
                                    "summary": "Formatter tool was unavailable in the review container.",
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["review_feedback"] = {"enabled": True}
            recipe["steps"][4]["input"]["role"] = "correctness"
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
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "review"],
            )
            self.assertEqual(result["review_cycles"][0]["status"], "request-changes")
            self.assertEqual(result["review_cycles"][0]["reviews"][0]["role"], "correctness")
            self.assertEqual(
                [item["classification"] for item in result["review_cycles"][0]["reviews"][0]["pipeline_follow_up"]],
                ["pipeline_failure", "tool_failure"],
            )
            self.assertEqual(result["tracker"]["review_cycles"], result["review_cycles"])
            self.assertEqual(result["tracker"]["status"], "review-findings-open")
            self.assertIn("review requested pipeline follow-up", result["publication"]["reason"])
            self.assertIn("Reviewer adapter timed out once in CI", result["publication"]["reason"])

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
            initial_checkout = json.loads(
                (ledger / result["steps"][1]["result_path"]).read_text(encoding="utf-8")
            )["output"]
            first_implementation = json.loads(
                (ledger / result["steps"][2]["result_path"]).read_text(encoding="utf-8")
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
            self.assertEqual(retried_checkout["base_commit"], initial_checkout["start_commit"])
            self.assertEqual(retried_checkout["start_commit"], first_implementation["git"]["after_commit"])
            self.assertEqual(retried_checkout["requested_ref"], first_implementation["git"]["after_commit"])
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

    def test_workstream_reselects_from_same_candidate_set_after_work_item_validation_failure(self):
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
            candidate_set = [
                {
                    **selected_fixture_item("central-lve.10", "Ready label without AFK readiness"),
                    "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    "afk": {"ready": False},
                },
                {
                    **selected_fixture_item("central-lve.11", "Dependency-blocked candidate"),
                    "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    "dependencies": [{"id": "central-lve.99", "status": "open"}],
                },
                {
                    **selected_fixture_item("central-lve.12", "First runnable candidate"),
                    "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                },
                {
                    **selected_fixture_item("central-lve.13", "Fallback runnable candidate"),
                    "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                },
            ]
            second_agent_code = textwrap.dedent(
                """
                import json
                import subprocess
                from pathlib import Path

                Path("implemented-second.txt").write_text("central-lve.13\\n", encoding="utf-8")
                subprocess.run(["git", "add", "implemented-second.txt"], check=True)
                subprocess.run(["git", "commit", "-m", "implement central-lve.13"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({"status": "completed", "summary": "implemented replacement work item"}),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][0]["input"] = {
                "required_labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "required_metadata": ["workstream", "acceptance_criteria", "afk.ready"],
                "selection_limit": 1,
                "sources": [{"type": "fixture", "id": "fixture", "items": candidate_set}],
            }
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
                            "summary": "tests failed",
                            "failureCount": 1,
                            "failures": [{"category": "validation_failed", "message": "unit tests failed"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["steps"] = recipe["steps"][:4] + [
                recipe["steps"][0],
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
            reselection = json.loads(Path(result["steps"][4]["result_abspath"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
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
            self.assertEqual(result["selected_work"][0]["external_id"], "central-lve.13")
            self.assertEqual(result["selected_work"][0]["result"], "blocked")
            self.assertEqual(
                [item["external_id"] for item in reselection["output"]["selected_work"]],
                ["central-lve.13"],
            )
            self.assertEqual(
                [
                    (item["candidate"]["external_id"], item["reason"])
                    for item in reselection["output"]["skipped_candidates"]
                ],
                [
                    ("central-lve.10", "missing_metadata:afk.ready"),
                    ("central-lve.11", "blocked"),
                    ("central-lve.12", "attempted_in_run"),
                ],
            )
            self.assertTrue((second_checkout / "implemented-second.txt").exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_blocks_on_validation_runtime_failure_without_reselection(self):
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
            recipe["steps"][0]["input"] = {
                "required_labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "required_metadata": ["workstream", "acceptance_criteria", "afk.ready"],
                "selection_limit": 1,
                "sources": [
                    {
                        "type": "fixture",
                        "id": "fixture",
                        "items": [
                            {
                                **selected_fixture_item("central-lve.12", "Runnable candidate"),
                                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                            },
                            {
                                **selected_fixture_item("central-lve.13", "Fallback candidate"),
                                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                            },
                        ],
                    }
                ],
            }
            failing_worker_code = textwrap.dedent(
                """
                import sys

                print("python3.13: command not found")
                sys.exit(127)
                """
            ).strip()
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", failing_worker_code]
            recipe["steps"] = recipe["steps"][:4] + [recipe["steps"][0]]

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
            validation_result = json.loads(Path(result["steps"][3]["result_abspath"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(
                [step["name"] for step in result["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate"],
            )
            self.assertEqual(result["selected_work"][0]["external_id"], "central-lve.12")
            self.assertEqual(result["selected_work"][0]["result"], "failed")
            self.assertEqual(validation_result["output"]["classification"], "missing_worker_result")
            self.assertFalse(fake_calls.exists())

    def test_workstream_surfaces_openai_codex_auth_failure_in_pipeline_retrospective(self):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            failing_agent_code = textwrap.dedent(
                """
                import sys

                print("No API key found for openai-codex.", file=sys.stderr)
                sys.exit(1)
                """
            ).strip()
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["agent"] = {
                "type": "real-agent-command",
                "provider": "openai-codex",
                "command": [sys.executable, "-c", failing_agent_code],
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
            implement_step = result["steps"][2]

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(
                result["pipeline_retrospective"]["signals"],
                [
                    {
                        "kind": "implementation-auth",
                        "scope": "pipeline-process",
                        "severity": "error",
                        "summary": "No API key found for openai-codex.",
                        "step": "implement",
                        "classification": "openai-codex-auth",
                        "excerpt": "No API key found for openai-codex.",
                        "evidence_paths": [
                            f"runs/{implement_step['run_id']}/step-result.json",
                            f"runs/{implement_step['run_id']}/agent-result.json",
                        ],
                    }
                ],
            )
            self.assertEqual(
                result["pipeline_retrospective"]["recommended_follow_up"],
                [
                    {
                        "summary": "Fix implement [openai-codex-auth]: No API key found for openai-codex.",
                        "labels": ["afk:follow-up", "area:implementation", "project:afk-composable-pipeline"],
                    }
                ],
            )

    def test_workstream_keeps_auth_preamble_runtime_failure_on_generic_blocked_path(self):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            failing_agent_code = textwrap.dedent(
                """
                import sys

                print("Replaying remote auth state for openai-codex", file=sys.stderr)
                print("Traceback: unit test assertion failed", file=sys.stderr)
                sys.exit(1)
                """
            ).strip()
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["agent"] = {
                "type": "real-agent-command",
                "provider": "openai-codex",
                "command": [sys.executable, "-c", failing_agent_code],
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

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(
                result["pipeline_retrospective"]["signals"],
                [
                    {
                        "kind": "retry-or-blocked",
                        "scope": "pipeline-process",
                        "severity": "error",
                        "summary": "implement did not reach implemented: failed_runtime",
                        "evidence_paths": [],
                    }
                ],
            )
            self.assertEqual(
                result["pipeline_retrospective"]["recommended_follow_up"],
                [
                    {
                        "summary": "Address the blocked publication or retry evidence before rerunning the workstream.",
                        "labels": ["afk:follow-up", "area:workstream", "project:afk-composable-pipeline"],
                    }
                ],
            )

    def test_workstream_prefers_explicit_auth_failure_from_stdout_over_generic_stderr_excerpt(self):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            failing_agent_code = textwrap.dedent(
                """
                import sys

                print("No API key found for openai-codex.")
                print("Traceback: unit test assertion failed", file=sys.stderr)
                sys.exit(1)
                """
            ).strip()
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["agent"] = {
                "type": "real-agent-command",
                "provider": "openai-codex",
                "command": [sys.executable, "-c", failing_agent_code],
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
            implement_step = result["steps"][2]

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(
                result["pipeline_retrospective"]["signals"],
                [
                    {
                        "kind": "implementation-auth",
                        "scope": "pipeline-process",
                        "severity": "error",
                        "summary": "No API key found for openai-codex.",
                        "step": "implement",
                        "classification": "openai-codex-auth",
                        "excerpt": "No API key found for openai-codex.",
                        "evidence_paths": [
                            f"runs/{implement_step['run_id']}/step-result.json",
                            f"runs/{implement_step['run_id']}/agent-result.json",
                        ],
                    }
                ],
            )
            self.assertEqual(
                result["pipeline_retrospective"]["recommended_follow_up"],
                [
                    {
                        "summary": "Fix implement [openai-codex-auth]: No API key found for openai-codex.",
                        "labels": ["afk:follow-up", "area:implementation", "project:afk-composable-pipeline"],
                    }
                ],
            )

    def test_workstream_prefers_provider_specific_auth_over_generic_auth_across_streams(self):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            failing_agent_code = textwrap.dedent(
                """
                import sys

                print("No API key found for openai-codex.")
                print("Authentication failed", file=sys.stderr)
                sys.exit(1)
                """
            ).strip()
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["agent"] = {
                "type": "real-agent-command",
                "provider": "openai-codex",
                "command": [sys.executable, "-c", failing_agent_code],
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
            implement_step = result["steps"][2]

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(
                result["pipeline_retrospective"]["signals"],
                [
                    {
                        "kind": "implementation-auth",
                        "scope": "pipeline-process",
                        "severity": "error",
                        "summary": "No API key found for openai-codex.",
                        "step": "implement",
                        "classification": "openai-codex-auth",
                        "excerpt": "No API key found for openai-codex.",
                        "evidence_paths": [
                            f"runs/{implement_step['run_id']}/step-result.json",
                            f"runs/{implement_step['run_id']}/agent-result.json",
                        ],
                    }
                ],
            )
            self.assertEqual(
                result["pipeline_retrospective"]["recommended_follow_up"],
                [
                    {
                        "summary": "Fix implement [openai-codex-auth]: No API key found for openai-codex.",
                        "labels": ["afk:follow-up", "area:implementation", "project:afk-composable-pipeline"],
                    }
                ],
            )

    def test_workstream_prefers_provider_specific_auth_line_within_same_stderr_stream(self):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            failing_agent_code = textwrap.dedent(
                """
                import sys

                print("Authentication failed", file=sys.stderr)
                print("Traceback: unit test assertion failed", file=sys.stderr)
                print("No API key found for openai-codex.", file=sys.stderr)
                sys.exit(1)
                """
            ).strip()
            recipe["publisher"] = {"enabled": False}
            recipe["steps"][2]["input"]["agent"] = {
                "type": "real-agent-command",
                "provider": "openai-codex",
                "command": [sys.executable, "-c", failing_agent_code],
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
            implement_step = result["steps"][2]

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(
                result["pipeline_retrospective"]["signals"],
                [
                    {
                        "kind": "implementation-auth",
                        "scope": "pipeline-process",
                        "severity": "error",
                        "summary": "No API key found for openai-codex.",
                        "step": "implement",
                        "classification": "openai-codex-auth",
                        "excerpt": "No API key found for openai-codex.",
                        "evidence_paths": [
                            f"runs/{implement_step['run_id']}/step-result.json",
                            f"runs/{implement_step['run_id']}/agent-result.json",
                        ],
                    }
                ],
            )
            self.assertEqual(
                result["pipeline_retrospective"]["recommended_follow_up"],
                [
                    {
                        "summary": "Fix implement [openai-codex-auth]: No API key found for openai-codex.",
                        "labels": ["afk:follow-up", "area:implementation", "project:afk-composable-pipeline"],
                    }
                ],
            )

    def test_workstream_redacts_bearer_secret_in_selected_auth_failure_line(self):
        for token in ("A1b2C3d4E5f6G7h8", "abcdefghijklmnop=="):
            with self.subTest(token=token):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
                    )
                    write_executable(
                        fake_gh,
                        f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
                    )
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    failing_agent_code = textwrap.dedent(
                        f"""
                        import sys

                        print("Authentication failed: Bearer " + {token!r}, file=sys.stderr)
                        sys.exit(1)
                        """
                    ).strip()
                    recipe["publisher"] = {"enabled": False}
                    recipe["steps"][2]["input"]["agent"] = {
                        "type": "real-agent-command",
                        "provider": "openai-codex",
                        "command": [sys.executable, "-c", failing_agent_code],
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
                    implement_step = result["steps"][2]

                    self.assertEqual(summary["status"], "blocked")
                    self.assertEqual(result["publication"]["status"], "blocked")
                    self.assertEqual(
                        result["pipeline_retrospective"]["signals"],
                        [
                            {
                                "kind": "implementation-auth",
                                "scope": "pipeline-process",
                                "severity": "error",
                                "summary": "Authentication failed: Bearer [REDACTED]",
                                "step": "implement",
                                "classification": "agent-auth",
                                "excerpt": "Authentication failed: Bearer [REDACTED]",
                                "evidence_paths": [
                                    f"runs/{implement_step['run_id']}/step-result.json",
                                    f"runs/{implement_step['run_id']}/agent-result.json",
                                ],
                            }
                        ],
                    )
                    self.assertEqual(
                        result["pipeline_retrospective"]["recommended_follow_up"],
                        [
                            {
                                "summary": "Fix implement [agent-auth]: Authentication failed: Bearer [REDACTED]",
                                "labels": ["afk:follow-up", "area:implementation", "project:afk-composable-pipeline"],
                            }
                        ],
                    )
                    retrospective_text = json.dumps(result["pipeline_retrospective"])
                    self.assertNotIn(token, retrospective_text)

    def test_workstream_redacts_quoted_bearer_secret_in_selected_auth_failure_line(self):
        for token in ('"abcdefghijklmnop=="', "'abcdefghijklmnop'"):
            with self.subTest(token=token):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
                    )
                    write_executable(
                        fake_gh,
                        f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
                    )
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    failing_agent_code = textwrap.dedent(
                        f"""
                        import sys

                        print("Authentication failed: Bearer " + {token!r}, file=sys.stderr)
                        sys.exit(1)
                        """
                    ).strip()
                    recipe["publisher"] = {"enabled": False}
                    recipe["steps"][2]["input"]["agent"] = {
                        "type": "real-agent-command",
                        "provider": "openai-codex",
                        "command": [sys.executable, "-c", failing_agent_code],
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
                    implement_step = result["steps"][2]

                    self.assertEqual(summary["status"], "blocked")
                    self.assertEqual(result["publication"]["status"], "blocked")
                    self.assertEqual(
                        result["pipeline_retrospective"]["signals"],
                        [
                            {
                                "kind": "implementation-auth",
                                "scope": "pipeline-process",
                                "severity": "error",
                                "summary": "Authentication failed: Bearer [REDACTED]",
                                "step": "implement",
                                "classification": "agent-auth",
                                "excerpt": "Authentication failed: Bearer [REDACTED]",
                                "evidence_paths": [
                                    f"runs/{implement_step['run_id']}/step-result.json",
                                    f"runs/{implement_step['run_id']}/agent-result.json",
                                ],
                            }
                        ],
                    )
                    self.assertEqual(
                        result["pipeline_retrospective"]["recommended_follow_up"],
                        [
                            {
                                "summary": "Fix implement [agent-auth]: Authentication failed: Bearer [REDACTED]",
                                "labels": ["afk:follow-up", "area:implementation", "project:afk-composable-pipeline"],
                            }
                        ],
                    )
                    retrospective_text = json.dumps(result["pipeline_retrospective"])
                    self.assertNotIn(token, retrospective_text)

    def test_workstream_redacts_backslash_escaped_quoted_bearer_secret_in_selected_auth_failure_line(self):
        for token in (r"\"abcdefghijklmnop==\"", r"\'abcdefghijklmnop\'"):
            with self.subTest(token=token):
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
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("git should not run\\n", encoding="utf-8")
""",
                    )
                    write_executable(
                        fake_gh,
                        f"""#!{sys.executable}
from pathlib import Path
Path({str(temp_path / "publisher-calls.txt")!r}).write_text("gh should not run\\n", encoding="utf-8")
""",
                    )
                    recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
                    failing_agent_code = textwrap.dedent(
                        f"""
                        import sys

                        print("Authentication failed: Bearer " + {token!r}, file=sys.stderr)
                        sys.exit(1)
                        """
                    ).strip()
                    recipe["publisher"] = {"enabled": False}
                    recipe["steps"][2]["input"]["agent"] = {
                        "type": "real-agent-command",
                        "provider": "openai-codex",
                        "command": [sys.executable, "-c", failing_agent_code],
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
                    implement_step = result["steps"][2]

                    self.assertEqual(summary["status"], "blocked")
                    self.assertEqual(result["publication"]["status"], "blocked")
                    self.assertEqual(
                        result["pipeline_retrospective"]["signals"],
                        [
                            {
                                "kind": "implementation-auth",
                                "scope": "pipeline-process",
                                "severity": "error",
                                "summary": "Authentication failed: Bearer [REDACTED]",
                                "step": "implement",
                                "classification": "agent-auth",
                                "excerpt": "Authentication failed: Bearer [REDACTED]",
                                "evidence_paths": [
                                    f"runs/{implement_step['run_id']}/step-result.json",
                                    f"runs/{implement_step['run_id']}/agent-result.json",
                                ],
                            }
                        ],
                    )
                    self.assertEqual(
                        result["pipeline_retrospective"]["recommended_follow_up"],
                        [
                            {
                                "summary": "Fix implement [agent-auth]: Authentication failed: Bearer [REDACTED]",
                                "labels": ["afk:follow-up", "area:implementation", "project:afk-composable-pipeline"],
                            }
                        ],
                    )
                    retrospective_text = json.dumps(result["pipeline_retrospective"])
                    self.assertNotIn(token, retrospective_text)

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

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_merges_existing_pr_closes_bead_and_runs_retrospective_afterward(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            follow_up = temp_path / "retrospective-follow-up"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
if sys.argv[1:3] == ["close", "central-lve.9"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    if any("mergeCommit" in arg for arg in sys.argv):
        print(json.dumps({{"url": "https://github.example/pr/123", "mergeCommit": {{"oid": "deadbeef"}}, "mergedAt": "2026-06-29T12:00:00Z"}}))
    else:
        print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}))
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "merge", "123"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                follow_up,
                f"""#!{sys.executable}
import json
import os
from pathlib import Path

record = {{
    "tool": "follow-up",
    "request_path": os.environ["AFK_RETROSPECTIVE_FOLLOW_UP_REQUEST"],
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
Path(os.environ["AFK_RETROSPECTIVE_FOLLOW_UP_RESULT"]).write_text(
    json.dumps({{"status": "created", "created": [{{"id": "central-r5kv", "summary": "Document close-mode follow-up."}}]}}),
    encoding="utf-8",
)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False
            recipe["review_cycles"] = [
                {
                    "status": "findings-addressed",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "request-changes",
                            "summary": "Please address the review findings before merge.",
                            "requires_response": True,
                            "response": {"status": "addressed", "summary": "Addressed on the PR."},
                        }
                    ],
                }
            ]
            recipe["retrospective"] = {
                "summary": "Merged the published PR and closed the source bead.",
                "changes": ["Merged PR #123 after addressed review feedback."],
                "follow_up": {
                    "recommended": [
                        {
                            "id": "central-r5kv",
                            "summary": "Document close-mode follow-up.",
                            "labels": ["project:afk-composable-pipeline"],
                        }
                    ]
                },
            }
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [str(follow_up)],
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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["publication"]["mode"], "close")
            self.assertEqual(result["publication"]["url"], "https://github.example/pr/123")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["close_reason"], "merged via deadbeef")
            self.assertEqual(result["tracker"]["terminal_decision"]["status"], "merged")
            self.assertEqual(result["tracker"]["terminal_decision"]["merge_commit"], "deadbeef")
            self.assertEqual(result["tracker"]["terminal_decision"]["pr_url"], "https://github.example/pr/123")
            self.assertEqual(
                [call["tool"] for call in calls],
                ["bd", "bd", "gh", "gh", "gh", "gh", "bd"],
            )
            self.assertEqual(calls[-1]["argv"][0:2], ["close", "central-lve.9"])
            self.assertEqual(
                result["pipeline_retrospective"]["follow_up"]["creation"]["status"],
                "recommendation-only",
            )

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_records_merged_terminal_decision_when_bead_close_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
if sys.argv[1:3] == ["close", "central-lve.9"]:
    print(f"BEADS_DOLT_PASSWORD={{os.environ.get('BEADS_DOLT_PASSWORD', '')}}")
    print(os.environ.get("BEADS_DOLT_PASSWORD", ""), file=sys.stderr)
    print("beads close exploded", file=sys.stderr)
    raise SystemExit(7)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    if any("mergeCommit" in arg for arg in sys.argv):
        print(json.dumps({{"url": "https://github.example/pr/123", "mergeCommit": {{"oid": "deadbeef"}}, "mergedAt": "2026-06-29T12:00:00Z"}}))
    else:
        print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}))
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "merge", "123"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False
            recipe["review_cycles"] = [
                {
                    "status": "findings-addressed",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "request-changes",
                            "summary": "Please address the review findings before merge.",
                            "requires_response": True,
                            "response": {"status": "addressed", "summary": "Addressed on the PR."},
                        }
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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["mode"], "close")
            self.assertEqual(result["publication"]["url"], "https://github.example/pr/123")
            self.assertEqual(result["publication"]["merge_commit"], "deadbeef")
            self.assertEqual(result["publication"]["terminal_decision"]["status"], "merged")
            self.assertEqual(result["publication"]["terminal_decision"]["merge_commit"], "deadbeef")
            self.assertEqual(result["publication"]["terminal_decision"]["pr_url"], "https://github.example/pr/123")
            self.assertEqual(result["publication"]["tracker_close"]["status"], "failed")
            self.assertEqual(result["publication"]["tracker_close"]["tool"], "bd")
            self.assertEqual(result["publication"]["tracker_close"]["reason"], "bd close failed")
            self.assertIn("source item remains open", result["publication"]["tracker_close"]["remediation"])
            self.assertEqual(
                result["publication"]["tracker_close"]["stdout_excerpt"],
                "BEADS_DOLT_PASSWORD=[REDACTED]\n",
            )
            self.assertEqual(
                result["publication"]["tracker_close"]["stderr_excerpt"],
                "[REDACTED]\nbeads close exploded\n",
            )
            self.assertEqual(result["tracker"]["terminal_decision"]["status"], "merged")
            self.assertEqual(result["tracker"]["merge_commit"], "deadbeef")
            self.assertEqual(result["tracker"]["pr_url"], "")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertIn("closure failed", result["tracker"]["comment"])
            self.assertNotIn("test-password", json.dumps(result))
            self.assertEqual(
                [call["tool"] for call in calls],
                ["bd", "bd", "gh", "gh", "gh", "gh", "bd"],
            )
            self.assertEqual(calls[-1]["argv"][0:2], ["close", "central-lve.9"])

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_records_blocked_terminal_decision_without_closing_bead(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            follow_up = temp_path / "retrospective-follow-up"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                follow_up,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write('{{"tool":"follow-up"}}\\n')
raise SystemExit(0)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "BLOCKED", "headRefOid": "abc123"}}))
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False
            recipe["review_cycles"] = [
                {
                    "status": "findings-addressed",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "request-changes",
                            "summary": "Please address the review findings before merge.",
                            "requires_response": True,
                            "response": {"status": "addressed", "summary": "Addressed on the PR."},
                        }
                    ],
                }
            ]
            recipe["retrospective"] = {
                "summary": "Should not be recorded before close mode reaches a merged or no-merge terminal closure.",
                "changes": ["Blocked merge left terminal evidence unavailable."],
            }
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [str(follow_up)],
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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(result["publication"]["mode"], "close")
            self.assertEqual(result["tracker"]["status"], "review-feedback-addressed")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["terminal_decision"]["status"], "blocked")
            self.assertEqual(result["tracker"]["terminal_decision"]["pr_url"], "https://github.example/pr/123")
            self.assertIn("BLOCKED", result["tracker"]["terminal_decision"]["reason"])
            self.assertEqual(result["retrospective"], {})
            self.assertEqual(result["tracker"]["retrospective"], {})
            self.assertNotIn("retrospective", result["artifacts"])
            self.assertNotIn("retrospective_follow_up_request", result["artifacts"])
            self.assertNotIn("retrospective_follow_up_result", result["artifacts"])
            self.assertFalse((ledger / "workstreams" / summary["run_id"] / "retrospective.json").exists())
            self.assertFalse((ledger / "workstreams" / summary["run_id"] / "retrospective-follow-up-request.json").exists())
            self.assertFalse((ledger / "workstreams" / summary["run_id"] / "retrospective-follow-up-result.json").exists())
            self.assertEqual([call["tool"] for call in calls], ["bd", "bd", "gh", "gh"])

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_requires_recorded_review_cycles_or_explicit_waiver(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}))
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "validated")
            self.assertEqual(result["status"], "validated")
            self.assertEqual(result["publication"]["status"], "tracker-close-blocked")
            self.assertEqual(result["publication"]["mode"], "close")
            self.assertIn("review cycle evidence", result["publication"]["reason"])
            self.assertEqual(result["tracker"]["status"], "validated")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["terminal_decision"]["status"], "blocked")
            self.assertEqual(result["tracker"]["terminal_decision"]["pr_url"], "https://github.example/pr/123")
            self.assertIn("review cycle evidence", result["tracker"]["comment"])
            self.assertEqual([call["tool"] for call in calls], ["bd", "bd", "gh", "gh"])

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_allows_explicit_waiver_without_recorded_review_cycles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
if sys.argv[1:3] == ["close", "central-lve.9"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    if any("mergeCommit" in arg for arg in sys.argv):
        print(json.dumps({{"url": "https://github.example/pr/123", "mergeCommit": {{"oid": "deadbeef"}}, "mergedAt": "2026-06-29T12:00:00Z"}}))
    else:
        print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}))
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "merge", "123"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False
            recipe["tracker"] = {"terminal_decision": {"review_feedback_status": "waived"}}

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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["publication"]["mode"], "close")
            self.assertEqual(result["publication"]["terminal_decision"]["review_feedback_status"], "waived")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertIn("explicitly waived", result["tracker"]["comment"])
            self.assertEqual(
                [call["tool"] for call in calls],
                ["bd", "bd", "gh", "gh", "gh", "gh", "bd"],
            )

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_allows_explicitly_resolved_review_feedback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
if sys.argv[1:3] == ["close", "central-lve.9"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    if any("mergeCommit" in arg for arg in sys.argv):
        print(json.dumps({{"url": "https://github.example/pr/123", "mergeCommit": {{"oid": "deadbeef"}}, "mergedAt": "2026-06-29T12:00:00Z"}}))
    else:
        print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}))
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "merge", "123"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False
            recipe["review_cycles"] = [
                {
                    "status": "request-changes",
                    "reviews": [
                        {
                            "role": "bug-risk",
                            "status": "request-changes",
                            "summary": "Please reply on the PR before merge.",
                            "requires_response": True,
                        }
                    ],
                }
            ]
            recipe["tracker"] = {"terminal_decision": {"review_feedback_status": "resolved"}}

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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["publication"]["terminal_decision"]["status"], "merged")
            self.assertEqual(result["publication"]["terminal_decision"]["review_feedback_status"], "resolved")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertIn("resolved before closure", result["tracker"]["comment"])
            self.assertEqual(
                [call["tool"] for call in calls],
                ["bd", "bd", "gh", "gh", "gh", "gh", "bd"],
            )

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_uses_runtime_review_cycles_after_review_feedback_repair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            review_count = temp_path / "review-count.txt"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
if sys.argv[1:3] == ["close", "central-lve.9"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    if any("mergeCommit" in arg for arg in sys.argv):
        print(json.dumps({{"url": "https://github.example/pr/123", "mergeCommit": {{"oid": "deadbeef"}}, "mergedAt": "2026-06-29T12:00:00Z"}}))
    else:
        print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}))
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "merge", "123"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            agent_code = textwrap.dedent(
                f"""
                import json
                import os
                import subprocess
                from pathlib import Path

                capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
                repair = capsule.get("repair_context")
                if repair:
                    Path("repair.txt").write_text("repair\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "repair.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "repair review feedback"], check=True)
                else:
                    Path("implemented.txt").write_text("initial\\n", encoding="utf-8")
                    subprocess.run(["git", "add", "implemented.txt"], check=True)
                    subprocess.run(["git", "commit", "-m", "initial implementation"], check=True)
                Path("agent-result.json").write_text(
                    json.dumps({{"status": "completed", "summary": "implementation complete"}}),
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
                            "summary": "tests passed",
                            "steps": [{"name": "unit", "status": "pass"}],
                        }
                    ),
                    encoding="utf-8",
                )
                """
            ).strip()
            reviewer_code = textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path

                count_path = Path({str(review_count)!r})
                prior = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
                count_path.write_text(str(prior + 1), encoding="utf-8")
                if prior == 0:
                    payload = {{
                        "status": "request_revision",
                        "summary": "review requested changes",
                        "findings": [
                            {{
                                "status": "request_revision",
                                "severity": "high",
                                "file": "src/demo.py",
                                "line": 41,
                                "required_fix": "Handle the empty review cycle before publishing.",
                                "summary": "Tracker close path still misses the empty review cycle case.",
                            }}
                        ],
                    }}
                else:
                    payload = {{
                        "status": "pass",
                        "summary": "review passed after repair",
                        "findings": [],
                    }}
                Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(json.dumps(payload), encoding="utf-8")
                """
            ).strip()
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False
            recipe["retry_policy"] = {"max_retries": 1}
            recipe["review_feedback"] = {"enabled": True}
            recipe["steps"][2]["input"]["agent"]["command"] = [sys.executable, "-c", agent_code]
            recipe["steps"][3]["input"]["worker"]["command"] = [sys.executable, "-c", worker_code]
            recipe["steps"][4]["input"]["role"] = "correctness"
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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "closed")
            self.assertEqual(result["status"], "closed")
            self.assertEqual(result["publication"]["status"], "tracker-closed")
            self.assertEqual(result["publication"]["mode"], "close")
            self.assertEqual(result["publication"]["terminal_decision"]["status"], "merged")
            self.assertEqual(result["tracker"]["status"], "closed")
            self.assertTrue(result["tracker"]["close_source_item"])
            self.assertEqual(len(result["review_cycles"]), 2)
            self.assertEqual(result["review_cycles"][0]["status"], "findings-addressed")
            self.assertEqual(result["review_cycles"][1]["status"], "passed")
            self.assertEqual(
                [call["tool"] for call in calls],
                ["bd", "bd", "gh", "gh", "gh", "gh", "bd"],
            )

    @unittest.skip("publisher close mode moved out of the minimal run-workstream path")
    def test_workstream_close_mode_blocks_unresolved_review_feedback_without_explicit_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            beads_workspace = temp_path / "beads"
            beads_secret_dir = beads_workspace / "secrets"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            beads_secret_dir.mkdir(parents=True)
            (beads_secret_dir / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            fake_bd = fake_bin / "bd"
            write_executable(
                fake_git,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(fake_calls)!r}).write_text("git should not run\\n", encoding="utf-8")
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "bd",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-lve.9"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-lve.9", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-lve.9",
                "title": "Compose workstream recipe and terminal PR publisher",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-lve", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Merge the published PR\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

record = {{"tool": "gh", "argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:4] == ["pr", "view", "123"]:
    print(json.dumps({{"url": "https://github.example/pr/123", "isDraft": False, "state": "OPEN", "mergeStateStatus": "CLEAN", "headRefOid": "abc123"}}))
    raise SystemExit(0)
raise SystemExit(9)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["steps"][0]["input"] = {
                "required_labels": ["ready-for-agent"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "status": "open",
                        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                    }
                ],
            }
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["pr"] = "123"
            recipe["publisher"]["git"]["push"] = False
            recipe["review_cycles"] = [
                {
                    "status": "request-changes",
                    "reviews": [
                        {
                            "role": "bug-risk",
                            "status": "request-changes",
                            "summary": "Please reply on the PR before merge.",
                            "requires_response": True,
                        }
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
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["status"], "review-findings-open")
            self.assertEqual(result["publication"]["status"], "tracker-close-blocked")
            self.assertEqual(result["publication"]["mode"], "close")
            self.assertIn("review_feedback_status", result["publication"]["reason"])
            self.assertEqual(result["tracker"]["status"], "review-findings-open")
            self.assertFalse(result["tracker"]["close_source_item"])
            self.assertEqual(result["tracker"]["terminal_decision"]["status"], "blocked")
            self.assertEqual(result["tracker"]["terminal_decision"]["pr_url"], "https://github.example/pr/123")
            self.assertEqual([call["tool"] for call in calls], ["bd", "bd", "gh", "gh"])

    def test_workstream_rejects_close_mode(self):
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
            recipe["publisher"]["mode"] = "close"
            recipe["publisher"]["git"]["push"] = False

            completed = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-lve.9",
                "--input",
                json.dumps(recipe),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertEqual(result["publication"]["reason"], "publisher.mode must be create or update")

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

    def test_workstream_pr_body_uses_indexed_fallback_for_unnamed_validation(self):
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
                "- validation-1: validated - result: missing - command: missing - summary: missing - evidence: runs/validate/step-result.json",
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
            self.assertEqual(result["tracker"]["pr_url"], "")
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

    def test_workstream_omits_ledger_flag_in_blocked_retry_when_using_default_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledgers"
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
                cwd=temp_path,
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

            self.assertIn(result["publication"]["status"], {"blocked", "failed-needs-human"})
            self.assertEqual(
                result["next_allowed_command"],
                "afk run-workstream --workstream-id central-lve.9 --input <recipe>",
            )
            self.assertIn(
                "afk run-workstream --workstream-id central-lve.9 --input <recipe>",
                result["retry"],
            )
            self.assertNotIn("--ledger", result["retry"])

    def test_workstream_preserves_explicit_ledger_path_in_blocked_retry_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "explicit ledger"
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
            expected = (
                "afk run-workstream --workstream-id central-lve.9 "
                f"--ledger {shlex.quote(str(ledger))} --input <recipe>"
            )

            self.assertEqual(result["publication"]["status"], "failed-needs-human")
            self.assertEqual(result["next_allowed_command"], expected)
            self.assertIn(expected, result["retry"])
            self.assertEqual(result["publication"]["next_allowed_command"], expected)

    def test_workstream_preserves_afk_ledger_dir_argument_in_blocked_retry_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger_arg = "relative ledger"
            ledger = temp_path / ledger_arg
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
                cwd=temp_path,
                env_overrides={
                    "AFK_LEDGER_DIR": ledger_arg,
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
            expected = (
                "afk run-workstream --workstream-id central-lve.9 "
                f"--ledger {shlex.quote(ledger_arg)} --input <recipe>"
            )

            self.assertEqual(result["publication"]["status"], "blocked")
            self.assertEqual(result["next_allowed_command"], expected)
            self.assertIn(expected, result["retry"])
            self.assertEqual(result["publication"]["next_allowed_command"], expected)

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
                f"afk run-workstream --workstream-id central-lve.9 --ledger {ledger} --input <recipe>",
            )
            self.assertNotIn("pr_body", result["artifacts"])
            self.assertFalse(result_path.parent.joinpath("pr-body.md").exists())
            self.assertFalse(fake_calls.exists())

    def test_workstream_disabled_close_mode_omits_terminal_retrospective_until_terminal_closure_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            follow_up_ran = temp_path / "follow-up-ran.txt"
            init_repo(repo)
            fake_git = temp_path / "publisher-git"
            fake_gh = temp_path / "publisher-gh"
            follow_up = temp_path / "retrospective-follow-up"
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
            write_executable(
                follow_up,
                f"""#!{sys.executable}
from pathlib import Path
Path({str(follow_up_ran)!r}).write_text("ran\\n", encoding="utf-8")
raise SystemExit(0)
""",
            )
            recipe = successful_recipe(temp_path, repo, checkout, fake_git, fake_gh)
            recipe["publisher"] = {"enabled": False, "mode": "close", "pr": "123"}
            recipe["retrospective"] = {
                "summary": "Should not be recorded while close mode is disabled.",
                "changes": ["Terminal evidence is only valid after merged or no-merge closure."],
            }
            recipe["retrospective_follow_up"] = {
                "enabled": True,
                "type": "local-command",
                "command": [str(follow_up)],
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
            result_path = ledger / summary["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(result["publication"]["status"], "validated-unpublished")
            self.assertEqual(result["retrospective"], {})
            self.assertEqual(result["tracker"]["retrospective"], {})
            self.assertNotIn("retrospective", result["artifacts"])
            self.assertNotIn("retrospective_follow_up_request", result["artifacts"])
            self.assertFalse(result_path.parent.joinpath("retrospective.json").exists())
            self.assertFalse(result_path.parent.joinpath("retrospective-follow-up-request.json").exists())
            self.assertFalse(follow_up_ran.exists())

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

            self.assertEqual(result["review_cycles"][0], expected_cycles[0])
            self.assertEqual(result["review_cycles"][1]["status"], "passed")
            self.assertEqual(
                [review["role"] for review in result["review_cycles"][1]["reviews"]],
                ["correctness", "bug-risk"],
            )
            self.assertEqual(
                [review["status"] for review in result["review_cycles"][1]["reviews"]],
                ["passed", "passed"],
            )
            self.assertEqual(result["tracker"]["review_cycles"], result["review_cycles"])
            self.assertEqual(tracker["review_cycles"], result["review_cycles"])
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

            self.assertEqual(len(result["review_cycles"]), 3)
            self.assertEqual(result["review_cycles"][0]["status"], "findings-open")
            self.assertEqual(result["review_cycles"][1]["status"], "findings-addressed")
            self.assertEqual(result["review_cycles"][2]["status"], "passed")
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
            self.assertEqual(
                [review["role"] for review in result["review_cycles"][2]["reviews"]],
                ["correctness", "bug-risk"],
            )
            self.assertEqual(
                [review["status"] for review in result["review_cycles"][2]["reviews"]],
                ["passed", "passed"],
            )
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
