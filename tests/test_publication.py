import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.publication import PublicationRequest, publish_terminal_pr  # noqa: E402
from afk.workstream import WorkstreamLedger  # noqa: E402


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class PublicationTest(unittest.TestCase):
    def test_publish_terminal_pr_uses_absolute_pr_body_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            checkout = temp_path / "checkout"
            checkout.mkdir()
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
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
            old_cwd = Path.cwd()
            try:
                os.chdir(runner)
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-success")
                ledger.prepare()
                result = publish_terminal_pr(
                    PublicationRequest(
                        publisher={
                            "enabled": True,
                            "mode": "create",
                            "repo": "thunderbump/afk-composable-pipeline",
                            "base": "main",
                            "head": "afk/workstream-terminal-pr",
                            "title": "central-yypj: publication seam",
                            "gh": {
                                "path": str(fake_gh),
                                "auth": {"config_dir": str(mounted_gh_config)},
                            },
                        },
                        workstream_id="central-yypj",
                        review_branch="afk/workstream-terminal-pr",
                        checkout_path=checkout,
                        checkout_base_commit="",
                        next_allowed_command="afk run-workstream --workstream-id central-yypj --input <recipe>",
                        ledger=ledger,
                        build_pr_body=lambda: "## Validation\n- tier1: validated\n",
                    )
                )
            finally:
                os.chdir(old_cwd)

            publication_text = json.dumps(result)
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            expected_body_path = (runner / ledger_arg / "workstreams" / ledger.run_id / "pr-body.md").resolve()
            pr_call = next(call for call in calls if call["argv"][0:2] == ["pr", "create"])
            command = result["commands"]["gh"]

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["auth"]["path"], "[REDACTED]")
            self.assertEqual(pr_call["cwd"], str(checkout))
            self.assertEqual(pr_call["gh_config_dir"], str(mounted_gh_config))
            self.assertEqual(Path(pr_call["body_path"]), expected_body_path)
            self.assertEqual(command[command.index("--body-file") + 1], str(expected_body_path))
            self.assertIn(str(expected_body_path), publication_text)
            self.assertNotIn(str(mounted_gh_config), publication_text)

    def test_publish_terminal_pr_update_fallback_uses_absolute_pr_update_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runner = temp_path / "runner"
            runner.mkdir()
            checkout = temp_path / "checkout"
            checkout.mkdir()
            ledger_arg = "relative-ledger"
            fake_calls = temp_path / "fake-calls.jsonl"
            mounted_gh_config = temp_path / "mounted-gh-config"
            mounted_gh_config.mkdir()
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
    record["body"] = Path(body_file).read_text(encoding="utf-8")
if "--input" in sys.argv:
    input_file = sys.argv[sys.argv.index("--input") + 1]
    record["input_path"] = input_file
    record["input"] = json.loads(Path(input_file).read_text(encoding="utf-8"))
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:4] == ["auth", "status", "--hostname"]:
    sys.exit(0)
if sys.argv[1:4] == ["pr", "edit", "feature/pull/123"]:
    print("GraphQL: Projects (classic) is deprecated", file=sys.stderr)
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
            old_cwd = Path.cwd()
            try:
                os.chdir(runner)
                ledger = WorkstreamLedger(Path(ledger_arg), "publisher-update-fallback")
                ledger.prepare()
                result = publish_terminal_pr(
                    PublicationRequest(
                        publisher={
                            "enabled": True,
                            "mode": "update",
                            "pr": "feature/pull/123",
                            "repo": "thunderbump/afk-composable-pipeline",
                            "head": "afk/workstream-terminal-pr",
                            "title": "central-yypj: publication seam",
                            "gh": {
                                "path": str(fake_gh),
                                "auth": {"config_dir": str(mounted_gh_config)},
                            },
                        },
                        workstream_id="central-yypj",
                        review_branch="afk/workstream-terminal-pr",
                        checkout_path=checkout,
                        checkout_base_commit="",
                        next_allowed_command="afk run-workstream --workstream-id central-yypj --input <recipe>",
                        ledger=ledger,
                        build_pr_body=lambda: "## Validation\n- tier1: validated\n",
                    )
                )
            finally:
                os.chdir(old_cwd)

            publication_text = json.dumps(result)
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]

            expected_input_path = (runner / ledger_arg / "workstreams" / ledger.run_id / "pr-update.json").resolve()
            api_call = next(call for call in calls if call["argv"][0:3] == ["api", "--method", "PATCH"])
            command = result["commands"]["gh"]

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["auth"]["path"], "[REDACTED]")
            self.assertEqual(api_call["cwd"], str(checkout))
            self.assertEqual(api_call["gh_config_dir"], str(mounted_gh_config))
            self.assertEqual(Path(api_call["input_path"]), expected_input_path)
            self.assertEqual(
                api_call["input"],
                {
                    "title": "central-yypj: publication seam",
                    "body": "## Validation\n- tier1: validated\n",
                },
            )
            self.assertEqual(command[command.index("--input") + 1], str(expected_input_path))
            self.assertIn(str(expected_input_path), publication_text)
            self.assertNotIn(str(mounted_gh_config), publication_text)


if __name__ == "__main__":
    unittest.main()
