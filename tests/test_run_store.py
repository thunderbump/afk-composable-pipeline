import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.run_store import (  # noqa: E402
    ATTEMPT_BYTE_LIMIT,
    GATE_BYTE_LIMIT,
    STREAM_BYTE_LIMIT,
    ActiveRunExists,
    EvidenceError,
    EvidenceTampered,
    EvidenceTooLarge,
    RunStore,
    RunStoreBusy,
)


BASE_SHA = "a" * 40


def run_afk(*args, env_overrides=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "afk", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class RunStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_home = Path(self.temporary_directory.name)
        self.root = self.state_home / "afk"
        self.store = RunStore(self.root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_run(self, run_id="run-001"):
        return self.store.create_run(
            bead_id="central-bnkl.1.1",
            repository="https://example.invalid/acme/beads-webui.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"note": "token=plain-secret-value"},
            run_id=run_id,
            created_at="2026-07-14T22:00:00Z",
        )

    def test_create_run_persists_redacted_identity_and_durable_files(self):
        projection = self.create_run()

        run_dir = self.root / "runs" / "run-001"
        identity = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        active = json.loads((self.root / "active.json").read_text(encoding="utf-8"))

        self.assertEqual(projection["state"], "created")
        self.assertEqual(identity["bead_id"], "central-bnkl.1.1")
        self.assertEqual(identity["start_request"]["note"], "token=[REDACTED]")
        self.assertEqual(json.loads(events[0])["sequence"], 1)
        self.assertEqual(active, {"run_id": "run-001"})
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
        for path in (
            run_dir / "run.json",
            run_dir / "events.jsonl",
            run_dir / "state.json",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_status_replays_stale_projection_and_ignores_torn_event_tail(self):
        self.create_run()
        self.store.append_event(
            "run-001",
            "bead.claimed",
            state="claimed",
            data={"owner": "afk/run-001"},
            recorded_at="2026-07-14T22:01:00Z",
        )
        run_dir = self.root / "runs" / "run-001"
        (run_dir / "state.json").write_text("{stale", encoding="utf-8")
        with (run_dir / "events.jsonl").open("ab") as stream:
            stream.write(b'{"sequence":3')

        projection = self.store.status("run-001")

        self.assertEqual(projection["state"], "claimed")
        self.assertEqual(projection["last_sequence"], 2)
        self.assertEqual(projection["last_event"], "bead.claimed")

    def test_global_lock_refuses_a_second_mutator(self):
        with self.store.lock():
            with self.assertRaises(RunStoreBusy):
                self.create_run()

    def test_one_active_run_prevents_another_run_from_being_created(self):
        self.create_run()

        with self.assertRaises(ActiveRunExists):
            self.create_run("run-002")

    def test_event_history_recovers_a_missing_active_pointer(self):
        self.create_run()
        (self.root / "active.json").unlink()

        self.assertEqual(self.store.status()["run_id"], "run-001")
        with self.assertRaises(ActiveRunExists):
            self.create_run("run-002")

    def test_completed_evidence_is_redacted_manifested_read_only_and_verified(self):
        self.create_run()
        evidence_path = self.store.write_evidence_text(
            "run-001",
            "attempts/attempt-1/stdout.txt",
            "request failed token=plain-secret-value\n",
        )

        manifest = self.store.seal_evidence("run-001", "attempts/attempt-1")

        self.assertEqual(
            evidence_path.read_text(encoding="utf-8"),
            "request failed token=[REDACTED]\n",
        )
        self.assertEqual(manifest["files"][0]["path"], "stdout.txt")
        self.assertTrue(self.store.verify_evidence("run-001", "attempts/attempt-1"))
        self.assertEqual(stat.S_IMODE(evidence_path.stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(evidence_path.parent.stat().st_mode), 0o500)

        evidence_path.chmod(0o600)
        evidence_path.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(EvidenceTampered):
            self.store.verify_evidence("run-001", "attempts/attempt-1")

    def test_stream_byte_limit_is_enforced_before_manifest_hashing(self):
        self.create_run()
        stream = (
            self.root / "runs" / "run-001" / "attempts" / "attempt-1" / "stdout.txt"
        )
        stream.parent.mkdir(parents=True)
        with stream.open("wb") as handle:
            handle.truncate(STREAM_BYTE_LIMIT + 1)

        with self.assertRaises(EvidenceTooLarge):
            self.store.seal_evidence("run-001", "attempts/attempt-1")

    def test_attempt_and_gate_byte_limits_are_enforced(self):
        self.create_run()
        cases = (
            ("attempts/attempt-1", ATTEMPT_BYTE_LIMIT),
            ("gates/cycle-1", GATE_BYTE_LIMIT),
        )
        for relative_directory, limit in cases:
            with self.subTest(relative_directory=relative_directory):
                payload = (
                    self.root / "runs" / "run-001" / relative_directory / "log.txt"
                )
                payload.parent.mkdir(parents=True)
                with payload.open("wb") as handle:
                    handle.truncate(limit + 1)
                with self.assertRaises(EvidenceTooLarge):
                    self.store.seal_evidence("run-001", relative_directory)

    def test_manifest_refuses_non_utf8_evidence(self):
        self.create_run()
        payload = self.root / "runs" / "run-001" / "attempts" / "attempt-1" / "log.txt"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"\xff\xfe")

        with self.assertRaises(EvidenceError):
            self.store.seal_evidence("run-001", "attempts/attempt-1")

    def test_evidence_writer_refuses_symlinked_path_components(self):
        self.create_run()
        attempts = self.root / "runs" / "run-001" / "attempts"
        target = attempts / "target"
        target.mkdir()
        (attempts / "linked").symlink_to(target, target_is_directory=True)

        with self.assertRaises(EvidenceError):
            self.store.write_evidence_text(
                "run-001",
                "attempts/linked/stdout.txt",
                "safe output\n",
            )

    def test_evidence_writer_cannot_reopen_a_sealed_directory(self):
        self.create_run()
        self.store.write_evidence_text(
            "run-001", "attempts/attempt-1/stdout.txt", "complete\n"
        )
        self.store.seal_evidence("run-001", "attempts/attempt-1")
        directory = self.root / "runs" / "run-001" / "attempts" / "attempt-1"

        with self.assertRaises(EvidenceError):
            self.store.write_evidence_text(
                "run-001", "attempts/attempt-1/stderr.txt", "late write\n"
            )
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o500)

    def test_status_cli_reports_named_and_active_run(self):
        self.create_run()
        env = {"XDG_STATE_HOME": str(self.state_home)}

        named = run_afk("status", "run-001", "--json", env_overrides=env)
        active = run_afk("status", "--json", env_overrides=env)
        readable = run_afk("status", "run-001", env_overrides=env)

        self.assertEqual(named.returncode, 0, named.stderr)
        self.assertEqual(json.loads(named.stdout)["run_id"], "run-001")
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(json.loads(active.stdout)["state"], "created")
        self.assertEqual(readable.returncode, 0, readable.stderr)
        self.assertIn("run-001", readable.stdout)
        self.assertIn("created", readable.stdout)


if __name__ == "__main__":
    unittest.main()
