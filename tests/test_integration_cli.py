import json
import os
import subprocess
import sys
import tempfile
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


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def write_workstream_result(path: Path, *, expected_head: str, pr_url: str = "https://github.com/acme/widgets/pull/17") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workstream_id": "central-umi2.1",
                "review_branch": "afk/central-umi2-1",
                "publication": {
                    "status": "published",
                    "url": pr_url,
                },
                "steps": [
                    {
                        "name": "implement",
                        "output": {
                            "git": {
                                "after_commit": expected_head,
                            }
                        },
                    }
                ],
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
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "gh_config_dir": os.environ.get("GH_CONFIG_DIR", ""),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("view.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "checks"]:
    print(Path(os.environ["GH_CONFIG_DIR"]).joinpath("checks.json").read_text(encoding="utf-8"))
    raise SystemExit(0)
raise SystemExit(9)
"""


class IntegrationCliTest(unittest.TestCase):
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
                json.dumps({"gh": {"path": str(fake_gh)}, "poll_seconds": 120}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = json.loads((workstream_path.parent / "integration-result.json").read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (workstream_path.parent / "integration-events.jsonl").read_text(encoding="utf-8").splitlines()
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
                json.dumps({"gh": {"path": str(fake_gh)}}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((workstream_path.parent / "integration-result.json").read_text(encoding="utf-8"))
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
                json.dumps({"gh": {"path": str(fake_gh)}, "poll_seconds": 45}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((workstream_path.parent / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "checks_inconclusive")
            self.assertEqual(result["next_poll_seconds"], 45)
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
                json.dumps({"gh": {"path": str(fake_gh)}}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((workstream_path.parent / "integration-result.json").read_text(encoding="utf-8"))
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(result["next_poll_seconds"], 0)
            self.assertEqual([call["argv"][0:2] for call in calls], [["auth", "status"], ["pr", "view"], ["pr", "checks"]])

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
                json.dumps({"gh": {"path": str(fake_gh)}}),
                "--gh-auth-config-dir",
                str(auth_dir),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((workstream_dir / "integration-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "merge_blocked")
            self.assertEqual(result["expected_head_sha"], "abc123")
            self.assertEqual(result["observed_head_sha"], "def456")
            self.assertIn("Exact head mismatch", result["remediation"])


if __name__ == "__main__":
    unittest.main()
