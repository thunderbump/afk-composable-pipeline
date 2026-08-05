import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_COMMANDS = (
    "generate-recipe",
    "integrate-pr",
    "run-next",
    "run-step",
    "run-workstream",
)


def run_afk(*args: str) -> subprocess.CompletedProcess[str]:
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


class CliSurfaceTest(unittest.TestCase):
    def test_help_exposes_only_run_lifecycle_commands(self):
        completed = run_afk("--help")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in ("start", "status", "resume"):
            self.assertIn(command, completed.stdout)
        for command in LEGACY_COMMANDS:
            self.assertNotIn(command, completed.stdout)

    def test_legacy_commands_are_rejected(self):
        for command in LEGACY_COMMANDS:
            with self.subTest(command=command):
                completed = run_afk(command)

                self.assertEqual(completed.returncode, 2)
                self.assertIn("invalid choice", completed.stderr)


if __name__ == "__main__":
    unittest.main()
