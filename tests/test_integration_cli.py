import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_afk(*args, env_extra=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "afk", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def git(cwd: Path, *args: str) -> str:
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


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "--initial-branch", "main")
    git(path, "config", "user.name", "AFK Test")
    git(path, "config", "user.email", "afk-test@example.test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "seed")


def write_workstream_result(
    path: Path,
    *,
    expected_head: str,
    pr_url: str = "https://github.com/acme/widgets/pull/17",
    selected_work=None,
    select_sources=None,
    tracker_terminal_decision=None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    steps = []
    if select_sources is not None:
        steps.append({"name": "select-work", "input": {"sources": select_sources}})
    steps.append(
        {
            "name": "implement",
            "output": {
                "git": {
                    "after_commit": expected_head,
                }
            },
        }
    )
    path.write_text(
        json.dumps(
            {
                "workstream_id": "central-umi2.1",
                "review_branch": "afk/central-umi2-1",
                "selected_work": selected_work or [],
                "tracker": {"terminal_decision": tracker_terminal_decision or {}},
                "publication": {
                    "status": "published",
                    "url": pr_url,
                },
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )


def write_publication_result(path: Path, *, pr_url: str = "https://github.com/acme/widgets/pull/17") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "published",
                "url": pr_url,
            }
        ),
        encoding="utf-8",
    )


def fake_gh_script(fake_calls: Path) -> str:
    return f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "tool": "gh",
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
if "--body-file" in sys.argv:
    body_file = sys.argv[sys.argv.index("--body-file") + 1]
    record["body"] = Path(body_file).read_text(encoding="utf-8")
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/acme/widgets/pull/17")
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    config_dir = Path(os.environ["GH_CONFIG_DIR"])
    if any("mergeCommit" in arg for arg in sys.argv):
        merged_path = config_dir / "view-merged.json"
        if merged_path.exists():
            print(merged_path.read_text(encoding="utf-8"))
        else:
            open_view = json.loads(config_dir.joinpath("view.json").read_text(encoding="utf-8"))
            print(json.dumps({{
                "url": open_view.get("url", ""),
                "mergeCommit": {{"oid": "deadbeef"}},
                "mergedAt": "2026-07-06T12:00:00Z",
            }}))
    else:
        print(config_dir.joinpath("view.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("checks.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "merge"]:
    raise SystemExit(0)
raise SystemExit(9)
"""


def output_dir_for(workstream_path: Path) -> Path:
    return workstream_path.parent / "output"


def lifecycle_recipe(temp_path: Path, repo: Path, checkout: Path, fake_git: Path, fake_gh: Path, beads_workspace: Path) -> dict[str, object]:
    agent_code = textwrap.dedent(
        """
        import json
        import subprocess
        from pathlib import Path

        Path("implemented.txt").write_text("central-umi2.3\\n", encoding="utf-8")
        subprocess.run(["git", "add", "implemented.txt"], check=True)
        subprocess.run(["git", "commit", "-m", "implement central-umi2.3"], check=True)
        Path("agent-result.json").write_text(
            json.dumps({"status": "completed", "summary": "implemented lifecycle smoke"}),
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
        "workstream_id": "central-umi2.3",
        "parent": "central-umi2",
        "review_branch": "afk/central-umi2-3",
        "steps": [
            {
                "name": "select-work",
                "input": {
                    "required_labels": ["ready-for-agent"],
                    "sources": [
                        {
                            "type": "beads",
                            "id": "central",
                            "workspace": str(beads_workspace),
                            "workspace_kind": "central",
                            "status": "open",
                            "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
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
            "repo": "acme/widgets",
            "base": "main",
            "head": "afk/central-umi2-3",
            "title": "central-umi2.3: lifecycle smoke",
        },
        "tracker": {"terminal_decision": {"review_feedback_status": "waived"}},
    }


class IntegrationCliTest(unittest.TestCase):
    def test_integrate_pr_smoke_runs_full_fake_backed_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            checkout = temp_path / "checkout"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "beads"
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            init_repo(repo)
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("secret-password\n", encoding="utf-8")
            fake_calls = temp_path / "fake-calls.jsonl"
            fake_git = temp_path / "publisher-git"
            fake_gh = fake_bin / "gh"
            fake_bd = fake_bin / "bd"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "placeholder",
                        "statusCheckRollup": [{"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "view-merged.json").write_text(
                json.dumps(
                    {
                        "url": "https://github.com/acme/widgets/pull/17",
                        "mergeCommit": {"oid": "deadbeef"},
                        "mergedAt": "2026-07-06T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(
                fake_git,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps({{"tool": "git", "argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n"
)
raise SystemExit(0)
""",
            )
            write_executable(fake_gh, fake_gh_script(fake_calls))
            write_executable(
                fake_bd,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(
    json.dumps(
        {{
            "tool": "bd",
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "password": os.environ.get("BEADS_DOLT_PASSWORD", ""),
        }}
    ) + "\\n"
)
if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-umi2.3"}}]))
    raise SystemExit(0)
if sys.argv[1:4] == ["show", "central-umi2.3", "--json"]:
    print(
        json.dumps(
            {{
                "id": "central-umi2.3",
                "title": "Add fake-backed full lifecycle terminal integration smoke",
                "status": "open",
                "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
                "metadata": {{"workstream": "central-umi2", "afk.ready": True}},
                "description": "Acceptance Criteria\\n- Add fake-backed smoke\\n",
                "dependencies": [],
            }}
        )
    )
    raise SystemExit(0)
if sys.argv[1:3] == ["close", "central-umi2.3"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )

            workstream_recipe = lifecycle_recipe(temp_path, repo, checkout, fake_git, fake_gh, beads_workspace)
            published = run_afk(
                "run-workstream",
                "--workstream-id",
                "central-umi2.3",
                "--input",
                json.dumps(workstream_recipe),
                "--ledger",
                str(ledger),
                env_extra={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "GIT_ALLOW_PROTOCOL": "file",
                    "GIT_AUTHOR_NAME": "AFK Test",
                    "GIT_AUTHOR_EMAIL": "afk-test@example.test",
                    "GIT_COMMITTER_NAME": "AFK Test",
                    "GIT_COMMITTER_EMAIL": "afk-test@example.test",
                },
            )

            self.assertEqual(published.returncode, 0, published.stderr)
            published_summary = json.loads(published.stdout)
            workstream_path = ledger / published_summary["result_path"]
            workstream_result = json.loads(workstream_path.read_text(encoding="utf-8"))
            implement_step = next(step for step in workstream_result["steps"] if step["name"] == "implement")
            implement_result = json.loads((ledger / implement_step["result_path"]).read_text(encoding="utf-8"))
            implemented_head = implement_result["output"]["git"]["after_commit"]
            open_view = json.loads((auth_dir / "view.json").read_text(encoding="utf-8"))
            open_view["headRefOid"] = implemented_head
            (auth_dir / "view.json").write_text(json.dumps(open_view), encoding="utf-8")

            integrated = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
                env_extra={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
            )

            self.assertEqual(integrated.returncode, 0, integrated.stderr)
            integrated_summary = json.loads(integrated.stdout)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(published_summary["status"], "published")
            self.assertEqual(workstream_result["publication"]["status"], "published")
            self.assertEqual(workstream_result["tracker"]["status"], "awaiting-review")
            self.assertEqual(integrated_summary["command"], "integrate-pr")
            self.assertEqual(integrated_summary["decision"], "merge_ready")
            self.assertEqual(result["status"], "tracker-closed")
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(result["expected_head_sha"], implemented_head)
            self.assertEqual(result["observed_head_sha"], implemented_head)
            self.assertEqual(result["merge"]["status"], "merged")
            self.assertEqual(result["merge"]["matched_head_sha"], implemented_head)
            self.assertEqual(result["terminal_decision"]["status"], "merged")
            self.assertEqual(result["terminal_decision"]["merge_commit"], "deadbeef")
            self.assertEqual(result["tracker_close"]["status"], "closed")
            self.assertEqual(result["tracker_close"]["command"], ["bd", "close", "central-umi2.3", "--reason", "merged via deadbeef"])
            self.assertEqual(calls[0]["tool"], "bd")
            self.assertEqual(calls[0]["argv"][:2], ["list", "--json"])
            self.assertEqual(calls[1]["tool"], "bd")
            self.assertEqual(calls[1]["argv"][:2], ["show", "central-umi2.3"])
            self.assertIn(["push", "origin"], [call["argv"][:2] for call in calls if call["tool"] == "git"])
            gh_commands = [call["argv"][:2] for call in calls if call["tool"] == "gh"]
            self.assertIn(["pr", "create"], gh_commands)
            self.assertEqual(gh_commands[-4:], [["auth", "status"], ["pr", "view"], ["pr", "merge"], ["pr", "view"]])
            self.assertEqual(calls[-1]["tool"], "bd")
            self.assertEqual(calls[-1]["argv"][:2], ["close", "central-umi2.3"])

    def test_integrate_pr_merges_exact_head_and_closes_bead(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            beads_workspace = temp_path / "beads"
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("secret-password\n", encoding="utf-8")
            workstream_path = temp_path / "ledger" / "workstreams" / "run-merge" / "workstream-result.json"
            write_workstream_result(
                workstream_path,
                expected_head="abc123",
                selected_work=[
                    {
                        "source_type": "beads",
                        "source_id": "central",
                        "external_id": "central-umi2.2",
                    }
                ],
                select_sources=[
                    {
                        "id": "central",
                        "type": "beads",
                        "workspace": str(beads_workspace),
                    }
                ],
                tracker_terminal_decision={"review_feedback_status": "resolved"},
            )
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_bd = fake_bin / "bd"
            fake_gh_calls = temp_path / "fake-gh-calls.jsonl"
            fake_bd_calls = temp_path / "fake-bd-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "view-merged.json").write_text(
                json.dumps(
                    {
                        "url": "https://github.com/acme/widgets/pull/17",
                        "mergeCommit": {"oid": "deadbeef"},
                        "mergedAt": "2026-07-06T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}}
Path({str(fake_gh_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    target = "view-merged.json" if any("mergeCommit" in arg for arg in sys.argv) else "view.json"
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath(target).read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("checks.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "merge"]:
    raise SystemExit(0)
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
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "password": os.environ.get("BEADS_DOLT_PASSWORD", ""),
}}
Path({str(fake_bd_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
raise SystemExit(0)
""",
            )

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
                env_extra={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            gh_calls = [json.loads(line) for line in fake_gh_calls.read_text(encoding="utf-8").splitlines()]
            bd_calls = [json.loads(line) for line in fake_bd_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(result["status"], "tracker-closed")
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(result["terminal_decision"]["status"], "merged")
            self.assertEqual(result["terminal_decision"]["merge_commit"], "deadbeef")
            self.assertEqual(result["terminal_decision"]["review_feedback_status"], "resolved")
            self.assertEqual(result["merge"]["status"], "merged")
            self.assertEqual(result["merge"]["method"], "merge")
            self.assertEqual(result["merge"]["matched_head_sha"], "abc123")
            self.assertEqual(result["merge"]["merge_commit"], "deadbeef")
            self.assertEqual(result["tracker_close"]["status"], "closed")
            self.assertEqual(result["commands"]["gh_merge"][-2:], ["--match-head-commit", "abc123"])
            self.assertEqual(result["tracker_close"]["command"], ["bd", "close", "central-umi2.2", "--reason", "merged via deadbeef"])
            self.assertEqual([call["argv"][0:2] for call in gh_calls], [["auth", "status"], ["pr", "view"], ["pr", "merge"], ["pr", "view"]])
            self.assertEqual(bd_calls[0]["argv"][0:2], ["close", "central-umi2.2"])
            self.assertEqual(bd_calls[0]["password"], "secret-password")

    def test_integrate_pr_does_not_merge_when_head_changed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-head-changed" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "def456",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{"argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("view.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("checks.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "merge"]:
    raise SystemExit(99)
raise SystemExit(9)
""",
            )

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(result["status"], "classified")
            self.assertEqual(result["decision"], "merge_blocked")
            self.assertNotIn("merge", result)
            self.assertEqual([call["argv"][0:2] for call in calls], [["auth", "status"], ["pr", "view"]])

    def test_integrate_pr_records_merge_blocked_when_merge_command_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-merge-fails" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{"argv": sys.argv[1:]}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("view.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("checks.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "merge"]:
    sys.stderr.write("pull request is not mergeable\\n")
    raise SystemExit(1)
raise SystemExit(9)
""",
            )

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(result["status"], "merge_blocked")
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(result["merge"]["status"], "blocked")
            self.assertEqual(result["merge"]["method"], "merge")
            self.assertEqual(result["merge"]["matched_head_sha"], "abc123")
            self.assertEqual(result["commands"]["gh_merge"][-2:], ["--match-head-commit", "abc123"])
            self.assertIn("pull request is not mergeable", result["merge"]["stderr_excerpt"])
            self.assertEqual([call["argv"][0:2] for call in calls], [["auth", "status"], ["pr", "view"], ["pr", "merge"]])

    def test_integrate_pr_retries_tracker_close_without_merging_twice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            beads_workspace = temp_path / "beads"
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("secret-password\n", encoding="utf-8")
            workstream_path = temp_path / "ledger" / "workstreams" / "run-retry-close" / "workstream-result.json"
            write_workstream_result(
                workstream_path,
                expected_head="abc123",
                selected_work=[
                    {
                        "source_type": "beads",
                        "source_id": "central",
                        "external_id": "central-umi2.2",
                    }
                ],
                select_sources=[
                    {
                        "id": "central",
                        "type": "beads",
                        "workspace": str(beads_workspace),
                    }
                ],
            )
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_bd = fake_bin / "bd"
            fake_gh_calls = temp_path / "fake-gh-calls.jsonl"
            fake_bd_calls = temp_path / "fake-bd-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "view-merged.json").write_text(
                json.dumps(
                    {
                        "url": "https://github.com/acme/widgets/pull/17",
                        "mergeCommit": {"oid": "deadbeef"},
                        "mergedAt": "2026-07-06T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{"argv": sys.argv[1:]}}
Path({str(fake_gh_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    target = "view-merged.json" if any("mergeCommit" in arg for arg in sys.argv) else "view.json"
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath(target).read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("checks.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "merge"]:
    raise SystemExit(0)
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
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "password": os.environ.get("BEADS_DOLT_PASSWORD", ""),
}}
Path({str(fake_bd_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if not Path({str(temp_path / "bd-ok")!r}).exists():
    sys.stderr.write("temporary tracker close failure\\n")
    raise SystemExit(1)
raise SystemExit(0)
""",
            )
            env_extra = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

            first = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
                env_extra=env_extra,
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            first_result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(first_result["status"], "tracker_close_failed")
            self.assertEqual(first_result["merge"]["status"], "merged")
            self.assertEqual(first_result["tracker_close"]["status"], "failed")

            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergeStateStatus": "UNKNOWN",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (temp_path / "bd-ok").write_text("ok\n", encoding="utf-8")
            second = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
                env_extra=env_extra,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            second_result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            gh_calls = [json.loads(line) for line in fake_gh_calls.read_text(encoding="utf-8").splitlines()]
            bd_calls = [json.loads(line) for line in fake_bd_calls.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(second_result["status"], "tracker-closed")
            self.assertEqual(second_result["merge"]["status"], "already_merged")
            self.assertEqual(second_result["merge"]["merge_commit"], "deadbeef")
            self.assertEqual(second_result["tracker_close"]["status"], "closed")
            self.assertEqual(len([call for call in gh_calls if call["argv"][0:2] == ["pr", "merge"]]), 1)
            self.assertEqual(len([call for call in bd_calls if call["argv"][0:2] == ["close", "central-umi2.2"]]), 2)

    def test_integrate_pr_records_pending_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-1" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(
                json.dumps(
                    [
                        {
                            "name": "build",
                            "state": "PENDING",
                            "bucket": "pending",
                            "workflow": "CI",
                            "link": "https://github.com/acme/widgets/actions/runs/1",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"], "poll_seconds": 120}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (output_dir_for(workstream_path) / "integration-events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["decision"], "checks_pending")
            self.assertEqual(result["repo"], "acme/widgets")
            self.assertEqual(result["pr_number"], 17)
            self.assertEqual(result["expected_head_sha"], "abc123")
            self.assertEqual(result["observed_head_sha"], "abc123")
            self.assertEqual(result["decision"], "checks_pending")
            self.assertEqual(result["next_poll_seconds"], 120)
            self.assertEqual(result["check_snapshots"][0]["status"], "pending")
            self.assertEqual(events[0]["decision"], "checks_pending")

    def test_integrate_pr_records_failed_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-2" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(
                json.dumps(
                    [
                        {
                            "name": "build",
                            "state": "FAILURE",
                            "bucket": "fail",
                            "workflow": "CI",
                            "link": "https://github.com/acme/widgets/actions/runs/2",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "checks_failed")
            self.assertEqual(result["next_poll_seconds"], 0)
            self.assertIn("Fix the failing checks", result["remediation"])
            self.assertEqual(result["check_snapshots"][0]["status"], "failed")

    def test_integrate_pr_records_inconclusive_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-3" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(
                json.dumps(
                    [
                        {
                            "name": "build",
                            "state": "EXPECTED",
                            "bucket": "skipping",
                            "workflow": "CI",
                            "link": "https://github.com/acme/widgets/actions/runs/3",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"], "poll_seconds": 45}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "checks_inconclusive")
            self.assertEqual(result["next_poll_seconds"], 0)
            self.assertIn("Investigate the inconclusive checks", result["remediation"])

    def test_integrate_pr_records_merge_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-4" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(
                json.dumps(
                    [
                        {
                            "name": "build",
                            "state": "SUCCESS",
                            "bucket": "pass",
                            "workflow": "CI",
                            "link": "https://github.com/acme/widgets/actions/runs/4",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(result["next_poll_seconds"], 0)
            self.assertEqual(result["status"], "merged")
            self.assertEqual(result["merge"]["status"], "merged")
            self.assertEqual(result["tracker_close"]["status"], "not_attempted")
            self.assertEqual(
                [call["argv"][0:2] for call in calls],
                [["auth", "status"], ["pr", "view"], ["pr", "checks"], ["pr", "merge"], ["pr", "view"]],
            )

    def test_integrate_pr_records_merge_blocked_when_pr_state_blocks_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-blocked" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "BLOCKED",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "merge_blocked")
            self.assertEqual(result["expected_head_sha"], "abc123")
            self.assertEqual(result["observed_head_sha"], "abc123")
            self.assertEqual(result["merge_state_status"], "BLOCKED")
            self.assertIn("current PR state", result["remediation"])

    def test_integrate_pr_records_exact_head_mismatch_from_publication_result_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_dir = temp_path / "ledger" / "workstreams" / "run-5"
            workstream_path = workstream_dir / "workstream-result.json"
            publication_path = workstream_dir / "publication-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            write_publication_result(publication_path)
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "def456",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(publication_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "merge_blocked")
            self.assertEqual(result["expected_head_sha"], "abc123")
            self.assertEqual(result["observed_head_sha"], "def456")
            self.assertIn("Exact head mismatch", result["remediation"])

    def test_integrate_pr_prefers_current_ledger_relative_implement_result_over_stale_absolute_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ledger = temp_path / "ledger"
            workstream_dir = ledger / "workstreams" / "run-stale-head"
            workstream_path = workstream_dir / "workstream-result.json"
            current_result_path = ledger / "runs" / "current-implement" / "step-result.json"
            stale_result_path = temp_path / "stale-ledger" / "runs" / "stale-implement" / "step-result.json"
            publication_path = workstream_dir / "publication-result.json"
            current_result_path.parent.mkdir(parents=True, exist_ok=True)
            stale_result_path.parent.mkdir(parents=True, exist_ok=True)
            current_result_path.write_text(
                json.dumps({"output": {"git": {"after_commit": "current-sha"}}}),
                encoding="utf-8",
            )
            stale_result_path.write_text(
                json.dumps({"output": {"git": {"after_commit": "stale-sha"}}}),
                encoding="utf-8",
            )
            workstream_path.parent.mkdir(parents=True, exist_ok=True)
            workstream_path.write_text(
                json.dumps(
                    {
                        "workstream_id": "central-umi2.3",
                        "publication": {
                            "status": "published",
                            "url": "https://github.com/acme/widgets/pull/17",
                        },
                        "steps": [
                            {
                                "name": "implement",
                                "result_abspath": str(stale_result_path),
                                "result_path": "runs/current-implement/step-result.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            write_publication_result(publication_path)
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "current-sha",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(publication_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": []}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["expected_head_sha"], "current-sha")
            self.assertEqual(result["observed_head_sha"], "current-sha")

    def test_integrate_pr_rejects_out_of_ledger_absolute_implement_result_without_current_ledger_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ledger = temp_path / "ledger"
            workstream_dir = ledger / "workstreams" / "run-missing-head"
            workstream_path = workstream_dir / "workstream-result.json"
            publication_path = workstream_dir / "publication-result.json"
            stale_result_path = temp_path / "stale-ledger" / "runs" / "stale-implement" / "step-result.json"
            stale_result_path.parent.mkdir(parents=True, exist_ok=True)
            stale_result_path.write_text(
                json.dumps({"output": {"git": {"after_commit": "stale-sha"}}}),
                encoding="utf-8",
            )
            workstream_path.parent.mkdir(parents=True, exist_ok=True)
            workstream_path.write_text(
                json.dumps(
                    {
                        "workstream_id": "central-umi2.3",
                        "publication": {
                            "status": "published",
                            "url": "https://github.com/acme/widgets/pull/17",
                        },
                        "steps": [
                            {
                                "name": "implement",
                                "result_abspath": str(stale_result_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            write_publication_result(publication_path)
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(publication_path),
                "--policy",
                json.dumps({"required_checks": []}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("could not determine expected head SHA from published artifact", completed.stderr)

    def test_integrate_pr_rejects_in_ledger_absolute_implement_result_without_current_relative_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ledger = temp_path / "ledger"
            workstream_dir = ledger / "workstreams" / "run-stale-in-ledger-head"
            workstream_path = workstream_dir / "workstream-result.json"
            publication_path = workstream_dir / "publication-result.json"
            stale_result_path = ledger / "runs" / "old-implement" / "step-result.json"
            stale_result_path.parent.mkdir(parents=True, exist_ok=True)
            stale_result_path.write_text(
                json.dumps({"output": {"git": {"after_commit": "stale-sha"}}}),
                encoding="utf-8",
            )
            workstream_path.parent.mkdir(parents=True, exist_ok=True)
            workstream_path.write_text(
                json.dumps(
                    {
                        "workstream_id": "central-umi2.3",
                        "publication": {
                            "status": "published",
                            "url": "https://github.com/acme/widgets/pull/17",
                        },
                        "steps": [
                            {
                                "name": "implement",
                                "result_abspath": str(stale_result_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            write_publication_result(publication_path)
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "stale-sha",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(publication_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": []}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("could not determine expected head SHA from published artifact", completed.stderr)

    def test_integrate_pr_uses_status_check_rollup_when_pr_checks_is_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-rollup" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(json.dumps([]), encoding="utf-8")
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"], "poll_seconds": 60}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(result["check_snapshots"], [{"name": "build", "workflow": "", "state": "COMPLETED", "bucket": "", "status": "passed", "link": ""}])

    def test_integrate_pr_uses_status_check_rollup_when_pr_checks_json_is_unsupported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-rollup-unsupported" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    if any("mergeCommit" in arg for arg in sys.argv):
        print(json.dumps({{
            "url": "https://github.com/acme/widgets/pull/17",
            "mergeCommit": {{"oid": "deadbeef"}},
            "mergedAt": "2026-07-06T12:00:00Z",
        }}))
    else:
        print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("view.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    sys.stderr.write("unknown flag: --json\\n")
    raise SystemExit(1)
if sys.argv[1:3] == ["pr", "merge"]:
    raise SystemExit(0)
raise SystemExit(9)
""",
            )

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"], "poll_seconds": 60}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(result["check_snapshots"], [{"name": "build", "workflow": "", "state": "COMPLETED", "bucket": "", "status": "passed", "link": ""}])
            self.assertEqual(result["status"], "merged")
            self.assertEqual(result["tracker_close"]["status"], "not_attempted")
            self.assertEqual(
                [call["argv"][0:2] for call in calls],
                [["auth", "status"], ["pr", "view"], ["pr", "merge"], ["pr", "view"]],
            )

    def test_integrate_pr_merges_rollup_and_pr_checks_by_required_check_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workstream_path = temp_path / "ledger" / "workstreams" / "run-mixed-check-sources" / "workstream-result.json"
            write_workstream_result(workstream_path, expected_head="abc123")
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                        "statusCheckRollup": [
                            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(
                json.dumps(
                    [
                        {
                            "name": "lint",
                            "state": "SUCCESS",
                            "bucket": "pass",
                            "workflow": "CI",
                            "link": "https://github.com/acme/widgets/actions/runs/7",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            write_executable(fake_gh, fake_gh_script(fake_calls))

            completed = run_afk(
                "integrate-pr",
                "--published-result",
                str(workstream_path),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build", "lint"], "poll_seconds": 60}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir_for(workstream_path) / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(
                result["check_snapshots"],
                [
                    {"name": "build", "workflow": "", "state": "COMPLETED", "bucket": "", "status": "passed", "link": ""},
                    {
                        "name": "lint",
                        "workflow": "CI",
                        "state": "SUCCESS",
                        "bucket": "pass",
                        "status": "passed",
                        "link": "https://github.com/acme/widgets/actions/runs/7",
                    },
                ],
            )

    def test_integrate_pr_writes_run_scoped_output_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_gh = temp_path / "fake-gh"
            fake_calls = temp_path / "fake-calls.jsonl"
            auth_dir = temp_path / "gh-config"
            auth_dir.mkdir()
            write_executable(fake_gh, fake_gh_script(fake_calls))

            run_one = temp_path / "ledger" / "workstreams" / "run-1" / "workstream-result.json"
            run_two = temp_path / "ledger" / "workstreams" / "run-2" / "workstream-result.json"
            write_workstream_result(run_one, expected_head="abc123")
            write_workstream_result(run_two, expected_head="abc123")

            (auth_dir / "view.json").write_text(
                json.dumps(
                    {
                        "number": 17,
                        "url": "https://github.com/acme/widgets/pull/17",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": "abc123",
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / "checks.json").write_text(
                json.dumps([{"name": "build", "state": "PENDING", "bucket": "pending", "workflow": "CI", "link": ""}]),
                encoding="utf-8",
            )
            first = run_afk(
                "integrate-pr",
                "--published-result",
                str(run_one),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"], "poll_seconds": 120}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            (auth_dir / "checks.json").write_text(
                json.dumps([{"name": "build", "state": "SUCCESS", "bucket": "pass", "workflow": "CI", "link": ""}]),
                encoding="utf-8",
            )
            second = run_afk(
                "integrate-pr",
                "--published-result",
                str(run_two),
                "--policy",
                json.dumps({"gh": {"path": str(fake_gh)}, "required_checks": ["build"]}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            first_result = json.loads((output_dir_for(run_one) / "integration-result.json").read_text(encoding="utf-8"))
            second_result = json.loads((output_dir_for(run_two) / "integration-result.json").read_text(encoding="utf-8"))

            self.assertEqual(first_result["decision"], "checks_pending")
            self.assertEqual(second_result["decision"], "merge_ready")
            self.assertFalse((temp_path / "ledger" / "output" / "integration-result.json").exists())


if __name__ == "__main__":
    unittest.main()
