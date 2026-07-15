import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.run_store import RunStore  # noqa: E402
from afk.start import StartError, _beads_password, start_run  # noqa: E402


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
        self.assertEqual(projection["checkpoint"], "worktree_ready")
        effect = RunStore(self.state_home / "afk").effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "confirmed")

    def test_worker_claims_exact_bead_prepares_clean_pinned_worktree_and_stops(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()

        completed = self.run_afk("_worker", run_id)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        projection = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(projection["state"], "attention_required")
        self.assertEqual(projection["checkpoint"], "worktree_ready")
        self.assertEqual(projection["attention"]["kind"], "unavailable")
        self.assertTrue(Path(projection["worktree_path"]).is_dir())
        effect = RunStore(self.state_home / "afk").effect(run_id, "worker-launch-1")
        self.assertEqual(effect["status"], "confirmed")
        commands = self.command_log.read_text(encoding="utf-8")
        self.assertIn(
            '"command":"bd","args":["update","central-bnkl.1.1","--claim"', commands
        )
        self.assertIn(BASE_SHA, commands)

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
        self.assertIn("Beads command failed", status["attention"]["summary"])
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
        self.assertNotIn("Traceback", completed.stderr)

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
                if command == "git":
                    if args[:2] == ["rev-parse", "--show-toplevel"]:
                        print(project)
                    elif args[:2] == ["remote", "get-url"]:
                        print("git@github.com:thunderbump/beads-webui.git")
                    elif args[:1] == ["ls-remote"]:
                        print(sha + "\\trefs/heads/main")
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
                    elif args[:1] == ["rev-parse"]:
                        print(sha)
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
                    else:
                        raise SystemExit(f"unexpected git args: {args}")
                elif command == "gh":
                    if os.environ.get("AFK_FAKE_GH_NON_OBJECT"):
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
                        print(
                            "authentication denied: "
                            + os.environ["BEADS_DOLT_PASSWORD"],
                            file=sys.stderr,
                        )
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
                            "status": status,
                            "assignee": assignee,
                            "labels": labels,
                        }]))
                    elif args[:1] == ["update"]:
                        if os.environ.get("AFK_FAKE_CLAIM_FAILURE"):
                            print("claim failed", file=sys.stderr)
                            raise SystemExit(1)
                        if os.environ.get("AFK_FAKE_CLAIM_MALFORMED"):
                            print("null")
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


if __name__ == "__main__":
    unittest.main()
