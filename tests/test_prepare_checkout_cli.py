import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


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


def commit_file(repo, relative_path, content, message):
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", relative_path)
    git(repo, "commit", "-m", message)


def create_repo_with_submodule(temp_path):
    submodule_repo = temp_path / "submodule-src"
    init_repo(submodule_repo)
    commit_file(submodule_repo, "submodule.txt", "submodule content\n", "seed submodule")
    submodule_sha = git(submodule_repo, "rev-parse", "HEAD")

    repo = temp_path / "repo-src"
    init_repo(repo)
    commit_file(repo, "README.md", "root repo\n", "seed root")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(submodule_repo), "deps/submodule")
    git(repo, "commit", "-m", "add submodule")
    start_commit = git(repo, "rev-parse", "HEAD")

    return repo, start_commit, submodule_sha


def relative_gitdir_path(submodule_checkout):
    git_file = submodule_checkout / ".git"
    prefix = "gitdir: "
    gitdir_text = git_file.read_text(encoding="utf-8").strip()
    if not gitdir_text.startswith(prefix):
        raise AssertionError(gitdir_text)
    return (submodule_checkout / gitdir_text[len(prefix) :]).resolve()


class PrepareCheckoutCliTest(unittest.TestCase):
    def test_prepare_checkout_creates_real_clone_with_local_submodule_gitdir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo, start_commit, submodule_sha = create_repo_with_submodule(temp_path)
            checkout_path = temp_path / "checkout"
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "prepare-checkout",
                "--input",
                json.dumps(
                    {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_path": str(checkout_path),
                        "review_branch": "afk/test-review",
                    }
                ),
                "--ledger",
                str(ledger),
                env_overrides={"GIT_ALLOW_PROTOCOL": "file"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            prepared = result["output"]

            self.assertEqual(summary["step"], "prepare-checkout")
            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(prepared["repo_url"], str(repo))
            self.assertEqual(prepared["base_ref"], "main")
            self.assertEqual(prepared["requested_ref"], "main")
            self.assertEqual(prepared["start_commit"], start_commit)
            self.assertEqual(prepared["review_branch"], "afk/test-review")
            self.assertEqual(prepared["checkout_path"], str(checkout_path))
            self.assertEqual(prepared["dirty"], False)
            self.assertEqual(prepared["publication"]["status"], "skipped_disabled")

            self.assertTrue((checkout_path / ".git").is_dir())
            submodule_gitdir = relative_gitdir_path(checkout_path / "deps/submodule")
            modules_dir = (checkout_path / ".git/modules").resolve()
            self.assertTrue(str(submodule_gitdir).startswith(str(modules_dir)))

            self.assertEqual(
                prepared["submodules"],
                [
                    {
                        "path": "deps/submodule",
                        "sha": submodule_sha,
                        "gitdir": ".git/modules/deps/submodule",
                    }
                ],
            )

    def test_prepare_checkout_refuses_dirty_existing_checkout_with_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo, _start_commit, _submodule_sha = create_repo_with_submodule(temp_path)
            checkout_path = temp_path / "checkout"
            ledger = temp_path / "ledger"
            payload = {
                "repo_url": str(repo),
                "base_ref": "main",
                "checkout_path": str(checkout_path),
                "review_branch": "afk/test-review",
            }

            first_run = run_afk(
                "run-step",
                "prepare-checkout",
                "--input",
                json.dumps(payload),
                "--ledger",
                str(ledger),
                env_overrides={"GIT_ALLOW_PROTOCOL": "file"},
            )
            self.assertEqual(first_run.returncode, 0, first_run.stderr)
            (checkout_path / "README.md").write_text("dirty change\n", encoding="utf-8")

            second_run = run_afk(
                "run-step",
                "prepare-checkout",
                "--input",
                json.dumps(payload),
                "--ledger",
                str(ledger),
                env_overrides={"GIT_ALLOW_PROTOCOL": "file"},
            )

            self.assertEqual(second_run.returncode, 0, second_run.stderr)
            summary = json.loads(second_run.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            failed = result["output"]

            self.assertEqual(failed["status"], "failed_dirty_checkout")
            self.assertEqual(failed["dirty"], True)
            self.assertIn("commit, stash, or remove", failed["message"])
            self.assertIn("M README.md", failed["dirty_status"])

    def test_prepare_checkout_can_publish_requested_review_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo, _start_commit, _submodule_sha = create_repo_with_submodule(temp_path)
            checkout_path = temp_path / "checkout"
            ledger = temp_path / "ledger"

            completed = run_afk(
                "run-step",
                "prepare-checkout",
                "--input",
                json.dumps(
                    {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_path": str(checkout_path),
                        "review_branch": "afk/test-review",
                        "publish": {"enabled": True, "branch": "afk/published-review"},
                    }
                ),
                "--ledger",
                str(ledger),
                env_overrides={"GIT_ALLOW_PROTOCOL": "file"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            prepared = result["output"]

            self.assertEqual(
                prepared["publication"],
                {
                    "status": "published",
                    "enabled": True,
                    "remote": "origin",
                    "branch": "afk/published-review",
                    "ref": "origin/afk/published-review",
                },
            )
            self.assertEqual(
                git(repo, "rev-parse", "refs/heads/afk/published-review"),
                prepared["start_commit"],
            )

    def test_prepare_checkout_refuses_existing_worktree_without_local_git_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            init_repo(repo)
            commit_file(repo, "README.md", "root repo\n", "seed root")
            worktree_path = temp_path / "worktree"
            git(repo, "worktree", "add", "-b", "afk/worktree", str(worktree_path), "main")

            completed = run_afk(
                "run-step",
                "prepare-checkout",
                "--input",
                json.dumps(
                    {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_path": str(worktree_path),
                        "review_branch": "afk/test-review",
                    }
                ),
                "--ledger",
                str(temp_path / "ledger"),
                env_overrides={"GIT_ALLOW_PROTOCOL": "file"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = temp_path / "ledger/runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            failed = result["output"]

            self.assertEqual(failed["status"], "failed_existing_checkout")
            self.assertIn("local .git directory", failed["message"])

    def test_prepare_checkout_rejects_malformed_publish_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo, _start_commit, _submodule_sha = create_repo_with_submodule(temp_path)

            completed = run_afk(
                "run-step",
                "prepare-checkout",
                "--input",
                json.dumps(
                    {
                        "repo_url": str(repo),
                        "base_ref": "main",
                        "checkout_path": str(temp_path / "checkout"),
                        "review_branch": "afk/test-review",
                        "publish": {"enabled": "false"},
                    }
                ),
                "--ledger",
                str(temp_path / "ledger"),
                env_overrides={"GIT_ALLOW_PROTOCOL": "file"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = temp_path / "ledger/runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            failed = result["output"]

            self.assertEqual(failed["status"], "failed_invalid_payload")
            self.assertEqual(failed["message"], "publish.enabled must be a boolean")


if __name__ == "__main__":
    unittest.main()
