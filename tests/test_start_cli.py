import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.run_store import RunStore  # noqa: E402


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
                "AFK_BEADS_WORKSPACE": str(self.temp / "beads"),
                "AFK_FAKE_LOG": str(self.command_log),
                "AFK_FAKE_PROJECT": str(self.project),
                "AFK_FAKE_SHA": BASE_SHA,
                "AFK_FAKE_BEAD": "central-bnkl.1.1",
                "AFK_FAKE_BEAD_STATUS": "open",
                "AFK_FAKE_ASSIGNEE": "",
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

    def test_worktree_failure_stops_at_claimed_checkpoint(self):
        run_id = self.run_afk("start", "central-bnkl.1.1").stdout.strip()

        completed = self.run_afk("_worker", run_id, AFK_FAKE_WORKTREE_FAILURE="1")

        self.assertEqual(completed.returncode, 2)
        status = json.loads(self.run_afk("status", run_id, "--json").stdout)
        self.assertEqual(status["checkpoint"], "claimed")
        self.assertEqual(status["attention"]["scope"], "worker")

    def test_preflight_rejects_missing_contract_without_creating_a_run(self):
        (self.project / "afk.toml").unlink()

        completed = self.run_afk("start", "central-bnkl.1.1")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("afk.toml", completed.stderr)
        self.assertFalse((self.state_home / "afk" / "runs").exists())

    def _write_fake_commands(self):
        script = self.fake_bin / "fake-command"
        script.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                command = Path(sys.argv[0]).name
                args = sys.argv[1:]
                log_path = Path(os.environ["AFK_FAKE_LOG"])
                with log_path.open("a", encoding="utf-8") as log:
                    record = {"command": command, "args": args}
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
                    elif args[:2] == ["status", "--porcelain"]:
                        pass
                    else:
                        raise SystemExit(f"unexpected git args: {args}")
                elif command == "gh":
                    print(json.dumps({
                        "nameWithOwner": "thunderbump/beads-webui",
                        "defaultBranchRef": {"name": "main"},
                    }))
                elif command == "bd":
                    status = os.environ["AFK_FAKE_BEAD_STATUS"]
                    assignee = os.environ["AFK_FAKE_ASSIGNEE"]
                    if args[:1] == ["show"]:
                        print(json.dumps([{
                            "id": os.environ["AFK_FAKE_BEAD"],
                            "status": status,
                            "assignee": assignee,
                            "labels": ["project:beads-webui"],
                        }]))
                    elif args[:1] == ["update"]:
                        if os.environ.get("AFK_FAKE_CLAIM_FAILURE"):
                            print("claim failed", file=sys.stderr)
                            raise SystemExit(1)
                        print(json.dumps({
                            "id": os.environ["AFK_FAKE_BEAD"],
                            "status": "in_progress",
                            "assignee": os.environ["USER"],
                        }))
                    else:
                        raise SystemExit(f"unexpected bd args: {args}")
                elif command == "systemd-run":
                    if os.environ.get("AFK_FAKE_SYSTEMD_FAILURE"):
                        print("launch failed", file=sys.stderr)
                        raise SystemExit(1)
                elif command == "systemctl":
                    print("ActiveState=active")
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
