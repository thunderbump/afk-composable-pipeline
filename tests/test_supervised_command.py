import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import afk.candidate_validation as candidate_validation  # noqa: E402
from afk.candidate_validation import (  # noqa: E402
    CandidateValidationError,
    run_supervised_command,
)


class SupervisedCommandTest(unittest.TestCase):
    def test_normal_success_accepts_stdin_and_captures_both_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = run_supervised_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; value=sys.stdin.read();"
                        "print('out:'+value); print('err:'+value,file=sys.stderr)"
                    ),
                ],
                cwd=Path(temporary),
                environment=os.environ.copy(),
                timeout_seconds=1,
                input_text="prompt",
                label="Codex",
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "out:prompt\n")
        self.assertEqual(completed.stderr, "err:prompt\n")

    def test_timeout_remains_live_while_child_does_not_read_large_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            with self.assertRaisesRegex(CandidateValidationError, "timed out"):
                run_supervised_command(
                    [sys.executable, "-c", "import time; time.sleep(1)"],
                    cwd=Path(temporary),
                    environment=os.environ.copy(),
                    timeout_seconds=0.1,
                    input_text="x" * (1024 * 1024),
                    label="Codex",
                )

        self.assertLess(time.monotonic() - started, 0.8)

    def test_each_output_stream_is_independently_size_limited(self):
        for stream in ("stdout", "stderr"):
            with (
                self.subTest(stream=stream),
                tempfile.TemporaryDirectory() as temporary,
            ):
                target = "sys.stdout" if stream == "stdout" else "sys.stderr"
                command = [
                    sys.executable,
                    "-c",
                    f"import sys; {target}.write('x'*17); {target}.flush()",
                ]
                with (
                    mock.patch.object(candidate_validation, "OUTPUT_BYTE_LIMIT", 16),
                    self.assertRaisesRegex(CandidateValidationError, "output exceeds"),
                ):
                    run_supervised_command(
                        command,
                        cwd=Path(temporary),
                        environment=os.environ.copy(),
                        timeout_seconds=1,
                        label="Codex",
                    )

    def test_timeout_kills_detached_term_resistant_descendants_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "late-mutation"
            child = (
                "import os,signal,time;"
                "os.setsid();"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "time.sleep(0.5);"
                f"open({str(marker)!r},'w').write('mutated')"
            )
            parent = (
                "import signal,subprocess,sys,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
                "time.sleep(30)"
            )

            with (
                mock.patch.object(candidate_validation, "PROCESS_CLEANUP_SECONDS", 0.1),
                self.assertRaisesRegex(CandidateValidationError, "timed out"),
            ):
                run_supervised_command(
                    [sys.executable, "-c", parent],
                    cwd=root,
                    environment=os.environ.copy(),
                    timeout_seconds=0.1,
                    input_text="",
                    label="Codex",
                )

            time.sleep(0.7)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
