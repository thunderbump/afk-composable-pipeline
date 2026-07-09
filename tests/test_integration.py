import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.integration import (  # noqa: E402
    classify_terminal_integration,
    terminal_no_merge_decision,
    terminal_integration_retrospective_status,
)


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def write_workstream_result(
    path: Path,
    *,
    repo: str,
    pr_number: int,
    expected_head_sha: str,
) -> Path:
    payload = {
        "schema_version": 1,
        "workstream_id": "central-umi2.1",
        "review_branch": "afk/central-umi2-1",
        "steps": [
            {
                "step": "implement",
                "output": {
                    "status": "implemented",
                    "git": {
                        "after_commit": expected_head_sha,
                    },
                },
            }
        ],
        "publication": {
            "status": "published",
            "url": f"https://github.com/{repo}/pull/{pr_number}",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TerminalIntegrationTest(unittest.TestCase):
    def test_classify_terminal_integration_marks_merge_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            artifact_path = write_workstream_result(
                temp_path / "workstream-result.json",
                repo="thunderbump/afk-composable-pipeline",
                pr_number=123,
                expected_head_sha="abc123",
            )
            ledger_dir = temp_path / "ledger"
            gh_config = temp_path / "gh-config"
            gh_config.mkdir()
            fake_gh = temp_path / "fake-gh"
            write_executable(
                fake_gh,
                f"""#!{sys.executable}
import json
import sys

if sys.argv[1:3] == ["auth", "status"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["pr", "view"]:
    print(json.dumps({{
        "number": 123,
        "url": "https://github.com/thunderbump/afk-composable-pipeline/pull/123",
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "abc123",
        "statusCheckRollup": [
            {{
                "__typename": "CheckRun",
                "name": "Build / Linux",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }},
            {{
                "__typename": "CheckRun",
                "name": "Build / Windows",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }}
        ]
    }}))
    raise SystemExit(0)
raise SystemExit(9)
""",
            )

            result = classify_terminal_integration(
                artifact_path,
                policy={
                    "required_checks": ["Build / Linux", "Build / Windows"],
                    "poll_interval_seconds": 60,
                },
                github={
                    "path": str(fake_gh),
                    "auth": {"config_dir": str(gh_config)},
                },
                ledger_dir=ledger_dir,
            )

            written = json.loads(
                (ledger_dir / "output" / "integration-result.json").read_text(
                    encoding="utf-8",
                )
            )
            events = (
                (ledger_dir / "output" / "integration-events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )

            self.assertEqual(result["decision"], "merge_ready")
            self.assertEqual(written["decision"], "merge_ready")
            self.assertEqual(written["repo"], "thunderbump/afk-composable-pipeline")
            self.assertEqual(written["pr_number"], 123)
            self.assertEqual(written["expected_head_sha"], "abc123")
            self.assertEqual(written["observed_head_sha"], "abc123")
            self.assertEqual(len(written["check_snapshots"]), 2)
            self.assertEqual(written["next_poll_seconds"], 0)
            self.assertEqual(written["remediation"], "")
            self.assertGreaterEqual(len(events), 2)

    def test_terminal_retrospective_status_marks_no_merge_ready(self):
        self.assertEqual(
            terminal_integration_retrospective_status(
                {
                    "status": "no-merge",
                    "reason": "checks failed",
                    "pr_url": "https://github.com/acme/widgets/pull/17",
                }
            ),
            {
                "status": "ready",
                "artifact": "integration-retrospective.json",
                "terminal_decision_status": "no-merge",
            },
        )

    def test_terminal_no_merge_decision_requires_feedback_resolution(self):
        workstream = {
            "tracker": {
                "terminal_decision": {
                    "status": "no-merge",
                    "reason": "checks failed",
                    "pr_url": "https://github.com/acme/widgets/pull/17",
                    "review_feedback_status": "",
                }
            }
        }

        self.assertEqual(terminal_no_merge_decision(workstream), {})


if __name__ == "__main__":
    unittest.main()
