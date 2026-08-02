import ctypes
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import afk.process_supervision as process_supervision  # noqa: E402
from afk.process_supervision import (  # noqa: E402
    SupervisedCommandError,
    run_supervised_command,
)


class SupervisedCommandTest(unittest.TestCase):
    def test_baseline_failure_restores_subreaper_state_and_releases_lock(self):
        libc = ctypes.CDLL(None, use_errno=True)
        initial_subreaper = ctypes.c_int()
        self.assertEqual(
            libc.prctl(37, ctypes.byref(initial_subreaper), 0, 0, 0),
            0,
        )
        with (
            mock.patch.object(
                process_supervision,
                "_proc_children",
                side_effect=SupervisedCommandError(
                    "supervision_unavailable",
                    "Linux Codex descendant supervision is unavailable",
                ),
            ),
            self.assertRaises(SupervisedCommandError) as raised,
        ):
            run_supervised_command(
                [sys.executable, "-c", "print('not launched')"],
                cwd=Path.cwd(),
                environment=os.environ.copy(),
                timeout_seconds=1,
                label="Codex",
            )

        self.assertEqual(raised.exception.classification, "supervision_unavailable")
        restored_subreaper = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(restored_subreaper), 0, 0, 0), 0)
        self.assertEqual(restored_subreaper.value, initial_subreaper.value)
        completed = run_supervised_command(
            [sys.executable, "-c", "print('released')"],
            cwd=Path.cwd(),
            environment=os.environ.copy(),
            timeout_seconds=1,
            label="Codex",
        )
        self.assertEqual(completed.stdout, "released\n")

    def test_overlapping_calls_serialize_process_wide_supervision(self):
        class ObservedLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.state_lock = threading.Lock()
                self.held = False
                self.waiting = threading.Event()

            def acquire(self):
                with self.state_lock:
                    if self.held:
                        self.waiting.set()
                self.lock.acquire()
                with self.state_lock:
                    self.held = True

            def release(self):
                with self.state_lock:
                    self.held = False
                self.lock.release()

        libc = ctypes.CDLL(None, use_errno=True)
        initial_subreaper = ctypes.c_int()
        self.assertEqual(
            libc.prctl(37, ctypes.byref(initial_subreaper), 0, 0, 0),
            0,
        )
        observed_lock = ObservedLock()
        first_launched = threading.Event()
        second_launched = threading.Event()
        results = {}
        failures = []
        real_popen = process_supervision.subprocess.Popen

        def observed_popen(*args, **kwargs):
            if first_launched.is_set():
                second_launched.set()
            else:
                first_launched.set()
            return real_popen(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release_first = root / "release-first"

            def invoke(name, command):
                try:
                    results[name] = run_supervised_command(
                        command,
                        cwd=root,
                        environment=os.environ.copy(),
                        timeout_seconds=3,
                        label=name,
                    )
                except BaseException as exc:
                    failures.append(exc)

            first = threading.Thread(
                target=invoke,
                args=(
                    "first",
                    [
                        sys.executable,
                        "-c",
                        (
                            "import pathlib,time;"
                            f"marker=pathlib.Path({str(release_first)!r});"
                            "\nwhile not marker.exists(): time.sleep(0.01);"
                            "\nprint('first')"
                        ),
                    ],
                ),
            )
            second = threading.Thread(
                target=invoke,
                args=("second", [sys.executable, "-c", "print('second')"]),
            )
            with (
                mock.patch.object(
                    process_supervision,
                    "_SUPERVISOR_LOCK",
                    observed_lock,
                    create=True,
                ),
                mock.patch.object(
                    process_supervision.subprocess,
                    "Popen",
                    side_effect=observed_popen,
                ),
            ):
                first.start()
                self.assertTrue(first_launched.wait(1))
                second.start()
                overlap_was_serialized = observed_lock.waiting.wait(1)
                second_was_held = not second_launched.is_set()
                release_first.write_text("release\n", encoding="utf-8")
                first.join(4)
                second.join(4)

        self.assertTrue(overlap_was_serialized)
        self.assertTrue(second_was_held)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results["first"].stdout, "first\n")
        self.assertEqual(results["second"].stdout, "second\n")
        final_subreaper = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(final_subreaper), 0, 0, 0), 0)
        self.assertEqual(final_subreaper.value, initial_subreaper.value)

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

    def test_redacted_success_output_stays_within_the_byte_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed = run_supervised_command(
                [sys.executable, "-c", "print('password=a')"],
                cwd=Path(temporary),
                environment=os.environ.copy(),
                timeout_seconds=1,
                label="Codex",
                output_byte_limit=11,
            )

        self.assertLessEqual(len(completed.stdout.encode("utf-8")), 11)
        self.assertNotIn("password=a", completed.stdout)

    def test_timeout_remains_live_while_child_does_not_read_large_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            with self.assertRaisesRegex(SupervisedCommandError, "timed out") as raised:
                run_supervised_command(
                    [sys.executable, "-c", "import time; time.sleep(1)"],
                    cwd=Path(temporary),
                    environment=os.environ.copy(),
                    timeout_seconds=0.1,
                    input_text="x" * (1024 * 1024),
                    label="Codex",
                )

        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(raised.exception.classification, "timeout")
        self.assertIsNone(raised.exception.exit_code)

    def test_initial_tracking_failure_terminates_detached_child_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "late-mutation"
            ready = root / "detached-ready"
            child = (
                "import time; time.sleep(0.3);"
                f"open({str(marker)!r},'w').write('mutated')"
            )
            command = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}],"
                "start_new_session=True);"
                f"open({str(ready)!r},'w').write('ready');"
                "time.sleep(30)"
            )

            def fail_after_detach(_pid):
                deadline = time.monotonic() + 1
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not ready.exists():
                    raise AssertionError("detached child was not launched")
                raise OSError("unavailable")

            with (
                mock.patch.object(
                    process_supervision.os,
                    "pidfd_open",
                    side_effect=fail_after_detach,
                ),
                self.assertRaisesRegex(
                    SupervisedCommandError, "supervision is unavailable"
                ),
            ):
                run_supervised_command(
                    [sys.executable, "-c", command],
                    cwd=root,
                    environment=os.environ.copy(),
                    timeout_seconds=1,
                    label="Codex",
                )

            time.sleep(0.4)
            self.assertFalse(marker.exists())

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
                with self.assertRaisesRegex(
                    SupervisedCommandError, "output exceeds"
                ) as raised:
                    run_supervised_command(
                        command,
                        cwd=Path(temporary),
                        environment=os.environ.copy(),
                        timeout_seconds=1,
                        label="Codex",
                        output_byte_limit=16,
                    )
                self.assertEqual(
                    raised.exception.classification,
                    "output_overflow",
                )
                self.assertIsNone(raised.exception.exit_code)

    def test_signal_exit_keeps_redacted_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = [
                sys.executable,
                "-c",
                (
                    "import os,signal,sys;"
                    "sys.stdout.write('password=hunter2\\n');sys.stdout.flush();"
                    "os.kill(os.getpid(),signal.SIGKILL)"
                ),
            ]
            with self.assertRaises(SupervisedCommandError) as raised:
                run_supervised_command(
                    command,
                    cwd=Path(temporary),
                    environment=os.environ.copy(),
                    timeout_seconds=1,
                    label="Codex",
                )

        self.assertEqual(raised.exception.classification, "abnormal_exit")
        self.assertEqual(raised.exception.exit_code, -signal.SIGKILL)
        self.assertIn("SIGKILL", raised.exception.summary)
        self.assertEqual(raised.exception.stdout, "password=[REDACTED]\n")

    def test_invalid_utf8_has_a_neutral_failure_classification(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(SupervisedCommandError) as raised:
                run_supervised_command(
                    [sys.executable, "-c", "import os; os.write(1, b'\\xff')"],
                    cwd=Path(temporary),
                    environment=os.environ.copy(),
                    timeout_seconds=1,
                    label="Codex",
                )

        self.assertEqual(raised.exception.classification, "invalid_utf8")
        self.assertIn("UTF-8", raised.exception.summary)

    def test_invalid_utf8_signal_diagnostics_stay_within_the_byte_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = [
                sys.executable,
                "-c",
                (
                    "import os,signal;"
                    "os.write(1,b'\\xff'*4);"
                    "os.kill(os.getpid(),signal.SIGKILL)"
                ),
            ]
            with self.assertRaises(SupervisedCommandError) as raised:
                run_supervised_command(
                    command,
                    cwd=Path(temporary),
                    environment=os.environ.copy(),
                    timeout_seconds=1,
                    label="Codex",
                    output_byte_limit=4,
                )

        self.assertEqual(raised.exception.classification, "abnormal_exit")
        self.assertLessEqual(len(raised.exception.stdout.encode("utf-8")), 4)

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

            with self.assertRaisesRegex(SupervisedCommandError, "timed out"):
                run_supervised_command(
                    [sys.executable, "-c", parent],
                    cwd=root,
                    environment=os.environ.copy(),
                    timeout_seconds=0.1,
                    input_text="",
                    label="Codex",
                    cleanup_seconds=0.1,
                )

            time.sleep(0.7)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
