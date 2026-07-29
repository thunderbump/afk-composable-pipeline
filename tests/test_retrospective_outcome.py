import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RetrospectiveOutcomeTest(unittest.TestCase):
    def test_status_import_does_not_load_attempt_executor(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys\n"
                    "sys.modules['afk.retrospective_attempt'] = None\n"
                    "import afk.retrospective_status\n"
                ),
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
