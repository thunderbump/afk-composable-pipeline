import json
import os
import shutil
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
from afk.run_store import RunStore, RunStoreBusy, RunStoreError  # noqa: E402
from afk.start import (  # noqa: E402
    StartError,
    _beads_password,
    run_worker_unit,
    start_run,
)


BASE_SHA = "a" * 40
CRASH_INJECTION_OVERRIDES = (
    "AFK_TEST_KILL_BEFORE_EVENT",
    "AFK_TEST_KILL_AFTER_EVENT",
    "AFK_TEST_KILL_AFTER_EVENT_WRITE",
    "AFK_TEST_KILL_BEFORE_EFFECT",
    "AFK_TEST_KILL_AFTER_EFFECT",
    "AFK_TEST_KILL_BEFORE_CONFIRM",
    "AFK_TEST_KILL_AFTER_CONFIRM",
)


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
        self.state_home.mkdir()
        self.home = self.temp / "home"
        self.home.mkdir()
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
        short_cleanup_timeout = overrides.pop("AFK_TEST_SHORT_CLEANUP_TIMEOUT", None)
        crash_injections = {
            key: target
            for key in CRASH_INJECTION_OVERRIDES
            if (target := overrides.pop(key, None))
        }
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
                "AFK_FAKE_BEAD_DESCRIPTION": "Implement one candidate.",
                "AFK_FAKE_BEAD_COMMENTS": "[]",
                "AFK_FAKE_PINNED_CONTRACT": "present",
                "AFK_FAKE_EXPECTED_PASSWORD": self.secret_value,
                "HOME": str(self.home),
                "USER": "bump",
            }
        )
        env.update(overrides)
        command = [sys.executable, "-m", "afk", *args]
        if crash_injections:
            env["AFK_TEST_CRASH_INJECTIONS"] = json.dumps(
                crash_injections, sort_keys=True
            )
            command = [
                sys.executable,
                "-c",
                (
                    "import json, os, signal, sys\n"
                    "from afk.run_store import RunStore\n"
                    "original = RunStore.append_event\n"
                    "original_write = RunStore._append_event_unlocked\n"
                    "original_effect = RunStore.prepare_effect\n"
                    "original_confirm = RunStore.confirm_effect\n"
                    "injections = json.loads(os.environ['AFK_TEST_CRASH_INJECTIONS'])\n"
                    "def injected(store, run_id, event, **kwargs):\n"
                    " if event == injections.get('AFK_TEST_KILL_BEFORE_EVENT'):\n"
                    "  os.kill(os.getpid(), signal.SIGKILL)\n"
                    " result = original(store, run_id, event, **kwargs)\n"
                    " if event == injections.get('AFK_TEST_KILL_AFTER_EVENT'):\n"
                    "  os.kill(os.getpid(), signal.SIGKILL)\n"
                    " return result\n"
                    "def injected_write(store, run_id, event, **kwargs):\n"
                    " result = original_write(store, run_id, event, **kwargs)\n"
                    " if event == injections.get('AFK_TEST_KILL_AFTER_EVENT_WRITE'):\n"
                    "  os.kill(os.getpid(), signal.SIGKILL)\n"
                    " return result\n"
                    "def injected_effect(store, run_id, effect_id, **kwargs):\n"
                    " if effect_id == injections.get('AFK_TEST_KILL_BEFORE_EFFECT'):\n"
                    "  os.kill(os.getpid(), signal.SIGKILL)\n"
                    " result = original_effect(store, run_id, effect_id, **kwargs)\n"
                    " if effect_id == injections.get('AFK_TEST_KILL_AFTER_EFFECT'):\n"
                    "  os.kill(os.getpid(), signal.SIGKILL)\n"
                    " return result\n"
                    "def injected_confirm(store, run_id, effect_id, **kwargs):\n"
                    " if effect_id == injections.get('AFK_TEST_KILL_BEFORE_CONFIRM'):\n"
                    "  os.kill(os.getpid(), signal.SIGKILL)\n"
                    " result = original_confirm(store, run_id, effect_id, **kwargs)\n"
                    " if effect_id == injections.get('AFK_TEST_KILL_AFTER_CONFIRM'):\n"
                    "  os.kill(os.getpid(), signal.SIGKILL)\n"
                    " return result\n"
                    "RunStore.append_event = injected\n"
                    "RunStore._append_event_unlocked = injected_write\n"
                    "RunStore.prepare_effect = injected_effect\n"
                    "RunStore.confirm_effect = injected_confirm\n"
                    "from afk.cli import main\n"
                    "raise SystemExit(main(sys.argv[1:]))\n"
                ),
                *args,
            ]
        elif short_cleanup_timeout:
            command = [
                sys.executable,
                "-c",
                (
                    "import sys; import afk.candidate as candidate; "
                    "import afk.start as start; "
                    "candidate._run.__kwdefaults__['timeout'] = 0.05; "
                    "start.COMMAND_TIMEOUT_SECONDS = 0.05; "
                    "from afk.cli import main; raise SystemExit(main(sys.argv[1:]))"
                ),
                *args,
            ]
        return subprocess.run(
            command,
            cwd=self.project,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def start_reviewed_run(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        worker = self.run_afk("_worker", run_id)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        return run_id

    def mutation_count(self, name, *, state_home=None):
        path = (state_home or self.state_home) / "fake-mutations.jsonl"
        if not path.exists():
            return 0
        return sum(
            json.loads(line)["mutation"] == name
            for line in path.read_text(encoding="utf-8").splitlines()
        )

    def launch_events(self, run_id, event, *, state_home=None):
        path = (
            (state_home or self.state_home) / "afk" / "runs" / run_id / "events.jsonl"
        )
        return [
            record
            for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if record["event"] == event
        ]

    def assert_launch_effect(self, store, run_id, status):
        unit = f"afk-{run_id}-worker-1"
        expected = {
            "schema_version": 1,
            "effect_id": "worker-launch-1",
            "kind": "worker-launch",
            "status": status,
            "intended": {"unit": unit},
        }
        if status == "confirmed":
            expected["observed"] = {"unit": unit}
        self.assertEqual(store.effect(run_id, "worker-launch-1"), expected)
        return unit

    def assert_launch_event(self, run_id, event, count, unit):
        records = self.launch_events(run_id, event)
        self.assertEqual(len(records), count)
        self.assertTrue(all(record["data"]["unit"] == unit for record in records))

    def assert_bead_claim(self, store, run_id, status="confirmed", claimant="bump"):
        intended = {
            "bead_id": "central-bnkl.1.1",
            "claimant": claimant,
            "project_label": "project:beads-webui",
        }
        expected = {
            "schema_version": 1,
            "effect_id": "bead-claim",
            "kind": "bead-claim",
            "status": status,
            "intended": intended,
        }
        if status == "confirmed":
            expected["observed"] = {**intended, "status": "in_progress"}
        self.assertEqual(store.effect(run_id, "bead-claim"), expected)
        return expected.get("observed")

    def assert_worktree_effect(
        self, store, run_id, status="confirmed", *, state_home=None
    ):
        selected_state_home = state_home or self.state_home
        branch = f"afk/central-bnkl-1-1-{run_id}/candidate"
        worktree = selected_state_home / "afk" / "worktrees" / run_id
        intended = {
            "repository": "thunderbump/beads-webui",
            "repository_root": str(self.project),
            "repository_common_dir": str(selected_state_home / "fake-git"),
            "base_sha": BASE_SHA,
            "branch": branch,
            "worktree_path": str(worktree),
        }
        expected = {
            "schema_version": 1,
            "effect_id": "worktree-create",
            "kind": "worktree-create",
            "status": status,
            "intended": intended,
        }
        if status == "confirmed":
            expected["observed"] = intended
        self.assertEqual(store.effect(run_id, "worktree-create"), expected)
        return intended

    def command_count(self, command, prefix, *, argument=None):
        return sum(
            record["command"] == command
            and record["args"][: len(prefix)] == prefix
            and (argument is None or argument in record["args"])
            for record in map(
                json.loads,
                self.command_log.read_text(encoding="utf-8").splitlines(),
            )
        )

    def start_bead_closed_run_with_lingering_remote(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(
            self.run_afk("resume", AFK_FAKE_REMOTE_BRANCH_LINGERS="1").returncode,
            0,
        )
        self.assertEqual(self.run_afk("resume").returncode, 0)
        return run_id

    def assert_exact_terminal_completion(self, run_id):
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        completion = status["completion"]
        self.assertEqual(status["checkpoint"], "completed")
        self.assertEqual(completion["schema_version"], 1)
        self.assertEqual(completion["repository"], "thunderbump/beads-webui")
        self.assertEqual(completion["bead_id"], "central-bnkl.1.1")
        self.assertEqual(completion["candidate_sha"], "d" * 40)
        self.assertEqual(completion["pr_number"], 17)
        self.assertEqual(completion["pr_url"], "https://example.test/pr/17")
        self.assertEqual(completion["merge_commit"], "f" * 40)
        self.assertEqual(completion["bead_closure"], status["bead_closure"])
        self.assertTrue(completion["remote_branch_deleted"])
        self.assertTrue(completion["worktree_removed"])
        self.assertTrue(completion["local_branch_deleted"])
        self.assertEqual(completion["cleanup_warnings"], [])
        self.assertEqual(completion["evidence"], "gates/completion-dddddddddddd")
        store = RunStore(self.state_home / "afk")
        close_effect = store.effect(run_id, "bead-close")
        self.assertEqual(close_effect["status"], "confirmed")
        self.assertEqual(
            close_effect["observed"],
            {
                "bead_id": "central-bnkl.1.1",
                "repository": "thunderbump/beads-webui",
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "merge_commit": "f" * 40,
                "status": "closed",
                "close_reason": "merged via " + "f" * 40,
            },
        )
        self.assertEqual(status["bead_closure"], close_effect["observed"])
        self.assertEqual(
            store.sealed_evidence_result(run_id, completion["evidence"]), completion
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
            f"{run_id} created bead=central-bnkl.1.1 sequence=3 "
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

    def test_start_forwards_only_approved_validation_execution_context(self):
        approved = {
            "TMPDIR": str(self.temp / "operator-tmp"),
            "XDG_RUNTIME_DIR": str(self.temp / "operator-runtime"),
            "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
            "DOCKER_CONTEXT": "akkstack",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": str(self.temp / "docker-certs"),
            "DOCKER_CONFIG": str(self.temp / "docker-config"),
        }
        denied = {
            "UNRELATED_SECRET": "must-not-cross",
            "GH_TOKEN": "github-secret",
            "BEADS_DOLT_PASSWORD": "beads-secret",
            "OPENAI_API_KEY": "model-secret",
            "DOCKER_AUTH_CONFIG": "docker-auth-secret",
        }

        completed = self.run_afk("start", "central-bnkl.1.1", **approved, **denied)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        systemd = next(
            record for record in records if record["command"] == "systemd-run"
        )
        for name, value in approved.items():
            self.assertIn(f"--setenv={name}={value}", systemd["args"])
        serialized = json.dumps(systemd)
        for name, value in denied.items():
            self.assertNotIn(f"--setenv={name}=", serialized)
            self.assertNotIn(value, serialized)

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

    def test_resume_recovers_process_crash_before_worker_launch_mutation(self):
        interrupted = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_BEFORE_MUTATION="worker-launch",
        )
        self.assertLess(interrupted.returncode, 0)
        run_id = json.loads(self.run_afk("status", "--json").stdout)["run_id"]
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "prepared")
        self.assertEqual(self.mutation_count("worker-launch"), 0)

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "prepared")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_process_crash_before_worker_launch_effect(self):
        interrupted = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_BEFORE_EFFECT="worker-launch-1",
        )
        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", "--json").stdout)
        run_id = before["run_id"]
        self.assertEqual(before["last_event"], "bead.spec_recorded")
        store = RunStore(self.state_home / "afk")
        lingering = store.identity(run_id)["start_request"]["lingering"]
        self.assertEqual(lingering, "enabled")
        with self.assertRaisesRegex(RunStoreError, "Effect is missing"):
            store.effect(run_id, "worker-launch-1")

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        effect = store.effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "prepared")
        events = [
            json.loads(line)["event"]
            for line in (self.state_home / "afk" / "runs" / run_id / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(events.count("worker.launch_prepared"), 1)
        self.assertEqual(events.count("worker.launch_retried"), 1)
        prepared = self.launch_events(run_id, "worker.launch_prepared")[0]
        self.assertEqual(prepared["data"]["lingering"], lingering)
        self.assertEqual(store.status(run_id)["lingering"], lingering)
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_process_crash_after_worker_launch_effect(self):
        interrupted = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_AFTER_EFFECT="worker-launch-1",
        )
        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", "--json").stdout)
        run_id = before["run_id"]
        self.assertEqual(before["last_event"], "bead.spec_recorded")
        store = RunStore(self.state_home / "afk")
        lingering = store.identity(run_id)["start_request"]["lingering"]
        self.assertEqual(lingering, "enabled")
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "prepared")

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "prepared")
        events = [
            json.loads(line)["event"]
            for line in (self.state_home / "afk" / "runs" / run_id / "events.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(events.count("worker.launch_prepared"), 1)
        self.assertEqual(events.count("worker.launch_retried"), 1)
        prepared = self.launch_events(run_id, "worker.launch_prepared")[0]
        self.assertEqual(prepared["data"]["lingering"], lingering)
        self.assertEqual(store.status(run_id)["lingering"], lingering)
        self.assertEqual(self.mutation_count("worker-launch"), 1)

        observed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "confirmed")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_crash_before_worker_launch_prepared_event(self):
        interrupted = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_BEFORE_EVENT="worker.launch_prepared",
        )
        self.assertLess(interrupted.returncode, 0)
        run_id = json.loads(self.run_afk("status", "--json").stdout)["run_id"]
        store = RunStore(self.state_home / "afk")
        unit = self.assert_launch_effect(store, run_id, "prepared")
        self.assert_launch_event(run_id, "worker.launch_prepared", 0, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 0)

        retried = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assert_launch_effect(store, run_id, "prepared")
        self.assert_launch_event(run_id, "worker.launch_prepared", 1, unit)
        self.assert_launch_event(run_id, "worker.launch_retried", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 1)
        observed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_crash_after_worker_launch_prepared_event_write(self):
        interrupted = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_AFTER_EVENT_WRITE="worker.launch_prepared",
        )
        self.assertLess(interrupted.returncode, 0)
        run_id = json.loads(self.run_afk("status", "--json").stdout)["run_id"]
        store = RunStore(self.state_home / "afk")
        unit = self.assert_launch_effect(store, run_id, "prepared")
        self.assert_launch_event(run_id, "worker.launch_prepared", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 0)

        retried = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assert_launch_event(run_id, "worker.launch_prepared", 1, unit)
        self.assert_launch_event(run_id, "worker.launch_retried", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 1)
        observed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_crash_before_worker_launch_confirmation(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        store = RunStore(self.state_home / "afk")
        unit = self.assert_launch_effect(store, run_id, "prepared")

        interrupted = self.run_afk(
            "_worker",
            run_id,
            AFK_TEST_KILL_BEFORE_CONFIRM="worker-launch-1",
        )

        self.assertLess(interrupted.returncode, 0)
        self.assert_launch_effect(store, run_id, "prepared")
        self.assert_launch_event(run_id, "worker.launched", 0, unit)
        ambiguous = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="ambiguous")
        self.assertEqual(ambiguous.returncode, 2, ambiguous.stderr)
        self.assertEqual(store.status(run_id)["attention"]["kind"], "inconclusive")
        self.assertEqual(self.mutation_count("worker-launch"), 1)
        retried = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assert_launch_effect(store, run_id, "prepared")
        self.assert_launch_event(run_id, "worker.launch_retried", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 2)
        observed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assertEqual(self.mutation_count("worker-launch"), 2)

    def test_resume_pauses_after_worker_launch_confirmation_was_written(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        store = RunStore(self.state_home / "afk")
        unit = f"afk-{run_id}-worker-1"

        interrupted = self.run_afk(
            "_worker",
            run_id,
            AFK_TEST_KILL_AFTER_CONFIRM="worker-launch-1",
        )

        self.assertLess(interrupted.returncode, 0)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assert_launch_event(run_id, "worker.launched", 0, unit)
        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(store.status(run_id)["attention"]["kind"], "inconclusive")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_crash_before_worker_launch_retried_event(self):
        interrupted_start = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_BEFORE_MUTATION="worker-launch",
        )
        self.assertLess(interrupted_start.returncode, 0)
        run_id = json.loads(self.run_afk("status", "--json").stdout)["run_id"]
        store = RunStore(self.state_home / "afk")
        unit = self.assert_launch_effect(store, run_id, "prepared")

        interrupted_retry = self.run_afk(
            "resume",
            AFK_FAKE_SYSTEMD_STATE="absent",
            AFK_TEST_KILL_BEFORE_EVENT="worker.launch_retried",
        )

        self.assertLess(interrupted_retry.returncode, 0)
        self.assert_launch_event(run_id, "worker.launch_retried", 0, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 1)
        observed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assert_launch_event(run_id, "worker.launch_reconciled", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_crash_after_worker_launch_retried_event_write(self):
        interrupted_start = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_BEFORE_MUTATION="worker-launch",
        )
        self.assertLess(interrupted_start.returncode, 0)
        run_id = json.loads(self.run_afk("status", "--json").stdout)["run_id"]
        store = RunStore(self.state_home / "afk")
        unit = self.assert_launch_effect(store, run_id, "prepared")

        interrupted_retry = self.run_afk(
            "resume",
            AFK_FAKE_SYSTEMD_STATE="absent",
            AFK_TEST_KILL_AFTER_EVENT_WRITE="worker.launch_retried",
        )

        self.assertLess(interrupted_retry.returncode, 0)
        self.assert_launch_event(run_id, "worker.launch_retried", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 1)
        observed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assert_launch_event(run_id, "worker.launch_retried", 1, unit)
        self.assert_launch_event(run_id, "worker.launch_reconciled", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_pauses_after_crash_before_worker_launched_event(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        store = RunStore(self.state_home / "afk")
        unit = f"afk-{run_id}-worker-1"

        interrupted = self.run_afk(
            "_worker",
            run_id,
            AFK_TEST_KILL_BEFORE_EVENT="worker.launched",
        )

        self.assertLess(interrupted.returncode, 0)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assert_launch_event(run_id, "worker.launched", 0, unit)
        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(store.status(run_id)["attention"]["kind"], "inconclusive")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_recovers_after_worker_launched_event_write(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        store = RunStore(self.state_home / "afk")
        unit = f"afk-{run_id}-worker-1"

        interrupted = self.run_afk(
            "_worker",
            run_id,
            AFK_TEST_KILL_AFTER_EVENT_WRITE="worker.launched",
        )

        self.assertLess(interrupted.returncode, 0)
        self.assert_launch_effect(store, run_id, "confirmed")
        self.assert_launch_event(run_id, "worker.launched", 1, unit)
        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(store.status(run_id)["checkpoint"], "worktree_ready")
        self.assert_bead_claim(store, run_id)
        self.assert_launch_event(run_id, "worker.launched", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 1)
        self.assertEqual(self.mutation_count("bead-claim"), 1)

    def test_resume_reconciles_process_crash_after_worker_launch_mutation(self):
        interrupted = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_AFTER_MUTATION="worker-launch",
        )
        self.assertLess(interrupted.returncode, 0)
        run_id = json.loads(self.run_afk("status", "--json").stdout)["run_id"]
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "prepared")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "confirmed")
        self.assertEqual(store.status(run_id)["last_event"], "worker.launch_reconciled")
        self.assertEqual(self.mutation_count("worker-launch"), 1)

    def test_resume_safely_retries_collected_unconfirmed_worker_launch(self):
        interrupted = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_TEST_KILL_AFTER_MUTATION="worker-launch",
        )
        self.assertLess(interrupted.returncode, 0)
        run_id = json.loads(self.run_afk("status", "--json").stdout)["run_id"]
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "prepared")
        self.assertIsNone(store.effect_if_present(run_id, "bead-claim"))
        run_dir = self.state_home / "afk" / "runs" / run_id
        self.assertEqual(
            sorted(path.name for path in (run_dir / "effects").iterdir()),
            ["worker-launch-1.json"],
        )
        events_path = run_dir / "events.jsonl"
        events = [
            json.loads(line)["event"] for line in events_path.read_text().splitlines()
        ]
        self.assertNotIn("worker.launched", events)
        self.assertNotIn("bead.claimed", events)
        self.assertNotIn("worktree.ready", events)
        self.assertEqual(self.mutation_count("worker-launch"), 1)

        retried = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "prepared")
        self.assertIsNone(store.effect_if_present(run_id, "bead-claim"))
        self.assertEqual(self.mutation_count("worker-launch"), 2)

        reconciled = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        self.assertEqual(store.effect(run_id, "worker-launch-1")["status"], "confirmed")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(self.mutation_count("worker-launch"), 2)

    def test_resume_recovers_crash_before_worker_reconciled_state_append(self):
        store, run_dir = self.create_resume_preflight_run()
        unit = self.assert_launch_effect(store, "crashed-run", "prepared")

        interrupted = self.run_afk(
            "resume",
            AFK_FAKE_SYSTEMD_STATE="active",
            AFK_TEST_KILL_BEFORE_EVENT="worker.launch_reconciled",
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(
            store.effect("crashed-run", "worker-launch-1")["status"], "confirmed"
        )
        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_launch_effect(store, "crashed-run", "confirmed")
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "worker.launch_reconciled" for event in events), 1
        )
        self.assert_launch_event("crashed-run", "worker.launch_reconciled", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 0)

    def test_resume_recovers_crash_after_worker_reconciled_state_append(self):
        store, run_dir = self.create_resume_preflight_run()
        unit = self.assert_launch_effect(store, "crashed-run", "prepared")

        interrupted = self.run_afk(
            "resume",
            AFK_FAKE_SYSTEMD_STATE="active",
            AFK_TEST_KILL_AFTER_EVENT_WRITE="worker.launch_reconciled",
        )

        self.assertLess(interrupted.returncode, 0)
        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_launch_effect(store, "crashed-run", "confirmed")
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            sum(event["event"] == "worker.launch_reconciled" for event in events), 1
        )
        self.assert_launch_event("crashed-run", "worker.launch_reconciled", 1, unit)
        self.assertEqual(self.mutation_count("worker-launch"), 0)

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
            if projection["state"] in {"attention_required", "reviewed"}:
                break
            time.sleep(0.05)
        self.assertEqual(projection["checkpoint"], "reviewed")
        effect = RunStore(self.state_home / "afk").effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "confirmed")

    def test_start_forwards_the_intended_beads_claimant_to_the_worker(self):
        completed = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_FAKE_LAUNCH_WORKER="1",
            BEADS_ACTOR="pipeline-agent",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        run_id = completed.stdout.strip()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            projection = json.loads(self.run_afk("status", run_id, "--json").stdout)
            if projection["state"] in {"attention_required", "reviewed"}:
                break
            time.sleep(0.05)
        self.assertEqual(projection["checkpoint"], "reviewed")
        self.assert_bead_claim(
            RunStore(self.state_home / "afk"),
            run_id,
            claimant="pipeline-agent",
        )

    def test_resume_recovers_crash_before_bead_claim_effect(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        interrupted = self.run_afk(
            "_worker", run_id, AFK_TEST_KILL_BEFORE_EFFECT="bead-claim"
        )
        self.assertLess(interrupted.returncode, 0)
        store = RunStore(self.state_home / "afk")
        self.assertIsNone(store.effect_if_present(run_id, "bead-claim"))
        self.assertEqual(self.mutation_count("bead-claim"), 0)

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        observed = self.assert_bead_claim(store, run_id)
        projection = store.status(run_id)
        self.assertEqual(projection["checkpoint"], "worktree_ready")
        self.assertEqual(projection["bead_claim"], observed)
        self.assertEqual(self.mutation_count("bead-claim"), 1)

    def test_resume_recovers_initial_worker_bead_claim_outage(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        unavailable = self.run_afk("_worker", run_id, AFK_FAKE_BEAD_SHOW_FAILURE="1")

        self.assertEqual(unavailable.returncode, 2, unavailable.stderr)
        store = RunStore(self.state_home / "afk")
        paused = store.status(run_id)
        self.assertEqual(paused["checkpoint"], "created")
        self.assertEqual(paused["attention"]["scope"], "bead_claim")
        self.assert_bead_claim(store, run_id, status="prepared")
        self.assertEqual(self.mutation_count("bead-claim"), 0)

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        observed = self.assert_bead_claim(store, run_id)
        projection = store.status(run_id)
        self.assertEqual(projection["checkpoint"], "worktree_ready")
        self.assertEqual(projection["bead_claim"], observed)
        self.assertEqual(projection["attention"], {})
        self.assertEqual(len(self.launch_events(run_id, "bead.claimed")), 1)
        self.assertEqual(self.mutation_count("bead-claim"), 1)

    def test_resume_claims_as_the_durable_actor_after_actor_drift(self):
        run_id = self.run_afk(
            "start", "central-bnkl.1.1", BEADS_ACTOR="pipeline-agent"
        ).stdout.strip()
        interrupted = self.run_afk(
            "_worker",
            run_id,
            BEADS_ACTOR="pipeline-agent",
            AFK_TEST_KILL_BEFORE_MUTATION="bead-claim",
        )
        self.assertLess(interrupted.returncode, 0)

        resumed = self.run_afk(
            "resume",
            BEADS_ACTOR="different-agent",
            AFK_FAKE_SYSTEMD_STATE="absent",
        )

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_bead_claim(
            RunStore(self.state_home / "afk"),
            run_id,
            claimant="pipeline-agent",
        )
        self.assertEqual(self.mutation_count("bead-claim"), 1)

    def test_resume_reconciles_bead_claim_across_every_crash_boundary(self):
        boundaries = {
            "after-effect": {"AFK_TEST_KILL_AFTER_EFFECT": "bead-claim"},
            "before-mutation": {"AFK_TEST_KILL_BEFORE_MUTATION": "bead-claim"},
            "after-mutation": {"AFK_TEST_KILL_AFTER_MUTATION": "bead-claim"},
            "before-confirm": {"AFK_TEST_KILL_BEFORE_CONFIRM": "bead-claim"},
            "after-confirm": {"AFK_TEST_KILL_AFTER_CONFIRM": "bead-claim"},
            "before-event": {"AFK_TEST_KILL_BEFORE_EVENT": "bead.claimed"},
            "after-event-write": {"AFK_TEST_KILL_AFTER_EVENT_WRITE": "bead.claimed"},
        }
        for name, injection in boundaries.items():
            with self.subTest(boundary=name):
                state_home = self.temp / f"claim-{name}"
                started = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                )
                run_id = started.stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    **injection,
                )
                self.assertLess(interrupted.returncode, 0)

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                )
                repeated = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                )

                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                store = RunStore(state_home / "afk")
                observed = self.assert_bead_claim(store, run_id)
                projection = store.status(run_id)
                self.assertEqual(projection["checkpoint"], "worktree_ready")
                self.assertEqual(projection["bead_claim"], observed)
                events = self.launch_events(
                    run_id, "bead.claimed", state_home=state_home
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(
                    self.mutation_count("bead-claim", state_home=state_home), 1
                )

    def test_resume_pauses_on_conflicting_or_unavailable_bead_claim_state(self):
        cases = {
            "other-owner": {
                "AFK_FAKE_BEAD_STATUS": "in_progress",
                "AFK_FAKE_ASSIGNEE": "another-agent",
            },
            "wrong-project": {"AFK_FAKE_PROJECT_LABEL": "project:another-repository"},
            "malformed-observation": {"AFK_FAKE_BEAD_SHOW_MALFORMED": "1"},
            "missing-assignee": {"AFK_FAKE_BEAD_SCHEMA": "missing-assignee"},
            "wrong-status-type": {"AFK_FAKE_BEAD_SCHEMA": "status-number"},
            "wrong-assignee-type": {"AFK_FAKE_BEAD_SCHEMA": "assignee-list"},
            "unavailable-observation": {"AFK_FAKE_BEAD_SHOW_FAILURE": "1"},
        }
        for name, observation in cases.items():
            with self.subTest(case=name):
                state_home = self.temp / f"claim-state-{name}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    AFK_TEST_KILL_BEFORE_MUTATION="bead-claim",
                )
                self.assertLess(interrupted.returncode, 0)

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                    **observation,
                )

                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                store = RunStore(state_home / "afk")
                self.assert_bead_claim(store, run_id, status="prepared")
                status = store.status(run_id)
                self.assertEqual(status["checkpoint"], "created")
                self.assertEqual(status["attention"]["scope"], "bead_claim")
                if name in {
                    "missing-assignee",
                    "wrong-status-type",
                    "wrong-assignee-type",
                }:
                    self.assertIn("project identity", status["attention"]["summary"])
                self.assertEqual(
                    self.mutation_count("bead-claim", state_home=state_home), 0
                )

    def test_resume_recovers_claim_after_malformed_mutation_output(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()
        interrupted = self.run_afk(
            "_worker", run_id, AFK_TEST_KILL_BEFORE_MUTATION="bead-claim"
        )
        self.assertLess(interrupted.returncode, 0)

        malformed = self.run_afk(
            "resume",
            AFK_FAKE_SYSTEMD_STATE="absent",
            AFK_FAKE_CLAIM_MALFORMED="1",
        )

        self.assertEqual(malformed.returncode, 2, malformed.stderr)
        store = RunStore(self.state_home / "afk")
        self.assert_bead_claim(store, run_id, status="prepared")
        self.assertEqual(store.status(run_id)["attention"]["scope"], "bead_claim")
        self.assertEqual(self.mutation_count("bead-claim"), 1)

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_bead_claim(store, run_id)
        self.assertEqual(self.mutation_count("bead-claim"), 1)

    def test_resume_refuses_durable_claim_without_its_confirmed_effect(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()
        interrupted = self.run_afk(
            "_worker", run_id, AFK_TEST_KILL_AFTER_EVENT_WRITE="bead.claimed"
        )
        self.assertLess(interrupted.returncode, 0)
        effect_path = (
            self.state_home / "afk" / "runs" / run_id / "effects" / "bead-claim.json"
        )
        effect_path.unlink()

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        self.assertFalse(effect_path.exists())
        status = RunStore(self.state_home / "afk").status(run_id)
        self.assertEqual(status["checkpoint"], "claimed")
        self.assertEqual(status["attention"]["scope"], "bead_claim")
        self.assertEqual(self.mutation_count("bead-claim"), 1)

    def test_resume_restores_durable_claim_after_observation_recovers(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()
        interrupted = self.run_afk(
            "_worker", run_id, AFK_TEST_KILL_AFTER_EVENT_WRITE="bead.claimed"
        )
        self.assertLess(interrupted.returncode, 0)

        unavailable = self.run_afk(
            "resume",
            AFK_FAKE_SYSTEMD_STATE="absent",
            AFK_FAKE_BEAD_SHOW_FAILURE="1",
        )
        store = RunStore(self.state_home / "afk")
        paused = store.status(run_id)
        self.assertEqual(unavailable.returncode, 2, unavailable.stderr)
        self.assertEqual(paused["checkpoint"], "claimed")
        self.assertEqual(paused["attention"]["scope"], "bead_claim")

        recovered = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        status = store.status(run_id)
        self.assertEqual(status["state"], "worktree_ready")
        self.assertEqual(status["checkpoint"], "worktree_ready")
        self.assertEqual(status["attention"], {})
        self.assertEqual(len(self.launch_events(run_id, "bead.claimed")), 1)
        self.assertEqual(self.mutation_count("bead-claim"), 1)

    def test_resume_reconciles_worktree_creation_across_every_crash_boundary(self):
        boundaries = {
            "before-effect": {"AFK_TEST_KILL_BEFORE_EFFECT": "worktree-create"},
            "after-effect": {"AFK_TEST_KILL_AFTER_EFFECT": "worktree-create"},
            "before-mutation": {"AFK_TEST_KILL_BEFORE_MUTATION": "worktree-create"},
            "after-mutation": {"AFK_TEST_KILL_AFTER_MUTATION": "worktree-create"},
            "before-confirm": {"AFK_TEST_KILL_BEFORE_CONFIRM": "worktree-create"},
            "after-confirm": {"AFK_TEST_KILL_AFTER_CONFIRM": "worktree-create"},
            "before-event": {"AFK_TEST_KILL_BEFORE_EVENT": "worktree.ready"},
            "after-event-write": {"AFK_TEST_KILL_AFTER_EVENT_WRITE": "worktree.ready"},
        }
        for name, injection in boundaries.items():
            with self.subTest(boundary=name):
                state_home = self.temp / f"worktree-{name}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()

                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    **injection,
                )

                self.assertLess(interrupted.returncode, 0)
                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                )
                repeated = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                )

                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                store = RunStore(state_home / "afk")
                intended = self.assert_worktree_effect(
                    store, run_id, state_home=state_home
                )
                projection = store.status(run_id)
                self.assertEqual(projection["checkpoint"], "worktree_ready")
                self.assertEqual(projection["worktree_path"], intended["worktree_path"])
                self.assertEqual(projection["branch"], intended["branch"])
                self.assertEqual(
                    len(
                        self.launch_events(
                            run_id, "worktree.ready", state_home=state_home
                        )
                    ),
                    1,
                )
                self.assertEqual(
                    self.mutation_count("worktree-create", state_home=state_home), 1
                )

    def test_resume_does_not_recreate_a_deleted_confirmed_worktree(self):
        boundaries = {
            "confirmed-effect": {"AFK_TEST_KILL_AFTER_CONFIRM": "worktree-create"},
            "durable-ready": {"AFK_TEST_KILL_AFTER_EVENT_WRITE": "worktree.ready"},
        }
        for name, injection in boundaries.items():
            with self.subTest(boundary=name):
                state_home = self.temp / f"worktree-deleted-{name}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    **injection,
                )
                self.assertLess(interrupted.returncode, 0)
                intended = self.assert_worktree_effect(
                    RunStore(state_home / "afk"), run_id, state_home=state_home
                )
                Path(intended["worktree_path"]).rename(
                    state_home / "externally-removed-worktree"
                )
                (state_home / "fake-worktree-created").unlink()

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                )

                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                projection = RunStore(state_home / "afk").status(run_id)
                self.assertEqual(projection["attention"]["scope"], "worktree")
                self.assertEqual(
                    self.mutation_count("worktree-create", state_home=state_home), 1
                )

    def test_resume_does_not_create_worktree_from_ambiguous_or_replaced_state(self):
        cases = {
            "branch-only": {"AFK_FAKE_WORKTREE_SCENARIO": "branch-only"},
            "branch-registered-elsewhere": {
                "AFK_FAKE_WORKTREE_SCENARIO": "registered-elsewhere"
            },
            "wrong-root": {"AFK_FAKE_REPOSITORY_ROOT": "/tmp/replaced-repository"},
            "wrong-repository": {
                "AFK_FAKE_ORIGIN_REPOSITORY": "thunderbump/another-repo"
            },
            "replaced-repository": {
                "AFK_FAKE_WORKTREE_SCENARIO": "replaced-repository"
            },
            "missing-base": {"AFK_FAKE_WORKTREE_SCENARIO": "missing-base"},
            "unavailable-registration": {"AFK_FAKE_WORKTREE_SCENARIO": "list-failure"},
            "malformed-registration": {"AFK_FAKE_WORKTREE_SCENARIO": "malformed-list"},
            "ambiguous-missing-branch": {
                "AFK_FAKE_WORKTREE_SCENARIO": "ambiguous-missing-branch",
            },
        }
        for name, observation in cases.items():
            with self.subTest(case=name):
                state_home = self.temp / f"worktree-absence-{name}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    AFK_TEST_KILL_BEFORE_MUTATION="worktree-create",
                )
                self.assertLess(interrupted.returncode, 0)

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                    **observation,
                )

                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                store = RunStore(state_home / "afk")
                self.assert_worktree_effect(
                    store, run_id, status="prepared", state_home=state_home
                )
                projection = store.status(run_id)
                self.assertEqual(projection["checkpoint"], "claimed")
                self.assertEqual(projection["attention"]["scope"], "worktree")
                self.assertEqual(
                    self.mutation_count("worktree-create", state_home=state_home), 0
                )

    def test_resume_rejects_a_worktree_record_with_two_checkout_modes(self):
        for mode in ("detached", "bare"):
            with self.subTest(mode=mode):
                state_home = self.temp / f"worktree-record-branch-{mode}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    AFK_TEST_KILL_BEFORE_MUTATION="worktree-create",
                )
                self.assertLess(interrupted.returncode, 0)
                store = RunStore(state_home / "afk")
                intended = self.assert_worktree_effect(
                    store, run_id, status="prepared", state_home=state_home
                )
                Path(intended["worktree_path"]).mkdir(parents=True)

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                    AFK_FAKE_WORKTREE_RECORD_EXTRA_MODE=mode,
                )

                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                self.assert_worktree_effect(
                    store, run_id, status="prepared", state_home=state_home
                )
                projection = store.status(run_id)
                self.assertEqual(projection["checkpoint"], "claimed")
                self.assertEqual(projection["attention"]["scope"], "worktree")
                self.assertEqual(
                    self.mutation_count("worktree-create", state_home=state_home), 0
                )

    def test_resume_accepts_worktree_record_metadata(self):
        for metadata in ("locked", "prunable"):
            with self.subTest(metadata=metadata):
                state_home = self.temp / f"worktree-record-{metadata}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    AFK_TEST_KILL_AFTER_MUTATION="worktree-create",
                )
                self.assertLess(interrupted.returncode, 0)

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                    AFK_FAKE_WORKTREE_RECORD_METADATA=metadata,
                )

                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assert_worktree_effect(
                    RunStore(state_home / "afk"), run_id, state_home=state_home
                )
                self.assertEqual(
                    self.mutation_count("worktree-create", state_home=state_home), 1
                )

    def test_resume_preserves_replaced_worktree_parent(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()
        interrupted = self.run_afk(
            "_worker", run_id, AFK_TEST_KILL_BEFORE_EFFECT="worktree-create"
        )
        self.assertLess(interrupted.returncode, 0)
        parent = self.state_home / "afk" / "worktrees"
        replacement = self.state_home / "user-directory"
        replacement.mkdir()
        parent.symlink_to(replacement, target_is_directory=True)

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        self.assertEqual(list(replacement.iterdir()), [])
        projection = RunStore(self.state_home / "afk").status(run_id)
        self.assertEqual(projection["attention"]["scope"], "worktree")
        self.assertEqual(self.mutation_count("worktree-create"), 0)

    def test_resume_preserves_user_owned_path_at_run_worktree_location(self):
        cases = {"directory": False, "symlink": True}
        for name, symlink in cases.items():
            with self.subTest(case=name):
                state_home = self.temp / f"worktree-replacement-{name}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    AFK_TEST_KILL_BEFORE_MUTATION="worktree-create",
                )
                self.assertLess(interrupted.returncode, 0)
                worktree = state_home / "afk" / "worktrees" / run_id
                if symlink:
                    replacement = state_home / "user-worktree"
                    replacement.mkdir()
                    (replacement / "owned").write_text("user", encoding="utf-8")
                    worktree.symlink_to(replacement, target_is_directory=True)
                else:
                    worktree.mkdir()
                    (worktree / "owned").write_text("user", encoding="utf-8")

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                )

                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                self.assertEqual(
                    (worktree / "owned").read_text(encoding="utf-8"), "user"
                )
                self.assertEqual(
                    self.mutation_count("worktree-create", state_home=state_home), 0
                )

    def test_resume_rejects_dirty_misbound_or_unregistered_created_worktree(self):
        cases = {
            "dirty": {"AFK_FAKE_DIRTY_WORKTREE": "1"},
            "wrong-head": {"AFK_FAKE_WRONG_WORKTREE_HEAD": "1"},
            "wrong-branch": {"AFK_FAKE_WRONG_WORKTREE_BRANCH": "1"},
            "unregistered": {"AFK_FAKE_UNREGISTERED_WORKTREE": "1"},
        }
        for name, observation in cases.items():
            with self.subTest(case=name):
                state_home = self.temp / f"worktree-created-{name}"
                run_id = self.run_afk(
                    "start",
                    "central-bnkl.1.1",
                    XDG_STATE_HOME=str(state_home),
                ).stdout.strip()
                interrupted = self.run_afk(
                    "_worker",
                    run_id,
                    XDG_STATE_HOME=str(state_home),
                    AFK_TEST_KILL_AFTER_MUTATION="worktree-create",
                )
                self.assertLess(interrupted.returncode, 0)

                resumed = self.run_afk(
                    "resume",
                    XDG_STATE_HOME=str(state_home),
                    AFK_FAKE_SYSTEMD_STATE="absent",
                    **observation,
                )

                self.assertEqual(resumed.returncode, 2, resumed.stderr)
                store = RunStore(state_home / "afk")
                self.assert_worktree_effect(
                    store, run_id, status="prepared", state_home=state_home
                )
                projection = store.status(run_id)
                self.assertEqual(projection["checkpoint"], "claimed")
                self.assertEqual(projection["attention"]["scope"], "worktree")
                self.assertEqual(
                    self.mutation_count("worktree-create", state_home=state_home), 1
                )

    def test_resume_restores_worktree_projection_after_observation_recovers(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()
        interrupted = self.run_afk(
            "_worker", run_id, AFK_TEST_KILL_AFTER_EVENT_WRITE="worktree.ready"
        )
        self.assertLess(interrupted.returncode, 0)

        unavailable = self.run_afk(
            "resume",
            AFK_FAKE_SYSTEMD_STATE="absent",
            AFK_FAKE_WORKTREE_SCENARIO="list-failure",
        )
        store = RunStore(self.state_home / "afk")
        paused = store.status(run_id)
        self.assertEqual(unavailable.returncode, 2, unavailable.stderr)
        self.assertEqual(paused["checkpoint"], "worktree_ready")
        self.assertEqual(paused["attention"]["scope"], "worktree")

        recovered = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        projection = store.status(run_id)
        self.assertEqual(projection["checkpoint"], "worktree_ready")
        self.assertEqual(projection["attention"], {})
        self.assertEqual(len(self.launch_events(run_id, "worktree.ready")), 1)
        self.assertEqual(len(self.launch_events(run_id, "worktree.reconciled")), 1)
        self.assertEqual(self.mutation_count("worktree-create"), 1)

    def test_resume_refuses_durable_worktree_without_confirmed_effect(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()
        interrupted = self.run_afk(
            "_worker", run_id, AFK_TEST_KILL_AFTER_EVENT_WRITE="worktree.ready"
        )
        self.assertLess(interrupted.returncode, 0)
        effect_path = (
            self.state_home
            / "afk"
            / "runs"
            / run_id
            / "effects"
            / "worktree-create.json"
        )
        effect_path.unlink()

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        self.assertFalse(effect_path.exists())
        projection = RunStore(self.state_home / "afk").status(run_id)
        self.assertEqual(projection["checkpoint"], "worktree_ready")
        self.assertEqual(projection["attention"]["scope"], "worktree")
        self.assertEqual(self.mutation_count("worktree-create"), 1)

    def test_resume_recovers_worktree_created_by_failing_command(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        failed = self.run_afk(
            "_worker", run_id, AFK_FAKE_WORKTREE_ADD_FAILS_AFTER_MUTATION="1"
        )

        self.assertEqual(failed.returncode, 2, failed.stderr)
        store = RunStore(self.state_home / "afk")
        self.assert_worktree_effect(store, run_id, status="prepared")
        self.assertEqual(self.mutation_count("worktree-create"), 1)

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")
        repeated = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="absent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assert_worktree_effect(store, run_id)
        self.assertEqual(store.status(run_id)["checkpoint"], "worktree_ready")
        self.assertEqual(self.mutation_count("worktree-create"), 1)

    def test_worker_claims_publishes_validates_and_reviews_the_exact_candidate(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()

        completed = self.run_afk("_worker", run_id)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        projection = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(projection["state"], "reviewed")
        self.assertEqual(projection["checkpoint"], "reviewed")
        self.assertEqual(projection["validation"]["status"], "passed")
        self.assertEqual(projection["candidate_sha"], "d" * 40)
        self.assertEqual(projection["pr_number"], 17)
        self.assertEqual(
            projection["branch"],
            f"afk/central-bnkl-1-1-{run_id}/candidate",
        )
        self.assertTrue(Path(projection["worktree_path"]).is_dir())
        effect = RunStore(self.state_home / "afk").effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "confirmed")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn(
            '"command":"bd","args":["update","central-bnkl.1.1","--claim"', commands
        )
        self.assertIn(BASE_SHA, commands)

    def test_resume_marks_the_exact_reviewed_candidate_pr_ready_idempotently(self):
        run_id = self.start_reviewed_run()

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(resumed.stdout.strip(), run_id)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "reviewed")
        self.assertEqual(
            status["pr_ready"],
            {
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "base": "main",
                "draft": False,
            },
        )
        effect = RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["observed"], status["pr_ready"])

        repeated = self.run_afk("resume")

        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        ready_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "ready"]
        ]
        self.assertEqual(len(ready_commands), 1)

    def test_resume_recovers_crash_before_ready_state_append(self):
        run_id = self.start_reviewed_run()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_BEFORE_EVENT="pr.marked_ready"
        )

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "reviewed")
        self.assertNotIn("pr_ready", before)
        store = RunStore(self.state_home / "afk")
        effect = store.effect(run_id, "pr-mark-ready")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "base": "main",
                "draft": False,
            },
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "reviewed")
        self.assertEqual(after["pr_ready"], effect["observed"])
        self.assertEqual(self.command_count("gh", ["pr", "ready"]), 1)

    def test_resume_recovers_process_crash_before_pr_ready_mutation(self):
        run_id = self.start_reviewed_run()

        interrupted = self.run_afk("resume", AFK_TEST_KILL_BEFORE_MUTATION="pr-ready")

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("pr-ready"), 0)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        effect = RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["observed"], status["pr_ready"])
        self.assertEqual(self.mutation_count("pr-ready"), 1)

    def test_resume_reconciles_process_crash_after_pr_ready_mutation(self):
        run_id = self.start_reviewed_run()

        interrupted = self.run_afk("resume", AFK_TEST_KILL_AFTER_MUTATION="pr-ready")

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("pr-ready"), 1)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        effect = RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["observed"], status["pr_ready"])
        self.assertEqual(self.mutation_count("pr-ready"), 1)

    def test_resume_requires_attention_when_crashed_pr_ready_is_ambiguous(self):
        run_id = self.start_reviewed_run()
        interrupted = self.run_afk("resume", AFK_TEST_KILL_AFTER_MUTATION="pr-ready")
        self.assertLess(interrupted.returncode, 0)

        ambiguous = self.run_afk("resume", AFK_FAKE_READY_PR_UNAVAILABLE="1")

        self.assertEqual(ambiguous.returncode, 2, ambiguous.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "publication")
        self.assertEqual(status["attention"]["kind"], "inconclusive")
        self.assertEqual(self.mutation_count("pr-ready"), 1)

    def test_resume_recovers_crash_after_ready_state_append(self):
        run_id = self.start_reviewed_run()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_EVENT="pr.marked_ready"
        )

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "reviewed")
        effect = RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "base": "main",
                "draft": False,
            },
        )
        self.assertEqual(before["pr_ready"], effect["observed"])

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "merged")
        self.assertEqual(after["pr_ready"], effect["observed"])
        self.assertEqual(self.command_count("gh", ["pr", "ready"]), 1)
        self.assertEqual(self.command_count("gh", ["pr", "merge"]), 1)

    def test_resume_squash_merges_the_exact_ready_candidate(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume")

        self.assertEqual(merged.returncode, 0, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "merged")
        self.assertEqual(
            status["merge"],
            {
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "base": "main",
                "merge_commit": "f" * 40,
            },
        )
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "confirmed")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "confirmed"
        )
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        merge_commands = [
            record["args"]
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(
            merge_commands,
            [
                [
                    "pr",
                    "merge",
                    "17",
                    "--repo",
                    "thunderbump/beads-webui",
                    "--squash",
                    "--delete-branch",
                    "--match-head-commit",
                    "d" * 40,
                ]
            ],
        )
        api_commands = [
            record["args"]
            for record in commands
            if record["command"] == "gh"
            and record["args"][:1] == ["api"]
            and "/git/commits/" in record["args"][1]
        ]
        self.assertEqual(
            api_commands,
            [
                [
                    "api",
                    "repos/thunderbump/beads-webui/git/commits/" + "d" * 40,
                    "--method",
                    "GET",
                ],
                [
                    "api",
                    "repos/thunderbump/beads-webui/git/commits/" + "f" * 40,
                    "--method",
                    "GET",
                ],
            ],
        )

    def test_resume_reconciles_interruption_after_squash_merge(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        interrupted = self.run_afk("resume", AFK_FAKE_PR_MERGE_INTERRUPTED="1")

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "prepared")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "merged")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "confirmed")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "confirmed"
        )
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)

    def test_resume_recovers_crash_before_merged_state_append(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_BEFORE_EVENT="pr.squash_merged"
        )

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "reviewed")
        self.assertNotIn("merge", before)
        store = RunStore(self.state_home / "afk")
        effect = store.effect(run_id, "pr-squash-merge")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "base": "main",
                "merge_commit": "f" * 40,
            },
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "merged")
        self.assertEqual(after["merge"], effect["observed"])
        self.assertEqual(self.command_count("gh", ["pr", "merge"]), 1)

    def test_resume_recovers_crash_after_merged_state_append(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_EVENT="pr.squash_merged"
        )

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")
        effect = RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "base": "main",
                "merge_commit": "f" * 40,
            },
        )
        self.assertEqual(before["merge"], effect["observed"])

        closed = self.run_afk("resume")
        completed = self.run_afk("resume")

        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.command_count("gh", ["pr", "merge"]), 1)
        self.assertEqual(self.command_count("bd", ["close"]), 1)

    def test_resume_recovers_process_crash_before_pr_merge_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_TEST_KILL_BEFORE_MUTATION="pr-merge")

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("pr-merge"), 0)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        effect = RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["observed"], status["merge"])
        self.assertEqual(self.mutation_count("pr-merge"), 1)

    def test_resume_reconciles_process_crash_after_pr_merge_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_TEST_KILL_AFTER_MUTATION="pr-merge")

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("pr-merge"), 1)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        effect = RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["observed"], status["merge"])
        self.assertEqual(self.mutation_count("pr-merge"), 1)
        self.assertEqual(self.command_count("gh", ["pr", "merge"]), 1)

    def test_resume_closes_bead_after_merged_candidate_branch_was_replaced(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_REPLACED_REMOTE_BRANCH="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "merged")
        self.assertEqual(status["merge"]["candidate_sha"], "d" * 40)
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)
        self.assertEqual(status["attention"]["scope"], "merge")
        self.assertEqual(status["attention"]["kind"], "conflict")
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "confirmed")
        deletion = store.effect(run_id, "remote-branch-delete")
        self.assertEqual(deletion["status"], "prepared")
        self.assertNotIn("observed", deletion)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "bead_closed")
        self.assertEqual(status["checkpoint"], "bead_closed")
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "confirmed")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            len(
                [
                    record
                    for record in commands
                    if record["command"] == "gh"
                    and record["args"][:2] == ["pr", "merge"]
                ]
            ),
            1,
        )

    def test_resume_defers_remote_cleanup_after_bead_close(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        conflicted = self.run_afk("resume", AFK_FAKE_REPLACED_REMOTE_BRANCH="1")
        self.assertEqual(conflicted.returncode, 2, conflicted.stderr)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")
        self.assertEqual(before["remote_branch_deleted"], False)
        (self.state_home / "fake-remote-replaced").unlink()

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "bead_closed")
        self.assertEqual(status["checkpoint"], "bead_closed")
        self.assertEqual(status["attention"], {})
        self.assertEqual(status["remote_branch_deleted"], False)
        self.assertEqual(status["last_event"], "bead.closed")
        self.assertEqual(status["last_sequence"], before["last_sequence"] + 1)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )

        repeated = self.run_afk("resume")

        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        repeated_status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(repeated_status["state"], "completed")
        self.assertEqual(repeated_status["last_sequence"], status["last_sequence"] + 1)
        self.assertTrue(repeated_status["completion"]["remote_branch_deleted"])
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)

    def test_resume_closes_exact_bead_after_confirmed_merge(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        closed = self.run_afk("resume")

        self.assertEqual(closed.returncode, 0, closed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "bead_closed")
        self.assertEqual(status["checkpoint"], "bead_closed")
        self.assertEqual(
            status["bead_closure"],
            {
                "bead_id": "central-bnkl.1.1",
                "repository": "thunderbump/beads-webui",
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "merge_commit": "f" * 40,
                "status": "closed",
                "close_reason": "merged via " + "f" * 40,
            },
        )
        store = RunStore(self.state_home / "afk")
        effect = store.effect(run_id, "bead-close")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["intended"],
            {
                "bead_id": "central-bnkl.1.1",
                "repository": "thunderbump/beads-webui",
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "merge_commit": "f" * 40,
                "reason": "merged via " + "f" * 40,
            },
        )
        self.assertEqual(effect["observed"], status["bead_closure"])

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        repeated_status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(repeated_status["state"], "completed")
        self.assertEqual(repeated_status["checkpoint"], "completed")
        self.assertEqual(repeated_status["completion"]["candidate_sha"], "d" * 40)
        self.assertEqual(repeated_status["completion"]["merge_commit"], "f" * 40)
        self.assertEqual(
            repeated_status["completion"]["bead_closure"], status["bead_closure"]
        )
        self.assertEqual(repeated_status["completion"]["cleanup_warnings"], [])
        self.assertFalse((self.state_home / "afk" / "worktrees" / run_id).exists())
        self.assertFalse((self.state_home / "afk" / "active.json").exists())
        evidence = (
            self.state_home
            / "afk"
            / "runs"
            / run_id
            / repeated_status["completion"]["evidence"]
        )
        self.assertTrue((evidence / "manifest.json").is_file())
        reported = self.run_afk("complete", run_id)
        self.assertEqual(reported.returncode, 0, reported.stderr)
        self.assertTrue(json.loads(reported.stdout)["complete"])
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn(
            [
                "close",
                "central-bnkl.1.1",
                "--reason",
                "merged via " + "f" * 40,
                "--json",
            ],
            [record["args"] for record in commands if record["command"] == "bd"],
        )
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)
        close_commands = [
            record
            for record in commands
            if record["command"] == "bd" and record["args"][:1] == ["close"]
        ]
        self.assertEqual(len(close_commands), 1)

    def test_terminal_cleanup_retries_exact_lingering_remote_branch(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(
            self.run_afk("resume", AFK_FAKE_REMOTE_BRANCH_LINGERS="1").returncode,
            0,
        )
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertTrue(status["completion"]["remote_branch_deleted"])
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "confirmed"
        )
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        deletes = [
            record
            for record in commands
            if record["command"] == "git" and "--delete" in record["args"]
        ]
        self.assertEqual(len(deletes), 1)
        self.assertIn(
            "afk/central-bnkl-1-1-" + run_id + "/candidate",
            deletes[0]["args"],
        )

    def test_terminal_cleanup_retries_exact_branch_recreated_after_confirmation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "confirmed"
        )
        (self.state_home / "fake-remote-deleted").unlink()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertTrue(status["completion"]["remote_branch_deleted"])
        self.assertEqual(status["completion"]["cleanup_warnings"], [])
        deletes = [
            record
            for record in map(
                json.loads,
                self.command_log.read_text(encoding="utf-8").splitlines(),
            )
            if record["command"] == "git" and "--delete" in record["args"]
        ]
        self.assertEqual(len(deletes), 1)
        self.assertIn(
            "--force-with-lease=refs/heads/afk/central-bnkl-1-1-"
            + run_id
            + "/candidate:"
            + "d" * 40,
            deletes[0]["args"],
        )

    def test_terminal_cleanup_recovers_process_crash_before_remote_delete(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_BEFORE_MUTATION="remote-branch-delete"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("remote-branch-delete"), 0)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("remote-branch-delete"), 1)

    def test_terminal_cleanup_reconciles_process_crash_after_remote_delete(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_MUTATION="remote-branch-delete"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("remote-branch-delete"), 1)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("remote-branch-delete"), 1)
        self.assertEqual(self.command_count("git", ["push"]), 2)

    def test_terminal_cleanup_recovers_process_crash_before_worktree_move(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_BEFORE_MUTATION="worktree-move"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("worktree-move"), 0)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("remote-branch-delete"), 1)
        self.assertEqual(self.mutation_count("worktree-move"), 1)

    def test_terminal_cleanup_reconciles_process_crash_after_worktree_move(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_MUTATION="worktree-move"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("worktree-move"), 1)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("worktree-move"), 1)
        self.assertEqual(self.command_count("git", ["worktree", "move"]), 1)

    def test_terminal_cleanup_recovers_process_crash_before_worktree_remove(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_BEFORE_MUTATION="worktree-remove"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("worktree-move"), 1)
        self.assertEqual(self.mutation_count("worktree-remove"), 0)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("worktree-move"), 1)
        self.assertEqual(self.mutation_count("worktree-remove"), 1)

    def test_terminal_cleanup_reconciles_process_crash_after_worktree_remove(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_MUTATION="worktree-remove"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("worktree-remove"), 1)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("worktree-move"), 1)
        self.assertEqual(self.mutation_count("worktree-remove"), 1)
        self.assertEqual(self.command_count("git", ["worktree", "remove"]), 1)

    def test_terminal_cleanup_recovers_process_crash_before_local_branch_delete(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_BEFORE_MUTATION="local-branch-delete"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("worktree-remove"), 1)
        self.assertEqual(self.mutation_count("local-branch-delete"), 0)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("worktree-move"), 1)
        self.assertEqual(self.mutation_count("worktree-remove"), 1)
        self.assertEqual(self.mutation_count("local-branch-delete"), 1)

    def test_terminal_cleanup_reconciles_process_crash_after_local_branch_delete(self):
        run_id = self.start_bead_closed_run_with_lingering_remote()

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_MUTATION="local-branch-delete"
        )

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("local-branch-delete"), 1)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.mutation_count("worktree-move"), 1)
        self.assertEqual(self.mutation_count("worktree-remove"), 1)
        self.assertEqual(self.mutation_count("local-branch-delete"), 1)
        self.assertEqual(self.command_count("git", ["update-ref", "-d"]), 1)

    def test_terminal_cleanup_reconciles_confirmed_remote_after_worktree_is_gone(
        self,
    ):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "confirmed"
        )
        worktree = self.state_home / "afk" / "worktrees" / run_id
        (self.state_home / "fake-worktree-removed").write_text(
            "removed", encoding="utf-8"
        )
        shutil.rmtree(worktree)

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertTrue(status["completion"]["remote_branch_deleted"])
        self.assertEqual(status["completion"]["cleanup_warnings"], [])

    def test_terminal_cleanup_retries_lingering_remote_after_worktree_is_gone(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(
            self.run_afk("resume", AFK_FAKE_REMOTE_BRANCH_LINGERS="1").returncode,
            0,
        )
        self.assertEqual(self.run_afk("resume").returncode, 0)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )
        worktree = self.state_home / "afk" / "worktrees" / run_id
        (self.state_home / "fake-worktree-removed").write_text(
            "removed", encoding="utf-8"
        )
        shutil.rmtree(worktree)

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertTrue(status["completion"]["remote_branch_deleted"])
        self.assertEqual(status["completion"]["cleanup_warnings"], [])
        deletes = [
            record
            for record in map(
                json.loads,
                self.command_log.read_text(encoding="utf-8").splitlines(),
            )
            if record["command"] == "git" and "--delete" in record["args"]
        ]
        self.assertEqual(len(deletes), 1)
        self.assertIn(
            "--force-with-lease=refs/heads/afk/central-bnkl-1-1-"
            + run_id
            + "/candidate:"
            + "d" * 40,
            deletes[0]["args"],
        )

    def test_terminal_cleanup_does_not_delete_branch_replaced_during_delete(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(
            self.run_afk("resume", AFK_FAKE_REMOTE_BRANCH_LINGERS="1").returncode,
            0,
        )
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk("resume", AFK_FAKE_REMOTE_MOVES_DURING_DELETE="1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertFalse(status["completion"]["remote_branch_deleted"])
        self.assertIn(
            "remote Candidate branch cleanup could not be confirmed",
            status["completion"]["cleanup_warnings"],
        )
        self.assertFalse((self.state_home / "fake-remote-deleted").exists())
        deletes = [
            record
            for record in map(
                json.loads,
                self.command_log.read_text(encoding="utf-8").splitlines(),
            )
            if record["command"] == "git" and "--delete" in record["args"]
        ]
        self.assertEqual(len(deletes), 1)
        self.assertIn(
            "--force-with-lease=refs/heads/afk/central-bnkl-1-1-"
            + run_id
            + "/candidate:"
            + "d" * 40,
            deletes[0]["args"],
        )

    def test_terminal_cleanup_accepts_branch_disappearing_during_delete(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(
            self.run_afk("resume", AFK_FAKE_REMOTE_BRANCH_LINGERS="1").returncode,
            0,
        )
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk("resume", AFK_FAKE_REMOTE_DISAPPEARS_DURING_DELETE="1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertTrue(status["completion"]["remote_branch_deleted"])
        self.assertEqual(status["completion"]["cleanup_warnings"], [])
        self.assertEqual(
            RunStore(self.state_home / "afk").effect(run_id, "remote-branch-delete")[
                "status"
            ],
            "confirmed",
        )

    def test_terminal_cleanup_reconciles_remote_delete_timeout_after_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(
            self.run_afk("resume", AFK_FAKE_REMOTE_BRANCH_LINGERS="1").returncode,
            0,
        )
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk(
            "resume",
            AFK_FAKE_REMOTE_DELETE_TIMES_OUT_AFTER_MUTATION="1",
            AFK_TEST_SHORT_CLEANUP_TIMEOUT="1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertTrue(status["completion"]["remote_branch_deleted"])
        self.assertNotIn(
            "remote Candidate branch cleanup could not be confirmed",
            status["completion"]["cleanup_warnings"],
        )

    def test_terminal_cleanup_warns_and_completes_without_unsafe_removal(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        store = RunStore(self.state_home / "afk")
        store.append_event(
            run_id,
            "test.worktree_identity_changed",
            data={"worktree_path": str(self.temp / "not-afk-owned")},
        )

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)
        self.assertEqual(status["bead_closure"]["status"], "closed")
        self.assertTrue(status["completion"]["cleanup_warnings"])
        self.assertTrue(
            any(
                "worktree identity" in warning
                for warning in status["completion"]["cleanup_warnings"]
            ),
            status["completion"]["cleanup_warnings"],
        )
        self.assertTrue((self.state_home / "afk" / "worktrees" / run_id).exists())
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["worktree","remove"', commands)
        self.assertNotIn('"args":["branch","-D"', commands)

    def test_terminal_cleanup_does_not_delete_local_branch_replaced_at_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk(
            "resume", AFK_FAKE_LOCAL_BRANCH_MOVES_DURING_DELETE="1"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertFalse(status["completion"]["local_branch_deleted"])
        self.assertIn(
            "Run branch cleanup failed",
            status["completion"]["cleanup_warnings"],
        )
        self.assertTrue((self.state_home / "fake-local-branch-replaced").exists())
        self.assertFalse((self.state_home / "fake-local-branch-removed").exists())
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        branch_ref = "refs/heads/afk/central-bnkl-1-1-" + run_id + "/candidate"
        self.assertIn(
            ["update-ref", "-d", branch_ref, "d" * 40],
            [record["args"] for record in commands if record["command"] == "git"],
        )
        self.assertNotIn(
            ["branch", "-D", branch_ref.removeprefix("refs/heads/")],
            [record["args"] for record in commands if record["command"] == "git"],
        )

    def test_terminal_cleanup_reconciles_local_branch_timeout_after_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk(
            "resume",
            AFK_FAKE_LOCAL_BRANCH_DELETE_TIMES_OUT_AFTER_MUTATION="1",
            AFK_TEST_SHORT_CLEANUP_TIMEOUT="1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertTrue(status["completion"]["worktree_removed"])
        self.assertTrue(status["completion"]["local_branch_deleted"])
        self.assertEqual(status["completion"]["cleanup_warnings"], [])
        self.assertTrue((self.state_home / "fake-local-branch-removed").exists())

    def test_terminal_cleanup_preserves_replacement_at_manifest_path(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk("resume", AFK_FAKE_WORKTREE_MOVES_DURING_REMOVE="1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertTrue(status["completion"]["worktree_removed"])
        self.assertNotIn(
            "Run worktree cleanup failed",
            status["completion"]["cleanup_warnings"],
        )
        self.assertTrue((self.state_home / "fake-worktree-replaced").exists())
        replacement = self.state_home / "afk" / "worktrees" / run_id
        self.assertEqual(
            (replacement / "not-afk-owned").read_text(encoding="utf-8"),
            "replacement",
        )
        git_commands = [
            json.loads(line)["args"]
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["command"] == "git"
        ]
        quarantine = self.state_home / "afk" / "worktree-quarantine" / run_id
        self.assertIn(
            ["worktree", "move", str(replacement), str(quarantine)], git_commands
        )
        self.assertIn(["worktree", "remove", str(quarantine)], git_commands)
        self.assertNotIn(["worktree", "remove", str(replacement)], git_commands)

    def test_terminal_cleanup_recovers_quarantine_without_git_lockfiles(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk(
            "resume", AFK_FAKE_TERMINAL_CLEANUP_INTERRUPTS_AFTER_MOVE="1"
        )

        self.assertLess(interrupted.returncode, 0)
        quarantine = self.state_home / "afk" / "worktree-quarantine" / run_id
        self.assertTrue(quarantine.is_dir())
        git_state = self.state_home / "fake-git"
        self.assertEqual(list(git_state.rglob("*.lock")), [])

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertTrue(status["completion"]["worktree_removed"])
        self.assertTrue(status["completion"]["local_branch_deleted"])
        self.assertFalse(quarantine.exists())
        self.assertEqual(list(git_state.rglob("*.lock")), [])

    def test_terminal_cleanup_preserves_unregistered_worktree_and_local_branch(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        worktree = self.state_home / "afk" / "worktrees" / run_id

        completed = self.run_afk("resume", AFK_FAKE_UNREGISTERED_WORKTREE="1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertFalse(status["completion"]["worktree_removed"])
        self.assertFalse(status["completion"]["local_branch_deleted"])
        self.assertIn(
            "Run worktree ownership could not be verified; cleanup skipped",
            status["completion"]["cleanup_warnings"],
        )
        self.assertTrue(worktree.is_dir())
        self.assertFalse((self.state_home / "fake-local-branch-removed").exists())

    def test_terminal_cleanup_reconciles_worktree_move_timeout_after_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk(
            "resume",
            AFK_FAKE_WORKTREE_MOVE_TIMES_OUT_AFTER_MUTATION="1",
            AFK_TEST_SHORT_CLEANUP_TIMEOUT="1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertTrue(status["completion"]["worktree_removed"])
        self.assertNotIn(
            "Run worktree cleanup failed", status["completion"]["cleanup_warnings"]
        )

    def test_terminal_cleanup_reconciles_worktree_remove_timeout_after_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk(
            "resume",
            AFK_FAKE_WORKTREE_REMOVE_TIMES_OUT_AFTER_MUTATION="1",
            AFK_TEST_SHORT_CLEANUP_TIMEOUT="1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertTrue(status["completion"]["worktree_removed"])
        self.assertNotIn(
            "Run worktree cleanup failed", status["completion"]["cleanup_warnings"]
        )

    def test_terminal_cleanup_failures_are_durable_warnings(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(
            self.run_afk("resume", AFK_FAKE_REMOTE_BRANCH_LINGERS="1").returncode,
            0,
        )
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk(
            "resume",
            AFK_FAKE_REMOTE_DELETE_FAILURE="1",
            AFK_FAKE_WORKTREE_REMOVE_FAILURE="1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        completion = status["completion"]
        self.assertFalse(completion["remote_branch_deleted"])
        self.assertFalse(completion["worktree_removed"])
        self.assertFalse(completion["local_branch_deleted"])
        self.assertEqual(len(completion["cleanup_warnings"]), 3)
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)
        self.assertEqual(status["bead_closure"]["status"], "closed")

    def test_terminal_cleanup_command_exception_is_a_durable_warning(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        completed = self.run_afk(
            "resume", AFK_FAKE_REPOSITORY_DISAPPEARS_DURING_CLEANUP="1"
        )
        self.project.mkdir()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(
            status["completion"]["cleanup_warnings"],
            ["Run worktree cleanup could not be inspected; cleanup skipped"],
        )
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)
        self.assertEqual(status["bead_closure"]["status"], "closed")

    def test_terminal_cleanup_filesystem_exception_is_a_durable_warning(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        (self.state_home / "afk" / "worktree-quarantine").write_text(
            "not a directory", encoding="utf-8"
        )

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(
            status["completion"]["cleanup_warnings"],
            ["Run worktree cleanup could not be inspected; cleanup skipped"],
        )
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)
        self.assertEqual(status["bead_closure"]["status"], "closed")

    def test_terminal_cleanup_recovers_sealed_completion_before_rechecking_cleanup(
        self,
    ):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        evidence = f"gates/completion-{before['candidate_sha'][:12]}"
        sealed_completion = {
            "schema_version": 1,
            "repository": "thunderbump/beads-webui",
            "bead_id": "central-bnkl.1.1",
            "candidate_sha": before["candidate_sha"],
            "pr_number": before["merge"]["number"],
            "pr_url": before["merge"]["url"],
            "merge_commit": before["merge"]["merge_commit"],
            "bead_closure": before["bead_closure"],
            "remote_branch_deleted": False,
            "worktree_removed": False,
            "local_branch_deleted": False,
            "cleanup_warnings": ["cleanup was interrupted"],
            "evidence": evidence,
        }
        store = RunStore(self.state_home / "afk")
        store.reconcile_evidence_result(run_id, evidence, sealed_completion)
        commands_before = self.command_log.read_text(encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["completion"], sealed_completion)
        self.assertTrue((self.state_home / "afk" / "worktrees" / run_id).exists())
        self.assertEqual(self.command_log.read_text(encoding="utf-8"), commands_before)

    def test_terminal_cleanup_recovers_unsealed_completion_before_rechecking_cleanup(
        self,
    ):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        evidence = f"gates/completion-{before['candidate_sha'][:12]}"
        interrupted_completion = {
            "schema_version": 1,
            "repository": "thunderbump/beads-webui",
            "bead_id": "central-bnkl.1.1",
            "candidate_sha": before["candidate_sha"],
            "pr_number": before["merge"]["number"],
            "pr_url": before["merge"]["url"],
            "merge_commit": before["merge"]["merge_commit"],
            "bead_closure": before["bead_closure"],
            "remote_branch_deleted": False,
            "worktree_removed": False,
            "local_branch_deleted": False,
            "cleanup_warnings": ["cleanup was interrupted"],
            "evidence": evidence,
        }
        store = RunStore(self.state_home / "afk")
        store.write_evidence_value(
            run_id, f"{evidence}/result.json", interrupted_completion
        )
        commands_before = self.command_log.read_text(encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["completion"], interrupted_completion)
        self.assertTrue(
            (
                self.state_home / "afk" / "runs" / run_id / evidence / "manifest.json"
            ).is_file()
        )
        self.assertTrue((self.state_home / "afk" / "worktrees" / run_id).exists())
        self.assertEqual(self.command_log.read_text(encoding="utf-8"), commands_before)

    def test_resume_recovers_crash_before_completed_state_append(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_TEST_KILL_BEFORE_EVENT="run.completed")

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "bead_closed")
        evidence = f"gates/completion-{before['candidate_sha'][:12]}"
        self.assertEqual(evidence, "gates/completion-dddddddddddd")
        store = RunStore(self.state_home / "afk")
        sealed_completion = store.sealed_evidence_result(run_id, evidence)
        self.assertIsNotNone(sealed_completion)
        commands_before = self.command_log.read_text(encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "completed")
        self.assertEqual(after["completion"], sealed_completion)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.command_log.read_text(encoding="utf-8"), commands_before)

    def test_resume_recovers_crash_after_completed_event_write(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_EVENT_WRITE="run.completed"
        )

        self.assertLess(interrupted.returncode, 0)
        completed = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(completed["checkpoint"], "completed")
        store = RunStore(self.state_home / "afk")
        sealed_completion = store.sealed_evidence_result(
            run_id, "gates/completion-dddddddddddd"
        )
        self.assertEqual(completed["completion"], sealed_completion)
        self.assert_exact_terminal_completion(run_id)
        active = self.state_home / "afk" / "active.json"
        self.assertTrue(active.is_file())

        resumed = self.run_afk("resume", run_id)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(resumed.stdout.strip(), run_id)
        self.assertFalse(active.exists())
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["completion"], sealed_completion)
        events = (self.state_home / "afk" / "runs" / run_id / "events.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertEqual(events.count('"event":"run.completed"'), 1)

    def test_named_resume_of_retained_completed_run_is_idempotent(self):
        run_id = self.start_reviewed_run()
        for _ in range(4):
            self.assertEqual(self.run_afk("resume").returncode, 0)
        active = self.state_home / "afk" / "active.json"
        self.assertFalse(active.exists())
        events_path = self.state_home / "afk" / "runs" / run_id / "events.jsonl"
        events_before = events_path.read_text(encoding="utf-8")
        self.assertEqual(events_before.count('"event":"run.completed"'), 1)

        first = self.run_afk("resume", run_id)
        second = self.run_afk("resume", run_id)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), run_id)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), run_id)
        self.assertFalse(active.exists())
        self.assertEqual(events_path.read_text(encoding="utf-8"), events_before)
        unnamed = self.run_afk("resume")
        self.assertEqual(unnamed.returncode, 2)
        self.assertIn("no Active Run", unnamed.stderr)

    def create_named_completed_resume_preflight_run(self):
        run_id = self.start_reviewed_run()
        for _ in range(4):
            self.assertEqual(self.run_afk("resume").returncode, 0)
        run_dir = self.state_home / "afk" / "runs" / run_id
        active = self.state_home / "afk" / "active.json"
        active.write_text(json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
        active.chmod(0o600)
        self.command_log.unlink(missing_ok=True)
        return run_id, run_dir, active

    def assert_named_completed_resume_preflight_rejected(self, run_id, active, message):
        active_before = active.read_text(encoding="utf-8")

        resumed = self.run_afk("resume", run_id)

        self.assertEqual(resumed.returncode, 2)
        self.assertIn(message, resumed.stderr)
        self.assertEqual(active.read_text(encoding="utf-8"), active_before)
        self.assertFalse(self.command_log.exists())

    def test_named_completed_resume_rejects_torn_event_tail_before_reconciliation(self):
        run_id, run_dir, active = self.create_named_completed_resume_preflight_run()
        with (run_dir / "events.jsonl").open("ab") as stream:
            stream.write(b'{"schema_version":1')

        self.assert_named_completed_resume_preflight_rejected(
            run_id, active, "Event History has an incomplete trailing record"
        )

    def test_named_completed_resume_rejects_invalid_effect_before_reconciliation(self):
        run_id, run_dir, active = self.create_named_completed_resume_preflight_run()
        effect_path = run_dir / "effects" / "worker-launch-1.json"
        effect = json.loads(effect_path.read_text(encoding="utf-8"))
        effect["status"] = "invalid"
        effect_path.write_text(json.dumps(effect) + "\n", encoding="utf-8")

        self.assert_named_completed_resume_preflight_rejected(
            run_id, active, "Effect is invalid: worker-launch-1"
        )

    def test_named_completed_resume_rejects_invalid_evidence_before_reconciliation(
        self,
    ):
        run_id, run_dir, active = self.create_named_completed_resume_preflight_run()
        result_path = run_dir / "gates" / "completion-dddddddddddd" / "result.json"
        result_path.chmod(0o600)
        result_path.write_text("{}\n", encoding="utf-8")

        self.assert_named_completed_resume_preflight_rejected(
            run_id, active, "evidence does not match its manifest"
        )

    def test_named_resume_does_not_advance_noncompleted_active_run(self):
        run_id = self.start_reviewed_run()

        named = self.run_afk("resume", run_id)

        self.assertEqual(named.returncode, 2)
        self.assertIn("only available for a completed Run", named.stderr)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "reviewed")

        unnamed = self.run_afk("resume")

        self.assertEqual(unnamed.returncode, 0, unnamed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["pr_ready"]["candidate_sha"], "d" * 40)

    def test_named_resume_of_completed_run_preserves_newer_active_run(self):
        completed_run_id = self.start_reviewed_run()
        for _ in range(4):
            self.assertEqual(self.run_afk("resume").returncode, 0)
        (self.state_home / "fake-bead-closed").unlink()
        started = self.run_afk(
            "start",
            "central-bnkl.1.2",
            AFK_FAKE_BEAD="central-bnkl.1.2",
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        active_run_id = started.stdout.strip()
        active = self.state_home / "afk" / "active.json"
        active_before = active.read_text(encoding="utf-8")

        resumed = self.run_afk("resume", completed_run_id)

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(resumed.stdout.strip(), completed_run_id)
        self.assertEqual(active.read_text(encoding="utf-8"), active_before)
        current = json.loads(self.run_afk("status", active_run_id, "--json").stdout)
        self.assertEqual(current["checkpoint"], "created")

    def test_resume_recovers_process_crash_before_bead_close_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_TEST_KILL_BEFORE_MUTATION="bead-close")

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("bead-close"), 0)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        effect = RunStore(self.state_home / "afk").effect(run_id, "bead-close")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["observed"], status["bead_closure"])
        self.assertEqual(self.mutation_count("bead-close"), 1)

    def test_resume_reconciles_process_crash_after_bead_close_mutation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_TEST_KILL_AFTER_MUTATION="bead-close")

        self.assertLess(interrupted.returncode, 0)
        self.assertEqual(self.mutation_count("bead-close"), 1)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        effect = RunStore(self.state_home / "afk").effect(run_id, "bead-close")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["observed"], status["bead_closure"])
        self.assertEqual(self.mutation_count("bead-close"), 1)
        self.assertEqual(self.command_count("bd", ["close"]), 1)

    def test_resume_reconciles_interruption_after_bead_close_without_second_close(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_FAKE_BEAD_CLOSE_INTERRUPTED="1")

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")
        self.assertEqual(before["merge"]["merge_commit"], "f" * 40)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "bead-close")["status"], "prepared")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "bead_closed")
        self.assertEqual(store.effect(run_id, "bead-close")["status"], "confirmed")
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        close_commands = [
            record
            for record in commands
            if record["command"] == "bd" and record["args"][:1] == ["close"]
        ]
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(close_commands), 1)
        self.assertEqual(len(merge_commands), 1)

    def test_resume_recovers_crash_before_bead_closed_state_append(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_TEST_KILL_BEFORE_EVENT="bead.closed")

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")
        self.assertNotIn("bead_closure", before)
        store = RunStore(self.state_home / "afk")
        effect = store.effect(run_id, "bead-close")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "bead_id": "central-bnkl.1.1",
                "repository": "thunderbump/beads-webui",
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "merge_commit": "f" * 40,
                "status": "closed",
                "close_reason": "merged via " + "f" * 40,
            },
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "bead_closed")
        self.assertEqual(after["bead_closure"], effect["observed"])
        self.assertEqual(self.command_count("bd", ["close"]), 1)

    def test_resume_recovers_crash_after_bead_closed_state_append(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk("resume", AFK_TEST_KILL_AFTER_EVENT="bead.closed")

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "bead_closed")
        effect = RunStore(self.state_home / "afk").effect(run_id, "bead-close")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "bead_id": "central-bnkl.1.1",
                "repository": "thunderbump/beads-webui",
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "merge_commit": "f" * 40,
                "status": "closed",
                "close_reason": "merged via " + "f" * 40,
            },
        )
        self.assertEqual(before["bead_closure"], effect["observed"])

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.command_count("bd", ["close"]), 1)

    def test_resume_retries_only_bead_close_after_close_failure(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        failed = self.run_afk("resume", AFK_FAKE_BEAD_CLOSE_FAILURE="1")

        self.assertEqual(failed.returncode, 2, failed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertEqual(status["attention"]["scope"], "bead_close")
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)

        retried = self.run_afk("resume")

        self.assertEqual(retried.returncode, 0, retried.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "bead_closed")
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)

    def test_resume_refuses_bead_closed_without_close_effect(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        resumed = self.run_afk("resume", AFK_FAKE_BEAD_STATUS="closed")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertIn("without AFK authorization", status["attention"]["summary"])
        store = RunStore(self.state_home / "afk")
        with self.assertRaises(RunStoreError):
            store.effect(run_id, "bead-close")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["close"', commands)

    def test_resume_refuses_cross_project_bead_before_close_effect(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        resumed = self.run_afk(
            "resume", AFK_FAKE_PROJECT_LABEL="project:another-repository"
        )

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertIn("facts disagree", status["attention"]["summary"])
        store = RunStore(self.state_home / "afk")
        with self.assertRaises(RunStoreError):
            store.effect(run_id, "bead-close")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["close"', commands)

    def test_resume_refuses_bead_project_change_after_effect_preparation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        resumed = self.run_afk(
            "resume", AFK_FAKE_BEAD_PROJECT_CHANGE_AFTER_FIRST_SHOW="1"
        )

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertIn("facts disagree", status["attention"]["summary"])
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "bead-close")["status"], "prepared")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["close"', commands)

    def test_resume_refuses_malformed_bead_close_observation(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        resumed = self.run_afk("resume", AFK_FAKE_BEAD_SHOW_MALFORMED="1")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertEqual(status["attention"]["classification"], "malformed_output")
        store = RunStore(self.state_home / "afk")
        with self.assertRaises(RunStoreError):
            store.effect(run_id, "bead-close")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["close"', commands)

    def test_resume_refuses_confirmed_close_effect_when_bead_is_open(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        store = RunStore(self.state_home / "afk")
        merge = store.status(run_id)["merge"]
        intended = {
            "bead_id": "central-bnkl.1.1",
            "repository": "thunderbump/beads-webui",
            "pr_number": 17,
            "pr_url": "https://example.test/pr/17",
            "candidate_sha": "d" * 40,
            "merge_commit": merge["merge_commit"],
            "reason": "merged via " + merge["merge_commit"],
        }
        observed = {key: value for key, value in intended.items() if key != "reason"}
        observed["status"] = "closed"
        observed["close_reason"] = intended["reason"]
        store.prepare_effect(run_id, "bead-close", kind="bead-close", intended=intended)
        store.confirm_effect(run_id, "bead-close", observed=observed)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertIn("contradicts live", status["attention"]["summary"])
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["close"', commands)

    def test_resume_refuses_prepared_close_effect_when_bead_has_wrong_reason(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)
        store = RunStore(self.state_home / "afk")
        merge = store.status(run_id)["merge"]
        intended = {
            "bead_id": "central-bnkl.1.1",
            "repository": "thunderbump/beads-webui",
            "pr_number": 17,
            "pr_url": "https://example.test/pr/17",
            "candidate_sha": "d" * 40,
            "merge_commit": merge["merge_commit"],
            "reason": "merged via " + merge["merge_commit"],
        }
        store.prepare_effect(run_id, "bead-close", kind="bead-close", intended=intended)

        for _ in range(2):
            resumed = self.run_afk(
                "resume",
                AFK_FAKE_BEAD_STATUS="closed",
                AFK_FAKE_BEAD_CLOSE_REASON="closed by an operator",
            )

            self.assertEqual(resumed.returncode, 2, resumed.stderr)
            status = json.loads(self.run_afk("status", run_id, "--json").stdout)
            self.assertEqual(status["checkpoint"], "merged")
            self.assertEqual(status["attention"]["scope"], "bead_close")
            self.assertEqual(store.effect(run_id, "bead-close")["status"], "prepared")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["close"', commands)

    def test_resume_closes_bead_without_reobserving_merged_pr(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        conflicted = self.run_afk("resume", AFK_FAKE_REPLACED_REMOTE_BRANCH="1")
        self.assertEqual(conflicted.returncode, 2, conflicted.stderr)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")

        unavailable = self.run_afk("resume", AFK_FAKE_MERGE_PR_UNAVAILABLE="1")

        self.assertEqual(unavailable.returncode, 0, unavailable.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "bead_closed")
        self.assertEqual(status["checkpoint"], "bead_closed")
        self.assertEqual(
            status["merge"],
            {
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "base": "main",
                "merge_commit": "f" * 40,
            },
        )
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "confirmed")
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)

    def test_resume_records_merge_before_remote_branch_observation_fails(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        interrupted = self.run_afk("resume", AFK_FAKE_POST_MERGE_REMOTE_UNAVAILABLE="1")

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "merged")
        self.assertEqual(status["merge"]["candidate_sha"], "d" * 40)
        self.assertEqual(status["merge"]["merge_commit"], "f" * 40)
        self.assertEqual(status["attention"]["scope"], "merge")
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "confirmed")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )

        resumed = self.run_afk("resume", AFK_FAKE_POST_MERGE_REMOTE_UNAVAILABLE="1")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "bead_closed")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)

    def test_resume_recovers_crash_before_remote_delete_state_append(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk(
            "resume",
            AFK_TEST_KILL_BEFORE_EVENT="pr.merge_reconciled",
        )

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")
        self.assertFalse(before["remote_branch_deleted"])
        store = RunStore(self.state_home / "afk")
        effect = store.effect(run_id, "remote-branch-delete")
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "repository": "thunderbump/beads-webui",
                "branch": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "deleted": True,
            },
        )

        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.command_count("git", [], argument="--delete"), 0)

    def test_resume_recovers_crash_after_remote_delete_state_append(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        interrupted = self.run_afk(
            "resume", AFK_TEST_KILL_AFTER_EVENT="pr.merge_reconciled"
        )

        self.assertLess(interrupted.returncode, 0)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")
        self.assertTrue(before["remote_branch_deleted"])
        effect = RunStore(self.state_home / "afk").effect(
            run_id, "remote-branch-delete"
        )
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(
            effect["observed"],
            {
                "repository": "thunderbump/beads-webui",
                "branch": f"afk/central-bnkl-1-1-{run_id}/candidate",
                "deleted": True,
            },
        )

        self.assertEqual(self.run_afk("resume").returncode, 0)
        self.assertEqual(self.run_afk("resume").returncode, 0)

        self.assert_exact_terminal_completion(run_id)
        self.assertEqual(self.command_count("gh", ["pr", "merge"]), 1)
        self.assertEqual(self.command_count("git", [], argument="--delete"), 0)

    def test_resume_closes_bead_without_reobserving_cleanup_origin(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        interrupted = self.run_afk("resume", AFK_FAKE_POST_MERGE_REMOTE_UNAVAILABLE="1")
        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)

        resumed = self.run_afk(
            "resume", AFK_FAKE_ORIGIN_REPOSITORY="thunderbump/another-repo"
        )

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "bead_closed")
        self.assertEqual(status["remote_branch_deleted"], False)
        self.assertEqual(status["attention"], {})
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )

    def test_resume_observes_branch_deletion_through_captured_origin_url(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk(
            "resume", AFK_FAKE_CLEANUP_ORIGIN_CHANGE_AFTER_GET_URL="1"
        )

        self.assertEqual(merged.returncode, 0, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertEqual(status["remote_branch_deleted"], False)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn(
            [
                "ls-remote",
                "git@github.com:thunderbump/beads-webui.git",
                f"refs/heads/afk/central-bnkl-1-1-{run_id}/candidate",
            ],
            [record["args"] for record in commands if record["command"] == "git"],
        )

    def test_resume_pauses_when_merged_pr_effect_identity_does_not_match(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        store = RunStore(self.state_home / "afk")
        status = store.status(run_id)
        store.prepare_effect(
            run_id,
            "pr-squash-merge",
            kind="pr-squash-merge",
            intended={"repository": "someone/else"},
        )
        store.prepare_effect(
            run_id,
            "remote-branch-delete",
            kind="remote-branch-delete",
            intended={
                "repository": "thunderbump/beads-webui",
                "branch": status["branch"],
                "candidate_sha": "d" * 40,
            },
        )

        resumed = self.run_afk("resume", AFK_FAKE_PR_MERGED="1")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_pauses_when_open_pr_merge_effect_is_already_confirmed(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        store = RunStore(self.state_home / "afk")
        identity = store.identity(run_id)
        status = store.status(run_id)
        store.prepare_effect(
            run_id,
            "pr-squash-merge",
            kind="pr-squash-merge",
            intended={
                "repository": identity["repository"],
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": status["branch"],
                "base": identity["base_branch"],
                "base_sha": identity["base_sha"],
                "strategy": "squash",
            },
        )
        store.confirm_effect(
            run_id,
            "pr-squash-merge",
            observed={
                "number": 17,
                "url": "https://example.test/pr/17",
                "candidate_sha": "d" * 40,
                "head": status["branch"],
                "base": identity["base_branch"],
                "merge_commit": "f" * 40,
            },
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_pauses_when_merged_pr_has_no_full_squash_commit(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_PR_MERGE_COMMIT="missing")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "prepared")
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)

    def test_resume_pauses_when_squash_commit_has_two_parents(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_MERGE_PARENTS="two")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "prepared")

    def test_resume_pauses_when_squash_commit_parent_is_not_pinned_base(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        merged = self.run_afk("resume", AFK_FAKE_MERGE_PARENT="e" * 40)

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "conflict")
        self.assertEqual(
            RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")[
                "status"
            ],
            "prepared",
        )

    def test_resume_pauses_when_squash_commit_tree_is_not_candidate_tree(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        merged = self.run_afk("resume", AFK_FAKE_MERGE_TREE="e" * 40)

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "conflict")
        self.assertEqual(
            RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")[
                "status"
            ],
            "prepared",
        )

    def test_resume_pauses_when_squash_commit_observation_is_unavailable(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        merged = self.run_afk("resume", AFK_FAKE_MERGE_COMMIT_UNAVAILABLE="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "inconclusive")
        self.assertEqual(
            RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")[
                "status"
            ],
            "prepared",
        )

    def test_resume_pauses_when_squash_commit_observation_is_malformed(self):
        run_id = self.start_reviewed_run()
        self.assertEqual(self.run_afk("resume").returncode, 0)

        merged = self.run_afk("resume", AFK_FAKE_MERGE_COMMIT_MALFORMED="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "inconclusive")
        self.assertEqual(
            RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")[
                "status"
            ],
            "prepared",
        )

    def test_resume_pauses_before_merge_when_pinned_target_drifts(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        (self.state_home / "fake-target-drift").write_text("drifted", encoding="utf-8")

        merged = self.run_afk("resume")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        with self.assertRaises(RunStoreError):
            RunStore(self.state_home / "afk").effect(run_id, "pr-squash-merge")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_pauses_before_merge_when_origin_does_not_match_repository(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk(
            "resume", AFK_FAKE_ORIGIN_REPOSITORY="thunderbump/another-repo"
        )

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        self.assertIn("origin", status["attention"]["summary"])
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_pauses_when_origin_changes_during_final_merge_checks(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_ORIGIN_CHANGE_AFTER_FIRST_CHECK="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        self.assertIn("origin", status["attention"]["summary"])
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_pauses_when_pr_is_retargeted_during_merge_checks(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_PR_RACE_DURING_GIT="retarget")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "prepared")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_pauses_when_target_drifts_after_final_pr_observation(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_TARGET_DRIFT_AFTER_SECOND_PR_VIEW="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-squash-merge")["status"], "prepared")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "prepared"
        )
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_pauses_when_target_drifts_after_third_pr_observation(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_TARGET_DRIFT_AFTER_THIRD_PR_VIEW="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["scope"], "merge")
        self.assertIn("target branch", status["attention"]["summary"])
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_refuses_base_branch_that_requires_merge_queue(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk(
            "resume",
            AFK_FAKE_BASE_REQUIRES_MERGE_QUEUE="1",
        )

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "conflict")
        self.assertIn("merge queue", status["attention"]["summary"])
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn(
            [
                "api",
                "repos/thunderbump/beads-webui/rules/branches/main",
                "--method",
                "GET",
                "--paginate",
                "--jq",
                ".[] | {type: .type}",
            ],
            [record["args"] for record in commands if record["command"] == "gh"],
        )
        self.assertNotIn("--slurp", json.dumps(commands))
        self.assertFalse(
            any(
                record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
                for record in commands
            )
        )

    def test_resume_refuses_merge_queue_rule_on_later_rules_page(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk(
            "resume",
            AFK_FAKE_BASE_RULES_SECOND_PAGE_MERGE_QUEUE="1",
        )

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "conflict")
        self.assertIn("merge queue", status["attention"]["summary"])
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertFalse(
            any(
                record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
                for record in commands
            )
        )

    def test_resume_pauses_when_paginated_branch_rules_output_is_malformed(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_BASE_RULES_MALFORMED="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "inconclusive")
        self.assertIn("malformed", status["attention"]["summary"])
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","merge"', commands)

    def test_resume_refuses_existing_auto_merge_request_on_exact_pr(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        merged = self.run_afk("resume", AFK_FAKE_PR_AUTO_MERGE="1")

        self.assertEqual(merged.returncode, 2, merged.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertEqual(status["attention"]["kind"], "conflict")
        self.assertIn("auto-merge or merge queue", status["attention"]["summary"])
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertFalse(
            any(
                record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
                for record in commands
            )
        )

    def test_resume_recovers_transient_attention_before_merge(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)

        unavailable = self.run_afk("resume", AFK_FAKE_MERGE_PR_UNAVAILABLE="1")
        self.assertEqual(unavailable.returncode, 2, unavailable.stderr)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "merged")
        self.assertEqual(status["attention"], {})
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        ready_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "ready"]
        ]
        self.assertEqual(len(ready_commands), 1)
        merge_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "merge"]
        ]
        self.assertEqual(len(merge_commands), 1)

    def test_resume_pauses_when_pr_was_readied_without_an_existing_effect(self):
        run_id = self.start_reviewed_run()
        pr_state = self.state_home / "fake-pr.json"
        pr = json.loads(pr_state.read_text(encoding="utf-8"))
        pr["isDraft"] = False
        pr_state.write_text(json.dumps(pr), encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["attention"]["scope"], "publication")
        self.assertEqual(status["attention"]["kind"], "conflict")
        with self.assertRaises(RunStoreError):
            RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_reconciles_interruption_after_marking_pr_ready(self):
        run_id = self.start_reviewed_run()

        interrupted = self.run_afk("resume", AFK_FAKE_PR_READY_INTERRUPTED="1")

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-mark-ready")["status"], "prepared")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertFalse(status["pr_ready"]["draft"])
        self.assertEqual(store.effect(run_id, "pr-mark-ready")["status"], "confirmed")
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        ready_commands = [
            record
            for record in commands
            if record["command"] == "gh" and record["args"][:2] == ["pr", "ready"]
        ]
        self.assertEqual(len(ready_commands), 1)

    def test_resume_retries_interruption_before_marking_pr_ready(self):
        run_id = self.start_reviewed_run()

        interrupted = self.run_afk("resume", AFK_FAKE_PR_READY_UNAVAILABLE="1")

        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(store.effect(run_id, "pr-mark-ready")["status"], "prepared")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(store.effect(run_id, "pr-mark-ready")["status"], "confirmed")
        effects = list(
            (self.state_home / "afk" / "runs" / run_id / "effects").glob(
                "pr-mark-ready.json"
            )
        )
        self.assertEqual(len(effects), 1)

    def test_resume_pauses_before_mutation_when_ready_pr_state_is_malformed(self):
        run_id = self.start_reviewed_run()

        resumed = self.run_afk("resume", AFK_FAKE_READY_PR_MALFORMED="1")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["attention"]["scope"], "publication")
        with self.assertRaises(RunStoreError):
            RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_pauses_before_mutation_when_ready_pr_url_drifts(self):
        run_id = self.start_reviewed_run()

        resumed = self.run_afk(
            "resume", AFK_FAKE_READY_PR_URL="https://example.test/pr/99"
        )

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["kind"], "conflict")
        with self.assertRaises(RunStoreError):
            RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_pauses_before_mutation_when_ready_pr_is_ambiguous(self):
        run_id = self.start_reviewed_run()

        resumed = self.run_afk("resume", AFK_FAKE_READY_PR_AMBIGUOUS="1")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["kind"], "conflict")
        with self.assertRaises(RunStoreError):
            RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_pauses_before_mutation_when_ready_pr_is_unavailable(self):
        run_id = self.start_reviewed_run()

        resumed = self.run_afk("resume", AFK_FAKE_READY_PR_UNAVAILABLE="1")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["scope"], "publication")
        with self.assertRaises(RunStoreError):
            RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_pauses_before_mutation_when_pinned_target_drifts(self):
        run_id = self.start_reviewed_run()
        (self.state_home / "fake-target-drift").write_text("drifted", encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["kind"], "conflict")
        with self.assertRaises(RunStoreError):
            RunStore(self.state_home / "afk").effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_pauses_before_mutation_when_passed_gate_evidence_is_invalid(self):
        run_id = self.start_reviewed_run()
        store = RunStore(self.state_home / "afk")
        status = store.status(run_id)
        evidence = status["gate_cycles"][-1]["evidence"]
        manifest = (
            self.state_home / "afk" / "runs" / run_id / evidence / "manifest.json"
        )
        manifest.chmod(0o600)
        manifest.write_text("{}\n", encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["scope"], "publication")
        self.assertEqual(status["attention"]["kind"], "invalid")
        with self.assertRaises(RunStoreError):
            store.effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def assert_projected_digest_rejected(self, field, *, digest=None, missing=False):
        run_id = self.start_reviewed_run()
        store = RunStore(self.state_home / "afk")
        record = json.loads(json.dumps(store.status(run_id)[field]))
        if missing:
            record.pop("manifest_sha256")
        else:
            record["manifest_sha256"] = digest
        store.append_event(
            run_id,
            f"{field}.digest_corrupted",
            state="reviewed",
            data={"checkpoint": "reviewed", field: record},
        )
        commands_before = self.command_log.read_text(encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["scope"], "publication")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertEqual(self.command_log.read_text(encoding="utf-8"), commands_before)

    def test_resume_rejects_missing_projected_validation_manifest_digest(self):
        self.assert_projected_digest_rejected("validation", missing=True)

    def test_resume_rejects_wrong_type_projected_validation_manifest_digest(self):
        self.assert_projected_digest_rejected("validation", digest=[])

    def test_resume_rejects_malformed_projected_validation_manifest_digest(self):
        self.assert_projected_digest_rejected("validation", digest="A" * 63)

    def test_resume_rejects_missing_projected_bead_spec_manifest_digest(self):
        self.assert_projected_digest_rejected("bead_spec", missing=True)

    def test_resume_rejects_missing_projected_gate_validation_manifest_digest(self):
        run_id = self.start_reviewed_run()
        store = RunStore(self.state_home / "afk")
        cycles = json.loads(json.dumps(store.status(run_id)["gate_cycles"]))
        cycles[-1]["validation"].pop("manifest_sha256")
        store.append_event(
            run_id,
            "gate.validation_digest_corrupted",
            state="reviewed",
            data={"checkpoint": "reviewed", "gate_cycles": cycles},
        )
        commands_before = self.command_log.read_text(encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertEqual(self.command_log.read_text(encoding="utf-8"), commands_before)

    def test_resume_pauses_before_mutation_when_projected_validation_is_tampered(self):
        run_id = self.start_reviewed_run()
        store = RunStore(self.state_home / "afk")
        evidence = store.status(run_id)["validation"]["evidence"]
        manifest = (
            self.state_home / "afk" / "runs" / run_id / evidence / "manifest.json"
        )
        evidence_dir = manifest.parent
        outcome = evidence_dir / "afk" / "outcome.json"
        evidence_dir.chmod(0o700)
        manifest.chmod(0o600)
        manifest.unlink()
        outcome.chmod(0o600)
        outcome.write_text("{}\n", encoding="utf-8")
        store.seal_evidence(run_id, evidence)
        self.assertTrue(store.verify_evidence(run_id, evidence))

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["scope"], "publication")
        self.assertEqual(status["attention"]["kind"], "invalid")
        with self.assertRaises(RunStoreError):
            store.effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_pauses_before_mutation_when_projected_completion_is_tampered(self):
        run_id = self.start_reviewed_run()
        store = RunStore(self.state_home / "afk")
        evidence = "gates/completion-projected"
        store.write_evidence_value(
            run_id, f"{evidence}/result.json", {"evidence": evidence}
        )
        store.seal_evidence(run_id, evidence)
        store.append_event(
            run_id,
            "completion.projected",
            state="reviewed",
            data={
                "checkpoint": "reviewed",
                "completion": {"evidence": evidence},
            },
        )
        manifest = (
            self.state_home / "afk" / "runs" / run_id / evidence / "manifest.json"
        )
        manifest.chmod(0o600)
        manifest.write_text("{}\n", encoding="utf-8")

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["kind"], "invalid")
        with self.assertRaises(RunStoreError):
            store.effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_resume_pauses_when_projected_gate_contradicts_sealed_outcome(self):
        run_id = self.start_reviewed_run()
        store = RunStore(self.state_home / "afk")
        status = store.status(run_id)
        contradictory = json.loads(json.dumps(status["gate_cycles"][-1]))
        contradictory["validation"]["status"] = "rejected"
        store.append_event(
            run_id,
            "gate.projection_corrupted",
            state="reviewed",
            data={"gate_cycles": [contradictory]},
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["attention"]["kind"], "invalid")
        with self.assertRaises(RunStoreError):
            store.effect(run_id, "pr-mark-ready")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"args":["pr","ready"', commands)

    def test_complete_cannot_bypass_the_durable_terminal_lifecycle(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        worker = self.run_afk("_worker", run_id)
        self.assertEqual(worker.returncode, 0, worker.stderr)

        completed = self.run_afk(
            "complete",
            run_id,
            AFK_FAKE_PR_MERGED="1",
            AFK_FAKE_BEAD_STATUS="closed",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("afk resume", completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "reviewed")
        self.assertEqual(status["checkpoint"], "reviewed")
        self.assertNotIn("completion", status)
        events = [
            json.loads(line)["event"]
            for line in (self.state_home / "afk" / "runs" / run_id / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertNotIn("run.completed", events)

    def test_complete_fails_closed_on_terminal_fact_mismatch(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        worker = self.run_afk("_worker", run_id)
        self.assertEqual(worker.returncode, 0, worker.stderr)

        cases = (
            (
                "head",
                {
                    "AFK_FAKE_PR_MERGED": "1",
                    "AFK_FAKE_PR_HEAD": "e" * 40,
                    "AFK_FAKE_BEAD_STATUS": "closed",
                },
            ),
            ("unmerged", {"AFK_FAKE_BEAD_STATUS": "closed"}),
            (
                "missing merge commit",
                {
                    "AFK_FAKE_PR_MERGED": "1",
                    "AFK_FAKE_PR_MERGE_COMMIT": "missing",
                    "AFK_FAKE_BEAD_STATUS": "closed",
                },
            ),
            (
                "malformed merge commit",
                {
                    "AFK_FAKE_PR_MERGED": "1",
                    "AFK_FAKE_PR_MERGE_COMMIT": '{"oid":"not-a-sha"}',
                    "AFK_FAKE_BEAD_STATUS": "closed",
                },
            ),
            ("bead", {"AFK_FAKE_PR_MERGED": "1"}),
        )
        for label, overrides in cases:
            with self.subTest(label=label):
                completed = self.run_afk("complete", run_id, **overrides)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")

        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "reviewed")
        self.assertNotIn("completion", status)
        self.assertFalse(
            (
                self.state_home
                / "afk"
                / "runs"
                / run_id
                / ("gates/completion-" + "d" * 12)
            ).exists()
        )
        events = [
            json.loads(line)["event"]
            for line in (self.state_home / "afk" / "runs" / run_id / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertNotIn("run.completed", events)

    def test_complete_does_not_seal_legacy_terminal_evidence(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        worker = self.run_afk("_worker", run_id)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        store = RunStore(self.state_home / "afk")
        status = store.status(run_id)
        evidence = "gates/completion-" + "d" * 12
        store.write_evidence_value(
            run_id,
            f"{evidence}/result.json",
            {
                "schema_version": 1,
                "candidate_sha": "d" * 40,
                "gate_evidence": status["gate_cycles"][-1]["evidence"],
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "merge_commit": "f" * 40,
                "bead_id": "central-bnkl.1.1",
                "bead_status": "closed",
                "evidence": evidence,
            },
        )

        completed = self.run_afk(
            "complete",
            run_id,
            AFK_FAKE_PR_MERGED="1",
            AFK_FAKE_BEAD_STATUS="closed",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("afk resume", completed.stderr)
        completion = self.state_home / "afk" / "runs" / run_id / evidence
        self.assertFalse((completion / "manifest.json").exists())
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "reviewed")
        self.assertNotIn("completion", status)

    def test_gate_uses_the_canonical_start_bead_after_live_tracker_mutation(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()

        completed = self.run_afk(
            "_worker",
            run_id,
            AFK_FAKE_BEAD_STATUS="in_progress",
            AFK_FAKE_ASSIGNEE="bump",
            AFK_FAKE_BEAD_DESCRIPTION="mutated live description",
            AFK_FAKE_BEAD_COMMENTS='[{"text":"mutated live comment"}]',
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        projection = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(projection["checkpoint"], "reviewed")
        bundle = json.loads(
            (
                self.state_home
                / "afk"
                / "runs"
                / run_id
                / projection["gate_cycles"][-1]["evidence"]
                / "review-bundle"
                / "bundle.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(bundle["bead"]["description"], "Implement one candidate.")
        self.assertEqual(bundle["bead"]["status"], "open")
        self.assertEqual(bundle["bead"].get("comments", []), [])

    def test_start_seals_tracker_comments_for_the_candidate_prompt(self):
        comments = [
            {
                "id": "comment-1",
                "issue_id": "central-bnkl.1.1",
                "author": "bump",
                "text": "previous tracker diagnostic",
                "created_at": "2026-07-17T20:00:00Z",
            },
            {
                "id": "comment-2",
                "issue_id": "central-bnkl.1.1",
                "author": "bump",
                "text": (
                    "latest tracker diagnostic: runtime assets are mode 0600; "
                    "password=comment-secret"
                ),
                "created_at": "2026-07-17T21:00:00Z",
            },
        ]

        started = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_FAKE_BEAD_COMMENTS=json.dumps(comments),
        )

        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = started.stdout.strip()
        sealed_bead = json.loads(
            (
                self.state_home
                / "afk"
                / "runs"
                / run_id
                / "attempts/start-bead-spec/bead.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(sealed_bead["comments"][0], comments[0])
        self.assertEqual(
            sealed_bead["comments"][1]["text"],
            "latest tracker diagnostic: runtime assets are mode 0600; "
            "password=[REDACTED]",
        )

        completed = self.run_afk("_worker", run_id)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        prompt = (
            self.state_home
            / "afk"
            / "runs"
            / run_id
            / "attempts/implementation-1/prompt.md"
        ).read_text(encoding="utf-8")
        self.assertIn("latest tracker diagnostic", prompt)
        self.assertNotIn("comment-secret", prompt)
        self.assertLess(
            prompt.index("latest tracker diagnostic"),
            prompt.index("previous tracker diagnostic"),
        )
        commands = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        bd_args = [record["args"] for record in commands if record["command"] == "bd"]
        self.assertIn(["show", "central-bnkl.1.1", "--json"], bd_args)
        self.assertIn(["comments", "central-bnkl.1.1", "--json"], bd_args)

    def test_start_fails_closed_on_a_malformed_tracker_comment(self):
        malformed = [
            {
                "id": "",
                "issue_id": "central-bnkl.1.1",
                "author": "bump",
                "text": "diagnostic",
                "created_at": "2026-07-17T21:00:00Z",
            }
        ]

        completed = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_FAKE_BEAD_COMMENTS=json.dumps(malformed),
        )

        self.assertEqual(completed.returncode, 2)
        run_id = completed.stdout.strip()
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "created")
        self.assertEqual(status["attention"]["scope"], "bead_preflight")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertEqual(status["attention"]["classification"], "malformed_output")
        self.assertFalse(
            (
                self.state_home / "afk" / "runs" / run_id / "attempts/start-bead-spec"
            ).exists()
        )
        self.assertNotIn(
            '"command":"systemd-run"',
            self.command_log.read_text(encoding="utf-8"),
        )

    def test_candidate_contract_changes_remain_proposals_for_later_validation(self):
        (self.home / ".fake-contract-proposal").write_text("enabled", encoding="utf-8")
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()

        completed = self.run_afk("_worker", run_id)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        projection = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(projection["checkpoint"], "candidate_ready")
        self.assertEqual(projection["attention"]["scope"], "validation")
        self.assertEqual(projection["attention"]["kind"], "invalid")
        self.assertEqual(
            projection["validation_contract"],
            {
                "source": "pinned_base",
                "base_sha": BASE_SHA,
                "blob_sha": "c" * 40,
            },
        )
        worktree = Path(projection["worktree_path"])
        self.assertEqual(
            (worktree / "afk.toml").read_text(encoding="utf-8"),
            "candidate contract proposal\n",
        )
        self.assertEqual(
            (worktree / "scripts/validation-worker.sh").read_text(encoding="utf-8"),
            "candidate harness proposal\n",
        )

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

        self.assertEqual(completed.returncode, 0, completed.stderr)
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

    def create_resume_preflight_run(self):
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
        return store, self.state_home / "afk" / "runs" / "crashed-run"

    def assert_resume_preflight_rejected(self, message):
        resumed = self.run_afk("resume")
        self.assertEqual(resumed.returncode, 2)
        self.assertIn(message, resumed.stderr)
        self.assertFalse(self.command_log.exists())

    def test_resume_rejects_a_malformed_active_pointer_before_external_commands(self):
        self.create_resume_preflight_run()
        (self.state_home / "afk" / "active.json").write_text(
            '{"run_id":', encoding="utf-8"
        )

        self.assert_resume_preflight_rejected("Active Run pointer is invalid")

    def test_resume_rejects_invalid_event_schema_before_external_commands(self):
        _, run_dir = self.create_resume_preflight_run()
        events_path = run_dir / "events.jsonl"
        event = json.loads(events_path.read_text(encoding="utf-8"))
        event["data"] = []
        events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

        self.assert_resume_preflight_rejected("Event History record 1 is invalid")

    def test_resume_rejects_wrong_event_version_before_external_commands(self):
        _, run_dir = self.create_resume_preflight_run()
        events_path = run_dir / "events.jsonl"
        event = json.loads(events_path.read_text(encoding="utf-8"))
        event["schema_version"] = 2
        events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

        self.assert_resume_preflight_rejected("Event History record 1 is invalid")

    def test_resume_rejects_misbound_open_validation_attempt_before_commands(self):
        store, _ = self.create_resume_preflight_run()
        store.append_event(
            "crashed-run",
            "validation.attempt_started",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "candidate_sha": "b" * 40,
                "validation_attempt": {
                    "status": "started",
                    "candidate_sha": "c" * 40,
                },
            },
        )

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2)
        self.assertFalse(self.command_log.exists())
        status = store.status("crashed-run")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn(
            "validation attempt lifecycle is invalid", status["attention"]["summary"]
        )

    def test_resume_repeatedly_rejects_malformed_outstanding_validation_attempt(self):
        store, _ = self.create_resume_preflight_run()
        attempt = {
            "attempt_id": "validation-bbbbbbbbbbbb",
            "candidate_sha": "b" * 40,
            "status": "started",
            "evidence": "attempts/validation-bbbbbbbbbbbb",
        }
        store.append_event(
            "crashed-run",
            "validation.attempt_started",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "candidate_sha": "b" * 40,
                "validation_attempt": attempt,
            },
        )
        store.append_event(
            "crashed-run",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "candidate_ready",
                "worker_exit_code": 0,
                "attention": {"scope": "validation", "kind": "unavailable"},
                "validation_attempt": {
                    key: value for key, value in attempt.items() if key != "status"
                },
            },
        )

        first = self.run_afk("resume")
        second = self.run_afk("resume")

        self.assertEqual(first.returncode, 2)
        self.assertEqual(second.returncode, 2)
        self.assertFalse(self.command_log.exists())
        status = store.status("crashed-run")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn(
            "open validation attempt is invalid", status["attention"]["summary"]
        )

    def assert_repeated_validation_lifecycle_rejected(self, store, run_dir):
        first = self.run_afk("resume")
        second = self.run_afk("resume")

        self.assertEqual(first.returncode, 2)
        self.assertEqual(second.returncode, 2)
        self.assertFalse(self.command_log.exists())
        for root in ("attempts", "gates", "retrospective"):
            self.assertEqual(list((run_dir / root).iterdir()), [])
        status = store.status("crashed-run")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn(
            "validation attempt lifecycle is invalid",
            status["attention"]["summary"],
        )

    def test_resume_rejects_started_projection_after_attempt_finished(self):
        store, run_dir = self.create_resume_preflight_run()
        started = {
            "attempt_id": "validation-bbbbbbbbbbbb",
            "candidate_sha": "b" * 40,
            "status": "started",
            "evidence": "attempts/validation-bbbbbbbbbbbb",
        }
        store.append_event(
            "crashed-run",
            "validation.attempt_started",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "candidate_sha": "b" * 40,
                "validation_attempt": started,
            },
        )
        store.append_event(
            "crashed-run",
            "validation.attempt_finished",
            data={
                "checkpoint": "candidate_ready",
                "validation_attempt": {**started, "status": "interrupted"},
            },
        )
        store.append_event(
            "crashed-run",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "candidate_ready",
                "worker_exit_code": 0,
                "attention": {"scope": "validation", "kind": "unavailable"},
                "validation_attempt": started,
            },
        )

        self.assert_repeated_validation_lifecycle_rejected(store, run_dir)

    def test_resume_rejects_validation_attempt_started_while_one_is_open(self):
        store, run_dir = self.create_resume_preflight_run()
        for suffix in ("a", "b"):
            attempt_id = f"validation-{suffix * 12}"
            store.append_event(
                "crashed-run",
                "validation.attempt_started",
                state="candidate_ready",
                data={
                    "checkpoint": "candidate_ready",
                    "candidate_sha": "b" * 40,
                    "validation_attempt": {
                        "attempt_id": attempt_id,
                        "candidate_sha": "b" * 40,
                        "status": "started",
                        "evidence": f"attempts/{attempt_id}",
                    },
                },
            )

        self.assert_repeated_validation_lifecycle_rejected(store, run_dir)

    def test_resume_rejects_invalid_terminal_validation_status(self):
        store, run_dir = self.create_resume_preflight_run()
        started = {
            "attempt_id": "validation-bbbbbbbbbbbb",
            "candidate_sha": "b" * 40,
            "status": "started",
            "evidence": "attempts/validation-bbbbbbbbbbbb",
        }
        store.append_event(
            "crashed-run",
            "validation.attempt_started",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "candidate_sha": "b" * 40,
                "validation_attempt": started,
            },
        )
        store.append_event(
            "crashed-run",
            "validation.attempt_finished",
            data={
                "checkpoint": "candidate_ready",
                "validation_attempt": {**started, "status": "not-a-real-outcome"},
            },
        )
        store.append_event(
            "crashed-run",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "candidate_ready",
                "worker_exit_code": 0,
                "attention": {"scope": "validation", "kind": "unavailable"},
            },
        )

        self.assert_repeated_validation_lifecycle_rejected(store, run_dir)

    def test_resume_repeatedly_rejects_misbound_outstanding_repair_attempt(self):
        store, _ = self.create_resume_preflight_run()
        brief = {
            "schema_version": 1,
            "candidate_sha": "b" * 40,
            "repair_attempt": 1,
            "blocking_findings": [],
        }
        store.append_event(
            "crashed-run",
            "gate.cycle_completed",
            state="validated",
            data={
                "checkpoint": "validated",
                "candidate_sha": "b" * 40,
                "gate_cycles": [{"next_action": "repair", "repair_brief": brief}],
            },
        )
        store.append_event(
            "crashed-run",
            "repair.started",
            data={
                "checkpoint": "validated",
                "repair_attempts_used": 1,
                "repair_brief": brief,
            },
        )
        store.append_event(
            "crashed-run",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "validated",
                "attention": {"scope": "repair", "kind": "unavailable"},
                "repair_attempts_used": 2,
                "repair_brief": {
                    **brief,
                    "candidate_sha": "c" * 40,
                    "repair_attempt": 2,
                },
            },
        )

        first = self.run_afk("resume")
        second = self.run_afk("resume")

        self.assertEqual(first.returncode, 2)
        self.assertEqual(second.returncode, 2)
        self.assertFalse(self.command_log.exists())
        status = store.status("crashed-run")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("open repair attempt is invalid", status["attention"]["summary"])

    def assert_repeated_repair_lifecycle_rejected(self, store, run_dir):
        first = self.run_afk("resume")
        second = self.run_afk("resume")

        self.assertEqual(first.returncode, 2)
        self.assertEqual(second.returncode, 2)
        self.assertFalse(self.command_log.exists())
        for root in ("attempts", "gates", "retrospective"):
            self.assertEqual(list((run_dir / root).iterdir()), [])
        status = store.status("crashed-run")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn(
            "repair attempt lifecycle is invalid", status["attention"]["summary"]
        )

    def test_resume_rejects_repair_attempt_started_while_one_is_open(self):
        store, run_dir = self.create_resume_preflight_run()
        for attempt in (1, 2):
            brief = {
                "schema_version": 1,
                "candidate_sha": "b" * 40,
                "repair_attempt": attempt,
                "blocking_findings": [],
            }
            store.append_event(
                "crashed-run",
                "repair.started",
                state="validated",
                data={
                    "checkpoint": "validated",
                    "candidate_sha": "b" * 40,
                    "repair_attempts_used": attempt,
                    "repair_brief": brief,
                },
            )

        self.assert_repeated_repair_lifecycle_rejected(store, run_dir)

    def test_resume_rejects_open_repair_projection_after_candidate_repaired(self):
        store, run_dir = self.create_resume_preflight_run()
        brief = {
            "schema_version": 1,
            "candidate_sha": "b" * 40,
            "repair_attempt": 1,
            "blocking_findings": [],
        }
        store.append_event(
            "crashed-run",
            "repair.started",
            state="validated",
            data={
                "checkpoint": "validated",
                "candidate_sha": "b" * 40,
                "repair_attempts_used": 1,
                "repair_brief": brief,
            },
        )
        store.append_event(
            "crashed-run",
            "candidate.repaired",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "previous_candidate_sha": "b" * 40,
                "candidate_sha": "c" * 40,
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "pr_head_sha": "c" * 40,
                "repair_attempts_used": 1,
                "repair_dispositions": [],
                "attention": {},
            },
        )
        store.append_event(
            "crashed-run",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "validated",
                "candidate_sha": "b" * 40,
                "repair_attempts_used": 1,
                "repair_brief": brief,
                "attention": {"scope": "repair", "kind": "unavailable"},
            },
        )

        self.assert_repeated_repair_lifecycle_rejected(store, run_dir)

    def test_resume_rejects_malformed_repair_brief_hidden_by_closure(self):
        store, run_dir = self.create_resume_preflight_run()
        malformed_brief = {
            "schema_version": 1,
            "candidate_sha": "b" * 40,
            "repair_attempt": 1,
            "blocking_findings": [],
            "unexpected": True,
        }
        store.append_event(
            "crashed-run",
            "repair.started",
            state="validated",
            data={
                "checkpoint": "validated",
                "candidate_sha": "b" * 40,
                "repair_attempts_used": 1,
                "repair_brief": malformed_brief,
            },
        )
        store.append_event(
            "crashed-run",
            "candidate.repaired",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "previous_candidate_sha": "b" * 40,
                "candidate_sha": "c" * 40,
                "pr_number": 17,
                "pr_url": "https://example.test/pr/17",
                "pr_head_sha": "c" * 40,
                "repair_attempts_used": 1,
                "repair_dispositions": [],
                "attention": {},
            },
        )
        store.append_event(
            "crashed-run",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "candidate_ready",
                "worker_exit_code": 0,
                "attention": {"scope": "validation", "kind": "unavailable"},
            },
        )

        self.assert_repeated_repair_lifecycle_rejected(store, run_dir)

    def test_resume_rejects_unrelated_malformed_effect_before_commands(self):
        store, run_dir = self.create_resume_preflight_run()
        malformed = run_dir / "effects" / "unrelated.json"
        malformed.write_text("{", encoding="utf-8")
        malformed.chmod(0o600)

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2)
        self.assertFalse(self.command_log.exists())
        status = store.status("crashed-run")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn(
            "Effect is missing or invalid: unrelated", status["attention"]["summary"]
        )

    def test_resume_rejects_torn_event_tail_before_external_commands(self):
        _, run_dir = self.create_resume_preflight_run()
        with (run_dir / "events.jsonl").open("ab") as stream:
            stream.write(b'{"schema_version":1')

        self.assert_resume_preflight_rejected(
            "Event History has an incomplete trailing record"
        )

    def test_resume_rejects_invalid_run_identity_before_external_commands(self):
        _, run_dir = self.create_resume_preflight_run()
        identity_path = run_dir / "run.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["schema_version"] = 2
        identity_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")

        self.assert_resume_preflight_rejected("Run identity is invalid: crashed-run")

    def test_resume_rejects_malformed_run_identity_before_external_commands(self):
        _, run_dir = self.create_resume_preflight_run()
        identity_path = run_dir / "run.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["start_request"] = []
        identity_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")

        self.assert_resume_preflight_rejected("Run identity is invalid: crashed-run")

    def test_resume_rejects_tampered_sealed_evidence_before_external_commands(self):
        store, _ = self.create_resume_preflight_run()
        result_path = store.write_evidence_text(
            "crashed-run", "attempts/attempt-1/result.txt", "complete\n"
        )
        store.seal_evidence("crashed-run", "attempts/attempt-1")
        result_path.chmod(0o600)
        result_path.write_text("tampered\n", encoding="utf-8")

        self.assert_resume_preflight_rejected("evidence does not match its manifest")

    def test_resume_rejects_external_symlinked_evidence_manifest_before_commands(self):
        store, _ = self.create_resume_preflight_run()
        store.write_evidence_text(
            "crashed-run", "attempts/attempt-1/result.txt", "complete\n"
        )
        store.seal_evidence("crashed-run", "attempts/attempt-1")
        evidence = self.state_home / "afk/runs/crashed-run/attempts/attempt-1"
        manifest = evidence / "manifest.json"
        external_manifest = self.temp / "external-manifest.json"
        external_manifest.write_bytes(manifest.read_bytes())
        external_manifest.chmod(0o400)
        evidence.chmod(0o700)
        manifest.unlink()
        manifest.symlink_to(external_manifest)
        evidence.chmod(0o500)

        self.assert_resume_preflight_rejected("evidence manifest is invalid")

    def test_resume_rejects_insecure_run_store_permissions_before_commands(self):
        self.create_resume_preflight_run()
        (self.state_home / "afk" / "active.json").chmod(0o644)

        self.assert_resume_preflight_rejected(
            "Active Run pointer permissions are invalid"
        )

    def test_resume_rejects_insecure_run_store_root_without_repairing_it(self):
        self.create_resume_preflight_run()
        root = self.state_home / "afk"
        root.chmod(0o755)

        self.assert_resume_preflight_rejected(
            "Run Store directory permissions are invalid"
        )
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)

    def test_resume_rejects_symlinked_lock_without_mutating_its_target(self):
        self.create_resume_preflight_run()
        root = self.state_home / "afk"
        external_lock = self.temp / "external-lock"
        external_lock.write_text("external lock sentinel\n", encoding="utf-8")
        external_lock.chmod(0o644)
        (root / "afk.lock").unlink()
        (root / "afk.lock").symlink_to(external_lock)

        self.assert_resume_preflight_rejected("AFK lock file is invalid")
        self.assertEqual(stat.S_IMODE(external_lock.stat().st_mode), 0o644)
        self.assertEqual(
            external_lock.read_text(encoding="utf-8"), "external lock sentinel\n"
        )

    def test_resume_rejects_insecure_existing_lock_without_repairing_it(self):
        self.create_resume_preflight_run()
        lock = self.state_home / "afk" / "afk.lock"
        lock.write_text("existing lock sentinel\n", encoding="utf-8")
        lock.chmod(0o644)

        self.assert_resume_preflight_rejected("AFK lock file permissions are invalid")
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)
        self.assertEqual(lock.read_text(encoding="utf-8"), "existing lock sentinel\n")

    def test_resume_regenerates_safe_active_pointer_and_projection(self):
        store, run_dir = self.create_resume_preflight_run()
        store.confirm_effect(
            "crashed-run",
            "worker-launch-1",
            observed={"unit": "afk-crashed-run-worker-1"},
        )
        (self.state_home / "afk" / "active.json").unlink()
        (run_dir / "state.json").write_text("{stale", encoding="utf-8")

        resumed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            json.loads(
                (self.state_home / "afk" / "active.json").read_text(encoding="utf-8")
            ),
            {"run_id": "crashed-run"},
        )
        self.assertEqual(
            json.loads((run_dir / "state.json").read_text(encoding="utf-8")),
            store.status("crashed-run"),
        )

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
        branch = "afk/central-bnkl-1-1-prior-slice-run/candidate"
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
        self.assertEqual(projection["attention"]["kind"], "invalid")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertNotIn('"command":"systemctl"', commands)

    def test_resume_advances_legacy_collected_worker_without_terminal_observation(self):
        store = RunStore(self.state_home / "afk")
        run_id = "legacy-prior-slice-run"
        branch = "afk/central-bnkl-1-1-legacy-prior-slice-run/candidate"
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
        self.assertEqual(projection["attention"]["kind"], "invalid")

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

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "reviewed")
        self.assertEqual(after["validation"]["status"], "passed")
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

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        after = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(after["checkpoint"], "reviewed")
        self.assertEqual(after["validation"]["status"], "passed")
        self.assertEqual(store.effect(run_id, "pr-create")["status"], "confirmed")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertEqual(commands.count('"command":"gh","args":["pr","create"'), 1)

        terminal_resume = self.run_afk("resume", HOME=home)

        self.assertEqual(terminal_resume.returncode, 0, terminal_resume.stderr)
        terminal = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(terminal["checkpoint"], "reviewed")
        self.assertEqual(terminal["validation"]["status"], "passed")

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
        observed = self.run_afk("resume", AFK_FAKE_SYSTEMD_STATE="active")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(
            store.effect("crashed-run", "worker-launch-1")["status"], "confirmed"
        )
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertEqual(commands.count('"command":"systemd-run"'), 1)

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
        self.assertEqual(status["attention"]["scope"], "bead_claim")

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

    def test_worktree_failure_pauses_at_claimed_checkpoint(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_WORKTREE_FAILURE="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "claimed")
        self.assertEqual(status["attention"]["scope"], "worktree")

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
        self.assertEqual(
            status["validation_contract"],
            {
                "source": "approved_bootstrap",
                "base_sha": BASE_SHA,
                "adapter_id": "afk.builtin.bootstrap-validation/v1",
            },
        )

        rejected = self.run_afk(
            "start",
            "central-bnkl.1.1",
            "--bootstrap-contract",
            AFK_FAKE_PINNED_CONTRACT="present",
            XDG_STATE_HOME=str(self.temp / "second-state"),
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("already contains afk.toml", rejected.stderr)

    def test_initial_bootstrap_worker_pauses_before_starting_validation(self):
        started = self.run_afk(
            "start",
            "central-bnkl.1.1",
            "--bootstrap-contract",
            AFK_FAKE_PINNED_CONTRACT="missing",
        )
        run_id = started.stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_PINNED_CONTRACT="missing")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "candidate_ready")
        self.assertEqual(status["attention"]["scope"], "validation")
        self.assertEqual(status["attention"]["kind"], "unavailable")
        self.assertIn("operator approval", status["attention"]["summary"])
        self.assertNotIn("validation_attempt", status)
        report = json.loads(self.run_afk("report", run_id).stdout)
        self.assertTrue(report["paused"])
        self.assertEqual(report["authorization"]["status"], "required")
        self.assertNotIn("artifact", report["authorization"])

    def test_normal_start_uses_pinned_contract_not_local_worktree_content(self):
        (self.project / "afk.toml").write_text("invalid local shim\n", encoding="utf-8")

        completed = self.run_afk("start", "central-bnkl.1.1")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(
            self.run_afk("status", completed.stdout.strip(), "--json").stdout
        )
        self.assertEqual(
            status["validation_contract"],
            {
                "source": "pinned_base",
                "base_sha": BASE_SHA,
                "blob_sha": "c" * 40,
            },
        )

    def test_start_pins_a_resolved_relative_repository_common_dir(self):
        completed = self.run_afk(
            "start",
            "central-bnkl.1.1",
            AFK_FAKE_REPOSITORY_COMMON_DIR=".git",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        identity = RunStore(self.state_home / "afk").identity(completed.stdout.strip())
        self.assertEqual(
            identity["start_request"]["repository_common_dir"],
            str((self.project / ".git").resolve()),
        )

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
                contract_proposal = Path(os.environ["HOME"]) / ".fake-contract-proposal"
                pr_state = Path(os.environ["XDG_STATE_HOME"]) / "fake-pr.json"
                pr_view_count = Path(os.environ["XDG_STATE_HOME"]) / "fake-pr-view-count"
                comment_state = Path(os.environ["XDG_STATE_HOME"]) / "fake-comment.json"
                target_drift = Path(os.environ["XDG_STATE_HOME"]) / "fake-target-drift"
                origin_checks = Path(os.environ["XDG_STATE_HOME"]) / "fake-origin-checks"
                cleanup_origin_changed = (
                    Path(os.environ["XDG_STATE_HOME"]) / "fake-cleanup-origin-changed"
                )
                remote_deleted = Path(os.environ["XDG_STATE_HOME"]) / "fake-remote-deleted"
                remote_replaced = Path(os.environ["XDG_STATE_HOME"]) / "fake-remote-replaced"
                worktree_removed = Path(os.environ["XDG_STATE_HOME"]) / "fake-worktree-removed"
                worktree_created = Path(os.environ["XDG_STATE_HOME"]) / "fake-worktree-created"
                worktree_replaced = Path(os.environ["XDG_STATE_HOME"]) / "fake-worktree-replaced"
                worktree_quarantined = Path(os.environ["XDG_STATE_HOME"]) / "fake-worktree-quarantined"
                local_branch_removed = Path(os.environ["XDG_STATE_HOME"]) / "fake-local-branch-removed"
                local_branch_replaced = Path(os.environ["XDG_STATE_HOME"]) / "fake-local-branch-replaced"
                bead_closed = Path(os.environ["XDG_STATE_HOME"]) / "fake-bead-closed"
                bead_claimed = Path(os.environ["XDG_STATE_HOME"]) / "fake-bead-claimed"
                bead_show_count = Path(os.environ["XDG_STATE_HOME"]) / "fake-bead-show-count"
                mutation_log = Path(os.environ["XDG_STATE_HOME"]) / "fake-mutations.jsonl"
                worktree_scenario = os.environ.get("AFK_FAKE_WORKTREE_SCENARIO")

                def before_mutation(name):
                    if os.environ.get("AFK_TEST_KILL_BEFORE_MUTATION") == name:
                        import signal
                        os.kill(os.getppid(), signal.SIGKILL)
                        raise SystemExit(137)

                def after_mutation(name):
                    with mutation_log.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"mutation": name}) + "\\n")
                    if os.environ.get("AFK_TEST_KILL_AFTER_MUTATION") == name:
                        import signal
                        os.kill(os.getppid(), signal.SIGKILL)
                        raise SystemExit(137)

                if command == "git":
                    if args[:2] == ["rev-parse", "--show-toplevel"]:
                        print(os.environ.get("AFK_FAKE_REPOSITORY_ROOT", project))
                    elif args[:2] == ["remote", "get-url"]:
                        repository = os.environ.get(
                            "AFK_FAKE_ORIGIN_REPOSITORY",
                            "thunderbump/beads-webui",
                        )
                        if os.environ.get("AFK_FAKE_ORIGIN_CHANGE_AFTER_FIRST_CHECK"):
                            checks = int(origin_checks.read_text()) if origin_checks.exists() else 0
                            origin_checks.write_text(str(checks + 1), encoding="utf-8")
                            if checks:
                                repository = "thunderbump/another-repo"
                        if (
                            os.environ.get(
                                "AFK_FAKE_CLEANUP_ORIGIN_CHANGE_AFTER_GET_URL"
                            )
                            and remote_deleted.exists()
                        ):
                            cleanup_origin_changed.write_text("changed", encoding="utf-8")
                        print("git@github.com:" + repository + ".git")
                    elif args[:1] == ["ls-remote"]:
                        requested = args[-1]
                        if requested == "refs/heads/main":
                            target_sha = "e" * 40 if target_drift.exists() else sha
                            print(target_sha + "\\trefs/heads/main")
                            if (
                                os.environ.get("AFK_FAKE_PR_RACE_DURING_GIT")
                                == "retarget"
                            ):
                                value = json.loads(pr_state.read_text())
                                value["baseRefName"] = "release"
                                pr_state.write_text(json.dumps(value), encoding="utf-8")
                        elif (
                            os.environ.get(
                                "AFK_FAKE_CLEANUP_ORIGIN_CHANGE_AFTER_GET_URL"
                            )
                            and cleanup_origin_changed.exists()
                            and args[1] != "origin"
                        ):
                            print(candidate_sha + "\\t" + requested)
                        elif (
                            os.environ.get("AFK_FAKE_POST_MERGE_REMOTE_UNAVAILABLE")
                            and remote_deleted.exists()
                        ):
                            raise SystemExit(1)
                        elif remote_replaced.exists():
                            print("a" * 40 + "\\t" + requested)
                        elif pushed_marker.exists() and not remote_deleted.exists():
                            print(candidate_sha + "\\t" + requested)
                    elif args[:1] == ["fetch"]:
                        pass
                    elif args[:3] == ["ls-tree", "-r", "--name-only"]:
                        pass
                    elif args[:1] == ["ls-tree"]:
                        requested = args[-1]
                        if "-z" in args and requested == "scripts/validation-worker.sh":
                            changed = (
                                args[2] == candidate_sha
                                and contract_proposal.exists()
                            )
                            blob = "f" * 40 if changed else "b" * 40
                            sys.stdout.buffer.write(
                                ("100755 blob " + blob + "\\t" + requested + "\\0").encode()
                            )
                        elif os.environ["AFK_FAKE_PINNED_CONTRACT"] == "present":
                            print("100644 blob " + "c" * 40 + "\\tafk.toml")
                    elif args[:2] == ["cat-file", "blob"]:
                        print("schema_version = 1")
                        print("[validation]")
                        print('command = ["./scripts/validation-worker.sh", "run"]')
                        print("timeout_seconds = 2700")
                    elif args[:1] == ["rev-parse"] and args[-1].endswith(":afk.toml"):
                        print("c" * 40)
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
                        if worktree_scenario == "replaced-repository":
                            common_dir = (
                                Path(os.environ["XDG_STATE_HOME"])
                                / "replaced-common-dir"
                            )
                        else:
                            common_dir = Path(
                                os.environ.get(
                                    "AFK_FAKE_REPOSITORY_COMMON_DIR",
                                    str(
                                        Path(os.environ["XDG_STATE_HOME"])
                                        / "fake-git"
                                    ),
                                )
                            )
                        common_dir.mkdir(parents=True, exist_ok=True)
                        print(common_dir)
                    elif args[:1] == ["rev-parse"]:
                        if (
                            args[-1].endswith("^{commit}")
                            and worktree_scenario == "missing-base"
                        ):
                            print("b" * 40)
                        elif args[-1].startswith("refs/heads/"):
                            if local_branch_replaced.exists():
                                print("a" * 40)
                            elif worktree_scenario == "branch-only":
                                print(sha)
                            elif os.environ.get(
                                "AFK_FAKE_WORKTREE_RECORD_EXTRA_MODE"
                            ):
                                print(sha)
                            elif worktree_scenario == "registered-elsewhere":
                                print(sha)
                            elif worktree_created.exists() and not candidate_marker.exists():
                                print(sha)
                            elif not candidate_marker.exists():
                                if worktree_scenario == "ambiguous-missing-branch":
                                    print(sha)
                                    print("ambiguous branch", file=sys.stderr)
                                raise SystemExit(1)
                            elif not local_branch_removed.exists():
                                print(candidate_sha)
                            else:
                                raise SystemExit(1)
                        else:
                            print(candidate_sha if candidate_marker.exists() and Path.cwd() != Path(project) else sha)  # noqa: E501
                    elif args[:2] == ["worktree", "add"]:
                        if os.environ.get("AFK_FAKE_WORKTREE_FAILURE"):
                            print("worktree failed", file=sys.stderr)
                            raise SystemExit(1)
                        before_mutation("worktree-create")
                        checkout = Path(args[-2])
                        checkout.mkdir(parents=True)
                        git_file = checkout / ".git"
                        git_file.write_text("gitdir: fake\\n", encoding="utf-8")
                        scripts = checkout / "scripts"
                        scripts.mkdir()
                        validation_worker = scripts / "validation-worker.sh"
                        validation_worker.write_text(
                            "#!/usr/bin/env python3\\n"
                            "import json, sys\\n"
                            "from pathlib import Path\\n"
                            "request = json.loads(Path(sys.argv[sys.argv.index('--request') + 1]).read_text())\\n"
                            "evidence = Path(request['evidence_dir'])\\n"
                            "(evidence / 'tests.log').write_text('passed\\\\n')\\n"
                            "(evidence / 'result.json').write_text(json.dumps({"
                            "'schema_version': 1, 'candidate_sha': request['candidate_sha'], "
                            "'status': 'passed', 'summary': 'validation passed', "
                            "'checks': [{'name': 'tests', 'status': 'passed', 'log_path': 'tests.log'}]}))\\n",
                            encoding="utf-8",
                        )
                        validation_worker.chmod(0o700)
                        worktree_created.write_text(str(checkout), encoding="utf-8")
                        after_mutation("worktree-create")
                        if os.environ.get(
                            "AFK_FAKE_WORKTREE_ADD_FAILS_AFTER_MUTATION"
                        ):
                            raise SystemExit(1)
                    elif args[:3] == ["worktree", "list", "--porcelain"]:
                        if worktree_scenario == "list-failure":
                            raise SystemExit(1)
                        if worktree_scenario == "malformed-list":
                            print("garbage")
                            raise SystemExit(0)
                        if os.environ.get(
                            "AFK_FAKE_REPOSITORY_DISAPPEARS_DURING_CLEANUP"
                        ):
                            import shutil
                            shutil.rmtree(project)
                        if (
                            not os.environ.get("AFK_FAKE_UNREGISTERED_WORKTREE")
                            and not worktree_removed.exists()
                        ):
                            if worktree_quarantined.exists():
                                checkouts = [Path(worktree_quarantined.read_text())]
                            else:
                                worktrees = (
                                    Path(os.environ["XDG_STATE_HOME"])
                                    / "afk"
                                    / "worktrees"
                                )
                                checkouts = (
                                    list(worktrees.iterdir())
                                    if worktrees.exists()
                                    else []
                                )
                            if worktree_scenario == "registered-elsewhere":
                                checkouts = [
                                    Path(os.environ["XDG_STATE_HOME"])
                                    / "user-owned-checkout"
                                ]
                            for checkout in checkouts:
                                run_id = checkout.name
                                head = (
                                    "b" * 40
                                    if os.environ.get("AFK_FAKE_WRONG_WORKTREE_HEAD")
                                    else candidate_sha if candidate_marker.exists() else sha
                                )
                                if os.environ.get("AFK_FAKE_WRONG_WORKTREE_BRANCH"):
                                    branch = "afk/wrong-branch"
                                else:
                                    if worktree_scenario == "registered-elsewhere":
                                        run_id = next(
                                            path.name
                                            for path in (
                                                Path(os.environ["XDG_STATE_HOME"])
                                                / "afk"
                                                / "runs"
                                            ).iterdir()
                                        )
                                    branch = (
                                        "afk/"
                                        + os.environ["AFK_FAKE_BEAD"].replace(".", "-")
                                        + "-"
                                        + run_id
                                        + "/candidate"
                                    )
                                print("worktree " + str(checkout))
                                print("HEAD " + head)
                                print("branch refs/heads/" + branch)
                                extra_mode = os.environ.get(
                                    "AFK_FAKE_WORKTREE_RECORD_EXTRA_MODE"
                                )
                                if extra_mode:
                                    print(extra_mode)
                                metadata = os.environ.get(
                                    "AFK_FAKE_WORKTREE_RECORD_METADATA"
                                )
                                if metadata == "locked":
                                    print("locked")
                                elif metadata == "prunable":
                                    print("prunable gitdir file points to missing location")
                                print()
                    elif args[:2] == ["status", "--porcelain"]:
                        if os.environ.get("AFK_FAKE_DIRTY_WORKTREE"):
                            print("?? user-owned-file")
                    elif args[:2] == ["worktree", "move"]:
                        source = Path(args[-2])
                        destination = Path(args[-1])
                        before_mutation("worktree-move")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(destination)
                        worktree_quarantined.write_text(
                            str(destination), encoding="utf-8"
                        )
                        after_mutation("worktree-move")
                        if os.environ.get(
                            "AFK_FAKE_WORKTREE_MOVE_TIMES_OUT_AFTER_MUTATION"
                        ):
                            import time
                            time.sleep(1)
                        if os.environ.get(
                            "AFK_FAKE_TERMINAL_CLEANUP_INTERRUPTS_AFTER_MOVE"
                        ):
                            import signal
                            os.kill(os.getppid(), signal.SIGKILL)
                        if os.environ.get("AFK_FAKE_WORKTREE_MOVES_DURING_REMOVE"):
                            source.mkdir()
                            (source / "not-afk-owned").write_text(
                                "replacement", encoding="utf-8"
                            )
                            worktree_replaced.write_text("replaced", encoding="utf-8")
                    elif args[:2] == ["worktree", "remove"]:
                        if os.environ.get("AFK_FAKE_WORKTREE_REMOVE_FAILURE"):
                            raise SystemExit(1)
                        before_mutation("worktree-remove")
                        worktree_removed.write_text("removed", encoding="utf-8")
                        checkout = Path(args[-1])
                        if checkout.exists():
                            import shutil
                            shutil.rmtree(checkout)
                        worktree_quarantined.unlink(missing_ok=True)
                        after_mutation("worktree-remove")
                        if os.environ.get(
                            "AFK_FAKE_WORKTREE_REMOVE_TIMES_OUT_AFTER_MUTATION"
                        ):
                            import time
                            time.sleep(1)
                    elif args[:2] == ["branch", "-D"]:
                        if os.environ.get("AFK_FAKE_BRANCH_DELETE_FAILURE"):
                            raise SystemExit(1)
                        if os.environ.get(
                            "AFK_FAKE_LOCAL_BRANCH_MOVES_DURING_DELETE"
                        ):
                            local_branch_replaced.write_text(
                                "replaced", encoding="utf-8"
                            )
                        local_branch_removed.write_text("removed", encoding="utf-8")
                    elif args[:2] == ["update-ref", "-d"]:
                        if os.environ.get("AFK_FAKE_BRANCH_DELETE_FAILURE"):
                            raise SystemExit(1)
                        if os.environ.get(
                            "AFK_FAKE_LOCAL_BRANCH_MOVES_DURING_DELETE"
                        ):
                            local_branch_replaced.write_text(
                                "replaced", encoding="utf-8"
                            )
                        current = "a" * 40 if local_branch_replaced.exists() else candidate_sha
                        if len(args) != 4 or args[3] != current:
                            raise SystemExit(1)
                        before_mutation("local-branch-delete")
                        local_branch_removed.write_text("removed", encoding="utf-8")
                        after_mutation("local-branch-delete")
                        if os.environ.get(
                            "AFK_FAKE_LOCAL_BRANCH_DELETE_TIMES_OUT_AFTER_MUTATION"
                        ):
                            import time
                            time.sleep(1)
                    elif args[:2] == ["branch", "--show-current"]:
                        run_id = Path.cwd().name
                        print("afk/" + os.environ["AFK_FAKE_BEAD"].replace(".", "-") + "-" + run_id + "/candidate")  # noqa: E501
                    elif args[:2] == ["merge-base", "--is-ancestor"]:
                        pass
                    elif args[:1] == ["rev-list"]:
                        pass
                    elif args[:1] == ["diff"]:
                        pass
                    elif args[:1] == ["push"]:
                        if "--delete" in args:
                            if os.environ.get("AFK_FAKE_REMOTE_DELETE_FAILURE"):
                                raise SystemExit(1)
                            if os.environ.get(
                                "AFK_FAKE_REMOTE_DISAPPEARS_DURING_DELETE"
                            ):
                                remote_deleted.write_text("deleted", encoding="utf-8")
                                raise SystemExit(1)
                            if os.environ.get("AFK_FAKE_REMOTE_MOVES_DURING_DELETE"):
                                remote_replaced.write_text("replaced", encoding="utf-8")
                                branch = args[-1]
                                expected_lease = (
                                    "--force-with-lease=refs/heads/"
                                    + branch
                                    + ":"
                                    + candidate_sha
                                )
                                if expected_lease in args:
                                    raise SystemExit(1)
                            before_mutation("remote-branch-delete")
                            remote_deleted.write_text("deleted", encoding="utf-8")
                            after_mutation("remote-branch-delete")
                            if os.environ.get(
                                "AFK_FAKE_REMOTE_DELETE_TIMES_OUT_AFTER_MUTATION"
                            ):
                                import time
                                time.sleep(1)
                        else:
                            pushed_marker.write_text(candidate_sha, encoding="utf-8")
                        if os.environ.get("AFK_FAKE_PUSH_INTERRUPTED"):
                            raise SystemExit(1)
                    else:
                        raise SystemExit(f"unexpected git args: {args}")
                elif command == "gh":
                    if args[:1] == ["api"] and "/git/commits/" in args[1]:
                        requested = args[1].rsplit("/", 1)[-1]
                        if os.environ.get("AFK_FAKE_MERGE_COMMIT_UNAVAILABLE"):
                            raise SystemExit(1)
                        if os.environ.get("AFK_FAKE_MERGE_COMMIT_MALFORMED"):
                            print("{")
                        else:
                            tree = "c" * 40
                            parents = [{"sha": sha}]
                            if requested == "f" * 40:
                                if os.environ.get("AFK_FAKE_MERGE_PARENTS") == "two":
                                    parents.append({"sha": candidate_sha})
                                if os.environ.get("AFK_FAKE_MERGE_PARENT"):
                                    parents = [{"sha": os.environ["AFK_FAKE_MERGE_PARENT"]}]
                                tree = os.environ.get("AFK_FAKE_MERGE_TREE", tree)
                            print(json.dumps({
                                "sha": requested,
                                "tree": {"sha": tree},
                                "parents": parents,
                            }))
                    elif args[:2] == ["api", "graphql"]:
                        value = json.loads(pr_state.read_text())
                        value["autoMergeRequest"] = (
                            {"enabledAt": "2026-07-18T00:00:00Z"}
                            if os.environ.get("AFK_FAKE_PR_AUTO_MERGE")
                            else None
                        )
                        value["mergeQueueEntry"] = (
                            {"id": "MQE_test", "state": "AWAITING_CHECKS"}
                            if os.environ.get("AFK_FAKE_PR_QUEUED")
                            else None
                        )
                        print(json.dumps({
                            "data": {
                                "repository": {"pullRequest": value},
                            },
                        }))
                    elif (
                        args[:1] == ["api"]
                        and args[1]
                        == "repos/thunderbump/beads-webui/rules/branches/main"
                    ):
                        if "--slurp" in args:
                            raise SystemExit("installed gh does not support --slurp")
                        if args[args.index("--jq") + 1] != ".[] | {type: .type}":
                            raise SystemExit("unexpected branch rules jq filter")
                        if os.environ.get("AFK_FAKE_BASE_RULES_MALFORMED"):
                            print("{")
                            raise SystemExit(0)
                        rules = []
                        if os.environ.get("AFK_FAKE_BASE_REQUIRES_MERGE_QUEUE"):
                            rules.append({"type": "merge_queue"})
                        if os.environ.get(
                            "AFK_FAKE_BASE_RULES_SECOND_PAGE_MERGE_QUEUE"
                        ):
                            rules = [
                                {"type": "required_status_checks"},
                                {"type": "merge_queue"},
                            ]
                        for rule in rules:
                            print(json.dumps(rule, separators=(",", ":")))
                    elif args[:2] == ["pr", "list"]:
                        if os.environ.get("AFK_FAKE_READY_PR_UNAVAILABLE"):
                            raise SystemExit(1)
                        elif os.environ.get("AFK_FAKE_READY_PR_MALFORMED"):
                            print("{")
                        else:
                            values = [json.loads(pr_state.read_text())] if pr_state.exists() else []  # noqa: E501
                            if values and os.environ.get("AFK_FAKE_READY_PR_URL"):
                                values[0]["url"] = os.environ["AFK_FAKE_READY_PR_URL"]
                            if values and os.environ.get("AFK_FAKE_READY_PR_AMBIGUOUS"):
                                values.append(dict(values[0]))
                            print(json.dumps(values))
                    elif args[:2] == ["pr", "view"]:
                        if os.environ.get("AFK_FAKE_MERGE_PR_UNAVAILABLE"):
                            raise SystemExit(1)
                        view_count = (
                            int(pr_view_count.read_text(encoding="utf-8")) + 1
                            if pr_view_count.exists()
                            else 1
                        )
                        pr_view_count.write_text(str(view_count), encoding="utf-8")
                        value = json.loads(pr_state.read_text())
                        if os.environ.get("AFK_FAKE_PR_MERGED"):
                            value.update({
                                "state": "MERGED",
                                "isDraft": False,
                                "mergeCommit": {"oid": "f" * 40},
                            })
                        if os.environ.get("AFK_FAKE_PR_HEAD"):
                            value["headRefOid"] = os.environ["AFK_FAKE_PR_HEAD"]
                        value["autoMergeRequest"] = (
                            {"enabledAt": "2026-07-18T00:00:00Z"}
                            if os.environ.get("AFK_FAKE_PR_AUTO_MERGE")
                            else None
                        )
                        value.setdefault("mergeQueueEntry", None)
                        merge_commit = os.environ.get("AFK_FAKE_PR_MERGE_COMMIT")
                        if merge_commit == "missing":
                            value.pop("mergeCommit", None)
                        elif merge_commit:
                            value["mergeCommit"] = json.loads(merge_commit)
                        print(json.dumps(value))
                        if (
                            view_count == 2
                            and os.environ.get(
                                "AFK_FAKE_TARGET_DRIFT_AFTER_SECOND_PR_VIEW"
                            )
                        ):
                            target_drift.write_text("drifted", encoding="utf-8")
                        if (
                            view_count == 3
                            and os.environ.get(
                                "AFK_FAKE_TARGET_DRIFT_AFTER_THIRD_PR_VIEW"
                            )
                        ):
                            target_drift.write_text("drifted", encoding="utf-8")
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
                    elif args[:2] == ["pr", "ready"]:
                        if os.environ.get("AFK_FAKE_PR_READY_UNAVAILABLE"):
                            raise SystemExit(1)
                        before_mutation("pr-ready")
                        value = json.loads(pr_state.read_text())
                        value["isDraft"] = False
                        pr_state.write_text(json.dumps(value), encoding="utf-8")
                        after_mutation("pr-ready")
                        if os.environ.get("AFK_FAKE_PR_READY_INTERRUPTED"):
                            raise SystemExit(1)
                        print(value["url"])
                    elif args[:2] == ["pr", "merge"]:
                        value = json.loads(pr_state.read_text())
                        if os.environ.get("AFK_FAKE_BASE_REQUIRES_MERGE_QUEUE"):
                            value["mergeQueueEntry"] = {"state": "AWAITING_CHECKS"}
                            pr_state.write_text(json.dumps(value), encoding="utf-8")
                            raise SystemExit(0)
                        before_mutation("pr-merge")
                        value.update(
                            {
                                "state": "MERGED",
                                "isDraft": False,
                                "mergeCommit": {"oid": "f" * 40},
                            }
                        )
                        pr_state.write_text(json.dumps(value), encoding="utf-8")
                        if not os.environ.get("AFK_FAKE_REMOTE_BRANCH_LINGERS"):
                            remote_deleted.write_text("deleted", encoding="utf-8")
                        if os.environ.get("AFK_FAKE_REPLACED_REMOTE_BRANCH"):
                            remote_replaced.write_text("replaced", encoding="utf-8")
                        after_mutation("pr-merge")
                        if os.environ.get("AFK_FAKE_PR_MERGE_INTERRUPTED"):
                            raise SystemExit(1)
                    elif args[:1] == ["api"]:
                        if "--method" in args:
                            body = json.loads(sys.stdin.read())["body"]
                            value = {
                                "body": body,
                                "html_url": "https://example.test/comment/1",
                            }
                            comment_state.write_text(json.dumps(value), encoding="utf-8")
                            print(json.dumps(value))
                        else:
                            comments = (
                                [json.loads(comment_state.read_text())]
                                if comment_state.exists()
                                else []
                            )
                            print(json.dumps(comments))
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
                    if bead_claimed.exists():
                        claim = json.loads(bead_claimed.read_text(encoding="utf-8"))
                        if (
                            claim["bead_id"] == os.environ["AFK_FAKE_BEAD"]
                            and status == "open"
                            and not assignee
                        ):
                            status = "in_progress"
                            assignee = claim["assignee"]
                    if bead_closed.exists():
                        status = "closed"
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
                        if os.environ.get("AFK_FAKE_BEAD_SHOW_FAILURE"):
                            print("show failed", file=sys.stderr)
                            raise SystemExit(1)
                        if os.environ.get("AFK_FAKE_BEAD_SHOW_MALFORMED"):
                            print("{}")
                            raise SystemExit(0)
                        if os.environ.get(
                            "AFK_FAKE_BEAD_PROJECT_CHANGE_AFTER_FIRST_SHOW"
                        ):
                            show_count = (
                                int(bead_show_count.read_text())
                                if bead_show_count.exists()
                                else 0
                            )
                            bead_show_count.write_text(
                                str(show_count + 1), encoding="utf-8"
                            )
                            if show_count:
                                labels = ["project:another-repository"]
                        payload = {
                            "id": os.environ["AFK_FAKE_BEAD"],
                            "title": "Create the first slice",
                            "description": os.environ["AFK_FAKE_BEAD_DESCRIPTION"],
                            "acceptance_criteria": "Candidate is committed.",
                            "status": status,
                            "close_reason": (
                                bead_closed.read_text(encoding="utf-8")
                                if bead_closed.exists()
                                else os.environ.get("AFK_FAKE_BEAD_CLOSE_REASON", "")
                            ),
                            "assignee": assignee,
                            "labels": labels,
                        }
                        malformed_schema = os.environ.get("AFK_FAKE_BEAD_SCHEMA")
                        if malformed_schema == "missing-assignee":
                            del payload["assignee"]
                        elif malformed_schema == "status-number":
                            payload["status"] = 0
                        elif malformed_schema == "assignee-list":
                            payload["assignee"] = []
                        print(json.dumps([payload]))
                    elif args[:1] == ["comments"]:
                        print(os.environ["AFK_FAKE_BEAD_COMMENTS"])
                    elif args[:1] == ["update"]:
                        if os.environ.get("AFK_FAKE_CLAIM_FAILURE"):
                            print("claim failed", file=sys.stderr)
                            raise SystemExit(1)
                        before_mutation("bead-claim")
                        bead_claimed.write_text(
                            json.dumps({
                                "bead_id": os.environ["AFK_FAKE_BEAD"],
                                "assignee": os.environ.get(
                                    "BEADS_ACTOR", os.environ["USER"]
                                ),
                            }),
                            encoding="utf-8",
                        )
                        after_mutation("bead-claim")
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
                            "assignee": os.environ.get(
                                "BEADS_ACTOR", os.environ["USER"]
                            ),
                        }))
                    elif args[:1] == ["close"]:
                        if os.environ.get("AFK_FAKE_BEAD_CLOSE_FAILURE"):
                            print("close failed", file=sys.stderr)
                            raise SystemExit(1)
                        before_mutation("bead-close")
                        bead_closed.write_text(args[args.index("--reason") + 1], encoding="utf-8")
                        after_mutation("bead-close")
                        if os.environ.get("AFK_FAKE_BEAD_CLOSE_INTERRUPTED"):
                            raise SystemExit(1)
                        print(json.dumps({
                            "id": os.environ["AFK_FAKE_BEAD"],
                            "status": "closed",
                        }))
                    else:
                        raise SystemExit(f"unexpected bd args: {args}")
                elif command == "systemd-run":
                    before_mutation("worker-launch")
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
                        worker_environment = os.environ.copy()
                        worker_environment.pop("BEADS_ACTOR", None)
                        for argument in args:
                            if argument.startswith("--setenv=BEADS_ACTOR="):
                                worker_environment["BEADS_ACTOR"] = argument.split(
                                    "=", 2
                                )[2]
                        subprocess.Popen(
                            args[-5:],
                            cwd=project,
                            env=worker_environment,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            close_fds=True,
                        )
                    if os.environ.get("AFK_FAKE_SYSTEMD_FAILURE"):
                        print("launch failed", file=sys.stderr)
                        raise SystemExit(1)
                    after_mutation("worker-launch")
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
                prompt = sys.stdin.read()
                base_sha = "a" * 40
                candidate_sha = "d" * 40
                worktree = Path(args[args.index("--cd") + 1])
                report = Path(args[args.index("--output-last-message") + 1])
                if "# AFK standards review" in prompt or "# AFK spec review" in prompt:
                    axis = (
                        "standards"
                        if "# AFK standards review" in prompt
                        else "spec"
                    )
                    report.write_text(json.dumps({
                        "schema_version": 1,
                        "candidate_sha": candidate_sha,
                        "axis": axis,
                        "status": "passed",
                        "summary": "review passed",
                        "findings": [],
                    }), encoding="utf-8")
                    print(json.dumps({"type": "result"}))
                    raise SystemExit(0)
                (Path(os.environ["HOME"]) / ".fake-candidate").write_text(
                    candidate_sha, encoding="utf-8"
                )
                if (Path(os.environ["HOME"]) / ".fake-contract-proposal").exists():
                    (worktree / "afk.toml").write_text(
                        "candidate contract proposal\\n", encoding="utf-8"
                    )
                    scripts = worktree / "scripts"
                    scripts.mkdir(exist_ok=True)
                    (scripts / "validation-worker.sh").write_text(
                        "candidate harness proposal\\n", encoding="utf-8"
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
