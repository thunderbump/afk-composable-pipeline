import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path


class SystemdIsolationTest(unittest.TestCase):
    def test_worker_control_group_kills_a_detached_descendant(self):
        available = subprocess.run(
            ["systemctl", "--user", "is-active", "default.target"],
            text=True,
            capture_output=True,
            check=False,
        )
        if available.returncode != 0:
            self.skipTest("a running user systemd manager is unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "detached-ready"
            mutation = root / "late-mutation"
            unit = f"afk-retrospective-isolation-{uuid.uuid4().hex}.service"
            child = (
                "import signal,sys,time;"
                "from pathlib import Path;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "Path(sys.argv[1]).write_text('ready');"
                "time.sleep(1);"
                "Path(sys.argv[2]).write_text('mutated')"
            )
            parent = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r},"
                "sys.argv[1],sys.argv[2]],start_new_session=True);"
                "deadline=time.monotonic()+1;"
                "ready=__import__('pathlib').Path(sys.argv[1]);"
                "\nwhile not ready.exists() and time.monotonic()<deadline:"
                "\n time.sleep(0.01)"
            )
            command = [
                "systemd-run",
                "--user",
                "--wait",
                "--collect",
                f"--unit={unit}",
                "--property=Type=exec",
                "--property=Restart=no",
                "--property=KillMode=control-group",
                "--property=KillSignal=SIGKILL",
                "--property=TimeoutStopSec=30",
                sys.executable,
                "-c",
                parent,
                str(ready),
                str(mutation),
            ]
            try:
                completed = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
            finally:
                subprocess.run(
                    ["systemctl", "--user", "stop", unit],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )
            self.assertTrue(ready.exists(), "detached descendant did not start")
            time.sleep(1.1)
            self.assertFalse(mutation.exists())


if __name__ == "__main__":
    unittest.main()
