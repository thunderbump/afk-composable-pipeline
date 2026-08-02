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
        self.repository = self.temp / "repository"
        self.repository.mkdir()
        self.candidate = self.temp / "candidate"
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "afk@example.invalid"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "AFK Test"],
            cwd=self.repository,
            check=True,
        )
        (self.repository / "input.txt").write_text(
            "exact candidate\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=self.repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "candidate"],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(self.candidate), "HEAD"],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        )
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
                "GIT_METADATA_HIDDEN"
                if not Path("/candidate/.git").exists()
                else "GIT_METADATA_VISIBLE"
            )
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
        fake_bin = self.temp / "bin"
        fake_bin.mkdir()
        real_bwrap = shutil.which("bwrap")
        launcher = fake_bin / "bwrap"
        launcher.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import os
                import sys
                from pathlib import Path

                Path({str(self.candidate / "input.txt")!r}).write_text(
                    "drifted after verification\\n", encoding="utf-8"
                )
                os.execv({real_bwrap!r}, [{real_bwrap!r}, *sys.argv[1:]])
                """
            ).lstrip(),
            encoding="utf-8",
        )
        launcher.chmod(0o755)
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
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
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
                    "GIT_METADATA_HIDDEN\n"
                    "STATE_HIDDEN\n"
                    "EVIDENCE_HIDDEN\n"
                ),
                "stderr": "candidate stderr\n",
            },
        )
        self.assertEqual(
            (self.candidate / "input.txt").read_text(encoding="utf-8"),
            "drifted after verification\n",
        )

    def test_fails_closed_when_exact_candidate_snapshot_is_unavailable(self):
        request = self.temp / "snapshot-failure-request.json"
        result = self.temp / "snapshot-failure-result.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate_sha": self.candidate_sha,
                    "candidate_path": str(self.candidate),
                    "command": ["/usr/bin/true"],
                }
            ),
            encoding="utf-8",
        )
        fake_bin = self.temp / "snapshot-failure-bin"
        fake_bin.mkdir()
        real_git = shutil.which("git")
        git = fake_bin / "git"
        git.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import os
                import sys

                if sys.argv[1:2] == ["archive"]:
                    raise SystemExit(1)
                os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
                """
            ).lstrip(),
            encoding="utf-8",
        )
        git.chmod(0o755)

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
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            },
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "exact Candidate snapshot is unavailable\n")
        self.assertFalse(result.exists())

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
