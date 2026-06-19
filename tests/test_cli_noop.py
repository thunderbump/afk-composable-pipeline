import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


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


if __name__ == "__main__":
    unittest.main()
