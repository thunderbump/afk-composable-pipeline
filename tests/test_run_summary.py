import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.jsonutil import canonical_json, sha256_json  # noqa: E402
from afk.run_store import RunStore, RunStoreError  # noqa: E402
from afk.run_summary import (  # noqa: E402
    MAX_RUN_SUMMARY_BYTES,
    build_run_summary,
)


BASE_SHA = "a" * 40


class RunSummaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.temporary_directory.name) / "afk")
        self.store.create_run(
            bead_id="central-bhap.8.1",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"token": "start-request-secret"},
            run_id="run-001",
            created_at="2026-07-27T10:00:00Z",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_builds_a_deterministic_bounded_episode_summary_from_durable_facts(self):
        self.store.prepare_effect(
            "run-001",
            "worker-launch-1",
            kind="worker-launch",
            intended={
                "unit": "afk-worker-1",
                "patch": "FULL-DIFF-MUST-NOT-CROSS-BOUNDARY",
            },
        )
        self.store.confirm_effect(
            "run-001",
            "worker-launch-1",
            observed={"token": "effect-secret"},
        )
        self.store.write_evidence_text(
            "run-001",
            "attempts/attempt-1/stdout.txt",
            "RAW-LOG-CONTENT token=evidence-secret\n",
        )
        manifest = self.store.seal_evidence("run-001", "attempts/attempt-1")
        self.store.append_event(
            "run-001",
            "worker.completed",
            state="implemented",
            data={
                "candidate_sha": "b" * 40,
                "worker_result": {
                    "stdout": "RAW-EVENT-LOG-MUST-NOT-CROSS-BOUNDARY",
                    "diff": "FULL-EVENT-DIFF-MUST-NOT-CROSS-BOUNDARY",
                },
            },
            recorded_at="2026-07-27T10:01:00Z",
        )
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "implemented",
                "attention": {
                    "scope": "validation",
                    "kind": "authorization",
                    "summary": "Bearer abcdefghijklmnop",
                },
            },
            recorded_at="2026-07-27T10:02:00Z",
        )

        first = build_run_summary(self.store, "run-001", episode_sequence=3)
        second = build_run_summary(self.store, "run-001", episode_sequence=3)
        summary = json.loads(first)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first.encode("utf-8")), MAX_RUN_SUMMARY_BYTES)
        self.assertEqual(
            summary["episode"],
            {
                "checkpoint": "implemented",
                "event": "run.attention_required",
                "recorded_at": "2026-07-27T10:02:00Z",
                "sequence": 3,
                "state": "attention_required",
            },
        )
        self.assertEqual(summary["run"]["run_id"], "run-001")
        self.assertEqual(summary["run"]["bead_id"], "central-bhap.8.1")
        self.assertEqual(summary["projection"]["candidate_sha"], "b" * 40)
        self.assertEqual(
            summary["projection"]["attention"]["summary"],
            "Bearer [REDACTED]",
        )
        self.assertEqual(
            summary["effects"],
            [
                {
                    "effect_id": "worker-launch-1",
                    "kind": "worker-launch",
                    "status": "confirmed",
                }
            ],
        )
        self.assertEqual(summary["evidence"][0]["unit"], "attempts/attempt-1")
        self.assertEqual(
            summary["evidence"][0]["manifest_sha256"],
            sha256_json(manifest),
        )
        self.assertEqual(summary["evidence"][0]["files"], manifest["files"])
        serialized = first
        for prohibited in (
            "start-request-secret",
            "effect-secret",
            "RAW-LOG-CONTENT",
            "RAW-EVENT-LOG-MUST-NOT-CROSS-BOUNDARY",
            "FULL-DIFF-MUST-NOT-CROSS-BOUNDARY",
            "FULL-EVENT-DIFF-MUST-NOT-CROSS-BOUNDARY",
        ):
            self.assertNotIn(prohibited, serialized)

    def test_rejects_a_sequence_that_is_not_an_attention_or_completion_episode(self):
        with self.assertRaisesRegex(
            RunStoreError, "sequence 1 is not a retrospective episode"
        ):
            build_run_summary(self.store, "run-001", episode_sequence=1)

    def test_caps_large_histories_and_reports_omitted_records(self):
        events = [
            {
                "schema_version": 1,
                "sequence": 1,
                "recorded_at": "2026-07-27T10:00:00Z",
                "event": "run.created",
                "state": "created",
                "data": {"bead_id": "central-bhap.8.1"},
            }
        ]
        for sequence in range(80):
            events.append(
                {
                    "schema_version": 1,
                    "sequence": sequence + 2,
                    "recorded_at": (
                        f"2026-07-27T10:{sequence // 60:02d}:{sequence % 60:02d}Z"
                    ),
                    "event": f"step.observed.{sequence:03d}",
                    "state": "working",
                    "data": {"worker_result": {"stdout": "x" * 10_000}},
                }
            )
        events.append(
            {
                "schema_version": 1,
                "sequence": 82,
                "recorded_at": "2026-07-27T12:00:00Z",
                "event": "run.completed",
                "state": "completed",
                "data": {
                    "checkpoint": "completed",
                    "completion": {"status": "merged"},
                },
            }
        )
        events_path = self.store.root / "runs" / "run-001" / "events.jsonl"
        events_path.write_text(
            "".join(f"{canonical_json(event)}\n" for event in events),
            encoding="utf-8",
        )

        rendered = build_run_summary(self.store, "run-001", episode_sequence=82)
        summary = json.loads(rendered)

        self.assertLessEqual(len(rendered.encode("utf-8")), MAX_RUN_SUMMARY_BYTES)
        self.assertGreater(summary["omitted"]["events"], 0)
        self.assertEqual(summary["events"][-1]["sequence"], 82)


if __name__ == "__main__":
    unittest.main()
