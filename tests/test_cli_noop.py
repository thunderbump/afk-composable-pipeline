import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from afk.contracts import load_project_contract  # noqa: E402


def run_afk(*args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "afk", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class NoopCliTest(unittest.TestCase):
    def test_bump_eqemu_contract_matches_current_project_defaults(self):
        contract = load_project_contract(
            "bump-eqemu",
            ROOT / "project-contracts",
            cwd=ROOT,
        )

        self.assertEqual(contract.repo_url, "git@github.com:thunderbump/bump-EQEmu.git")
        self.assertEqual(contract.base_branch, "master")
        self.assertEqual(contract.pr_target["branch"], "master")
        self.assertEqual(
            contract.validation_profiles,
            (
                "preflight",
                "tier1",
                "tier2-readonly",
                "tier3-harness",
                "tier1-tier3-harness",
            ),
        )
        self.assertEqual(contract.validation_profile_requests["tier1"]["profile"], "safe")
        self.assertEqual(contract.validation_profile_requests["tier2-readonly"]["profile"], "safe")
        self.assertEqual(
            contract.validation_profile_requests["tier1-tier3-harness"]["profile"],
            "tier1-tier3-harness",
        )

    def test_afk_composable_pipeline_contract_supports_self_dogfood(self):
        contract = load_project_contract(
            "afk-composable-pipeline",
            ROOT / "project-contracts",
            cwd=ROOT,
        )

        self.assertEqual(
            contract.repo_url,
            "git@github.com:thunderbump/afk-composable-pipeline.git",
        )
        self.assertEqual(contract.base_branch, "main")
        self.assertEqual(contract.beads_labels, ("project:afk-composable-pipeline",))
        self.assertEqual(contract.pr_target, {"remote": "origin", "branch": "main"})
        self.assertEqual(contract.validation_profile_requests["tier1"]["profile"], "safe")
        self.assertEqual(contract.identity.path, "project-contracts/afk-composable-pipeline.json")

    def test_unknown_step_is_rejected_with_registry_error(self):
        input_json = (FIXTURES / "noop-input.json").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "missing-step",
                "--input",
                input_json,
                "--ledger",
                str(ledger),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unknown step 'missing-step'", completed.stderr)
            self.assertIn(
                "known steps: implement, noop, prepare-checkout, review, select-work, validate",
                completed.stderr,
            )
            self.assertFalse(ledger.exists())

    def test_project_contract_validation_errors_are_reported(self):
        input_json = (FIXTURES / "noop-input.json").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            contracts_dir.mkdir()
            (contracts_dir / "bump-eqemu.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_slug": "bump-eqemu",
                        "repo_url": "git@github.com:thunderbump/bump-EQEmu.git",
                        "base_branch": "main",
                        "beads_labels": ["project:bump-eqemu"],
                        "validation_profiles": ["unit"],
                        "artifact_retention": {"ledger_days": -1, "log_days": 30},
                        "pr_target": {"remote": "origin", "branch": "main"},
                    }
                ),
                encoding="utf-8",
            )

            completed = run_afk(
                "run-step",
                "noop",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                str(contracts_dir),
                "--input",
                input_json,
                "--ledger",
                str(temp_path / "ledger"),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("invalid project contract", completed.stderr)
            self.assertIn("artifact_retention.ledger_days", completed.stderr)
            self.assertFalse((temp_path / "ledger").exists())

    def test_noop_step_records_project_contract_identity(self):
        input_json = (FIXTURES / "noop-input.json").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "noop",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--input",
                input_json,
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]

            command = json.loads((run_dir / "command.json").read_text(encoding="utf-8"))
            self.assertEqual(command["project"], "bump-eqemu")
            self.assertEqual(
                command["project_contract"]["path"],
                "project-contracts/bump-eqemu.json",
            )
            self.assertRegex(command["project_contract"]["sha256"], r"^[0-9a-f]{64}$")

            events = [
                json.loads(line)
                for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["project"], "bump-eqemu")
            self.assertEqual(events[0]["project_contract"], command["project_contract"])

    def test_noop_step_records_replayable_ledger(self):
        input_json = (FIXTURES / "noop-input.json").read_text(encoding="utf-8")
        input_data = json.loads(input_json)

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "noop",
                "--input",
                input_json,
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["step"], "noop")
            self.assertEqual(summary["status"], "succeeded")

            run_dir = ledger / "runs" / summary["run_id"]
            self.assertTrue(run_dir.is_dir())

            command = json.loads((run_dir / "command.json").read_text(encoding="utf-8"))
            self.assertEqual(command["command"], ["afk", "run-step", "noop"])
            self.assertEqual(command["step"], "noop")
            self.assertEqual(command["input"], input_data)

            self.assertEqual((run_dir / "stdout.log").read_text(encoding="utf-8"), "")
            self.assertEqual((run_dir / "stderr.log").read_text(encoding="utf-8"), "")

            events = [
                json.loads(line)
                for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["event"] for event in events],
                ["run.started", "step.started", "step.completed", "run.completed"],
            )

            replay = {"status": None, "result_path": None, "result_sha256": None}
            for event in events:
                if event["event"] == "step.completed":
                    replay["result_path"] = event["result_path"]
                    replay["result_sha256"] = event["result_sha256"]
                if event["event"] == "run.completed":
                    replay["status"] = event["status"]

            self.assertEqual(replay["status"], "succeeded")
            result_path = run_dir / replay["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["step"], "noop")
            self.assertEqual(result["output"], input_data)
            self.assertEqual(result["result_sha256"], replay["result_sha256"])

            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in [
                    run_dir / "command.json",
                    run_dir / "ledger.jsonl",
                    run_dir / "step-result.json",
                ]
            )
            self.assertNotIn(str(Path.home()), artifact_text)

    def test_noop_output_cannot_request_extra_ledger_artifacts(self):
        input_data = {
            "artifacts": {"publication": "publication-result.json"},
            "publication": {"status": "should-not-write"},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "noop",
                "--input",
                json.dumps(input_data),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            self.assertFalse((run_dir / "publication-result.json").exists())
            events = [
                json.loads(line)
                for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            completed_event = next(event for event in events if event["event"] == "step.completed")
            self.assertEqual(completed_event["artifacts"], {})


if __name__ == "__main__":
    unittest.main()
