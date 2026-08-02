import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CandidateBrokerCliTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("bwrap") is None:
            self.skipTest("bubblewrap is unavailable")
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary_directory.name)
        self.candidate = self.temp / "candidate"
        self.candidate.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "afk@example.invalid")
        self.git("config", "user.name", "AFK Test")
        (self.candidate / "input.txt").write_text("exact candidate\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "candidate")
        self.candidate_sha = self.git("rev-parse", "HEAD")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_runs_against_read_only_exact_candidate_with_writable_scratch(self):
        probe = textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path

            candidate = Path("/candidate/input.txt")
            scratch = Path("/work/output.txt")
            scratch.write_text("scratch works\\n", encoding="utf-8")
            try:
                candidate.write_text("mutated\\n", encoding="utf-8")
            except OSError:
                read_only = True
            else:
                read_only = False
            print(candidate.read_text(encoding="utf-8").strip())
            print(scratch.read_text(encoding="utf-8").strip())
            print("READ_ONLY" if read_only else "WRITABLE")
            print(
                "STATE_HIDDEN"
                if "XDG_STATE_HOME" not in os.environ
                else "STATE_LEAKED"
            )
            print(
                "EVIDENCE_HIDDEN"
                if "AFK_EVIDENCE_DIR" not in os.environ
                else "EVIDENCE_LEAKED"
            )
            print("candidate stderr", file=sys.stderr)
            raise SystemExit(7)
            """
        ).lstrip()
        request = self.temp / "request.json"
        result = self.temp / "result.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate_sha": self.candidate_sha,
                    "candidate_path": str(self.candidate),
                    "command": ["/usr/bin/python3", "-c", probe],
                }
            ),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "afk.candidate_broker",
                "--request",
                str(request),
                "--result",
                str(result),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "XDG_STATE_HOME": str(self.temp / "state"),
                "AFK_EVIDENCE_DIR": str(self.temp / "evidence"),
            },
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "completed",
                "exit_code": 7,
                "stdout": (
                    "exact candidate\n"
                    "scratch works\n"
                    "READ_ONLY\n"
                    "STATE_HIDDEN\n"
                    "EVIDENCE_HIDDEN\n"
                ),
                "stderr": "candidate stderr\n",
            },
        )
        self.assertEqual(
            (self.candidate / "input.txt").read_text(encoding="utf-8"),
            "exact candidate\n",
        )

    def git(self, *args):
        completed = subprocess.run(
            ["git", *args],
            cwd=self.candidate,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
