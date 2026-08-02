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
from afk import process_io  # noqa: E402
from afk.process_supervision import (  # noqa: E402
    SupervisedCommandError,
    run_supervised_command,
)


class SupervisedCommandTest(unittest.TestCase):
    def test_helper_launch_failure_is_a_neutral_supervision_failure(self):
        with (
            mock.patch.object(
                process_supervision.subprocess,
                "Popen",
                side_effect=OSError("unavailable"),
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

        self.assertEqual(raised.exception.classification, "supervision_failure")
        self.assertIn("helper is unavailable", raised.exception.summary)

    def test_helper_protocol_failure_cleans_detached_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "detached-ready"
            late_mutation = root / "late-mutation"
            child = (
                "import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"open({str(ready)!r},'w').write('ready');"
                "time.sleep(0.5);"
                f"open({str(late_mutation)!r},'w').write('mutated')"
            )
            command = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}],"
                "start_new_session=True);"
                "time.sleep(30)"
            )

            def fail_after_detach(_channel):
                deadline = time.monotonic() + 1
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not ready.exists():
                    raise AssertionError("detached child was not launched")
                raise ValueError("invalid protocol")

            with (
                mock.patch.object(
                    process_supervision,
                    "_receive_protocol_message",
                    side_effect=fail_after_detach,
                ),
                self.assertRaises(SupervisedCommandError) as raised,
            ):
                run_supervised_command(
                    [sys.executable, "-c", command],
                    cwd=root,
                    environment=os.environ.copy(),
                    timeout_seconds=2,
                    label="Codex",
                    cleanup_seconds=0.1,
                )

            self.assertEqual(raised.exception.classification, "supervision_failure")
            time.sleep(0.7)
            self.assertFalse(late_mutation.exists())

    def test_helper_cleans_detached_descendants_after_parent_death(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "parent-death-ready"
            late_mutation = root / "parent-death-mutation"
            child = (
                "import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"open({str(ready)!r},'w').write('ready');"
                "time.sleep(0.5);"
                f"open({str(late_mutation)!r},'w').write('mutated')"
            )
            command = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}],"
                "start_new_session=True);"
                "time.sleep(30)"
            )
            caller = (
                "import os,sys; from pathlib import Path;"
                "from afk.process_supervision import run_supervised_command;"
                f"run_supervised_command([sys.executable,'-c',{command!r}],"
                f"cwd=Path({str(root)!r}),environment=os.environ.copy(),"
                "timeout_seconds=30,label='Codex',cleanup_seconds=0.1)"
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "src")
            parent = process_supervision.subprocess.Popen(
                [sys.executable, "-c", caller],
                env=environment,
                stdin=process_supervision.subprocess.DEVNULL,
                stdout=process_supervision.subprocess.DEVNULL,
                stderr=process_supervision.subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                parent.kill()
                parent.wait(timeout=2)
                time.sleep(0.7)
                self.assertFalse(late_mutation.exists())
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=2)

    def test_protocol_round_trips_os_surrogate_arguments(self):
        argument = os.fsdecode(b"\xff")
        completed = run_supervised_command(
            [
                sys.executable,
                "-c",
                "import os,sys; print(os.fsencode(sys.argv[1]).hex())",
                argument,
            ],
            cwd=Path.cwd(),
            environment=os.environ.copy(),
            timeout_seconds=1,
            label="Codex",
        )

        self.assertEqual(completed.stdout, "ff\n")

    def test_supervised_cleanup_does_not_terminate_an_unrelated_host_child(self):
        result = []
        failures = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervised_ready = root / "supervised-ready"
            release_supervised = root / "release-supervised"
            command = (
                "import pathlib,time;"
                f"ready=pathlib.Path({str(supervised_ready)!r});"
                f"release=pathlib.Path({str(release_supervised)!r});"
                "ready.write_text('ready');"
                "\nwhile not release.exists(): time.sleep(0.01)"
            )

            def invoke_supervised():
                try:
                    result.append(
                        run_supervised_command(
                            [sys.executable, "-c", command],
                            cwd=root,
                            environment=os.environ.copy(),
                            timeout_seconds=2,
                            label="Codex",
                        )
                    )
                except BaseException as exc:
                    failures.append(exc)

            supervised = threading.Thread(target=invoke_supervised)
            supervised.start()
            deadline = time.monotonic() + 1
            while not supervised_ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(supervised_ready.exists())
            unrelated = process_supervision.subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=process_supervision.subprocess.DEVNULL,
                stdout=process_supervision.subprocess.DEVNULL,
                stderr=process_supervision.subprocess.DEVNULL,
            )
            try:
                release_supervised.write_text("release\n", encoding="utf-8")
                supervised.join(4)
                self.assertFalse(supervised.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(result[0].returncode, 0)
                self.assertIsNone(unrelated.poll())
            finally:
                if unrelated.poll() is None:
                    unrelated.terminate()
                unrelated.wait(timeout=2)

    def test_output_read_failure_is_a_neutral_supervision_failure(self):
        real_read = process_io.os.read

        def fail_reader_read(descriptor, byte_count):
            if threading.current_thread() is not threading.main_thread():
                raise OSError("read failed")
            return real_read(descriptor, byte_count)

        started = time.monotonic()
        with (
            mock.patch.object(process_io.os, "read", side_effect=fail_reader_read),
            self.assertRaises(SupervisedCommandError) as raised,
        ):
            process_supervision._run_supervised_command_local(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=Path.cwd(),
                environment=os.environ.copy(),
                timeout_seconds=2,
                label="Codex",
                cleanup_seconds=0.1,
            )

        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(raised.exception.classification, "supervision_failure")
        self.assertIn("output streams could not be read", raised.exception.summary)

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
            process_supervision._run_supervised_command_local(
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

    def test_concurrent_calls_have_isolated_process_ownership(self):
        results = {}
        failures = []
        libc = ctypes.CDLL(None, use_errno=True)
        initial_subreaper = ctypes.c_int()
        self.assertEqual(
            libc.prctl(37, ctypes.byref(initial_subreaper), 0, 0, 0),
            0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_ready = root / "first-ready"
            release_first = root / "release-first"
            first_command = (
                "import pathlib,time;"
                f"ready=pathlib.Path({str(first_ready)!r});"
                f"release=pathlib.Path({str(release_first)!r});"
                "ready.write_text('ready');"
                "\nwhile not release.exists(): time.sleep(0.01);"
                "\nprint('first')"
            )

            def invoke_first():
                try:
                    results["first"] = run_supervised_command(
                        [sys.executable, "-c", first_command],
                        cwd=root,
                        environment=os.environ.copy(),
                        timeout_seconds=3,
                        label="first",
                    )
                except BaseException as exc:
                    failures.append(exc)

            first = threading.Thread(target=invoke_first)
            first.start()
            deadline = time.monotonic() + 1
            while not first_ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(first_ready.exists())
            results["second"] = run_supervised_command(
                [sys.executable, "-c", "print('second')"],
                cwd=root,
                environment=os.environ.copy(),
                timeout_seconds=1,
                label="second",
            )
            self.assertTrue(first.is_alive())
            release_first.write_text("release\n", encoding="utf-8")
            first.join(4)

        self.assertFalse(first.is_alive())
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
                process_supervision._run_supervised_command_local(
                    [sys.executable, "-c", command],
                    cwd=root,
                    environment=os.environ.copy(),
                    timeout_seconds=1,
                    label="Codex",
                )

            time.sleep(0.4)
            self.assertFalse(marker.exists())

    def test_each_output_stream_is_independently_size_limited(self):
        for stream, descriptor in (("stdout", 1), ("stderr", 2)):
            with (
                self.subTest(stream=stream),
                tempfile.TemporaryDirectory() as temporary,
            ):
                command = [
                    sys.executable,
                    "-c",
                    (
                        f"import os,time; os.write({descriptor},b'x'*17);"
                        "time.sleep(30)"
                    ),
                ]
                started = time.monotonic()
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
                self.assertLess(time.monotonic() - started, 0.8)
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
