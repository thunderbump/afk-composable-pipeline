import json
import os
import select
import signal
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

import tests.test_start_cli as start_cli_tests
from afk.durable_id import is_durable_id


ROOT = Path(__file__).resolve().parents[1]


class SystemdIsolationTest(unittest.TestCase):
    def test_public_start_worker_kills_a_detached_retrospective_descendant(self):
        systemctl = shutil.which("systemctl")
        systemd_run = shutil.which("systemd-run")
        if systemctl is None or systemd_run is None:
            self.skipTest("systemd executables are unavailable")
        available = subprocess.run(
            [systemctl, "--user", "is-active", "default.target"],
            text=True,
            capture_output=True,
            check=False,
        )
        if available.returncode != 0:
            self.skipTest("a running user systemd manager is unavailable")

        fixture = start_cli_tests.StartCliTest(
            methodName=(
                "test_start_launches_numbered_transient_worker_"
                "and_reports_checkpoint"
            )
        )
        fixture.setUp()
        unit = None
        run_id = None
        child_identity = None
        child_pidfd = None

        def published_child_identity():
            try:
                identity = json.loads(ready.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
                return None
            if (
                not isinstance(identity, dict)
                or set(identity) != {"pid", "start_time"}
                or type(identity["pid"]) is not int
                or identity["pid"] <= 0
                or not isinstance(identity["start_time"], str)
                or not identity["start_time"].isdigit()
            ):
                return None
            return identity

        def process_start_time(pid):
            try:
                stat_fields = (
                    Path(f"/proc/{pid}/stat")
                    .read_text(encoding="utf-8")
                    .rsplit(")", 1)[1]
                    .split()
                )
            except (FileNotFoundError, IndexError):
                return None
            return stat_fields[19]

        def pidfd_exited(descriptor):
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            return bool(poller.poll(0))

        def retain_published_child(identity):
            try:
                descriptor = os.pidfd_open(identity["pid"])
            except ProcessLookupError:
                return None
            try:
                matches = process_start_time(identity["pid"]) == identity[
                    "start_time"
                ] and not pidfd_exited(descriptor)
            except BaseException:
                os.close(descriptor)
                raise
            if matches:
                return descriptor
            os.close(descriptor)
            return None

        try:
            fake_systemd_run = fixture.fake_bin / "systemd-run"
            fake_systemd_run.unlink()
            fake_systemd_run.symlink_to(systemd_run)
            ready = fixture.temp / "detached-ready"
            mutation = fixture.temp / "late-mutation"
            analyzer = fixture.fake_bin / "codex"
            analyzer.write_text(
                textwrap.dedent(
                    f"""
                    #!{sys.executable}
                    import signal
                    import subprocess
                    import sys
                    import time
                    from pathlib import Path

                    if "--skip-git-repo-check" not in sys.argv:
                        raise SystemExit(7)
                    child = (
                        "import json,os,signal,sys,time;"
                        "from pathlib import Path;"
                        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                        "start_time=Path('/proc/self/stat').read_text()"
                        ".rsplit(')',1)[1].split()[19];"
                        "ready=Path(sys.argv[1]);"
                        "temporary=ready.with_suffix('.tmp');"
                        "temporary.write_text(json.dumps({{"
                        "'pid':os.getpid(),'start_time':start_time}}));"
                        "os.replace(temporary,ready);"
                        "time.sleep(2);"
                        "Path(sys.argv[2]).write_text('mutated')"
                    )
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            child,
                            {str(ready)!r},
                            {str(mutation)!r},
                        ],
                        start_new_session=True,
                    )
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    time.sleep(60)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            analyzer.chmod(0o700)

            hook = fixture.temp / "public-worker-hook"
            hook.mkdir()
            (hook / "sitecustomize.py").write_text(
                "import os\n"
                f"os.environ['PATH'] = {f'{fixture.fake_bin}:/usr/bin:/bin'!r}\n"
                f"os.environ['HOME'] = {str(fixture.home)!r}\n"
                f"os.environ['XDG_CONFIG_HOME'] = {str(fixture.config_home)!r}\n"
                f"os.environ['AFK_FAKE_LOG'] = {str(fixture.command_log)!r}\n"
                f"os.environ['AFK_FAKE_PROJECT'] = {str(fixture.project)!r}\n"
                "os.environ['AFK_FAKE_CLAIM_FAILURE'] = '1'\n",
                encoding="utf-8",
            )
            pythonpath = f"{hook}:{ROOT / 'src'}"

            started = fixture.run_afk(
                "start",
                "central-bnkl.1.1",
                PYTHONPATH=pythonpath,
            )

            run_id = next(
                (
                    candidate
                    for candidate in started.stdout.split()
                    if is_durable_id(candidate)
                    and (
                        fixture.state_home / "afk" / "runs" / candidate / "run.json"
                    ).is_file()
                ),
                None,
            )
            if run_id is not None:
                unit = f"afk-{run_id}-worker-1"
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertIsNotNone(run_id, started.stdout)
            deadline = time.monotonic() + 10
            while child_identity is None and time.monotonic() < deadline:
                child_identity = published_child_identity()
                time.sleep(0.02)
            self.assertIsNotNone(
                child_identity,
                "retrospective descendant did not publish a valid identity",
            )
            child_pidfd = retain_published_child(child_identity)
            self.assertIsNotNone(
                child_pidfd,
                "retrospective descendant did not retain a pidfd",
            )

            stopped = subprocess.run(
                [systemctl, "--user", "stop", unit],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            deadline = time.monotonic() + 10
            while not pidfd_exited(child_pidfd) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(
                pidfd_exited(child_pidfd),
                "detached retrospective descendant survived the AFK unit stop",
            )
            status = start_cli_tests.RunStore(fixture.state_home / "afk").status(run_id)
            self.assertEqual(status["state"], "attention_required")
            self.assertEqual(status["last_event"], "lifecycle.signal_interrupted")
            self.assertEqual(
                status["lifecycle_interruption"],
                {
                    "schema_version": 1,
                    "status": "interrupted",
                    "signal": "SIGTERM",
                },
            )
            self.assertEqual(status["attention"]["scope"], "lifecycle")
            self.assertEqual(status["attention"]["kind"], "interrupted")
            self.assertEqual(
                status["attention"]["summary"],
                "AFK lifecycle received SIGTERM",
            )
            self.assertFalse(mutation.exists())
        finally:
            try:
                if unit is not None:
                    subprocess.run(
                        [systemctl, "--user", "stop", unit],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
            finally:
                try:
                    if child_pidfd is not None and not pidfd_exited(child_pidfd):
                        try:
                            signal.pidfd_send_signal(child_pidfd, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                finally:
                    try:
                        if child_pidfd is not None:
                            os.close(child_pidfd)
                    finally:
                        fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
