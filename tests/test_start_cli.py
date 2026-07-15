import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import afk.run_store as run_store_module  # noqa: E402
from afk.run_store import RunStore, RunStoreBusy  # noqa: E402
from afk.start import (  # noqa: E402
    StartError,
    _beads_password,
    run_worker_unit,
    start_run,
)


BASE_SHA = "a" * 40


class StartCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary_directory.name)
        self.project = self.temp / "beads-webui"
        self.project.mkdir()
        (self.project / "afk.toml").write_text(
            textwrap.dedent(
                """
                schema_version = 1

                [validation]
                command = ["./scripts/validation-worker.sh", "run"]
                timeout_seconds = 2700
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.state_home = self.temp / "state"
        self.secret_value = "dogfood-password-value"
        self.secret_path = self.temp / "secrets" / "beads-password.txt"
        self.secret_path.parent.mkdir(mode=0o700)
        self.secret_path.write_text(self.secret_value + "\n", encoding="utf-8")
        self.secret_path.chmod(0o600)
        self.config_home = self.temp / "config"
        config_dir = self.config_home / "afk"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.toml"
        config_path.write_text(
            "schema_version = 1\n"
            "[beads]\n"
            f'password_file = "{self.secret_path}"\n',
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        (self.temp / "beads").mkdir()
        self.fake_bin = self.temp / "bin"
        self.fake_bin.mkdir()
        self.command_log = self.temp / "commands.jsonl"
        self._write_fake_commands()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_afk(self, *args, **overrides):
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{self.fake_bin}:{env['PATH']}",
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "AFK_BEADS_WORKSPACE": str(self.temp / "beads"),
                "AFK_FAKE_LOG": str(self.command_log),
                "AFK_FAKE_PROJECT": str(self.project),
                "AFK_FAKE_SHA": BASE_SHA,
                "AFK_FAKE_BEAD": "central-bnkl.1.1",
                "AFK_FAKE_BEAD_STATUS": "open",
                "AFK_FAKE_ASSIGNEE": "",
                "AFK_FAKE_PINNED_CONTRACT": "present",
                "AFK_FAKE_EXPECTED_PASSWORD": self.secret_value,
                "USER": "bump",
            }
        )
        env.update(overrides)
        return subprocess.run(
            [sys.executable, "-m", "afk", *args],
            cwd=self.project,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_start_launches_numbered_transient_worker_and_reports_checkpoint(self):
        completed = self.run_afk("start", "central-bnkl.1.1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        run_id = completed.stdout.strip()
        status = self.run_afk("status", run_id, "--json")
        projection = json.loads(status.stdout)
        self.assertEqual(projection["state"], "created")
        self.assertEqual(projection["checkpoint"], "created")
        self.assertEqual(projection["unit"], f"afk-{run_id}-worker-1")
        self.assertEqual(projection["lingering"], "enabled")
        readable = self.run_afk("status", run_id)
        self.assertEqual(
            readable.stdout,
            f"{run_id} created bead=central-bnkl.1.1 sequence=2 "
            f"checkpoint=created unit=afk-{run_id}-worker-1\n",
        )
        effect = json.loads(
            (
                self.state_home
                / "afk"
                / "runs"
                / run_id
                / "effects"
                / "worker-launch-1.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(effect["status"], "prepared")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn('"command":"systemd-run"', commands)
        self.assertIn('"--property=Restart=no"', commands)
        self.assertIn('"--property=UMask=0077"', commands)

    def test_start_holds_global_lock_through_systemd_handoff(self):
        completed = self.run_afk(
            "start", "central-bnkl.1.1", AFK_FAKE_RESUME_DURING_LAUNCH="1"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        run_id = completed.stdout.strip()
        effect = RunStore(self.state_home / "afk").effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "prepared")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn('"command":"resume-probe","returncode":2', commands)

    def test_new_worker_retries_until_launcher_releases_global_lock(self):
        completed = self.run_afk(
            "start", "central-bnkl.1.1", AFK_FAKE_LAUNCH_WORKER="1"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        run_id = completed.stdout.strip()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = self.run_afk("status", run_id, "--json")
            projection = json.loads(status.stdout)
            if projection["state"] == "attention_required":
                break
            time.sleep(0.05)
        self.assertEqual(projection["checkpoint"], "candidate_ready")
        effect = RunStore(self.state_home / "afk").effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "confirmed")

    def test_worker_claims_exact_bead_and_publishes_candidate_before_stopping(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()

        completed = self.run_afk("_worker", run_id)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        projection = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(projection["state"], "attention_required")
        self.assertEqual(projection["checkpoint"], "candidate_ready")
        self.assertEqual(projection["attention"]["kind"], "unavailable")
        self.assertEqual(projection["attention"]["scope"], "validation")
        self.assertEqual(projection["candidate_sha"], "d" * 40)
        self.assertEqual(projection["pr_number"], 17)
        self.assertTrue(Path(projection["worktree_path"]).is_dir())
        effect = RunStore(self.state_home / "afk").effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "confirmed")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn(
            '"command":"bd","args":["update","central-bnkl.1.1","--claim"', commands
        )
        self.assertIn(BASE_SHA, commands)

    def test_worker_requires_attention_when_target_branch_drifts_before_candidate_ready(
        self,
    ):
        home = str(self.temp)
        started = self.run_afk("start", "central-bnkl.1.1", HOME=home)
        run_id = started.stdout.strip()

        completed = self.run_afk(
            "_worker_unit", run_id, HOME=home, AFK_FAKE_TARGET_DRIFT_ON_PR="1"
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        projection = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(projection["checkpoint"], "change_committed")
        self.assertEqual(projection["attention"]["scope"], "candidate")
        self.assertIn("target branch", projection["attention"]["summary"])

    def test_beads_password_is_scoped_to_bd_children_and_never_persisted(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()

        completed = self.run_afk("_worker", run_id)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        records = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        bd_records = [record for record in records if record["command"] == "bd"]
        self.assertTrue(bd_records)
        self.assertTrue(all(record["credential_present"] for record in bd_records))
        systemd = next(
            record for record in records if record["command"] == "systemd-run"
        )
        self.assertNotIn(self.secret_value, json.dumps(systemd))
        run_dir = self.state_home / "afk" / "runs" / run_id
        self.assertFalse(
            any(
                self.secret_value.encode() in path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            )
        )

    def test_missing_worker_password_file_enters_attention_without_secret(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        self.secret_path.unlink()

        completed = self.run_afk("_worker", run_id)

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertIn("credential", status["attention"]["summary"].lower())
        self.assertNotIn(self.secret_value, json.dumps(status))

    def test_initial_missing_credential_config_creates_attention_without_launch(self):
        (self.config_home / "afk" / "config.toml").unlink()

        completed = self.run_afk("start", "central-bnkl.1.1")

        self.assertEqual(completed.returncode, 2)
        run_id = completed.stdout.strip()
        self.assertTrue(run_id)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "bead_preflight")
        self.assertEqual(status["attention"]["kind"], "unavailable")
        self.assertIn("credential", status["attention"]["summary"].lower())
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"command":"systemd-run"', commands)
        run_dir = self.state_home / "afk" / "runs" / run_id
        self.assertFalse(
            any(
                self.secret_value.encode() in path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            )
        )

    def test_initial_missing_password_file_creates_attention_without_launch(self):
        self.secret_path.unlink()

        completed = self.run_afk("start", "central-bnkl.1.1")

        self.assertEqual(completed.returncode, 2)
        run_id = completed.stdout.strip()
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "bead_preflight")
        self.assertEqual(status["attention"]["kind"], "unavailable")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"command":"systemd-run"', commands)

    def test_initial_rejected_password_creates_attention_without_leak_or_launch(self):
        rejected_password = "initial-rejected-password"
        self.secret_path.write_text(rejected_password + "\n", encoding="utf-8")

        completed = self.run_afk(
            "start", "central-bnkl.1.1", AFK_FAKE_REJECT_CREDENTIAL="1"
        )

        self.assertEqual(completed.returncode, 2)
        run_id = completed.stdout.strip()
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "bead_preflight")
        self.assertEqual(status["attention"]["kind"], "unavailable")
        self.assertEqual(status["attention"]["classification"], "authentication_denied")
        self.assertEqual(status["attention"]["summary"], "Beads authentication failed")
        self.assertNotIn(rejected_password, json.dumps(status))
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn(rejected_password, commands)
        self.assertNotIn('"command":"systemd-run"', commands)
        run_dir = self.state_home / "afk" / "runs" / run_id
        self.assertFalse(
            any(
                rejected_password.encode() in path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            )
        )
        artifacts = b"\n".join(
            path.read_bytes() for path in run_dir.rglob("*") if path.is_file()
        )
        self.assertNotIn(b"dogfood.internal:3306", artifacts)
        self.assertNotIn(b"database_user", artifacts)

    def test_credential_reads_use_the_same_inodes_they_validate(self):
        config_path = self.config_home / "afk" / "config.toml"
        replacement_config = self.temp / "replacement-config.toml"
        replacement_secret = self.temp / "replacement-secret.txt"
        replacement_secret.write_text("replacement-password\n", encoding="utf-8")
        replacement_secret.chmod(0o600)
        replacement_config.write_text(
            "schema_version = 1\n"
            "[beads]\n"
            f'password_file = "{replacement_secret}"\n',
            encoding="utf-8",
        )
        replacement_config.chmod(0o600)
        real_stat = Path.stat

        config_stats = 0

        def replace_config_after_validation(path, *args, **kwargs):
            nonlocal config_stats
            result = real_stat(path, *args, **kwargs)
            if path == config_path:
                config_stats += 1
                if config_stats == 3:
                    path.unlink()
                    path.symlink_to(replacement_config)
            return result

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_home)}):
            with patch.object(Path, "stat", replace_config_after_validation):
                self.assertEqual(_beads_password(), self.secret_value)

        config_path.unlink()
        config_path.write_text(
            "schema_version = 1\n"
            "[beads]\n"
            f'password_file = "{self.secret_path}"\n',
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        password_stats = 0

        def replace_password_after_validation(path, *args, **kwargs):
            nonlocal password_stats
            result = real_stat(path, *args, **kwargs)
            if path == self.secret_path:
                password_stats += 1
                if password_stats == 4:
                    path.unlink()
                    path.symlink_to(replacement_secret)
            return result

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_home)}):
            with patch.object(Path, "stat", replace_password_after_validation):
                self.assertEqual(_beads_password(), self.secret_value)

    def test_credential_reader_rejects_symlinked_files(self):
        config_path = self.config_home / "afk" / "config.toml"
        real_config = self.temp / "real-config.toml"
        config_path.rename(real_config)
        config_path.symlink_to(real_config)

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_home)}):
            with self.assertRaisesRegex(StartError, "missing or invalid"):
                _beads_password()

        config_path.unlink()
        real_config.rename(config_path)
        real_secret = self.temp / "real-secret.txt"
        self.secret_path.rename(real_secret)
        self.secret_path.symlink_to(real_secret)

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_home)}):
            with self.assertRaisesRegex(StartError, "missing or invalid"):
                _beads_password()

    def test_rejected_worker_password_enters_attention_without_secret(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        rejected_password = "rejected-password-value"
        self.secret_path.write_text(rejected_password + "\n", encoding="utf-8")

        completed = self.run_afk("_worker", run_id, AFK_FAKE_REJECT_CREDENTIAL="1")

        self.assertEqual(completed.returncode, 2)
        self.assertNotIn(rejected_password, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["summary"], "Beads authentication failed")
        self.assertEqual(status["attention"]["classification"], "authentication_denied")
        self.assertNotIn(rejected_password, json.dumps(status))
        run_dir = self.state_home / "afk" / "runs" / run_id
        self.assertFalse(
            any(
                rejected_password.encode() in path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            )
        )

    def test_resume_confirms_launch_that_succeeded_before_effect_confirmation(self):
        store = RunStore(self.state_home / "afk")
        projection = store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="crashed-run",
        )
        self.assertEqual(projection["state"], "created")
        store.prepare_effect(
            "crashed-run",
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": "afk-crashed-run-worker-1"},
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        effect = store.effect("crashed-run", "worker-launch-1")
        self.assertEqual(effect["status"], "confirmed")
        status = json.loads(self.run_afk("status", "--json").stdout)
        self.assertEqual(status["unit"], "afk-crashed-run-worker-1")

    def test_worker_unit_persists_normalized_terminal_results(self):
        for exit_code, result in (
            (0, "completed"),
            (2, "attention_required"),
            (1, "failed"),
        ):
            with self.subTest(exit_code=exit_code):
                state_home = self.temp / f"terminal-{exit_code}"
                store = RunStore(state_home / "afk")
                store.create_run(
                    bead_id="central-bnkl.1.1",
                    repository="thunderbump/beads-webui",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    start_request={"repository_root": str(self.project)},
                    run_id="terminal-run",
                )
                with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                    with patch("afk.start.run_worker", return_value=exit_code):
                        self.assertEqual(run_worker_unit("terminal-run"), exit_code)
                status = store.status("terminal-run")
                self.assertEqual(status["worker_exit_code"], exit_code)
                self.assertEqual(status["worker_result"], result)

    def test_readable_status_reports_terminal_worker_observation(self):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            run_id="terminal-run",
        )
        store.append_event(
            "terminal-run",
            "worker.terminal",
            data={
                "checkpoint": "created",
                "unit": "afk-terminal-run-worker-1",
                "worker_exit_code": 2,
                "worker_result": "attention_required",
            },
        )

        completed = self.run_afk("status", "terminal-run")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout,
            "terminal-run created bead=central-bnkl.1.1 sequence=2 "
            "checkpoint=created unit=afk-terminal-run-worker-1 "
            "worker_exit_code=2 worker_result=attention_required\n",
        )

    def test_worker_unit_retries_transient_terminal_persistence_failure(self):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="terminal-run",
        )
        original_append_event = RunStore.append_event
        failures = iter(
            (
                RunStoreBusy("temporary lock contention"),
                OSError("temporary storage failure"),
            )
        )
        terminal_attempts = 0

        def flaky_append_event(store, run_id, event, **kwargs):
            nonlocal terminal_attempts
            if event == "worker.terminal" and terminal_attempts < 2:
                failure = next(failures)
                terminal_attempts += 1
                raise failure
            return original_append_event(store, run_id, event, **kwargs)

        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_home)}):
            with patch("afk.start.run_worker", return_value=2):
                with patch.object(RunStore, "append_event", new=flaky_append_event):
                    with patch("afk.start.time.sleep"):
                        with patch(
                            "afk.start.sys.stderr", new_callable=StringIO
                        ) as stderr:
                            self.assertEqual(run_worker_unit("terminal-run"), 2)

        self.assertEqual(terminal_attempts, 2)
        self.assertIn("temporary lock contention", stderr.getvalue())
        self.assertIn("temporary storage failure", stderr.getvalue())
        status = store.status("terminal-run")
        self.assertEqual(status["worker_exit_code"], 2)
        self.assertEqual(status["worker_result"], "attention_required")

    def test_worker_unit_deduplicates_terminal_after_projection_write_failure(self):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="terminal-run",
        )
        original_atomic_json = run_store_module._atomic_json
        projection_failed = False

        def fail_terminal_projection_once(path, value):
            nonlocal projection_failed
            if value.get("last_event") == "worker.terminal" and not projection_failed:
                projection_failed = True
                raise OSError("projection write failed after event fsync")
            return original_atomic_json(path, value)

        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state_home)}):
            with patch("afk.start.run_worker", return_value=2):
                with patch(
                    "afk.run_store._atomic_json", new=fail_terminal_projection_once
                ):
                    with patch("afk.start.time.sleep"):
                        with patch("afk.start.sys.stderr", new_callable=StringIO):
                            self.assertEqual(run_worker_unit("terminal-run"), 2)

        events_path = self.state_home / "afk" / "runs" / "terminal-run" / "events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        self.assertEqual(
            [event["event"] for event in events].count("worker.terminal"), 1
        )
        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        self.assertEqual(resumed.returncode, 2)
        self.assertFalse(self.command_log.exists())

    def test_worker_unit_rejects_partial_or_conflicting_terminal_observation(self):
        for name, terminal_data in (
            ("partial", {"worker_exit_code": 2}),
            ("conflict", {"worker_exit_code": 1, "worker_result": "failed"}),
        ):
            with self.subTest(name=name):
                state_home = self.temp / name
                store = RunStore(state_home / "afk")
                store.create_run(
                    bead_id="central-bnkl.1.1",
                    repository="thunderbump/beads-webui",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    run_id="terminal-run",
                )
                store.append_event(
                    "terminal-run",
                    "worker.terminal",
                    data={"checkpoint": "created", **terminal_data},
                )

                with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                    with patch("afk.start.run_worker", return_value=2):
                        with self.assertRaisesRegex(StartError, "terminal observation"):
                            run_worker_unit("terminal-run")

                events_path = (
                    state_home / "afk" / "runs" / "terminal-run" / "events.jsonl"
                )
                events = [
                    json.loads(line) for line in events_path.read_text().splitlines()
                ]
                self.assertEqual(
                    [event["event"] for event in events].count("worker.terminal"),
                    1,
                )

    def test_resume_uses_terminal_observation_after_unit_collection(self):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="collected-run",
        )
        store.append_event(
            "collected-run",
            "worker.terminal",
            data={
                "checkpoint": "created",
                "unit": "afk-collected-run-worker-1",
                "worker_exit_code": 2,
                "worker_result": "attention_required",
            },
        )

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 2)
        self.assertEqual(resumed.stdout.strip(), "collected-run")
        self.assertFalse(self.command_log.exists())

    def test_resume_advances_the_prior_implementation_unavailable_checkpoint(self):
        store = RunStore(self.state_home / "afk")
        run_id = "prior-slice-run"
        branch = "afk/central-bnkl-1-1-prior-slice-run"
        worktree = self.state_home / "afk" / "worktrees" / run_id
        worktree.mkdir(parents=True)
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={
                "repository_root": str(self.project),
                "beads_workspace": str(self.temp / "beads"),
                "claimant": "bump",
            },
            run_id=run_id,
        )
        store.append_event(
            run_id,
            "worktree.ready",
            state="worktree_ready",
            data={
                "checkpoint": "worktree_ready",
                "worktree_path": str(worktree),
                "branch": branch,
            },
        )
        store.append_event(
            run_id,
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "worktree_ready",
                "attention": {
                    "scope": "implementation",
                    "kind": "unavailable",
                    "summary": "implementation is not available in this AFK slice",
                },
            },
        )
        store.append_event(
            run_id,
            "worker.terminal",
            data={
                "checkpoint": "worktree_ready",
                "unit": f"afk-{run_id}-worker-1",
                "worker_exit_code": 2,
                "worker_result": "attention_required",
            },
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        projection = store.status(run_id)
        self.assertEqual(projection["checkpoint"], "candidate_ready")
        self.assertEqual(projection["attention"]["scope"], "validation")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"command":"systemctl"', commands)

    def test_resume_advances_legacy_collected_worker_without_terminal_observation(self):
        store = RunStore(self.state_home / "afk")
        run_id = "legacy-prior-slice-run"
        branch = "afk/central-bnkl-1-1-legacy-prior-slice-run"
        worktree = self.state_home / "afk" / "worktrees" / run_id
        worktree.mkdir(parents=True)
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={
                "repository_root": str(self.project),
                "beads_workspace": str(self.temp / "beads"),
                "claimant": "bump",
            },
            run_id=run_id,
        )
        unit = f"afk-{run_id}-worker-1"
        store.prepare_effect(
            run_id,
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": unit},
        )
        store.confirm_effect(
            run_id,
            "worker-launch-1",
            observed={"unit": unit},
        )
        store.append_event(
            run_id,
            "worktree.ready",
            state="worktree_ready",
            data={
                "checkpoint": "worktree_ready",
                "worktree_path": str(worktree),
                "branch": branch,
            },
        )
        store.append_event(
            run_id,
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "worktree_ready",
                "attention": {
                    "scope": "implementation",
                    "kind": "unavailable",
                    "summary": "implementation is not available in this AFK slice",
                },
            },
        )

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        projection = store.status(run_id)
        self.assertEqual(projection["checkpoint"], "candidate_ready")
        self.assertEqual(projection["attention"]["scope"], "validation")

    def test_resume_reconciles_a_candidate_push_completed_before_confirmation(self):
        home = str(self.temp)
        started = self.run_afk("start", "central-bnkl.1.1", HOME=home)
        run_id = started.stdout.strip()

        interrupted = self.run_afk(
            "_worker_unit", run_id, HOME=home, AFK_FAKE_PUSH_INTERRUPTED="1"
        )

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "change_committed")
        self.assertEqual(before["attention"]["scope"], "candidate")
        push_effect = RunStore(self.state_home / "afk").effect(
            run_id, f'branch-push-{"d" * 40}'
        )
        self.assertEqual(push_effect["status"], "prepared")

        resumed = self.run_afk("resume", HOME=home)

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "candidate_ready")
        self.assertEqual(after["attention"]["scope"], "validation")
        push_effect = RunStore(self.state_home / "afk").effect(
            run_id, f'branch-push-{"d" * 40}'
        )
        self.assertEqual(push_effect["status"], "confirmed")

    def test_resume_reconciles_a_candidate_pr_completed_before_confirmation(self):
        home = str(self.temp)
        started = self.run_afk("start", "central-bnkl.1.1", HOME=home)
        run_id = started.stdout.strip()

        interrupted = self.run_afk(
            "_worker_unit", run_id, HOME=home, AFK_FAKE_PR_INTERRUPTED="1"
        )

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "change_committed")
        self.assertEqual(before["attention"]["scope"], "candidate")
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-create")["status"], "prepared")

        resumed = self.run_afk("resume", HOME=home)

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "candidate_ready")
        self.assertEqual(after["attention"]["scope"], "validation")
        self.assertEqual(store.effect(run_id, "pr-create")["status"], "confirmed")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertEqual(commands.count('"command":"gh","args":["pr","create"'), 1)

        terminal_resume = self.run_afk("resume", HOME=home)

        self.assertEqual(terminal_resume.returncode, 2, terminal_resume.stderr)
        terminal = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(terminal["checkpoint"], "candidate_ready")
        self.assertEqual(terminal["attention"]["scope"], "validation")

    def test_resume_requires_attention_for_confirmed_collected_worker_without_terminal(
        self,
    ):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="missing-terminal-run",
        )
        store.prepare_effect(
            "missing-terminal-run",
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": "afk-missing-terminal-run-worker-1"},
        )
        store.confirm_effect(
            "missing-terminal-run",
            "worker-launch-1",
            observed={"unit": "afk-missing-terminal-run-worker-1"},
        )

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 2)
        status = store.status("missing-terminal-run")
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["attention"]["kind"], "inconclusive")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn('"command":"systemctl"', commands)
        self.assertNotIn('"command":"systemd-run"', commands)

    def test_resume_leaves_confirmed_active_worker_running(self):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="active-worker-run",
        )
        store.prepare_effect(
            "active-worker-run",
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": "afk-active-worker-run-worker-1"},
        )
        store.confirm_effect(
            "active-worker-run",
            "worker-launch-1",
            observed={"unit": "afk-active-worker-run-worker-1"},
        )

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")

        self.assertEqual(resumed.returncode, 0)
        self.assertEqual(store.status("active-worker-run")["state"], "created")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn('"command":"systemctl"', commands)
        self.assertNotIn('"command":"systemd-run"', commands)

    def test_resume_does_not_confirm_an_activating_unit(self):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="crashed-run",
        )
        store.prepare_effect(
            "crashed-run",
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": "afk-crashed-run-worker-1"},
        )

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="activating")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        self.assertEqual(
            store.effect("crashed-run", "worker-launch-1")["status"], "prepared"
        )
        status = json.loads(self.run_afk("status", "--json").stdout)
        self.assertEqual(status["state"], "attention_required")

    def test_resume_retries_a_proven_absent_unit(self):
        store = RunStore(self.state_home / "afk")
        store.create_run(
            bead_id="central-bnkl.1.1",
            repository="thunderbump/beads-webui",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"repository_root": str(self.project)},
            run_id="crashed-run",
        )
        store.prepare_effect(
            "crashed-run",
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": "afk-crashed-run-worker-1"},
        )

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            store.effect("crashed-run", "worker-launch-1")["status"], "prepared"
        )
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn('"command":"systemd-run"', commands)

    def test_resume_requires_attention_for_other_unit_states_and_query_failure(self):
        for unit_state in ("inactive", "ambiguous", "failure"):
            with self.subTest(unit_state=unit_state):
                state_home = self.temp / f"state-{unit_state}"
                store = RunStore(state_home / "afk")
                store.create_run(
                    bead_id="central-bnkl.1.1",
                    repository="thunderbump/beads-webui",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    start_request={"repository_root": str(self.project)},
                    run_id="crashed-run",
                )
                store.prepare_effect(
                    "crashed-run",
                    "worker-launch-1",
                    kind="worker-launch",
                    intended={"unit": "afk-crashed-run-worker-1"},
                )

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE=unit_state,
                )

                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                self.assertEqual(
                    store.effect("crashed-run", "worker-launch-1")["status"],
                    "prepared",
                )
                status = store.status("crashed-run")
                self.assertEqual(status["state"], "attention_required")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"command":"systemd-run"', commands)

    def test_launch_failure_durably_enters_attention(self):
        completed = self.run_afk(
            "start", "central-bnkl.1.1", AFK_FAKE_SYSTEMD_FAILURE="1"
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        status = json.loads(self.run_afk("status", "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "worker_launch")

    def test_claim_failure_stops_at_created_checkpoint(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_CLAIM_FAILURE="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "worker")

    def test_malformed_claim_result_stops_at_created_checkpoint(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_CLAIM_MALFORMED="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["classification"], "malformed_output")
        self.assertEqual(
            status["attention"]["summary"], "Beads returned malformed output"
        )
        run_dir = self.state_home / "afk" / "runs" / run_id
        artifacts = b"\n".join(
            path.read_bytes() for path in run_dir.rglob("*") if path.is_file()
        )
        self.assertNotIn(b"raw-database-user", artifacts)
        self.assertNotIn(b"dogfood.internal:3306", artifacts)
        self.assertNotIn("Traceback", completed.stderr)

    def test_structurally_malformed_claim_result_uses_fixed_attention(self):
        for shape in ("null", "empty", "multi", "non-object"):
            with self.subTest(shape=shape):
                state_home = self.temp / f"claim-{shape}"
                started = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                )
                run_id = started.stdout.strip()

                completed = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_CLAIM_SHAPE=shape,
                )

                self.assertEqual(completed.returncode, 2)
                status = RunStore(state_home / "afk").status(run_id)
                self.assertEqual(
                    status["attention"]["classification"], "malformed_output"
                )
                self.assertEqual(
                    status["attention"]["summary"],
                    "Beads returned malformed output",
                )
                run_dir = state_home / "afk" / "runs" / run_id
                artifacts = b"\n".join(
                    path.read_bytes() for path in run_dir.rglob("*") if path.is_file()
                )
                self.assertNotIn(b"raw-database-user", artifacts)
                self.assertNotIn(b"dogfood.internal:3306", artifacts)

    def test_mismatched_claim_result_stops_at_created_checkpoint(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_CLAIM_MISMATCH="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertIn("central-bnkl.1.1", status["attention"]["summary"])

    def test_worktree_failure_stops_at_claimed_checkpoint(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_WORKTREE_FAILURE="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "claimed")
        self.assertEqual(status["attention"]["scope"], "worker")

    def test_worker_rejects_a_clean_unregistered_preexisting_worktree(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()
        worktree = self.state_home / "afk" / "worktrees" / run_id
        worktree.mkdir(parents=True)

        completed = self.run_afk("_worker", run_id, AFK_FAKE_UNREGISTERED_WORKTREE="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "claimed")
        self.assertIn("registered", status["attention"]["summary"])

    def test_worker_rejects_a_worktree_at_the_wrong_head(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_WRONG_WORKTREE_HEAD="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "claimed")
        self.assertIn("pinned base", status["attention"]["summary"])

    def test_worker_rejects_a_worktree_on_the_wrong_branch(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_WRONG_WORKTREE_BRANCH="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "claimed")
        self.assertIn("intended branch", status["attention"]["summary"])

    def test_preflight_rejects_missing_pinned_contract_without_bootstrap(self):
        completed = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_FAKE_PINNED_CONTRACT="missing",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("afk.toml", completed.stderr)
        self.assertFalse((self.state_home / "afk" / "runs").exists())

    def test_explicit_bootstrap_starts_only_when_pinned_contract_is_missing(self):
        completed = self.run_afk(
            "start",
            "central-bnkl.1.1",
            "--bootstrap-contract",
            AFK_FAKE_PINNED_CONTRACT="missing",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(
            self.run_afk("status", completed.stdout.strip(), "--json").stdout
        )
        self.assertEqual(status["validation_contract"], "bootstrap_required")

        rejected = self.run_afk(
            "start",
            "central-bnkl.1.1",
            "--bootstrap-contract",
            AFK_FAKE_PINNED_CONTRACT="present",
            XDG_STATE_HOME=str(self.temp / "second-state"),
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("already contains afk.toml", rejected.stderr)

    def test_normal_start_uses_pinned_contract_not_local_worktree_content(self):
        (self.project / "afk.toml").write_text("invalid local shim\n", encoding="utf-8")

        completed = self.run_afk("start", "central-bnkl.1.1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(
            self.run_afk("status", completed.stdout.strip(), "--json").stdout
        )
        self.assertEqual(status["validation_contract"], "pinned")

    def test_start_classifies_a_preflight_command_timeout(self):
        expired = subprocess.TimeoutExpired(["git", "rev-parse"], timeout=30)

        with patch("afk.start.subprocess.run", side_effect=expired) as run:
            with self.assertRaisesRegex(StartError, "timed out"):
                start_run("central-bnkl.1.1", cwd=self.project)

        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_preflight_rejects_a_non_object_github_payload(self):
        completed = self.run_afk(
            "start", "central-bnkl.1.1", AFK_FAKE_GH_NON_OBJECT="1"
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("GitHub repository", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_preflight_rejects_an_invalid_bead_labels_shape(self):
        completed = self.run_afk(
            "start", "central-bnkl.1.1", AFK_FAKE_INVALID_LABELS="1"
        )

        self.assertEqual(completed.returncode, 2)
        run_id = completed.stdout.strip()
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "bead_preflight")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("Bead labels", status["attention"]["summary"])
        self.assertNotIn("Traceback", completed.stderr)

    def test_start_records_closed_bead_as_durable_invalid_attention(self):
        completed = self.run_afk(
            "start", "central-bnkl.1.1", AFK_FAKE_BEAD_STATUS="closed"
        )

        self.assertEqual(completed.returncode, 2)
        run_id = completed.stdout.strip()
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "bead_preflight")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("not open and exact", status["attention"]["summary"])

    def test_start_records_wrong_project_as_durable_invalid_attention(self):
        completed = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_FAKE_PROJECT_LABEL="project:another-project",
        )

        self.assertEqual(completed.returncode, 2)
        run_id = completed.stdout.strip()
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "bead_preflight")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("does not belong", status["attention"]["summary"])

    def _write_fake_commands(self):
        script = self.fake_bin / "fake-command"
        script.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env python3
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                command = Path(sys.argv[0]).name
                args = sys.argv[1:]
                log_path = Path(os.environ["AFK_FAKE_LOG"])
                with log_path.open("a", encoding="utf-8") as log:
                    record = {"command": command, "args": args}
                    if command == "bd":
                        record["credential_present"] = (
                            os.environ.get("BEADS_DOLT_PASSWORD")
                            == os.environ["AFK_FAKE_EXPECTED_PASSWORD"]
                        )
                    log.write(json.dumps(record, separators=(",", ":")) + "\\n")

                project = os.environ["AFK_FAKE_PROJECT"]
                sha = os.environ["AFK_FAKE_SHA"]
                candidate_sha = "d" * 40
                candidate_marker = Path(os.environ["HOME"]) / ".fake-candidate"
                pushed_marker = Path(os.environ["HOME"]) / ".fake-pushed"
                pr_state = Path(os.environ["XDG_STATE_HOME"]) / "fake-pr.json"
                target_drift = Path(os.environ["XDG_STATE_HOME"]) / "fake-target-drift"
                if command == "git":
                    if args[:2] == ["rev-parse", "--show-toplevel"]:
                        print(project)
                    elif args[:2] == ["remote", "get-url"]:
                        print("git@github.com:thunderbump/beads-webui.git")
                    elif args[:1] == ["ls-remote"]:
                        requested = args[-1]
                        if requested == "refs/heads/main":
                            target_sha = "e" * 40 if target_drift.exists() else sha
                            print(target_sha + "\\trefs/heads/main")
                        elif pushed_marker.exists():
                            print(candidate_sha + "\\t" + requested)
                    elif args[:1] == ["fetch"]:
                        pass
                    elif args[:1] == ["ls-tree"]:
                        if os.environ["AFK_FAKE_PINNED_CONTRACT"] == "present":
                            print("100644 blob " + "c" * 40 + "\\tafk.toml")
                    elif args[:2] == ["cat-file", "blob"]:
                        print("schema_version = 1")
                        print("[validation]")
                        print('command = ["./scripts/validation-worker.sh", "run"]')
                        print("timeout_seconds = 2700")
                    elif args[:2] == ["rev-parse", "--git-dir"]:
                        git_dir = (
                            Path(os.environ["XDG_STATE_HOME"])
                            / "fake-git"
                            / "worktrees"
                            / Path.cwd().name
                        )
                        git_dir.mkdir(parents=True, exist_ok=True)
                        print(git_dir)
                    elif args[:2] == ["rev-parse", "--git-common-dir"]:
                        common_dir = Path(os.environ["XDG_STATE_HOME"]) / "fake-git"
                        common_dir.mkdir(parents=True, exist_ok=True)
                        print(common_dir)
                    elif args[:1] == ["rev-parse"]:
                        print(candidate_sha if candidate_marker.exists() and Path.cwd() != Path(project) else sha)  # noqa: E501
                    elif args[:2] == ["worktree", "add"]:
                        if os.environ.get("AFK_FAKE_WORKTREE_FAILURE"):
                            print("worktree failed", file=sys.stderr)
                            raise SystemExit(1)
                        checkout = Path(args[-2])
                        checkout.mkdir(parents=True)
                        git_file = checkout / ".git"
                        git_file.write_text("gitdir: fake\\n", encoding="utf-8")
                    elif args[:3] == ["worktree", "list", "--porcelain"]:
                        if not os.environ.get("AFK_FAKE_UNREGISTERED_WORKTREE"):
                            worktrees = (
                                Path(os.environ["XDG_STATE_HOME"])
                                / "afk"
                                / "worktrees"
                            )
                            for checkout in worktrees.iterdir():
                                run_id = checkout.name
                                head = (
                                    "b" * 40
                                    if os.environ.get("AFK_FAKE_WRONG_WORKTREE_HEAD")
                                    else sha
                                )
                                branch = (
                                    "afk/wrong-branch"
                                    if os.environ.get("AFK_FAKE_WRONG_WORKTREE_BRANCH")
                                    else "afk/"
                                    + os.environ["AFK_FAKE_BEAD"].replace(".", "-")
                                    + "-"
                                    + run_id
                                )
                                print("worktree " + str(checkout))
                                print("HEAD " + head)
                                print("branch refs/heads/" + branch)
                                print()
                    elif args[:2] == ["status", "--porcelain"]:
                        pass
                    elif args[:2] == ["branch", "--show-current"]:
                        run_id = Path.cwd().name
                        print("afk/" + os.environ["AFK_FAKE_BEAD"].replace(".", "-") + "-" + run_id)  # noqa: E501
                    elif args[:2] == ["merge-base", "--is-ancestor"]:
                        pass
                    elif args[:1] == ["rev-list"]:
                        pass
                    elif args[:1] == ["push"]:
                        pushed_marker.write_text(candidate_sha, encoding="utf-8")
                        if os.environ.get("AFK_FAKE_PUSH_INTERRUPTED"):
                            raise SystemExit(1)
                    else:
                        raise SystemExit(f"unexpected git args: {args}")
                elif command == "gh":
                    if args[:2] == ["pr", "list"]:
                        print(json.dumps([json.loads(pr_state.read_text())] if pr_state.exists() else []))  # noqa: E501
                    elif args[:2] == ["pr", "create"]:
                        value = {
                            "number": 17,
                            "url": "https://example.test/pr/17",
                            "state": "OPEN",
                            "isDraft": True,
                            "headRefOid": candidate_sha,
                            "headRefName": args[args.index("--head") + 1],
                            "baseRefName": args[args.index("--base") + 1],
                        }
                        pr_state.write_text(json.dumps(value), encoding="utf-8")
                        print(value["url"])
                        if os.environ.get("AFK_FAKE_TARGET_DRIFT_ON_PR"):
                            target_drift.write_text("drifted", encoding="utf-8")
                        if os.environ.get("AFK_FAKE_PR_INTERRUPTED"):
                            raise SystemExit(1)
                    elif os.environ.get("AFK_FAKE_GH_NON_OBJECT"):
                        print("[]")
                    else:
                        print(json.dumps({
                            "nameWithOwner": "thunderbump/beads-webui",
                            "defaultBranchRef": {"name": "main"},
                        }))
                elif command == "bd":
                    if (
                        os.environ.get("AFK_FAKE_REJECT_CREDENTIAL")
                        and not record["credential_present"]
                    ):
                        print(json.dumps({
                            "error": "authentication denied",
                            "endpoint": "dogfood.internal:3306",
                            "database_user": "raw-database-user",
                            "password": os.environ["BEADS_DOLT_PASSWORD"],
                        }), file=sys.stderr)
                        raise SystemExit(1)
                    status = os.environ["AFK_FAKE_BEAD_STATUS"]
                    assignee = os.environ["AFK_FAKE_ASSIGNEE"]
                    labels = (
                        None
                        if os.environ.get("AFK_FAKE_INVALID_LABELS")
                        else [
                            os.environ.get(
                                "AFK_FAKE_PROJECT_LABEL", "project:beads-webui"
                            )
                        ]
                    )
                    if args[:1] == ["show"]:
                        print(json.dumps([{
                            "id": os.environ["AFK_FAKE_BEAD"],
                            "title": "Create the first slice",
                            "description": "Implement one candidate.",
                            "acceptance_criteria": "Candidate is committed.",
                            "status": status,
                            "assignee": assignee,
                            "labels": labels,
                        }]))
                    elif args[:1] == ["update"]:
                        if os.environ.get("AFK_FAKE_CLAIM_FAILURE"):
                            print("claim failed", file=sys.stderr)
                            raise SystemExit(1)
                        if os.environ.get("AFK_FAKE_CLAIM_MALFORMED"):
                            print(
                                '{"database_user":"raw-database-user",'
                                '"endpoint":"dogfood.internal:3306"'
                            )
                            raise SystemExit(0)
                        shape = os.environ.get("AFK_FAKE_CLAIM_SHAPE")
                        if shape:
                            payload = {
                                "null": None,
                                "empty": [],
                                "multi": [
                                    {
                                        "database_user": "raw-database-user",
                                        "endpoint": "dogfood.internal:3306",
                                    },
                                    {},
                                ],
                                "non-object": [
                                    "raw-database-user@dogfood.internal:3306"
                                ],
                            }[shape]
                            print(json.dumps(payload))
                            raise SystemExit(0)
                        print(json.dumps({
                            "id": (
                                "central-other.1"
                                if os.environ.get("AFK_FAKE_CLAIM_MISMATCH")
                                else os.environ["AFK_FAKE_BEAD"]
                            ),
                            "status": "in_progress",
                            "assignee": os.environ["USER"],
                        }))
                    else:
                        raise SystemExit(f"unexpected bd args: {args}")
                elif command == "systemd-run":
                    if os.environ.get("AFK_FAKE_RESUME_DURING_LAUNCH"):
                        resumed = subprocess.run(
                            [sys.executable, "-m", "afk", "resume"],
                            cwd=project,
                            env=os.environ,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        with log_path.open("a", encoding="utf-8") as log:
                            log.write(json.dumps({
                                "command": "resume-probe",
                                "returncode": resumed.returncode,
                            }, separators=(",", ":")) + "\\n")
                    if os.environ.get("AFK_FAKE_LAUNCH_WORKER"):
                        subprocess.Popen(
                            args[-5:],
                            cwd=project,
                            env=os.environ,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            close_fds=True,
                        )
                    if os.environ.get("AFK_FAKE_SYSTEMD_FAILURE"):
                        print("launch failed", file=sys.stderr)
                        raise SystemExit(1)
                elif command == "systemctl":
                    state = os.environ.get("AFK_FAKE_SYSTEMD_STATE", "active")
                    if state == "failure":
                        print("query failed", file=sys.stderr)
                        raise SystemExit(1)
                    if state == "ambiguous":
                        print("LoadState=loaded")
                    elif state == "absent":
                        print("LoadState=not-found")
                        print("ActiveState=inactive")
                    else:
                        print("LoadState=loaded")
                        print("ActiveState=" + state)
                elif command == "loginctl":
                    print("yes")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        for name in ("git", "gh", "bd", "systemd-run", "systemctl", "loginctl"):
            (self.fake_bin / name).symlink_to(script)
        codex = self.fake_bin / "codex"
        codex.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                base_sha = "a" * 40
                candidate_sha = "d" * 40
                report = Path(args[args.index("--output-last-message") + 1])
                (Path(os.environ["HOME"]) / ".fake-candidate").write_text(
                    candidate_sha, encoding="utf-8"
                )
                report.write_text(json.dumps({
                    "status": "completed",
                    "starting_sha": base_sha,
                    "ending_sha": candidate_sha,
                    "summary": "implemented",
                    "checks": [],
                    "changed_areas": ["candidate.txt"],
                }), encoding="utf-8")
                print(json.dumps({"type": "result"}))
                """
            ).lstrip(),
            encoding="utf-8",
        )
        codex.chmod(codex.stat().st_mode | stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
