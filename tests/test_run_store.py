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
    RunNotFound,
    RunStore,
    RunStoreBusy,
    RunStoreError,
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

    def test_global_lock_is_reentrant_and_refuses_a_distinct_mutator(self):
        other_store = RunStore(self.root)

        with self.store.lock():
            self.create_run()
            with self.assertRaises(RunStoreBusy):
                other_store.append_event("run-001", "bead.claimed", state="claimed")

        projection = other_store.append_event(
            "run-001", "bead.claimed", state="claimed"
        )
        self.assertEqual(projection["state"], "claimed")

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

    def test_completing_a_run_clears_active_pointer_and_allows_the_next_run(self):
        self.create_run()

        completed = self.store.append_event(
            "run-001", "run.completed", state="completed"
        )

        self.assertFalse((self.root / "active.json").exists())
        with self.assertRaises(RunNotFound):
            self.store.status()
        self.assertEqual(self.store.status("run-001"), completed)

        next_run = self.create_run("run-002")
        self.assertEqual(next_run["run_id"], "run-002")

    def test_attention_required_run_remains_active(self):
        self.create_run()

        attention = self.store.append_event(
            "run-001", "run.attention_required", state="attention_required"
        )

        active = json.loads((self.root / "active.json").read_text(encoding="utf-8"))
        self.assertEqual(active, {"run_id": "run-001"})
        self.assertEqual(self.store.status(), attention)
        with self.assertRaises(ActiveRunExists):
            self.create_run("run-002")

    def test_effect_rejects_malformed_durable_record_shapes(self):
        self.create_run()
        prepared = self.store.prepare_effect(
            "run-001",
            "worker-launch-1",
            kind="worker-launch",
            intended={"unit": "afk-run-001-worker-1"},
        )
        effect_path = (
            self.root / "runs" / "run-001" / "effects" / "worker-launch-1.json"
        )
        cases = {
            "kind": {**prepared, "kind": ""},
            "intended": {**prepared, "intended": []},
            "confirmed observed": {
                **prepared,
                "status": "confirmed",
                "observed": [],
            },
        }

        for field, record in cases.items():
            with self.subTest(field=field):
                effect_path.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(RunStoreError):
                    self.store.effect("run-001", "worker-launch-1")

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

    def test_evidence_ingestion_redacts_before_writing_to_the_run_store(self):
        self.create_run()
        source_path = self.state_home / "worker-output.txt"
        source_path.write_text("token=plain-secret-value\n", encoding="utf-8")

        evidence_path = self.store.ingest_evidence_file(
            "run-001",
            "attempts/attempt-1/raw.txt",
            source_path,
        )
        manifest = self.store.seal_evidence("run-001", "attempts/attempt-1")

        self.assertEqual(
            evidence_path.read_text(encoding="utf-8"), "token=[REDACTED]\n"
        )
        run_dir = self.root / "runs" / "run-001"
        self.assertFalse(
            any(
                b"plain-secret-value" in path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            )
        )
        self.assertEqual(
            manifest["files"][0]["sha256"],
            "6f878ef066794d2e71b92b9d70e321cf7cbd1d0361168fca105df2b87e7a3b9a",
        )

    def test_sealing_refuses_evidence_that_bypassed_ingestion(self):
        self.create_run()
        evidence_path = (
            self.root / "runs" / "run-001" / "attempts" / "attempt-1" / "raw.txt"
        )
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_text("token=plain-secret-value\n", encoding="utf-8")

        with self.assertRaises(EvidenceError):
            self.store.seal_evidence("run-001", "attempts/attempt-1")

        self.assertFalse((evidence_path.parent / "manifest.json").exists())

    def test_verification_rejects_a_tampered_manifest_total(self):
        self.create_run()
        self.store.write_evidence_text(
            "run-001", "attempts/attempt-1/output.txt", "complete\n"
        )
        self.store.seal_evidence("run-001", "attempts/attempt-1")
        manifest_path = (
            self.root / "runs" / "run-001" / "attempts" / "attempt-1" / "manifest.json"
        )
        manifest_path.chmod(0o600)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["total_bytes"] += 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.chmod(0o400)

        with self.assertRaises(EvidenceTampered):
            self.store.verify_evidence("run-001", "attempts/attempt-1")

    def test_verification_rejects_booleans_in_manifest_integer_fields(self):
        self.create_run()
        self.store.write_evidence_text("run-001", "attempts/attempt-1/output.txt", "x")
        self.store.seal_evidence("run-001", "attempts/attempt-1")
        manifest_path = (
            self.root / "runs" / "run-001" / "attempts" / "attempt-1" / "manifest.json"
        )
        original = json.loads(manifest_path.read_text(encoding="utf-8"))

        for field in ("schema_version", "file_bytes"):
            with self.subTest(field=field):
                manifest = json.loads(json.dumps(original))
                if field == "schema_version":
                    manifest["schema_version"] = True
                else:
                    manifest["files"][0]["bytes"] = True
                manifest_path.chmod(0o600)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                manifest_path.chmod(0o400)

                with self.assertRaises(EvidenceTampered):
                    self.store.verify_evidence("run-001", "attempts/attempt-1")

    def test_verification_rejects_writable_sealed_directories(self):
        self.create_run()
        self.store.write_evidence_text(
            "run-001", "attempts/attempt-1/nested/output.txt", "complete\n"
        )
        self.store.seal_evidence("run-001", "attempts/attempt-1")
        sealed_root = self.root / "runs" / "run-001" / "attempts" / "attempt-1"

        for directory in (sealed_root, sealed_root / "nested"):
            with self.subTest(directory=directory.relative_to(sealed_root)):
                directory.chmod(0o700)
                with self.assertRaises(EvidenceTampered):
                    self.store.verify_evidence("run-001", "attempts/attempt-1")
                directory.chmod(0o500)

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
