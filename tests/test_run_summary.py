import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.jsonutil import canonical_json, sha256_json  # noqa: E402
from afk.run_store import RunStore, RunStoreError  # noqa: E402
from afk.run_summary import (  # noqa: E402
    MAX_RUN_SUMMARY_BYTES,
    RUN_SUMMARY_EVIDENCE_PREFIX,
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
        self.assertEqual(
            summary["citation_manifest"],
            {
                "effects.json": {"kind": "json", "summary_pointer": "/effects"},
                "episode-checkpoint.txt": {
                    "kind": "text",
                    "summary_pointer": "/episode/checkpoint",
                },
                "episode.json": {"kind": "json", "summary_pointer": "/episode"},
                "events.jsonl": {"kind": "event", "summary_pointer": "/events"},
                "evidence.json": {"kind": "json", "summary_pointer": "/evidence"},
                "omitted.json": {"kind": "json", "summary_pointer": "/omitted"},
                "projection.json": {
                    "kind": "json",
                    "summary_pointer": "/projection",
                },
                "run.json": {"kind": "json", "summary_pointer": "/run"},
            },
        )
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

    def test_builds_current_summary_without_mutating_sealed_v1_summary(self):
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "created"},
            recorded_at="2026-07-27T10:01:00Z",
        )
        v1_summary = canonical_json(
            {
                "schema_version": 1,
                "run": {
                    "run_id": "run-001",
                    "bead_id": "central-bhap.8.1",
                    "repository": "https://example.invalid/acme/pipeline.git",
                    "base_branch": "main",
                    "base_sha": BASE_SHA,
                    "created_at": "2026-07-27T10:00:00Z",
                },
                "episode": {
                    "sequence": 2,
                    "event": "run.attention_required",
                    "recorded_at": "2026-07-27T10:01:00Z",
                    "state": "attention_required",
                    "checkpoint": "created",
                },
                "projection": {
                    "state": "attention_required",
                    "checkpoint": "created",
                },
                "events": [
                    {
                        "sequence": 1,
                        "event": "run.created",
                        "recorded_at": "2026-07-27T10:00:00Z",
                        "state": "created",
                    },
                    {
                        "sequence": 2,
                        "event": "run.attention_required",
                        "recorded_at": "2026-07-27T10:01:00Z",
                        "state": "attention_required",
                    },
                ],
                "effects": [],
                "evidence": [],
                "omitted": {
                    "events": 0,
                    "effects": 0,
                    "evidence_units": 0,
                    "evidence_files": 0,
                },
            }
        )
        v1_result = {
            "schema_version": 1,
            "run_id": "run-001",
            "episode_sequence": 2,
            "episode_event": "run.attention_required",
            "episode_state": "attention_required",
            "summary": v1_summary,
        }
        v1_identity = "retrospective/run-summary-00000000000000000002"
        self.store.reconcile_evidence_result("run-001", v1_identity, v1_result)

        current_summary = build_run_summary(
            self.store,
            "run-001",
            episode_sequence=2,
        )

        self.assertEqual(json.loads(current_summary)["schema_version"], 2)
        self.assertIn("citation_manifest", json.loads(current_summary))
        self.assertEqual(
            self.store.sealed_evidence_result("run-001", v1_identity),
            v1_result,
        )
        self.assertEqual(
            self.store.sealed_evidence_result(
                "run-001",
                f"{RUN_SUMMARY_EVIDENCE_PREFIX}{2:020d}",
            )["summary"],
            current_summary,
        )

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

    def test_concurrent_builder_returns_the_cache_sealed_during_reconciliation(self):
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "created"},
            recorded_at="2026-07-27T10:01:00Z",
        )
        other = RunStore(self.store.root)
        original_sealed = self.store.sealed_evidence_result
        original_reconcile = self.store.reconcile_evidence_result
        raced = []

        def seal_from_other_builder():
            if not raced:
                raced.append(build_run_summary(other, "run-001", episode_sequence=2))

        def sealed_during_lookup(*args, **kwargs):
            result = original_sealed(*args, **kwargs)
            if result is None:
                seal_from_other_builder()
            return result

        def sealed_during_reconcile(*args, **kwargs):
            seal_from_other_builder()
            return original_reconcile(*args, **kwargs)

        with (
            patch.object(
                self.store,
                "sealed_evidence_result",
                side_effect=sealed_during_lookup,
            ),
            patch.object(
                self.store,
                "reconcile_evidence_result",
                side_effect=sealed_during_reconcile,
            ),
        ):
            recovered = build_run_summary(self.store, "run-001", episode_sequence=2)

        self.assertEqual(recovered, raced[0])

    def test_first_build_for_an_old_episode_excludes_later_artifacts(self):
        self.store.prepare_effect(
            "run-001",
            "prior-effect",
            kind="worker-launch",
            intended={},
        )
        self.store.confirm_effect("run-001", "prior-effect", observed={})
        self.store.write_evidence_text(
            "run-001",
            "attempts/prior/stdout.txt",
            "prior evidence\n",
        )
        self.store.seal_evidence("run-001", "attempts/prior")
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "created"},
            recorded_at="2026-07-27T10:01:00Z",
        )

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
        self.store.append_event(
            "run-001",
            "run.activity_resumed",
            state="created",
            data={"checkpoint": "created"},
            recorded_at="2026-07-27T10:02:00Z",
        )

        first = build_run_summary(self.store, "run-001", episode_sequence=2)
        second = build_run_summary(self.store, "run-001", episode_sequence=2)
        summary = json.loads(first)

        self.assertEqual(second, first)
        self.assertEqual(
            summary["effects"],
            [
                {
                    "effect_id": "prior-effect",
                    "kind": "worker-launch",
                    "status": "confirmed",
                }
            ],
        )
        self.assertEqual(
            [record["unit"] for record in summary["evidence"]],
            ["attempts/prior"],
        )

    def test_preupgrade_episode_without_inventory_is_explicitly_unavailable(self):
        with patch("afk.run_store.is_episode_event", return_value=False):
            self.append_completion_episode()

        self.assertEqual(self.store.status("run-001")["state"], "completed")
        self.assertEqual(self.store.event("run-001", 2)["event"], "run.completed")
        with self.assertRaisesRegex(
            RunStoreError,
            "retrospective inventory is unavailable for episode 2",
        ):
            build_run_summary(self.store, "run-001", episode_sequence=2)

    def test_corrupt_artifacts_do_not_block_attention_but_make_summary_unavailable(
        self,
    ):
        self.store.write_evidence_text(
            "run-001",
            "gates/validation/result.json",
            "{}\n",
        )
        self.store.seal_evidence("run-001", "gates/validation")
        result_path = self.store.root / "runs/run-001/gates/validation/result.json"
        result_path.chmod(0o600)
        result_path.write_text("tampered\n", encoding="utf-8")

        projection = self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "created",
                "attention": {
                    "scope": "validation",
                    "kind": "invalid",
                    "summary": "Validation evidence is invalid",
                },
            },
            recorded_at="2026-07-27T10:01:00Z",
        )

        self.assertEqual(projection["attention"]["scope"], "validation")
        self.assertEqual(projection["attention"]["kind"], "invalid")
        with self.assertRaisesRegex(
            RunStoreError,
            "retrospective inventory is unavailable for episode 2",
        ):
            build_run_summary(self.store, "run-001", episode_sequence=2)

    def test_seals_and_reuses_a_complete_summary_after_an_interrupted_seal(self):
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "created"},
            recorded_at="2026-07-27T10:01:00Z",
        )
        evidence = f"{RUN_SUMMARY_EVIDENCE_PREFIX}{2:020d}"
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

    def fabricated_summary_result(self):
        summary = {
            "schema_version": 2,
            "run": {
                "run_id": "run-001",
                "bead_id": "central-bhap.8.1",
                "repository": "https://example.invalid/acme/pipeline.git",
                "base_branch": "main",
                "base_sha": BASE_SHA,
                "created_at": "2026-07-27T10:00:00Z",
            },
            "episode": {
                "sequence": 2,
                "event": "run.completed",
                "recorded_at": "2026-07-27T11:00:00Z",
                "state": "completed",
                "checkpoint": "completed",
            },
            "projection": {
                "state": "completed",
                "checkpoint": "completed",
                "candidate_sha": "b" * 40,
            },
            "events": [
                {
                    "sequence": 1,
                    "event": "fabricated.event",
                    "recorded_at": "2026-07-27T10:00:00Z",
                    "state": "created",
                },
                {
                    "sequence": 2,
                    "event": "run.completed",
                    "recorded_at": "2026-07-27T11:00:00Z",
                    "state": "completed",
                },
            ],
            "effects": [
                {
                    "effect_id": "fabricated-effect",
                    "kind": "worker-launch",
                    "status": "confirmed",
                }
            ],
            "evidence": [],
            "omitted": {
                "events": 0,
                "effects": 0,
                "evidence_units": 0,
                "evidence_files": 0,
            },
        }
        summary["citation_manifest"] = {
            "effects.json": {"kind": "json", "summary_pointer": "/effects"},
            "episode-checkpoint.txt": {
                "kind": "text",
                "summary_pointer": "/episode/checkpoint",
            },
            "episode.json": {"kind": "json", "summary_pointer": "/episode"},
            "events.jsonl": {"kind": "event", "summary_pointer": "/events"},
            "evidence.json": {"kind": "json", "summary_pointer": "/evidence"},
            "omitted.json": {"kind": "json", "summary_pointer": "/omitted"},
            "projection.json": {
                "kind": "json",
                "summary_pointer": "/projection",
            },
            "run.json": {"kind": "json", "summary_pointer": "/run"},
        }
        return {
            "schema_version": 1,
            "run_id": "run-001",
            "episode_sequence": 2,
            "episode_event": "run.completed",
            "episode_state": "completed",
            "summary": json.dumps(
                summary,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    def append_completion_episode(self):
        self.store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={"checkpoint": "completed"},
            recorded_at="2026-07-27T11:00:00Z",
        )

    def test_rejects_a_schema_valid_presealed_summary_with_fabricated_facts(self):
        self.append_completion_episode()
        self.store.reconcile_evidence_result(
            "run-001",
            f"{RUN_SUMMARY_EVIDENCE_PREFIX}{2:020d}",
            self.fabricated_summary_result(),
        )

        with self.assertRaisesRegex(
            RunStoreError, "Run Summary does not match durable facts"
        ):
            build_run_summary(self.store, "run-001", episode_sequence=2)

    def test_rejects_a_schema_valid_unsealed_summary_with_fabricated_facts(self):
        self.append_completion_episode()
        self.store.write_evidence_value(
            "run-001",
            f"{RUN_SUMMARY_EVIDENCE_PREFIX}{2:020d}/result.json",
            self.fabricated_summary_result(),
        )

        with self.assertRaisesRegex(
            RunStoreError, "Run Summary does not match durable facts"
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

    def test_replaces_a_wide_mandatory_checkpoint_with_one_bounded_marker(self):
        def wide_value(depth):
            if depth == 0:
                return "leaf"
            return {f"branch-{index}": wide_value(depth - 1) for index in range(16)}

        self.store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={
                "checkpoint": wide_value(4),
                "completion": {"status": "merged"},
            },
            recorded_at="2026-07-27T12:00:00Z",
        )

        first = build_run_summary(self.store, "run-001", episode_sequence=2)
        second = build_run_summary(self.store, "run-001", episode_sequence=2)
        summary = json.loads(first)

        self.assertEqual(second, first)
        self.assertLessEqual(len(first.encode("utf-8")), MAX_RUN_SUMMARY_BYTES)
        self.assertEqual(summary["episode"]["checkpoint"], "[TRUNCATED]")
        self.assertEqual(summary["projection"]["checkpoint"], "[TRUNCATED]")
        self.assertEqual(summary["projection"]["completion"], {"status": "merged"})

    def test_bounds_an_oversized_checkpoint_key(self):
        oversized_key = "checkpoint-" + ("x" * MAX_RUN_SUMMARY_BYTES)
        secret_key = "token=checkpoint-key-secret"
        self.store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={"checkpoint": {oversized_key: "kept", secret_key: "redacted"}},
            recorded_at="2026-07-27T12:00:00Z",
        )

        first = build_run_summary(self.store, "run-001", episode_sequence=2)
        second = build_run_summary(self.store, "run-001", episode_sequence=2)
        summary = json.loads(first)
        checkpoint = summary["episode"]["checkpoint"]

        self.assertEqual(first, second)
        self.assertLessEqual(len(first.encode("utf-8")), MAX_RUN_SUMMARY_BYTES)
        self.assertNotIn(oversized_key, first)
        self.assertNotIn(secret_key, first)
        self.assertEqual(
            checkpoint,
            {"[TRUNCATED]": "kept", "token=[REDACTED]": "[REDACTED]"},
        )
        self.assertEqual(summary["projection"]["checkpoint"], checkpoint)

    def test_truncates_a_mapping_when_bounded_keys_would_collide(self):
        shared_prefix = "x" * MAX_RUN_SUMMARY_BYTES
        self.store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={
                "checkpoint": {
                    f"{shared_prefix}-first": "first",
                    f"{shared_prefix}-second": "second",
                }
            },
            recorded_at="2026-07-27T12:00:00Z",
        )

        first = build_run_summary(self.store, "run-001", episode_sequence=2)
        second = build_run_summary(self.store, "run-001", episode_sequence=2)
        summary = json.loads(first)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first.encode("utf-8")), MAX_RUN_SUMMARY_BYTES)
        self.assertEqual(summary["episode"]["checkpoint"], "[TRUNCATED]")
        self.assertEqual(summary["projection"]["checkpoint"], "[TRUNCATED]")


if __name__ == "__main__":
    unittest.main()
