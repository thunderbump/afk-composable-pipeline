import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.test_validate_cli import ROOT, git, run_afk


class SelfHostedValidationCliTest(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "linux", "stopped-process detection requires Linux /proc"
    )
    def test_outer_validation_ignores_stopped_fixtures_owned_by_inner_validators(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ledger = temp_path / "ledger"
            head = git(ROOT, "rev-parse", "HEAD")
            worker_code = textwrap.dedent(
                """
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text())
                repo = Path(request["repo"]["path"])
                env = os.environ.copy()
                env["PYTHONPATH"] = str(repo / "src")
                tests = [
                    "test_validate_reports_stopped_worker_process_before_timeout",
                    "test_validate_reports_stopped_worker_with_invalid_json_before_timeout_as_runtime_failure",
                    "test_validate_overwrites_pass_evidence_when_worker_stops_after_reporting_success",
                    "test_validate_reports_stopped_worker_descendant_before_timeout",
                    "test_validate_waits_for_inherited_stdio_descendants_after_worker_exit",
                    "test_validate_reports_stopped_descendant_after_parent_exit_before_timeout",
                    "test_validate_scans_owned_process_group_after_pipes_drain",
                ]
                prefix = "tests.test_validate_cli.ValidateCliTest."
                command = [sys.executable, "-m", "unittest"]
                command.extend(prefix + name for name in tests)
                completed = subprocess.run(
                    command,
                    cwd=repo,
                    env=env,
                    text=True,
                    capture_output=True,
                )
                status = "pass" if completed.returncode == 0 else "fail"
                Path(os.environ["AFK_WORKER_RESULT"]).write_text(
                    json.dumps({"status": status, "steps": []}),
                    encoding="utf-8",
                )
                raise SystemExit(completed.returncode)
                """
            ).strip()

            completed = run_afk(
                "run-step",
                "validate",
                "--profile",
                "tier1",
                "--input",
                json.dumps(
                    {
                        "checkout": {
                            "status": "prepared",
                            "checkout_path": str(ROOT),
                            "review_branch": "central-bqui",
                            "requested_ref": "main",
                            "start_commit": head,
                        },
                        "validation": {"dry_run": False, "timeout_seconds": 30},
                        "worker": {
                            "type": "local-command",
                            "command": [sys.executable, "-c", worker_code],
                        },
                    }
                ),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text())
            self.assertEqual(result["output"]["status"], "validated")


if __name__ == "__main__":
    unittest.main()
