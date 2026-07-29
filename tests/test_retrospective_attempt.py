import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.retrospective_attempt import (  # noqa: E402
    RETROSPECTIVE_TIMEOUT_SECONDS,
    retrospective_evidence_identity,
    run_retrospective_attempt,
)
from afk.run_store import RunStore  # noqa: E402


BASE_SHA = "a" * 40


class RetrospectiveAttemptTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = RunStore(self.root / "afk")
        self.store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"secret": "must-not-cross-the-boundary"},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "validated"},
            recorded_at="2026-07-28T10:01:00Z",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_runs_one_fresh_contained_analysis_and_reuses_its_sealed_outcome(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        analyzer = self.analyzer(
            f"""
            import json, sys
            request = json.load(sys.stdin)
            print(json.dumps({analysis!r}))
            print("REQUEST=" + json.dumps(request, sort_keys=True), file=sys.stderr)
            """
        )

        first = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_command=[str(analyzer)],
        )
        second = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_command=[str(self.root / "must-not-run")],
        )

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "empty")
        self.assertFalse(first["warning"])
        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        self.assertTrue(self.store.verify_evidence("run-001", evidence))
        files = self.evidence_files(evidence)
        self.assertEqual(
            files,
            {
                "analysis.json",
                "command.json",
                "input.json",
                "manifest.json",
                "outcome.json",
                "result.json",
                "stderr.log",
                "stdout.log",
            },
        )
        command = self.evidence_json(evidence, "command.json")
        self.assertEqual(command["timeout_seconds"], RETROSPECTIVE_TIMEOUT_SECONDS)
        self.assertEqual(
            command["policy"],
            {
                "filesystem": "read-only",
                "interactive": False,
                "network": "disabled",
                "session": "fresh",
            },
        )
        self.assertIn("--sandbox", command["argv"])
        self.assertEqual(
            command["argv"][command["argv"].index("--sandbox") + 1],
            "read-only",
        )
        self.assertIn("--ephemeral", command["argv"])
        self.assertIn(
            "sandbox_workspace_write.network_access=false",
            command["argv"],
        )
        self.assertIn("features.web_search=false", command["argv"])
        request = self.evidence_json(evidence, "input.json")
        serialized_request = json.dumps(request)
        self.assertEqual(request["run"]["run_id"], "run-001")
        self.assertNotIn("must-not-cross-the-boundary", serialized_request)
        self.assertNotIn("repository source", serialized_request)

    def test_nonempty_valid_analysis_is_passed(self):
        analysis = self.empty_analysis(summary="One process issue was found.")
        analysis["process_findings"] = [
            {
                "id": "finding-1",
                "category": "orchestration",
                "title": "Attention interrupted the run",
                "evidence": [
                    {"artifact": "events.jsonl", "event_sequence": 2},
                ],
                "impact": "An operator had to resume it.",
                "confidence": "high",
            }
        ]
        analyzer = self.analyzer(f"print({json.dumps(json.dumps(analysis))})")

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_command=[str(analyzer)],
        )

        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(outcome["process_findings_count"], 1)
        self.assertEqual(outcome["improvement_proposals_count"], 0)

    def test_completed_episode_gets_its_own_stable_identity(self):
        store = RunStore(self.root / "completed" / "afk")
        store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={},
            recorded_at="2026-07-28T10:01:00Z",
        )
        analysis = self.empty_analysis(summary="No actionable findings.")
        analysis["terminal_outcome"] = "completed"
        analyzer = self.analyzer(f"print({json.dumps(json.dumps(analysis))})")

        outcome = run_retrospective_attempt(
            store,
            "run-001",
            episode_sequence=2,
            codex_command=[str(analyzer)],
        )

        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(
            retrospective_evidence_identity(
                store,
                "run-001",
                episode_sequence=2,
            ),
            "retrospective/completed-2",
        )

    def test_invalid_and_unavailable_results_are_sealed_warnings(self):
        cases = (
            ("invalid", [str(self.analyzer("print('not-json')"))]),
            ("unavailable", [str(self.root / "missing-codex")]),
            (
                "unavailable",
                [str(self.analyzer("raise SystemExit(7)"))],
            ),
        )
        for index, (expected, command) in enumerate(cases, start=1):
            with self.subTest(expected=expected, index=index):
                store, sequence = self.distinct_episode(index)
                outcome = run_retrospective_attempt(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                    codex_command=command,
                )
                self.assertEqual(outcome["status"], expected)
                self.assertTrue(outcome["warning"])
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                self.assertTrue(store.verify_evidence("run-001", evidence))
                self.assertNotIn(
                    "analysis.json",
                    self.evidence_files(evidence, store=store),
                )

    def test_timeout_is_interrupted_and_never_retried(self):
        analyzer = self.analyzer(
            """
            import subprocess, time
            subprocess.Popen(["sleep", "60"])
            time.sleep(60)
            """
        )
        with patch("afk.retrospective_attempt.RETROSPECTIVE_TIMEOUT_SECONDS", 0.05):
            first = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_command=[str(analyzer)],
            )

        second = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_command=[str(self.root / "must-not-run")],
        )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "interrupted")
        self.assertTrue(first["warning"])

    def empty_analysis(self, *, summary):
        return {
            "schema_version": 1,
            "run_id": "run-001",
            "terminal_outcome": "attention_required",
            "summary": summary,
            "process_findings": [],
            "improvement_proposals": [],
        }

    def analyzer(self, program):
        path = self.root / f"codex-{len(list(self.root.glob('codex-*')))}"
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(program).strip() + "\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def distinct_episode(self, index):
        root = self.root / f"case-{index}"
        store = RunStore(root / "afk")
        store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": f"case-{index}"},
            recorded_at="2026-07-28T10:01:00Z",
        )
        return store, 2

    def evidence_files(self, evidence, *, store=None):
        selected = store or self.store
        directory = selected.root / "runs" / "run-001" / evidence
        return {path.name for path in directory.iterdir()}

    def evidence_json(self, evidence, name):
        path = self.store.root / "runs" / "run-001" / evidence / name
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
