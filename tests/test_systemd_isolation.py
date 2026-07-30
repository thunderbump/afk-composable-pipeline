import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

import tests.test_start_cli as start_cli_tests


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
                        "import signal,sys,time;"
                        "from pathlib import Path;"
                        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                        "Path(sys.argv[1]).write_text('ready');"
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

            self.assertEqual(started.returncode, 0, started.stderr)
            run_id = started.stdout.strip()
            unit = f"afk-{run_id}-worker-1"
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "retrospective descendant did not start")

            stopped = subprocess.run(
                [systemctl, "--user", "stop", unit],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            time.sleep(2.1)
            self.assertFalse(mutation.exists())
        finally:
            if unit is not None:
                subprocess.run(
                    [systemctl, "--user", "stop", unit],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
