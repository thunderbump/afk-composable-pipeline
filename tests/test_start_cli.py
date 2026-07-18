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
from afk.run_store import RunStore, RunStoreBusy, RunStoreError  # noqa: E402
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
                "AFK_FAKE_BEAD_DESCRIPTION": "Implement one candidate.",
                "AFK_FAKE_BEAD_COMMENTS": "[]",
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

    def start_reviewed_run(self):
        started = self.run_afk("start", "central-bnkl.1.1")
        run_id = started.stdout.strip()
        worker = self.run_afk("_worker", run_id)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        return run_id

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

    def test_resume_pauses_when_merged_candidate_branch_was_replaced(self):
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

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "merged")
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

    def test_resume_reconciles_merged_cleanup_after_remote_branch_remediation(self):
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
        self.assertEqual(status["state"], "merged")
        self.assertEqual(status["checkpoint"], "merged")
        self.assertEqual(status["attention"], {})
        self.assertEqual(status["remote_branch_deleted"], True)
        self.assertEqual(status["last_event"], "pr.merge_reconciled")
        self.assertEqual(status["last_sequence"], before["last_sequence"] + 1)
        store = RunStore(self.state_home / "afk")
        self.assertEqual(
            store.effect(run_id, "remote-branch-delete")["status"], "confirmed"
        )

        repeated = self.run_afk("resume")

        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        repeated_status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(repeated_status["last_sequence"], status["last_sequence"])
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

    def test_resume_keeps_merged_checkpoint_when_pr_observation_is_unavailable(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        conflicted = self.run_afk("resume", AFK_FAKE_REPLACED_REMOTE_BRANCH="1")
        self.assertEqual(conflicted.returncode, 2, conflicted.stderr)
        before = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(before["checkpoint"], "merged")

        unavailable = self.run_afk("resume", AFK_FAKE_MERGE_PR_UNAVAILABLE="1")

        self.assertEqual(unavailable.returncode, 2, unavailable.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["state"], "attention_required")
        self.assertEqual(status["checkpoint"], "merged")
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

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
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

    def test_resume_does_not_confirm_branch_deletion_for_mismatched_origin(self):
        run_id = self.start_reviewed_run()
        ready = self.run_afk("resume")
        self.assertEqual(ready.returncode, 0, ready.stderr)
        interrupted = self.run_afk("resume", AFK_FAKE_POST_MERGE_REMOTE_UNAVAILABLE="1")
        self.assertEqual(interrupted.returncode, 2, interrupted.stderr)

        resumed = self.run_afk(
            "resume", AFK_FAKE_ORIGIN_REPOSITORY="thunderbump/another-repo"
        )

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "merged")
        self.assertEqual(status["remote_branch_deleted"], False)
        self.assertIn("origin", status["attention"]["summary"])
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

    def test_complete_reconciles_merged_candidate_and_closed_bead_idempotently(self):
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

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["complete"])
        self.assertFalse(report["paused"])
        self.assertEqual(report["state"], "completed")
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "completed")
        self.assertEqual(status["completion"]["candidate_sha"], "d" * 40)
        self.assertEqual(status["completion"]["merge_commit"], "f" * 40)
        evidence = (
            self.state_home / "afk" / "runs" / run_id / status["completion"]["evidence"]
        )
        self.assertTrue((evidence / "manifest.json").is_file())

        repeated = self.run_afk(
            "complete",
            run_id,
            AFK_FAKE_PR_MERGED="1",
            AFK_FAKE_BEAD_STATUS="closed",
        )

        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertTrue(json.loads(repeated.stdout)["complete"])
        events = [
            json.loads(line)["event"]
            for line in (self.state_home / "afk" / "runs" / run_id / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(events.count("run.completed"), 1)

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

    def test_complete_recovers_matching_unsealed_terminal_evidence(self):
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

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["complete"])
        completion = self.state_home / "afk" / "runs" / run_id / evidence
        self.assertTrue((completion / "manifest.json").is_file())

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
        home = self.temp
        (home / ".fake-contract-proposal").write_text("enabled", encoding="utf-8")
        started = self.run_afk("start", "central-bnkl.1.1", HOME=str(home))
        run_id = started.stdout.strip()

        completed = self.run_afk("_worker", run_id, HOME=str(home))

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
                pr_view_count = Path(os.environ["XDG_STATE_HOME"]) / "fake-pr-view-count"
                comment_state = Path(os.environ["XDG_STATE_HOME"]) / "fake-comment.json"
                target_drift = Path(os.environ["XDG_STATE_HOME"]) / "fake-target-drift"
                origin_checks = Path(os.environ["XDG_STATE_HOME"]) / "fake-origin-checks"
                cleanup_origin_changed = (
                    Path(os.environ["XDG_STATE_HOME"]) / "fake-cleanup-origin-changed"
                )
                remote_deleted = Path(os.environ["XDG_STATE_HOME"]) / "fake-remote-deleted"
                remote_replaced = Path(os.environ["XDG_STATE_HOME"]) / "fake-remote-replaced"
                if command == "git":
                    if args[:2] == ["rev-parse", "--show-toplevel"]:
                        print(project)
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
                                and (Path(os.environ["HOME"]) / ".fake-contract-proposal").exists()
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
                                    + "/candidate"
                                )
                                print("worktree " + str(checkout))
                                print("HEAD " + head)
                                print("branch refs/heads/" + branch)
                                print()
                    elif args[:2] == ["status", "--porcelain"]:
                        pass
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
                        value = json.loads(pr_state.read_text())
                        value["isDraft"] = False
                        pr_state.write_text(json.dumps(value), encoding="utf-8")
                        if os.environ.get("AFK_FAKE_PR_READY_INTERRUPTED"):
                            raise SystemExit(1)
                        print(value["url"])
                    elif args[:2] == ["pr", "merge"]:
                        value = json.loads(pr_state.read_text())
                        if os.environ.get("AFK_FAKE_BASE_REQUIRES_MERGE_QUEUE"):
                            value["mergeQueueEntry"] = {"state": "AWAITING_CHECKS"}
                            pr_state.write_text(json.dumps(value), encoding="utf-8")
                            raise SystemExit(0)
                        value.update(
                            {
                                "state": "MERGED",
                                "isDraft": False,
                                "mergeCommit": {"oid": "f" * 40},
                            }
                        )
                        pr_state.write_text(json.dumps(value), encoding="utf-8")
                        remote_deleted.write_text("deleted", encoding="utf-8")
                        if os.environ.get("AFK_FAKE_REPLACED_REMOTE_BRANCH"):
                            remote_replaced.write_text("replaced", encoding="utf-8")
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
                            "description": os.environ["AFK_FAKE_BEAD_DESCRIPTION"],
                            "acceptance_criteria": "Candidate is committed.",
                            "status": status,
                            "assignee": assignee,
                            "labels": labels,
                        }]))
                    elif args[:1] == ["comments"]:
                        print(os.environ["AFK_FAKE_BEAD_COMMENTS"])
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
