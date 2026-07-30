import gc
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import afk.retrospective_attempt as retrospective_attempt_module  # noqa: E402
from afk.retrospective_attempt import (  # noqa: E402
    RETROSPECTIVE_PERMISSION_PROFILE,
    RETROSPECTIVE_PROMPT,
    RETROSPECTIVE_TIMEOUT_SECONDS,
    _runtime_config,
    retrospective_evidence_identity,
    run_retrospective_attempt,
)
from afk.retrospective_result import (  # noqa: E402
    CATEGORIES,
    CONFIDENCE,
    PRIORITIES,
    SCOPES,
    normalize_retrospective_result,
)
from afk.run_store import RunStore, RunStoreBusy, RunStoreError  # noqa: E402
from afk.run_summary import build_run_summary  # noqa: E402


BASE_SHA = "a" * 40


class RetrospectiveAttemptTest(unittest.TestCase):
    def setUp(self):
        self.real_codex = shutil.which("codex")
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.path_patch = patch.dict(
            "os.environ",
            {"PATH": f"{self.bin}:/usr/bin:/bin"},
            clear=False,
        )
        self.path_patch.start()
        self.store = RunStore(self.root / "afk")
        self.store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"secret": "must-not-cross-the-boundary"},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "validated"},
            recorded_at="2026-07-28T10:01:00Z",
        )

    def tearDown(self):
        self.path_patch.stop()
        self.temporary_directory.cleanup()

    def test_runs_one_fresh_contained_analysis_and_reuses_its_sealed_outcome(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        self.analyzer(
            f"""
            import json, sys
            request = json.load(sys.stdin)
            print(json.dumps({analysis!r}))
            print("REQUEST=" + json.dumps(request, sort_keys=True), file=sys.stderr)
            """
        )

        first = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )
        self.analyzer("raise SystemExit('must not run')")
        second = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "empty")
        self.assertFalse(first["warning"])
        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        self.assertTrue(self.store.verify_evidence("run-001", evidence))
        files = self.evidence_files(evidence)
        self.assertEqual(
            files,
            {
                "analysis.json",
                "command.json",
                "input.json",
                "manifest.json",
                "outcome.json",
                "result.json",
                "stderr.log",
                "stdout.log",
            },
        )
        command = self.evidence_json(evidence, "command.json")
        self.assertEqual(command["timeout_seconds"], RETROSPECTIVE_TIMEOUT_SECONDS)
        self.assertEqual(
            command["policy"],
            {
                "control_plane_network": "model-api-only",
                "filesystem": "minimal-read",
                "interactive": False,
                "network": "disabled",
                "permission_profile": "retrospective-analysis",
                "runtime_home": "isolated",
                "session": "fresh",
            },
        )
        self.assertNotIn("--sandbox", command["argv"])
        self.assertIn("--ephemeral", command["argv"])
        self.assertEqual(command["argv"][:2], ["codex", "exec"])
        request = self.evidence_json(evidence, "input.json")
        serialized_request = json.dumps(request)
        self.assertEqual(request["run"]["run_id"], "run-001")
        self.assertNotIn("must-not-cross-the-boundary", serialized_request)
        self.assertNotIn("repository source", serialized_request)

    def test_uses_private_auth_only_runtime_and_least_privilege_codex_profile(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        configured_codex_home = self.root / "configured-codex"
        configured_codex_home.mkdir(mode=0o700)
        auth = configured_codex_home / "auth.json"
        auth.write_text('{"access_token":"runtime-secret"}\n', encoding="utf-8")
        auth.chmod(0o600)
        sentinel = configured_codex_home / "must-not-change"
        sentinel.write_text("original\n", encoding="utf-8")
        observation = self.root / "runtime-observation.json"
        self.analyzer(
            f"""
            import json, os, pathlib, sys, tomllib
            codex_home = pathlib.Path(os.environ["CODEX_HOME"])
            home = pathlib.Path(os.environ["HOME"])
            observed = {{
                "argv": sys.argv,
                "auth_loaded": json.loads(
                    (codex_home / "auth.json").read_text(encoding="utf-8")
                ) == {{"access_token": "runtime-secret"}},
                "auth_present": (codex_home / "auth.json").is_file(),
                "codex_entries": sorted(path.name for path in codex_home.iterdir()),
                "config": tomllib.loads(
                    (codex_home / "config.toml").read_text(encoding="utf-8")
                ),
                "codex_home": str(codex_home),
                "home": str(home),
            }}
            pathlib.Path({str(observation)!r}).write_text(
                json.dumps(observed), encoding="utf-8"
            )
            print(json.dumps({analysis!r}))
            """
        )

        with patch.dict(
            "os.environ",
            {"CODEX_HOME": str(configured_codex_home)},
            clear=False,
        ):
            outcome = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )

        observed = json.loads(observation.read_text(encoding="utf-8"))
        config = observed["config"]
        profile = config["permissions"]["retrospective-analysis"]
        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(observed["argv"][1], "exec")
        self.assertTrue(observed["auth_loaded"])
        self.assertTrue(observed["auth_present"])
        self.assertEqual(observed["codex_entries"], ["auth.json", "config.toml"])
        self.assertEqual(config["default_permissions"], "retrospective-analysis")
        self.assertEqual(config["web_search"], "disabled")
        self.assertEqual(profile["filesystem"], {":minimal": "read"})
        self.assertFalse(profile["network"]["enabled"])
        self.assertEqual(config["approval_policy"], "never")
        self.assertNotIn("mcp_servers", config)
        for feature in (
            "apps",
            "browser_use",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "computer_use",
            "enable_mcp_apps",
            "in_app_browser",
            "multi_agent",
            "multi_agent_v2",
            "plugins",
            "plugin_sharing",
            "remote_plugin",
            "shell_tool",
            "standalone_web_search",
            "unified_exec",
        ):
            self.assertFalse(config["features"][feature], feature)
        self.assertIn("--strict-config", observed["argv"])
        self.assertIn("--ephemeral", observed["argv"])
        self.assertIn("--ignore-rules", observed["argv"])
        self.assertNotIn("--sandbox", observed["argv"])
        self.assertIn('approval_policy="never"', observed["argv"])
        self.assertFalse(Path(observed["codex_home"]).exists())
        self.assertFalse(Path(observed["home"]).exists())
        self.assertEqual(
            auth.read_text(encoding="utf-8"),
            '{"access_token":"runtime-secret"}\n',
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(
            {path.name for path in configured_codex_home.iterdir()},
            {"auth.json", "must-not-change"},
        )

    def test_malformed_auth_returns_promptly_without_leaking_descriptors(self):
        def timeout_handler(_signum, _frame):
            raise AssertionError("auth validation blocked before analysis timeout")

        for index, kind in enumerate(
            ("fifo", "symlink", "directory", "oversized"),
            start=1,
        ):
            with self.subTest(kind=kind):
                store, sequence = self.distinct_episode(index + 140)
                configured_codex_home = self.root / f"malformed-auth-{kind}"
                configured_codex_home.mkdir(mode=0o700)
                auth = configured_codex_home / "auth.json"
                if kind == "fifo":
                    os.mkfifo(auth, mode=0o600)
                elif kind == "symlink":
                    target = configured_codex_home / "target.json"
                    target.write_text("{}\n", encoding="utf-8")
                    auth.symlink_to(target)
                elif kind == "directory":
                    auth.mkdir(mode=0o700)
                else:
                    auth.write_bytes(b"x" * (1024 * 1024 + 1))
                marker = self.root / f"malformed-auth-launched-{kind}"
                self.analyzer(
                    f"""
                    import pathlib
                    pathlib.Path({str(marker)!r}).touch()
                    """
                )
                descriptors_before = set(os.listdir("/proc/self/fd"))
                previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.setitimer(signal.ITIMER_REAL, 1)
                try:
                    with patch.dict(
                        "os.environ",
                        {"CODEX_HOME": str(configured_codex_home)},
                        clear=False,
                    ):
                        outcome = run_retrospective_attempt(
                            store,
                            "run-001",
                            episode_sequence=sequence,
                        )
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, previous_handler)

                self.assertEqual(outcome["status"], "unavailable")
                self.assertTrue(outcome["warning"])
                self.assertFalse(marker.exists())
                self.assertEqual(
                    set(os.listdir("/proc/self/fd")),
                    descriptors_before,
                )
                evidence = retrospective_evidence_identity(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                )
                self.assertTrue(store.verify_evidence("run-001", evidence))

    def test_auth_changed_during_copy_is_sealed_as_unavailable(self):
        store, sequence = self.distinct_episode(150)
        configured_codex_home = self.root / "changing-auth"
        configured_codex_home.mkdir(mode=0o700)
        auth = configured_codex_home / "auth.json"
        auth.write_bytes(b"auth")
        real_read = os.read
        changed = False

        def read_then_change(descriptor, count):
            nonlocal changed
            payload = real_read(descriptor, count)
            if payload and not changed:
                changed = True
                with auth.open("ab") as stream:
                    stream.write(b"x")
            return payload

        descriptors_before = set(os.listdir("/proc/self/fd"))
        with (
            patch.dict(
                "os.environ",
                {"CODEX_HOME": str(configured_codex_home)},
                clear=False,
            ),
            patch("afk.retrospective_attempt.os.read", side_effect=read_then_change),
        ):
            outcome = run_retrospective_attempt(
                store,
                "run-001",
                episode_sequence=sequence,
            )

        self.assertTrue(changed)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("changed while being copied", outcome["warning_summary"])
        self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
        evidence = retrospective_evidence_identity(
            store,
            "run-001",
            episode_sequence=sequence,
        )
        self.assertTrue(store.verify_evidence("run-001", evidence))

    def test_same_size_auth_mutation_during_copy_is_sealed_as_unavailable(self):
        store, sequence = self.distinct_episode(151)
        configured_codex_home = self.root / "torn-auth"
        configured_codex_home.mkdir(mode=0o700)
        auth = configured_codex_home / "auth.json"
        auth.write_bytes(b"a" * 8192)
        marker = self.root / "torn-auth-analyzer-launched"
        analysis = self.empty_analysis(summary="No actionable findings.")
        self.analyzer(
            f"""
            import json, pathlib
            pathlib.Path({str(marker)!r}).touch()
            print(json.dumps({analysis!r}))
            """
        )
        real_read = os.read
        changed = False

        def read_chunk_then_mutate(descriptor, count):
            nonlocal changed
            payload = real_read(descriptor, min(count, 4096))
            if payload and not changed:
                changed = True
                with auth.open("r+b") as stream:
                    stream.seek(4096)
                    stream.write(b"b" * 4096)
                    stream.flush()
                    os.fsync(stream.fileno())
            return payload

        descriptors_before = set(os.listdir("/proc/self/fd"))
        with (
            patch.dict(
                "os.environ",
                {"CODEX_HOME": str(configured_codex_home)},
                clear=False,
            ),
            patch(
                "afk.retrospective_attempt.os.read",
                side_effect=read_chunk_then_mutate,
            ),
        ):
            outcome = run_retrospective_attempt(
                store,
                "run-001",
                episode_sequence=sequence,
            )

        self.assertTrue(changed)
        self.assertEqual(auth.stat().st_size, 8192)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("changed while being copied", outcome["warning_summary"])
        self.assertFalse(marker.exists())
        self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
        evidence = retrospective_evidence_identity(
            store,
            "run-001",
            episode_sequence=sequence,
        )
        self.assertTrue(store.verify_evidence("run-001", evidence))

    def test_public_attempt_has_no_child_command_override(self):
        with self.assertRaisesRegex(
            TypeError,
            "unexpected keyword argument 'codex_executable'",
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=["codex", "--dangerously-bypass-approvals"],
            )

    def test_io_initialization_failure_cleans_analysis_runtime_and_descriptors(self):
        configured_codex_home = self.root / "configured-codex"
        configured_codex_home.mkdir(mode=0o700)
        secret = b"initialization-failure-auth"
        (configured_codex_home / "auth.json").write_bytes(secret)
        observation = self.root / "initialization-failure.json"
        self.analyzer(
            f"""
            import json, os, pathlib, time
            codex_home = pathlib.Path(os.environ["CODEX_HOME"])
            pathlib.Path({str(observation)!r}).write_text(
                json.dumps({{
                    "auth_loaded": (
                        codex_home / "auth.json"
                    ).read_bytes() == {secret!r},
                    "codex_home": str(codex_home),
                    "pgid": os.getpgid(0),
                    "pid": os.getpid(),
                }}),
                encoding="utf-8",
            )
            time.sleep(60)
            """
        )

        def fail_after_analysis_starts(_descriptor, _blocking):
            deadline = time.monotonic() + 2
            while not observation.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not observation.exists():
                raise AssertionError("analysis process did not start")
            raise OSError("injected I/O initialization failure")

        descriptors_before = set(os.listdir("/proc/self/fd"))
        observed = None
        try:
            with (
                patch.dict(
                    "os.environ",
                    {"CODEX_HOME": str(configured_codex_home)},
                    clear=False,
                ),
                patch(
                    "afk.process_io.os.set_blocking",
                    side_effect=fail_after_analysis_starts,
                ),
            ):
                outcome = run_retrospective_attempt(
                    self.store,
                    "run-001",
                    episode_sequence=2,
                )

            observed = json.loads(observation.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while self.process_is_live(observed["pid"]) and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual(outcome["status"], "unavailable")
            self.assertTrue(outcome["warning"])
            self.assertEqual(
                outcome["warning_summary"],
                "injected I/O initialization failure",
            )
            self.assertTrue(observed["auth_loaded"])
            self.assertFalse(self.process_is_live(observed["pid"]))
            with self.assertRaises(ProcessLookupError):
                os.killpg(observed["pgid"], 0)
            self.assertFalse(Path(observed["codex_home"]).parents[1].exists())
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
            evidence = retrospective_evidence_identity(
                self.store,
                "run-001",
                episode_sequence=2,
            )
            self.assertTrue(self.store.verify_evidence("run-001", evidence))
            self.assertEqual(
                run_retrospective_attempt(
                    self.store,
                    "run-001",
                    episode_sequence=2,
                ),
                outcome,
            )
        finally:
            if observed is not None:
                try:
                    os.killpg(observed["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_pidfd_failure_cleans_analysis_process_pipes_and_status_pipe(self):
        observation = self.root / "pidfd-failure.json"
        self.analyzer(
            f"""
            import json, os, pathlib, time
            pathlib.Path({str(observation)!r}).write_text(
                json.dumps({{
                    "pgid": os.getpgid(0),
                    "pid": os.getpid(),
                }}),
                encoding="utf-8",
            )
            time.sleep(60)
            """
        )

        def fail_after_analysis_starts(_process_id):
            deadline = time.monotonic() + 2
            while not observation.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not observation.exists():
                raise AssertionError("analysis process did not start")
            raise OSError("injected pidfd failure")

        descriptors_before = set(os.listdir("/proc/self/fd"))
        observed = None
        try:
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always", ResourceWarning)
                with patch(
                    "afk.retrospective_attempt.os.pidfd_open",
                    side_effect=fail_after_analysis_starts,
                ):
                    outcome = run_retrospective_attempt(
                        self.store,
                        "run-001",
                        episode_sequence=2,
                    )
                gc.collect()

            observed = json.loads(observation.read_text(encoding="utf-8"))
            self.assertEqual(outcome["status"], "unavailable")
            self.assertTrue(outcome["warning"])
            self.assertEqual(outcome["warning_summary"], "injected pidfd failure")
            self.assertFalse(self.process_is_live(observed["pid"]))
            with self.assertRaises(ProcessLookupError):
                os.killpg(observed["pgid"], 0)
            self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors_before)
            self.assertEqual(
                [
                    warning
                    for warning in recorded
                    if isinstance(warning.message, ResourceWarning)
                ],
                [],
            )
            evidence = retrospective_evidence_identity(
                self.store,
                "run-001",
                episode_sequence=2,
            )
            self.assertTrue(self.store.verify_evidence("run-001", evidence))
            self.assertEqual(
                run_retrospective_attempt(
                    self.store,
                    "run-001",
                    episode_sequence=2,
                ),
                outcome,
            )
        finally:
            if observed is not None:
                try:
                    os.killpg(observed["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_real_codex_profile_denies_external_write_and_direct_socket(self):
        if self.real_codex is None:
            self.skipTest("local Codex executable is unavailable")
        try:
            probe_directory = tempfile.TemporaryDirectory(
                prefix="afk-codex-sandbox-",
                dir=Path.home(),
            )
        except OSError as exc:
            self.skipTest(f"local Codex sandbox directory is unavailable: {exc}")
        self.addCleanup(probe_directory.cleanup)
        probe = Path(probe_directory.name)
        codex_home = probe / "codex-home"
        workspace = probe / "workspace"
        outside = probe / "outside.txt"
        codex_home.mkdir(parents=True)
        workspace.mkdir()
        (codex_home / "config.toml").write_text(
            _runtime_config(),
            encoding="utf-8",
        )
        environment = {
            **os.environ,
            "CODEX_HOME": str(codex_home),
            "HOME": str(probe / "home"),
            "PATH": "/usr/bin:/bin",
        }
        command = [
            self.real_codex,
            "sandbox",
            "-P",
            RETROSPECTIVE_PERMISSION_PROFILE,
            "--sandbox-state-readable-root",
            str(Path(self.real_codex).resolve().parent.parent),
            "-C",
            str(workspace),
        ]
        available = subprocess.run(
            [*command, "true"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if available.returncode != 0:
            detail = (available.stderr or available.stdout).strip()[:200]
            unavailable_markers = (
                "bwrap:",
                "bubblewrap",
                "landlock",
                "Operation not permitted",
                "No such file or directory",
                "sandbox is not supported",
            )
            if any(marker in detail for marker in unavailable_markers):
                self.skipTest(f"local Codex sandbox is unavailable: {detail}")
            self.fail(f"generated Codex profile could not start: {detail}")

        write = subprocess.run(
            [
                *command,
                "python3",
                "-c",
                (
                    "from pathlib import Path\n"
                    "try:\n"
                    f" Path({str(outside)!r}).write_text('escaped')\n"
                    "except OSError:\n"
                    " print('WRITE_DENIED')\n"
                    "else:\n"
                    " raise SystemExit('WRITE_ALLOWED')\n"
                ),
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        socket_probe = subprocess.run(
            [
                *command,
                "python3",
                "-c",
                (
                    "import socket\n"
                    "try:\n"
                    " socket.create_connection(('127.0.0.1', 9), timeout=0.1)\n"
                    "except PermissionError:\n"
                    " print('SOCKET_DENIED')\n"
                    "else:\n"
                    " raise SystemExit('SOCKET_ALLOWED')\n"
                ),
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(write.returncode, 0, write.stderr or write.stdout)
        self.assertEqual(write.stdout.strip(), "WRITE_DENIED")
        self.assertFalse(outside.exists())
        self.assertEqual(
            socket_probe.returncode,
            0,
            socket_probe.stderr or socket_probe.stdout,
        )
        self.assertEqual(socket_probe.stdout.strip(), "SOCKET_DENIED")

    def test_concurrent_callers_start_exactly_one_analysis_process(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        starts = self.root / "analysis-starts"
        self.analyzer(
            f"""
            import json, time
            with open({str(starts)!r}, "a", encoding="utf-8") as stream:
                stream.write("started\\n")
                stream.flush()
            time.sleep(0.25)
            print(json.dumps({analysis!r}))
            """
        )
        result = []

        worker = threading.Thread(
            target=lambda: result.append(
                run_retrospective_attempt(
                    self.store,
                    "run-001",
                    episode_sequence=2,
                )
            )
        )
        worker.start()
        deadline = time.monotonic() + 2
        while not starts.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        with self.assertRaises(RunStoreBusy):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["status"], "empty")
        self.assertEqual(starts.read_text(encoding="utf-8"), "started\n")

    def test_analysis_cleanup_does_not_signal_or_reap_unrelated_process(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        started = self.root / "analysis-started"
        self.analyzer(
            f"""
            import json, pathlib, time
            pathlib.Path({str(started)!r}).write_text("started", encoding="utf-8")
            time.sleep(0.3)
            print(json.dumps({analysis!r}))
            """
        )
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                run_retrospective_attempt(
                    self.store,
                    "run-001",
                    episode_sequence=2,
                )
            )
        )
        worker.start()
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        unrelated = subprocess.Popen(["sleep", "60"])
        try:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0]["status"], "empty")
            self.assertIsNone(unrelated.poll())
        finally:
            if unrelated.poll() is None:
                unrelated.terminate()
                unrelated.wait(timeout=2)

    def test_sigkill_sigterm_blocked_worker_kills_analysis_tree_and_auth_access(self):
        configured_codex_home = self.root / "configured-codex"
        configured_codex_home.mkdir(mode=0o700)
        secret = b"hard-kill-auth-secret"
        (configured_codex_home / "auth.json").write_bytes(secret)
        observation = self.root / "hard-kill-observation.json"
        self.analyzer(
            f"""
            import json, os, pathlib, subprocess, time
            descendant = subprocess.Popen(["sleep", "60"])
            codex_home = pathlib.Path(os.environ["CODEX_HOME"])
            pathlib.Path({str(observation)!r}).write_text(
                json.dumps({{
                    "analyzer_pid": os.getpid(),
                    "auth_loaded": (
                        codex_home / "auth.json"
                    ).read_bytes() == {secret!r},
                    "codex_home": str(codex_home),
                    "descendant_pid": descendant.pid,
                }}),
                encoding="utf-8",
            )
            time.sleep(60)
            """
        )

        with patch.dict(
            "os.environ",
            {"CODEX_HOME": str(configured_codex_home)},
            clear=False,
        ):
            worker_pid = os.fork()
            if worker_pid == 0:
                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
                run_retrospective_attempt(
                    self.store,
                    "run-001",
                    episode_sequence=2,
                )
                os._exit(0)
            deadline = time.monotonic() + 3
            while not observation.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(observation.exists(), "analysis process did not start")
            observed = json.loads(observation.read_text(encoding="utf-8"))
            self.assertTrue(observed["auth_loaded"])
            os.kill(worker_pid, signal.SIGKILL)
            os.waitpid(worker_pid, 0)

        process_ids = (
            observed["analyzer_pid"],
            observed["descendant_pid"],
        )
        deadline = time.monotonic() + 3
        while (
            any(self.process_is_live(pid) for pid in process_ids)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        try:
            self.assertFalse(
                any(self.process_is_live(pid) for pid in process_ids),
                process_ids,
            )
        finally:
            for pid in process_ids:
                if self.process_is_live(pid):
                    os.kill(pid, signal.SIGKILL)

        relaunch_marker = self.root / "hard-kill-relaunch"
        self.analyzer(
            f"""
            import pathlib
            pathlib.Path({str(relaunch_marker)!r}).touch()
            raise SystemExit("must not relaunch")
            """
        )
        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )
        evidence = retrospective_evidence_identity(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(outcome["status"], "interrupted")
        self.assertTrue(outcome["warning"])
        self.assertFalse(relaunch_marker.exists())
        self.assertEqual(
            self.store.effect(
                "run-001",
                "retrospective-analysis-2",
            )["status"],
            "confirmed",
        )
        self.assertTrue(self.store.verify_evidence("run-001", evidence))

        abandoned_runtime = Path(observed["codex_home"]).parents[1]
        try:
            self.assertTrue(abandoned_runtime.exists())
            auth_link = Path(observed["codex_home"]) / "auth.json"
            self.assertTrue(auth_link.is_symlink())
            self.assertFalse(auth_link.exists())
            with self.assertRaises(FileNotFoundError):
                auth_link.read_bytes()
            for path in abandoned_runtime.rglob("*"):
                if not path.is_symlink() and path.is_file():
                    self.assertNotIn(secret, path.read_bytes(), path)
        finally:
            shutil.rmtree(abandoned_runtime)

    def test_stale_prepared_claim_recovers_partial_evidence_without_relaunch(self):
        class SimulatedCrash(RuntimeError):
            pass

        analysis = self.empty_analysis(summary="No actionable findings.")
        starts = self.root / "analysis-starts"
        self.analyzer(
            f"""
            import json
            with open({str(starts)!r}, "a", encoding="utf-8") as stream:
                stream.write("started\\n")
            print(json.dumps({analysis!r}))
            """
        )
        original_write = self.store.write_evidence_text
        writes = 0

        def crash_after_partial_evidence(*args, **kwargs):
            nonlocal writes
            result = original_write(*args, **kwargs)
            writes += 1
            if writes == 1:
                raise SimulatedCrash("crash during evidence persistence")
            return result

        with (
            patch.object(
                self.store,
                "write_evidence_text",
                side_effect=crash_after_partial_evidence,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(outcome["status"], "interrupted")
        self.assertTrue(outcome["warning"])
        self.assertEqual(starts.read_text(encoding="utf-8"), "started\n")
        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        self.assertTrue(self.store.verify_evidence("run-001", evidence))

    def test_crash_boundaries_never_relaunch_or_duplicate_an_attempt(self):
        class SimulatedCrash(RuntimeError):
            pass

        cases = (
            ("before-claim", "prepare", "before", "empty", 1),
            ("after-claim", "prepare", "after", "interrupted", 0),
            ("before-process", "process", "before", "interrupted", 0),
            ("after-process", "process", "after", "interrupted", 1),
            ("before-result", "result", "before", "empty", 1),
            ("after-result", "result", "after", "empty", 1),
            ("before-seal", "seal", "before", "empty", 1),
            ("after-seal", "seal", "after", "empty", 1),
            ("before-confirm", "confirm", "before", "empty", 1),
            ("after-confirm", "confirm", "after", "empty", 1),
        )
        for index, (
            label,
            boundary,
            side,
            expected_status,
            expected_starts,
        ) in enumerate(cases, start=1):
            with self.subTest(label=label):
                store, sequence = self.distinct_episode(index + 200)
                analysis = self.empty_analysis(summary="No actionable findings.")
                starts = self.root / f"boundary-starts-{index}"
                self.analyzer(
                    f"""
                    import json
                    with open({str(starts)!r}, "a", encoding="utf-8") as stream:
                        stream.write("started\\n")
                    print(json.dumps({analysis!r}))
                    """
                )
                evidence = retrospective_evidence_identity(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                )
                claim_id = f"retrospective-analysis-{sequence}"
                crashed = False

                if boundary == "prepare":
                    original = store.prepare_effect

                    def selected(args):
                        return args[1] == claim_id

                    target = patch.object(store, "prepare_effect")
                elif boundary == "process":
                    original = retrospective_attempt_module._run_retrospective_process

                    def selected(_args):
                        return True

                    target = patch(
                        "afk.retrospective_attempt._run_retrospective_process"
                    )
                elif boundary == "result":
                    original = store.reconcile_evidence_value

                    def selected(args):
                        return args[1] == f"{evidence}/result.json"

                    target = patch.object(store, "reconcile_evidence_value")
                elif boundary == "seal":
                    original = store.seal_evidence

                    def selected(args):
                        return args[1] == evidence

                    target = patch.object(store, "seal_evidence")
                else:
                    original = store.confirm_effect

                    def selected(args):
                        return args[1] == claim_id

                    target = patch.object(store, "confirm_effect")

                def injected(*args, **kwargs):
                    nonlocal crashed
                    matches = selected(args)
                    if matches and not crashed and side == "before":
                        crashed = True
                        raise SimulatedCrash(label)
                    result = original(*args, **kwargs)
                    if matches and not crashed and side == "after":
                        crashed = True
                        raise SimulatedCrash(label)
                    return result

                with (
                    target as injected_target,
                    self.assertRaisesRegex(SimulatedCrash, label),
                ):
                    injected_target.side_effect = injected
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

                outcome = run_retrospective_attempt(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                )
                manifest_path = (
                    store.root / "runs" / "run-001" / evidence / "manifest.json"
                )
                manifest_before = manifest_path.read_bytes()
                repeated = run_retrospective_attempt(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                )

                self.assertEqual(outcome, repeated)
                self.assertEqual(outcome["status"], expected_status)
                self.assertEqual(
                    (
                        starts.read_text(encoding="utf-8").splitlines()
                        if starts.exists()
                        else []
                    ),
                    ["started"] * expected_starts,
                )
                self.assertEqual(
                    store.effect("run-001", claim_id)["status"],
                    "confirmed",
                )
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertTrue(store.verify_evidence("run-001", evidence))

    def test_stale_claim_finishes_durable_result_without_relaunch(self):
        class SimulatedCrash(RuntimeError):
            pass

        analysis = self.empty_analysis(summary="No actionable findings.")
        starts = self.root / "analysis-starts"
        self.analyzer(
            f"""
            import json
            with open({str(starts)!r}, "a", encoding="utf-8") as stream:
                stream.write("started\\n")
            print(json.dumps({analysis!r}))
            """
        )
        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        original_seal = self.store.seal_evidence
        crashed = False

        def crash_before_attempt_seal(run_id, relative_directory):
            nonlocal crashed
            if relative_directory == evidence and not crashed:
                crashed = True
                raise SimulatedCrash("crash after durable result")
            return original_seal(run_id, relative_directory)

        with (
            patch.object(
                self.store,
                "seal_evidence",
                side_effect=crash_before_attempt_seal,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(outcome["status"], "empty")
        self.assertFalse(outcome["warning"])
        self.assertEqual(starts.read_text(encoding="utf-8"), "started\n")
        self.assertTrue(self.store.verify_evidence("run-001", evidence))

    def test_stale_claim_rejects_forged_command_before_persisting_recovery(self):
        cases = ("argv", "policy", "timeout")
        for index, field in enumerate(cases, start=1):
            with self.subTest(field=field):
                store, sequence = self.distinct_episode(index + 120)
                with (
                    patch(
                        "afk.retrospective_attempt._run_retrospective_process",
                        side_effect=RuntimeError("crash before analysis"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "crash before analysis"),
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

                effect_path = (
                    store.root
                    / "runs"
                    / "run-001"
                    / "effects"
                    / f"retrospective-analysis-{sequence}.json"
                )
                effect = json.loads(effect_path.read_text(encoding="utf-8"))
                command = effect["intended"]["command"]
                if field == "argv":
                    command["argv"] = [
                        "codex",
                        "exec",
                        "--dangerously-bypass-approvals-and-sandbox",
                    ]
                elif field == "policy":
                    command["policy"]["network"] = "enabled"
                else:
                    command["timeout_seconds"] = 1
                effect_path.write_text(
                    json.dumps(effect, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                evidence = retrospective_evidence_identity(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                )
                evidence_path = store.root / "runs" / "run-001" / evidence
                self.assertFalse(evidence_path.exists())

                with self.assertRaisesRegex(
                    RunStoreError,
                    "retrospective analysis command is invalid",
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

                self.assertFalse(evidence_path.exists())

    def test_stale_claim_rejects_forged_partial_authoritative_evidence(self):
        def forge_json(path, value):
            path.write_text(
                json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

        cases = (
            ("input.json", {"schema_version": 2, "forged": True}),
            ("command.json", {"schema_version": 1, "forged": True}),
            (
                "outcome.json",
                {
                    "schema_version": 1,
                    "run_id": "run-001",
                    "episode_sequence": 2,
                    "status": "interrupted",
                    "warning": True,
                    "process_findings_count": 0,
                    "improvement_proposals_count": 0,
                    "warning_summary": "forged outcome",
                },
            ),
            (
                "result.json",
                {
                    "schema_version": 1,
                    "run_id": "run-001",
                    "episode_sequence": 2,
                    "status": "interrupted",
                    "warning": True,
                    "process_findings_count": 0,
                    "improvement_proposals_count": 0,
                    "warning_summary": "forged result",
                },
            ),
            (
                "analysis.json",
                {
                    "schema_version": 1,
                    "run_id": "run-001",
                    "terminal_outcome": "attention_required",
                    "summary": "A forged finding changes the accepted counts.",
                    "process_findings": [
                        {
                            "id": "finding-forged",
                            "category": "orchestration",
                            "title": "Forged finding",
                            "evidence": [
                                {
                                    "artifact": "events.jsonl",
                                    "event_sequence": 2,
                                }
                            ],
                            "impact": "It contradicts the durable empty outcome.",
                            "confidence": "high",
                        }
                    ],
                    "improvement_proposals": [],
                },
            ),
            ("unexpected.json", {"forged": True}),
            ("missing-input.json", None),
            ("missing-result.json", None),
        )
        for index, (filename, forged) in enumerate(cases, start=1):
            with self.subTest(filename=filename):
                store, sequence = self.distinct_episode(index + 40)
                analysis = self.empty_analysis(summary="No actionable findings.")
                self.analyzer(f"print({json.dumps(json.dumps(analysis))})")
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                original_seal = store.seal_evidence

                def crash_before_attempt_seal(run_id, relative_directory):
                    if relative_directory == evidence:
                        raise RuntimeError("crash after durable result")
                    return original_seal(run_id, relative_directory)

                with (
                    patch.object(
                        store,
                        "seal_evidence",
                        side_effect=crash_before_attempt_seal,
                    ),
                    self.assertRaisesRegex(RuntimeError, "crash after durable result"),
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )
                target_name = filename.removeprefix("missing-")
                target = store.root / "runs" / "run-001" / evidence / target_name
                if filename.startswith("missing-"):
                    target.unlink()
                else:
                    forge_json(target, forged)

                with self.assertRaises(RunStoreError):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

    def test_standalone_sealed_result_without_claim_cannot_bypass_launch(self):
        evidence = retrospective_evidence_identity(
            self.store,
            "run-001",
            episode_sequence=2,
        )
        outcome = {
            "schema_version": 1,
            "run_id": "run-001",
            "episode_sequence": 2,
            "status": "empty",
            "warning": False,
            "process_findings_count": 0,
            "improvement_proposals_count": 0,
        }
        self.store.write_evidence_value(
            "run-001",
            f"{evidence}/result.json",
            outcome,
        )
        self.store.seal_evidence("run-001", evidence)
        marker = self.root / "must-not-launch"
        self.analyzer(
            f"""
            import pathlib
            pathlib.Path({str(marker)!r}).touch()
            """
        )

        with self.assertRaisesRegex(
            RunStoreError,
            "sealed retrospective evidence lacks a valid claim",
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )

        self.assertFalse(marker.exists())

    def test_sealed_bundle_with_claim_rejects_missing_required_file(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        self.analyzer(f"print({json.dumps(json.dumps(analysis))})")
        with (
            patch.object(
                self.store,
                "confirm_effect",
                side_effect=RuntimeError("crash after seal"),
            ),
            self.assertRaisesRegex(RuntimeError, "crash after seal"),
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )
        evidence = retrospective_evidence_identity(
            self.store,
            "run-001",
            episode_sequence=2,
        )
        self.reseal_after(
            self.store,
            evidence,
            lambda directory: (directory / "stdout.log").unlink(),
        )

        with self.assertRaisesRegex(
            RunStoreError,
            "sealed retrospective evidence files are invalid",
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )

    def test_complete_sealed_bundle_confirms_prepared_claim_without_relaunch(self):
        evidence = self.create_sealed_prepared_attempt(self.store, 2)
        self.analyzer("raise SystemExit('must not run')")

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(
            self.store.effect("run-001", "retrospective-analysis-2")["status"],
            "confirmed",
        )
        self.assertTrue(self.store.verify_evidence("run-001", evidence))

    def test_sealed_bundle_rejects_missing_forged_and_unexpected_payloads(self):
        def write_json(path, value):
            path.write_text(
                json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

        finding_analysis = self.empty_analysis(summary="A finding was forged.")
        finding_analysis["process_findings"] = [
            {
                "id": "finding-forged",
                "category": "orchestration",
                "title": "Forged finding",
                "evidence": [
                    {"artifact": "events.jsonl", "event_sequence": 2},
                ],
                "impact": "The counts no longer match.",
                "confidence": "high",
            }
        ]
        forged_outcome = {
            "schema_version": 1,
            "run_id": "run-001",
            "episode_sequence": 2,
            "status": "interrupted",
            "warning": True,
            "process_findings_count": 0,
            "improvement_proposals_count": 0,
            "warning_summary": "forged",
        }
        cases = (
            ("missing stderr", lambda path: (path / "stderr.log").unlink()),
            ("missing input", lambda path: (path / "input.json").unlink()),
            ("missing command", lambda path: (path / "command.json").unlink()),
            ("missing outcome", lambda path: (path / "outcome.json").unlink()),
            ("missing result", lambda path: (path / "result.json").unlink()),
            ("missing analysis", lambda path: (path / "analysis.json").unlink()),
            (
                "unexpected file",
                lambda path: write_json(path / "unexpected.json", {"forged": True}),
            ),
            (
                "forged input",
                lambda path: write_json(path / "input.json", {"forged": True}),
            ),
            (
                "forged command",
                lambda path: write_json(path / "command.json", {"forged": True}),
            ),
            (
                "mismatched outcome",
                lambda path: write_json(path / "outcome.json", forged_outcome),
            ),
            (
                "analysis count mismatch",
                lambda path: write_json(path / "analysis.json", finding_analysis),
            ),
        )
        for index, (label, mutate) in enumerate(cases, start=1):
            with self.subTest(label=label):
                store, sequence = self.distinct_episode(index + 80)
                evidence = self.create_sealed_prepared_attempt(store, sequence)
                self.reseal_after(store, evidence, mutate)

                with self.assertRaises(RunStoreError):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

    def test_sealed_warning_bundle_rejects_analysis_payload(self):
        self.analyzer("print('not-json')")
        with (
            patch.object(
                self.store,
                "confirm_effect",
                side_effect=RuntimeError("crash after seal"),
            ),
            self.assertRaisesRegex(RuntimeError, "crash after seal"),
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )
        evidence = retrospective_evidence_identity(
            self.store,
            "run-001",
            episode_sequence=2,
        )
        analysis = self.empty_analysis(summary="Forged warning analysis.")
        self.reseal_after(
            self.store,
            evidence,
            lambda directory: (directory / "analysis.json").write_text(
                json.dumps(analysis, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            ),
        )

        with self.assertRaisesRegex(
            RunStoreError,
            "sealed retrospective evidence files are invalid",
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )

    def test_sealed_bundle_rejects_forged_claim_command_and_observation(self):
        cases = ("command", "timeout", "observation")
        for index, field in enumerate(cases, start=1):
            with self.subTest(field=field):
                store, sequence = self.distinct_episode(index + 100)
                evidence = self.create_sealed_prepared_attempt(store, sequence)
                effect_path = (
                    store.root
                    / "runs"
                    / "run-001"
                    / "effects"
                    / f"retrospective-analysis-{sequence}.json"
                )
                effect = json.loads(effect_path.read_text(encoding="utf-8"))
                if field in {"command", "timeout"}:
                    if field == "command":
                        effect["intended"]["command"]["argv"] = [
                            "codex",
                            "exec",
                            "--evil",
                        ]
                    else:
                        effect["intended"]["command"]["timeout_seconds"] = (
                            RETROSPECTIVE_TIMEOUT_SECONDS - 1
                        )
                    self.reseal_after(
                        store,
                        evidence,
                        lambda directory: (directory / "command.json").write_text(
                            json.dumps(
                                effect["intended"]["command"],
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n",
                            encoding="utf-8",
                        ),
                    )
                else:
                    effect["status"] = "confirmed"
                    effect["observed"] = {
                        "evidence": evidence,
                        "status": "passed",
                    }
                effect_path.write_text(
                    json.dumps(effect, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                expected = (
                    "retrospective analysis command is invalid"
                    if field in {"command", "timeout"}
                    else "sealed retrospective evidence claim is invalid"
                )
                with self.assertRaisesRegex(RunStoreError, expected):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

    def test_malformed_sealed_outcomes_are_rejected(self):
        def base():
            return {
                "schema_version": 1,
                "run_id": "run-001",
                "episode_sequence": 2,
                "status": "empty",
                "warning": False,
                "process_findings_count": 0,
                "improvement_proposals_count": 0,
            }

        cases = []
        for label, mutate in (
            ("boolean schema", lambda value: value.update(schema_version=True)),
            (
                "boolean count",
                lambda value: value.update(process_findings_count=False),
            ),
            ("non-string status", lambda value: value.update(status=[])),
            ("missing field", lambda value: value.pop("warning")),
            ("extra field", lambda value: value.update(extra="unexpected")),
            ("success warning", lambda value: value.update(warning=True)),
            (
                "empty with findings",
                lambda value: value.update(process_findings_count=1),
            ),
            ("passed without findings", lambda value: value.update(status="passed")),
            (
                "warning without summary",
                lambda value: value.update(status="invalid", warning=True),
            ),
        ):
            value = base()
            mutate(value)
            cases.append((label, value))

        for index, (label, value) in enumerate(cases, start=1):
            with self.subTest(label=label):
                store, sequence = self.distinct_episode(index)
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                store.write_evidence_value(
                    "run-001",
                    f"{evidence}/result.json",
                    value,
                )
                store.seal_evidence("run-001", evidence)

                with self.assertRaisesRegex(
                    RunStoreError,
                    "sealed retrospective outcome is invalid",
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

    def test_malformed_partial_outcomes_are_rejected_without_relaunch(self):
        cases = (
            {
                "schema_version": 1,
                "run_id": "run-001",
                "episode_sequence": 2,
                "status": "interrupted",
                "warning": True,
                "process_findings_count": 1,
                "improvement_proposals_count": 0,
                "warning_summary": "contradictory count",
            },
            {
                "schema_version": 1,
                "run_id": "run-001",
                "episode_sequence": 2,
                "status": "unavailable",
                "warning": True,
                "process_findings_count": 0,
                "improvement_proposals_count": 0,
                "warning_summary": "x" * 1025,
            },
        )
        for index, malformed in enumerate(cases, start=1):
            with self.subTest(index=index):
                store, sequence = self.distinct_episode(index + 20)
                analysis = self.empty_analysis(summary="No actionable findings.")
                self.analyzer(f"print({json.dumps(json.dumps(analysis))})")
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                original_seal = store.seal_evidence

                def crash_before_attempt_seal(run_id, relative_directory):
                    if relative_directory == evidence:
                        raise RuntimeError("crash after durable result")
                    return original_seal(run_id, relative_directory)

                with (
                    patch.object(
                        store,
                        "seal_evidence",
                        side_effect=crash_before_attempt_seal,
                    ),
                    self.assertRaisesRegex(RuntimeError, "crash after durable result"),
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )
                result_path = store.root / "runs" / "run-001" / evidence / "result.json"
                result_path.write_text(
                    json.dumps(malformed, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    RunStoreError,
                    "sealed retrospective outcome is invalid",
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

    def test_nonempty_valid_analysis_is_passed(self):
        analysis = self.empty_analysis(summary="One process issue was found.")
        analysis["process_findings"] = [
            {
                "id": "finding-1",
                "category": "orchestration",
                "title": "Attention interrupted the run",
                "evidence": [
                    {"artifact": "events.jsonl", "event_sequence": 2},
                ],
                "impact": "An operator had to resume it.",
                "confidence": "high",
            }
        ]
        self.analyzer(f"print({json.dumps(json.dumps(analysis))})")

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(outcome["process_findings_count"], 1)
        self.assertEqual(outcome["improvement_proposals_count"], 0)

    def test_prompt_advertises_every_validator_vocabulary_value(self):
        summary = build_run_summary(self.store, "run-001", episode_sequence=2)
        analysis = self.empty_analysis(summary="One process issue was found.")
        analysis["process_findings"] = [
            {
                "id": "finding-1",
                "category": CATEGORIES[0],
                "title": "Attention interrupted the run",
                "evidence": [
                    {"artifact": "events.jsonl", "event_sequence": 2},
                ],
                "impact": "An operator had to resume it.",
                "confidence": CONFIDENCE[0],
            }
        ]
        analysis["improvement_proposals"] = [
            {
                "id": "proposal-1",
                "addresses": ["finding-1"],
                "scope": SCOPES[0],
                "priority": PRIORITIES[0],
                "title": "Reduce interruptions",
                "rationale": "The Run needed operator attention.",
                "suggested_change": "Improve interruption recovery.",
                "requires_human_decision": True,
            }
        ]
        cases = (
            ("category", CATEGORIES),
            ("confidence", CONFIDENCE),
            ("scope", SCOPES),
            ("priority", PRIORITIES),
        )

        for field, values in cases:
            target = (
                analysis["process_findings"][0]
                if field in {"category", "confidence"}
                else analysis["improvement_proposals"][0]
            )
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertIn(value, RETROSPECTIVE_PROMPT)
                    target[field] = value
                    self.assertEqual(
                        normalize_retrospective_result(summary, analysis),
                        analysis,
                    )

    def test_completed_episode_gets_its_own_stable_identity(self):
        store = RunStore(self.root / "completed" / "afk")
        store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={},
            recorded_at="2026-07-28T10:01:00Z",
        )
        analysis = self.empty_analysis(summary="No actionable findings.")
        analysis["terminal_outcome"] = "completed"
        self.analyzer(f"print({json.dumps(json.dumps(analysis))})")

        outcome = run_retrospective_attempt(
            store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(
            retrospective_evidence_identity(
                store,
                "run-001",
                episode_sequence=2,
            ),
            "retrospective/completed-2",
        )

    def test_invalid_and_unavailable_results_are_sealed_warnings(self):
        cases = (
            ("invalid", "print('not-json')"),
            ("unavailable", None),
            ("unavailable", "raise SystemExit(7)"),
        )
        for index, (expected, program) in enumerate(cases, start=1):
            with self.subTest(expected=expected, index=index):
                if program is None:
                    (self.bin / "codex").unlink(missing_ok=True)
                else:
                    self.analyzer(program)
                store, sequence = self.distinct_episode(index)
                outcome = run_retrospective_attempt(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                )
                self.assertEqual(outcome["status"], expected)
                self.assertTrue(outcome["warning"])
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                self.assertTrue(store.verify_evidence("run-001", evidence))
                self.assertNotIn(
                    "analysis.json",
                    self.evidence_files(evidence, store=store),
                )

    def test_timeout_is_interrupted_and_never_retried(self):
        observation = self.root / "timeout-observation.json"
        self.analyzer(
            f"""
            import json, os, pathlib, subprocess, time
            descendant = subprocess.Popen(["sleep", "60"])
            pathlib.Path({str(observation)!r}).write_text(
                json.dumps({{
                    "analyzer_pid": os.getpid(),
                    "descendant_pid": descendant.pid,
                }}),
                encoding="utf-8",
            )
            time.sleep(60)
            """
        )
        with patch("afk.retrospective_attempt.RETROSPECTIVE_TIMEOUT_SECONDS", 0.05):
            first = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )
            self.analyzer("raise SystemExit('must not run')")
            second = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "interrupted")
        self.assertTrue(first["warning"])
        observed = json.loads(observation.read_text(encoding="utf-8"))
        process_ids = (
            observed["analyzer_pid"],
            observed["descendant_pid"],
        )
        self.assertFalse(any(self.process_is_live(pid) for pid in process_ids))

    def test_combined_output_limit_is_sealed_as_invalid(self):
        self.analyzer(
            """
            import os
            os.write(1, b"x" * 9)
            os.write(2, b"y" * 9)
            """
        )
        with patch(
            "afk.retrospective_attempt.RETROSPECTIVE_OUTPUT_BYTE_LIMIT",
            16,
        ):
            outcome = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
            )

        self.assertEqual(outcome["status"], "invalid")
        self.assertIn("size limit", outcome["warning_summary"])

    def test_ignored_sigchld_preserves_real_analysis_status(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        cases = (
            ("nonzero", "raise SystemExit(7)", "unavailable"),
            (
                "signalled",
                "os.kill(os.getpid(), signal.SIGKILL)",
                "interrupted",
            ),
            ("success", "pass", "empty"),
        )
        for index, (label, termination, expected) in enumerate(cases, start=1):
            with self.subTest(label=label):
                store, sequence = self.distinct_episode(index + 140)
                self.analyzer(
                    f"""
                    import json, os, signal
                    print(json.dumps({analysis!r}), flush=True)
                    {termination}
                    """
                )
                previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
                try:
                    outcome = run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )
                finally:
                    signal.signal(signal.SIGCHLD, previous)

                self.assertEqual(outcome["status"], expected)
                self.assertEqual(
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    ),
                    outcome,
                )

    def test_missing_and_malformed_analysis_status_fail_closed(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        cases = (
            (
                "missing",
                f"print({json.dumps(json.dumps(analysis))})",
                "interrupted",
            ),
            (
                "malformed",
                (
                    "import os,sys;"
                    "os.write(int(sys.argv[3]),b'not-a-status\\n');"
                    f"print({json.dumps(json.dumps(analysis))})"
                ),
                "invalid",
            ),
        )
        for index, (label, guard, expected) in enumerate(cases, start=1):
            with self.subTest(label=label):
                store, sequence = self.distinct_episode(index + 160)
                with patch("afk.retrospective_attempt._EXEC_GUARD", guard):
                    outcome = run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                    )

                self.assertEqual(outcome["status"], expected)
                self.assertTrue(outcome["warning"])
                evidence = retrospective_evidence_identity(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                )
                self.assertTrue(store.verify_evidence("run-001", evidence))

    def test_signal_exit_is_sealed_as_interrupted(self):
        self.analyzer(
            """
            import os, signal
            os.kill(os.getpid(), signal.SIGKILL)
            """
        )

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(outcome["status"], "interrupted")
        self.assertTrue(outcome["warning"])

    def empty_analysis(self, *, summary):
        return {
            "schema_version": 1,
            "run_id": "run-001",
            "terminal_outcome": "attention_required",
            "summary": summary,
            "process_findings": [],
            "improvement_proposals": [],
        }

    def analyzer(self, program):
        path = self.bin / "codex"
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(program).strip() + "\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def process_is_live(self, process_id):
        try:
            state = (
                Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8").split()[2]
            )
        except FileNotFoundError:
            return False
        return state != "Z"

    def reseal_after(self, store, evidence, mutate):
        directory = store.root / "runs" / "run-001" / evidence
        directory.chmod(0o700)
        manifest = directory / "manifest.json"
        manifest.chmod(0o600)
        manifest.unlink()
        receipts = store.root / "runs" / "run-001" / ".evidence-receipts"
        for receipt in receipts.iterdir():
            if json.loads(receipt.read_text(encoding="utf-8"))["evidence"] == evidence:
                receipt.chmod(0o600)
                receipt.unlink()
        for path in directory.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
            elif path.is_dir():
                path.chmod(0o700)
        mutate(directory)
        store.seal_evidence("run-001", evidence)

    def create_sealed_prepared_attempt(self, store, sequence):
        analysis = self.empty_analysis(summary="No actionable findings.")
        self.analyzer(f"print({json.dumps(json.dumps(analysis))})")
        with (
            patch.object(
                store,
                "confirm_effect",
                side_effect=RuntimeError("crash after seal"),
            ),
            self.assertRaisesRegex(RuntimeError, "crash after seal"),
        ):
            run_retrospective_attempt(
                store,
                "run-001",
                episode_sequence=sequence,
            )
        evidence = retrospective_evidence_identity(
            store,
            "run-001",
            episode_sequence=sequence,
        )
        self.assertEqual(
            store.effect("run-001", f"retrospective-analysis-{sequence}")["status"],
            "prepared",
        )
        return evidence

    def distinct_episode(self, index):
        root = self.root / f"case-{index}"
        store = RunStore(root / "afk")
        store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": f"case-{index}"},
            recorded_at="2026-07-28T10:01:00Z",
        )
        return store, 2

    def evidence_files(self, evidence, *, store=None):
        selected = store or self.store
        directory = selected.root / "runs" / "run-001" / evidence
        return {path.name for path in directory.iterdir()}

    def evidence_json(self, evidence, name):
        path = self.store.root / "runs" / "run-001" / evidence / name
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
