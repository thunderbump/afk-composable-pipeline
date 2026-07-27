import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.jsonutil import sha256_json  # noqa: E402
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

    def test_reuses_the_sealed_episode_summary_after_later_artifacts_are_added(self):
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "created"},
            recorded_at="2026-07-27T10:01:00Z",
        )

        first = build_run_summary(self.store, "run-001", episode_sequence=2)

        self.store.prepare_effect(
            "run-001",
            "later-effect",
            kind="worker-launch",
            intended={"token": "later-effect-secret"},
        )
        self.store.confirm_effect(
            "run-001",
            "later-effect",
            observed={"status": "later"},
        )
        self.store.write_evidence_text(
            "run-001",
            "attempts/later/stdout.txt",
            "later evidence\n",
        )
        self.store.seal_evidence("run-001", "attempts/later")

        second = build_run_summary(self.store, "run-001", episode_sequence=2)

        self.assertEqual(second, first)
        self.assertEqual(json.loads(second)["effects"], [])
        self.assertEqual(json.loads(second)["evidence"], [])

    def test_seals_and_reuses_a_complete_summary_after_an_interrupted_seal(self):
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "created"},
            recorded_at="2026-07-27T10:01:00Z",
        )
        evidence = "retrospective/run-summary-00000000000000000002"
        with patch.object(
            self.store,
            "seal_evidence",
            side_effect=RuntimeError("crash before summary seal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "crash before summary seal"):
                build_run_summary(self.store, "run-001", episode_sequence=2)
        durable = self.store.unsealed_evidence_result("run-001", evidence)

        self.store.prepare_effect(
            "run-001",
            "later-effect",
            kind="worker-launch",
            intended={},
        )
        self.store.write_evidence_text(
            "run-001",
            "attempts/later/stdout.txt",
            "later evidence\n",
        )
        self.store.seal_evidence("run-001", "attempts/later")

        recovered = build_run_summary(self.store, "run-001", episode_sequence=2)

        self.assertEqual(recovered, durable["summary"])
        self.assertEqual(
            self.store.sealed_evidence_result("run-001", evidence), durable
        )
        self.assertEqual(json.loads(recovered)["effects"], [])
        self.assertEqual(json.loads(recovered)["evidence"], [])

    def test_rejects_a_presealed_summary_outside_the_public_contract(self):
        self.store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={"checkpoint": "completed"},
            recorded_at="2026-07-27T11:00:00Z",
        )
        self.store.reconcile_evidence_result(
            "run-001",
            "retrospective/run-summary-00000000000000000002",
            {
                "schema_version": 1,
                "run_id": "run-001",
                "episode_sequence": 2,
                "episode_event": "run.completed",
                "episode_state": "completed",
                "summary": json.dumps(
                    {
                        "run": {
                            "run_id": "run-001",
                            "repository": "Bearer abcdefghijklmnop",
                        },
                        "episode": {
                            "sequence": 2,
                            "event": "run.completed",
                            "state": "completed",
                        },
                        "raw_log": "RAW-LOG-CONTENT",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )

        with self.assertRaisesRegex(
            RunStoreError, "sealed Run Summary content is invalid"
        ):
            build_run_summary(self.store, "run-001", episode_sequence=2)

    def test_rejects_a_sequence_that_is_not_an_attention_or_completion_episode(self):
        with self.assertRaisesRegex(
            RunStoreError, "sequence 1 is not a retrospective episode"
        ):
            build_run_summary(self.store, "run-001", episode_sequence=1)

    def test_bounds_a_large_completion_fact_and_keeps_the_terminal_episode(self):
        self.store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={
                "checkpoint": "completed",
                "completion": {
                    "status": "merged",
                    "detail": [
                        "x" * 1024 for _ in range((MAX_RUN_SUMMARY_BYTES * 2) // 1024)
                    ],
                },
            },
            recorded_at="2026-07-27T12:00:00Z",
        )

        rendered = build_run_summary(self.store, "run-001", episode_sequence=2)
        summary = json.loads(rendered)

        self.assertLessEqual(len(rendered.encode("utf-8")), MAX_RUN_SUMMARY_BYTES)
        self.assertEqual(
            summary["episode"],
            {
                "checkpoint": "completed",
                "event": "run.completed",
                "recorded_at": "2026-07-27T12:00:00Z",
                "sequence": 2,
                "state": "completed",
            },
        )
        self.assertEqual(summary["events"][-1]["sequence"], 2)
        self.assertEqual(summary["projection"]["completion"]["status"], "merged")
        self.assertEqual(len(summary["projection"]["completion"]["detail"]), 16)
        self.assertTrue(
            all(
                detail.endswith("…[TRUNCATED]")
                for detail in summary["projection"]["completion"]["detail"]
            )
        )


if __name__ == "__main__":
    unittest.main()
