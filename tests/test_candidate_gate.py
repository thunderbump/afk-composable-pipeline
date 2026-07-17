import json
import sys
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk import candidate_gate as candidate_gate_module  # noqa: E402
from afk.candidate_gate import (  # noqa: E402
    GateError,
    build_repair_brief,
    complete_gate_cycle,
    normalize_review_result,
    reconcile_gate_comment,
    run_candidate_reviews,
)
from afk.run_store import EvidenceTampered, RunStore  # noqa: E402
from afk.start import _advance_completed_gate, resume_run  # noqa: E402


class CandidateGateTest(unittest.TestCase):
    def test_review_permission_profile_keeps_inputs_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            bundle = root / "bundle"
            output = root / "output"
            worktree.mkdir()
            bundle.mkdir()
            output.mkdir()

            args = candidate_gate_module._review_permission_args(
                worktree, bundle, output
            )

            config = "\n".join(
                args[index + 1] for index, value in enumerate(args) if value == "-c"
            )
            self.assertIn('default_permissions="afk_review"', config)
            self.assertIn(f'"{worktree}" = "read"', config)
            self.assertIn(f'"{bundle}" = "read"', config)
            self.assertIn(f'"{output}" = "write"', config)
            self.assertNotIn(f'"{worktree}" = "write"', config)
            self.assertNotIn(f'"{bundle}" = "write"', config)
            self.assertIn("network = { enabled = false }", config)

    def test_repaired_bootstrap_candidate_pauses_for_explicit_reapproval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            store.append_event(
                run_id,
                "gate.cycle_completed",
                state="candidate_ready",
                data={
                    "checkpoint": "candidate_ready",
                    "candidate_sha": "b" * 40,
                    "validation_contract": {
                        "source": "approved_bootstrap",
                        "base_sha": "a" * 40,
                        "adapter_id": "afk.builtin.bootstrap-validation/v1",
                        "approval": {},
                    },
                },
            )
            outcome = {
                "next_action": "repair",
                "repair_brief": {
                    "candidate_sha": "b" * 40,
                    "repair_attempt": 1,
                    "blocking_findings": [],
                },
            }

            with (
                mock.patch("afk.start.produce_repair_candidate"),
                mock.patch("afk.start._advance_validation") as validation,
            ):
                exit_code = _advance_completed_gate(
                    store,
                    run_id,
                    outcome=outcome,
                    bead={"id": "central-test.1"},
                )

            self.assertEqual(exit_code, 2)
            validation.assert_not_called()
            status = store.status(run_id)
            self.assertEqual(status["attention"]["scope"], "validation")
            self.assertEqual(status["attention"]["kind"], "unavailable")
            self.assertIn("reapproval", status["attention"]["summary"])

    def test_resume_continues_after_durable_candidate_repaired_event(self):
        contracts = (
            ("pinned_base", 0, True),
            ("approved_bootstrap", 2, False),
        )
        for source, expected_exit, advances_validation in contracts:
            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as temporary,
            ):
                store = RunStore(Path(temporary) / "state")
                run_id = store.create_run(
                    bead_id="central-test.1",
                    repository="owner/project",
                    base_branch="main",
                    base_sha="a" * 40,
                    start_request={},
                    run_id="run-1",
                )["run_id"]
                store.append_event(
                    run_id,
                    "gate.cycle_completed",
                    state="candidate_ready",
                    data={
                        "checkpoint": "candidate_ready",
                        "candidate_sha": "b" * 40,
                        "worker_exit_code": 0,
                        "validation_contract": {"source": source},
                        "repair_brief": {
                            "candidate_sha": "b" * 40,
                            "repair_attempt": 1,
                        },
                    },
                )
                store.append_event(
                    run_id,
                    "candidate.repaired",
                    state="candidate_ready",
                    data={
                        "checkpoint": "candidate_ready",
                        "previous_candidate_sha": "b" * 40,
                        "candidate_sha": "c" * 40,
                        "repair_attempts_used": 1,
                        "attention": {},
                    },
                )

                with (
                    mock.patch("afk.start.RunStore", return_value=store),
                    mock.patch(
                        "afk.start._advance_validation", return_value=0
                    ) as validation,
                ):
                    resumed = resume_run()

                self.assertEqual(resumed, (run_id, expected_exit))
                if advances_validation:
                    validation.assert_called_once_with(store, run_id)
                else:
                    validation.assert_not_called()
                    attention = store.status(run_id)["attention"]
                    self.assertEqual(attention["scope"], "validation")
                    self.assertEqual(attention["kind"], "unavailable")
                    self.assertIn("reapproval", attention["summary"])

    def test_resume_continues_validated_repair_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore(Path(temporary) / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            brief = {
                "candidate_sha": "b" * 40,
                "repair_attempt": 1,
                "blocking_findings": [],
            }
            outcome = {"next_action": "repair", "repair_brief": brief}
            store.append_event(
                run_id,
                "gate.cycle_completed",
                state="validated",
                data={
                    "checkpoint": "validated",
                    "candidate_sha": "b" * 40,
                    "gate_cycles": [outcome],
                },
            )
            store.append_event(
                run_id,
                "repair.started",
                data={
                    "checkpoint": "validated",
                    "repair_attempts_used": 1,
                    "repair_brief": brief,
                },
            )

            with (
                mock.patch("afk.start.RunStore", return_value=store),
                mock.patch(
                    "afk.start._advance_completed_gate", return_value=0
                ) as repair,
                mock.patch("afk.start._advance_gate") as gate,
            ):
                resumed = resume_run()

            self.assertEqual(resumed, (run_id, 0))
            repair.assert_called_once_with(store, run_id)
            gate.assert_not_called()

    def test_passed_validation_and_both_reviews_reach_reviewed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            store.write_evidence_text(run_id, "gates/validation-b/result.json", "{}\n")
            store.seal_evidence(run_id, "gates/validation-b")
            store.append_event(
                run_id,
                "validation.passed",
                state="validated",
                data={
                    "checkpoint": "validated",
                    "candidate_sha": "b" * 40,
                    "worktree_path": str(checkout),
                    "pr_number": 7,
                    "validation": {
                        "status": "passed",
                        "candidate_sha": "b" * 40,
                        "summary": "passed",
                        "evidence": "gates/validation-b",
                        "checks": [],
                    },
                },
            )
            reviews = [
                {
                    "axis": axis,
                    "process_status": "succeeded",
                    "status": "passed",
                    "summary": "passed",
                    "findings": [],
                }
                for axis in ("standards", "spec")
            ]

            with (
                mock.patch(
                    "afk.candidate_gate.run_candidate_reviews", return_value=reviews
                ) as run_reviews,
                mock.patch("afk.candidate_gate.reconcile_gate_comment"),
            ):
                outcome = complete_gate_cycle(
                    store, run_id, bead={"id": "central-test.1"}
                )

            self.assertEqual(outcome["next_action"], "complete")
            self.assertEqual(store.status(run_id)["checkpoint"], "reviewed")
            run_reviews.assert_called_once()

    def test_inconclusive_review_preserves_validated_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            store.write_evidence_text(run_id, "gates/validation-b/result.json", "{}\n")
            store.seal_evidence(run_id, "gates/validation-b")
            store.append_event(
                run_id,
                "validation.passed",
                state="validated",
                data={
                    "checkpoint": "validated",
                    "candidate_sha": "b" * 40,
                    "worktree_path": str(checkout),
                    "pr_number": 7,
                    "validation": {
                        "status": "passed",
                        "candidate_sha": "b" * 40,
                        "summary": "passed",
                        "evidence": "gates/validation-b",
                        "checks": [],
                    },
                },
            )
            reviews = [
                {
                    "axis": "standards",
                    "process_status": "failed",
                    "status": "inconclusive",
                    "summary": "reviewer timed out",
                    "findings": [],
                },
                {
                    "axis": "spec",
                    "process_status": "succeeded",
                    "status": "passed",
                    "summary": "passed",
                    "findings": [],
                },
            ]

            with (
                mock.patch(
                    "afk.candidate_gate.run_candidate_reviews", return_value=reviews
                ),
                mock.patch("afk.candidate_gate.reconcile_gate_comment"),
            ):
                outcome = complete_gate_cycle(
                    store, run_id, bead={"id": "central-test.1"}
                )

            self.assertEqual(outcome["next_action"], "attention")
            self.assertEqual(store.status(run_id)["checkpoint"], "validated")

    def test_four_rejected_repairs_exhaust_the_budget_without_a_fifth_brief(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            store.write_evidence_text(run_id, "gates/validation-b/result.json", "{}\n")
            store.seal_evidence(run_id, "gates/validation-b")
            store.append_event(
                run_id,
                "validation.rejected",
                state="candidate_ready",
                data={
                    "checkpoint": "candidate_ready",
                    "candidate_sha": "b" * 40,
                    "worktree_path": str(checkout),
                    "pr_number": 7,
                    "repair_attempts_used": 4,
                    "validation": {
                        "status": "rejected",
                        "candidate_sha": "b" * 40,
                        "summary": "still failing",
                        "evidence": "gates/validation-b",
                        "checks": [
                            {
                                "name": "smoke",
                                "status": "rejected",
                                "log_path": "smoke.log",
                            }
                        ],
                    },
                },
            )

            with mock.patch("afk.candidate_gate.reconcile_gate_comment"):
                outcome = complete_gate_cycle(
                    store, run_id, bead={"id": "central-test.1"}
                )

            self.assertEqual(outcome["next_action"], "attention")
            self.assertIn("four", outcome["stop_reason"])
            self.assertNotIn("repair_brief", outcome)
            self.assertEqual(store.status(run_id)["checkpoint"], "candidate_ready")

    def test_fourth_review_rejection_exhaustion_preserves_validated_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            store.write_evidence_text(run_id, "gates/validation-b/result.json", "{}\n")
            store.seal_evidence(run_id, "gates/validation-b")
            store.append_event(
                run_id,
                "validation.passed",
                state="validated",
                data={
                    "checkpoint": "validated",
                    "candidate_sha": "b" * 40,
                    "worktree_path": str(checkout),
                    "pr_number": 7,
                    "repair_attempts_used": 4,
                    "validation": {
                        "status": "passed",
                        "candidate_sha": "b" * 40,
                        "summary": "passed",
                        "evidence": "gates/validation-b",
                        "checks": [],
                    },
                },
            )
            reviews = [
                {
                    "axis": "standards",
                    "process_status": "succeeded",
                    "status": "rejected",
                    "summary": "still rejected",
                    "findings": [
                        {
                            "id": "standards-blocker",
                            "priority": "high",
                            "title": "Blocking issue",
                            "body": "The issue remains.",
                            "path": "app.py",
                            "line": 1,
                            "blocking": True,
                        }
                    ],
                },
                {
                    "axis": "spec",
                    "process_status": "succeeded",
                    "status": "passed",
                    "summary": "passed",
                    "findings": [],
                },
            ]

            with (
                mock.patch(
                    "afk.candidate_gate.run_candidate_reviews", return_value=reviews
                ),
                mock.patch("afk.candidate_gate.reconcile_gate_comment"),
            ):
                outcome = complete_gate_cycle(
                    store, run_id, bead={"id": "central-test.1"}
                )

            self.assertEqual(outcome["next_action"], "attention")
            self.assertIn("four", outcome["stop_reason"])
            self.assertEqual(store.status(run_id)["checkpoint"], "validated")

    def test_rejected_validation_completes_cycle_and_returns_one_repair_brief(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            store.write_evidence_text(run_id, "gates/validation-b/result.json", "{}\n")
            store.seal_evidence(run_id, "gates/validation-b")
            store.append_event(
                run_id,
                "validation.rejected",
                state="candidate_ready",
                data={
                    "checkpoint": "candidate_ready",
                    "candidate_sha": "b" * 40,
                    "worktree_path": str(checkout),
                    "pr_number": 7,
                    "validation": {
                        "status": "rejected",
                        "candidate_sha": "b" * 40,
                        "summary": "smoke failed",
                        "evidence": "gates/validation-b",
                        "checks": [
                            {
                                "name": "smoke",
                                "status": "rejected",
                                "log_path": "smoke.log",
                            }
                        ],
                    },
                },
            )

            with mock.patch("afk.candidate_gate.reconcile_gate_comment") as comment:
                outcome = complete_gate_cycle(
                    store,
                    run_id,
                    bead={"id": "central-test.1"},
                )

            self.assertEqual(outcome["next_action"], "repair")
            self.assertEqual(outcome["repair_brief"]["repair_attempt"], 1)
            self.assertEqual(outcome["reviews"], [])
            comment.assert_called_once()
            status = store.status(run_id)
            self.assertEqual(status["last_event"], "gate.cycle_completed")
            self.assertEqual(status["gate_cycles"][0]["candidate_sha"], "b" * 40)
            gate = root / "state" / "runs" / run_id / outcome["evidence"]
            self.assertTrue((gate / "manifest.json").is_file())

    def test_gate_comment_reconciles_a_post_confirmation_crash_without_duplication(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            gate = {
                "cycle": 1,
                "candidate_sha": "b" * 40,
                "validation": {"status": "rejected", "summary": "failed"},
                "reviews": [],
                "next_action": "repair",
            }
            posted = []
            comments = []

            def post(repository, pr_number, body, worktree):
                posted.append(body)
                comments.append({"url": "https://example.test/comment/1", "body": body})
                return comments[0]["url"]

            original_confirm = store.confirm_effect
            with (
                mock.patch(
                    "afk.candidate_gate._github_comments",
                    side_effect=lambda *args: list(comments),
                ),
                mock.patch("afk.candidate_gate._post_gate_comment", side_effect=post),
                mock.patch.object(
                    store, "confirm_effect", side_effect=RuntimeError("crash")
                ),
                self.assertRaisesRegex(RuntimeError, "crash"),
            ):
                reconcile_gate_comment(
                    store, run_id, pr_number=7, worktree=root, gate=gate
                )

            with mock.patch(
                "afk.candidate_gate._github_comments", return_value=comments
            ):
                with mock.patch("afk.candidate_gate._post_gate_comment") as duplicate:
                    with mock.patch.object(
                        store, "confirm_effect", side_effect=original_confirm
                    ):
                        reconcile_gate_comment(
                            store, run_id, pr_number=7, worktree=root, gate=gate
                        )

            self.assertEqual(len(posted), 1)
            duplicate.assert_not_called()
            self.assertEqual(
                store.effect(run_id, "gate-comment-1")["status"], "confirmed"
            )

            marker = posted[0].splitlines()[0]
            mismatches = {
                "edited": posted[0] + "edited\n",
                "truncated": marker,
                "marker collision": f"{marker}\nunrelated evidence\n",
            }
            for label, mismatched_body in mismatches.items():
                with self.subTest(label=label):
                    comments[0]["body"] = mismatched_body
                    with (
                        mock.patch(
                            "afk.candidate_gate._github_comments",
                            return_value=comments,
                        ),
                        mock.patch(
                            "afk.candidate_gate._post_gate_comment"
                        ) as replacement,
                        self.assertRaisesRegex(GateError, "content"),
                    ):
                        reconcile_gate_comment(
                            store, run_id, pr_number=7, worktree=root, gate=gate
                        )
                    replacement.assert_not_called()

            duplicate_cases = {
                "two exact": [posted[0], posted[0]],
                "exact and marker collision": [posted[0], marker],
            }
            for label, bodies in duplicate_cases.items():
                with self.subTest(label=label):
                    duplicates = [
                        {
                            "url": f"https://example.test/comment/{index}",
                            "body": duplicate_body,
                        }
                        for index, duplicate_body in enumerate(bodies, start=1)
                    ]
                    with (
                        mock.patch(
                            "afk.candidate_gate._github_comments",
                            return_value=duplicates,
                        ),
                        mock.patch(
                            "afk.candidate_gate._post_gate_comment"
                        ) as replacement,
                        self.assertRaisesRegex(GateError, "duplicate"),
                    ):
                        reconcile_gate_comment(
                            store, run_id, pr_number=7, worktree=root, gate=gate
                        )
                    replacement.assert_not_called()

    def test_gate_comment_reconciliation_reads_all_paginated_comment_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha="a" * 40,
                start_request={},
                run_id="run-1",
            )["run_id"]
            gate = {
                "cycle": 1,
                "candidate_sha": "b" * 40,
                "validation": {"status": "rejected", "summary": "failed"},
                "reviews": [],
                "next_action": "repair",
            }
            posted = []

            def post(repository, pr_number, body, worktree):
                posted.append(body)
                return "https://example.test/comment/1"

            with (
                mock.patch("afk.candidate_gate._github_comments", return_value=[]),
                mock.patch("afk.candidate_gate._post_gate_comment", side_effect=post),
            ):
                reconcile_gate_comment(
                    store, run_id, pr_number=7, worktree=root, gate=gate
                )

            pages = [
                [{"url": "https://example.test/comment/other", "body": "unrelated"}],
                [
                    {
                        "url": "https://example.test/comment/1",
                        "body": posted[0],
                    }
                ],
            ]

            def paginated(command, worktree, **kwargs):
                self.assertIn("--slurp", command)
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(pages), stderr=""
                )

            with (
                mock.patch("afk.candidate_gate._run_gh", side_effect=paginated),
                mock.patch("afk.candidate_gate._post_gate_comment") as duplicate,
            ):
                reconcile_gate_comment(
                    store, run_id, pr_number=7, worktree=root, gate=gate
                )

            duplicate.assert_not_called()

            malformed = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps([[{"body": posted[0]}], {"not": "a page"}]),
                stderr="",
            )
            with (
                mock.patch("afk.candidate_gate._run_gh", return_value=malformed),
                mock.patch("afk.candidate_gate._post_gate_comment") as replacement,
                self.assertRaisesRegex(GateError, "malformed"),
            ):
                reconcile_gate_comment(
                    store, run_id, pr_number=7, worktree=root, gate=gate
                )

            replacement.assert_not_called()

    def test_gate_review_recovery_reuses_only_manifest_valid_completed_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "afk@example.test"],
                cwd=checkout,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "AFK Test"], cwd=checkout, check=True
            )
            (checkout / "app.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.txt"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "commit", "-m", "base"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (checkout / "app.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-am", "candidate"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            candidate_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha=base_sha,
                start_request={"repository_root": str(checkout)},
                run_id="run-1",
            )["run_id"]
            store.write_evidence_text(run_id, "gates/validation-b/result.json", "{}\n")
            store.seal_evidence(run_id, "gates/validation-b")
            store.append_event(
                run_id,
                "candidate.ready",
                state="candidate_ready",
                data={
                    "checkpoint": "candidate_ready",
                    "candidate_sha": candidate_sha,
                    "worktree_path": str(checkout),
                    "pr_number": 7,
                    "validation": {
                        "status": "passed",
                        "candidate_sha": candidate_sha,
                        "evidence": "gates/validation-b",
                    },
                },
            )
            calls = []
            full_bead = {
                "id": "central-test.1",
                "title": "Test",
                "description": "Test the gate.",
                "acceptance_criteria": "Both axes pass.",
            }

            def seal_bundle_before_crash(cycle, bead):
                bundle = f"gates/gate-cycle-{cycle}-{candidate_sha[:12]}/review-bundle"
                original_seal = store.seal_evidence

                def seal_then_crash(observed_run_id, relative_directory):
                    manifest = original_seal(observed_run_id, relative_directory)
                    if relative_directory == bundle:
                        raise RuntimeError("crash after review bundle seal")
                    return manifest

                with (
                    mock.patch.object(
                        store, "seal_evidence", side_effect=seal_then_crash
                    ),
                    mock.patch("afk.candidate_gate._execute_reviewer") as reviewer,
                    self.assertRaisesRegex(RuntimeError, "review bundle seal"),
                ):
                    run_candidate_reviews(
                        store,
                        run_id,
                        cycle=cycle,
                        bead=bead,
                    )
                reviewer.assert_not_called()

            seal_bundle_before_crash(1, full_bead)

            def reviewer(axis, bundle_path, attempt_path, worktree):
                calls.append((axis, bundle_path, attempt_path))
                self.assertTrue((bundle_path / "manifest.json").is_file())
                return (
                    0,
                    {
                        "status": "passed",
                        "summary": f"{axis} passed",
                        "findings": [],
                    },
                    "",
                    "",
                )

            with (
                mock.patch(
                    "afk.candidate_gate._execute_reviewer", side_effect=reviewer
                ),
                mock.patch("afk.candidate_gate.reconcile_gate_comment"),
            ):
                outcome = complete_gate_cycle(
                    store,
                    run_id,
                    bead=full_bead,
                )
            reviews = outcome["reviews"]

            self.assertEqual(
                [review["axis"] for review in reviews], ["standards", "spec"]
            )
            self.assertEqual(calls[0][1], calls[1][1])
            self.assertNotEqual(calls[0][2], calls[1][2])
            self.assertTrue((calls[0][2] / "manifest.json").is_file())
            self.assertTrue((calls[1][2] / "manifest.json").is_file())
            self.assertEqual(outcome["next_action"], "complete")

            seal_bundle_before_crash(2, {"id": "central-test.1"})
            completed_standards = {
                "axis": "standards",
                "process_status": "succeeded",
                "status": "passed",
                "summary": "standards passed before crash",
                "findings": [],
            }
            store.write_evidence_text(
                run_id,
                "attempts/review-cycle-2-standards/report.json",
                (
                    '{"axis":"standards","findings":[],"process_status":'
                    '"succeeded","status":"passed","summary":'
                    '"standards passed before crash"}'
                ),
            )
            store.seal_evidence(run_id, "attempts/review-cycle-2-standards")
            resumed_calls = []

            def resumed_reviewer(axis, bundle_path, attempt_path, worktree):
                resumed_calls.append(axis)
                return (
                    0,
                    {"status": "passed", "summary": "spec passed", "findings": []},
                    "",
                    "",
                )

            with mock.patch(
                "afk.candidate_gate._execute_reviewer",
                side_effect=resumed_reviewer,
            ):
                resumed_reviews = run_candidate_reviews(
                    store,
                    run_id,
                    cycle=2,
                    bead={"id": "central-test.1"},
                )

            self.assertEqual(resumed_calls, ["spec"])
            self.assertEqual(resumed_reviews[0], completed_standards)
            self.assertEqual(resumed_reviews[1]["axis"], "spec")

            store.append_event(
                run_id,
                "repair.started",
                data={"checkpoint": "candidate_ready", "repair_attempts_used": 1},
            )
            gate = f"gates/gate-cycle-2-{candidate_sha[:12]}"
            original_seal = store.seal_evidence

            def crash_before_gate_seal(observed_run_id, relative_directory):
                if relative_directory == gate:
                    raise RuntimeError("crash before Gate seal")
                return original_seal(observed_run_id, relative_directory)

            with (
                mock.patch.object(
                    store, "seal_evidence", side_effect=crash_before_gate_seal
                ),
                mock.patch("afk.candidate_gate.reconcile_gate_comment"),
                self.assertRaisesRegex(RuntimeError, "crash before Gate seal"),
            ):
                complete_gate_cycle(store, run_id, bead={"id": "central-test.1"})

            with (
                mock.patch("afk.candidate_gate._execute_reviewer") as rerun,
                mock.patch(
                    "afk.candidate_gate.reconcile_gate_comment",
                    side_effect=RuntimeError("crash after Gate seal"),
                ),
                self.assertRaisesRegex(RuntimeError, "crash after Gate seal"),
            ):
                complete_gate_cycle(store, run_id, bead={"id": "central-test.1"})

            rerun.assert_not_called()
            with (
                mock.patch("afk.candidate_gate._execute_reviewer") as sealed_rerun,
                mock.patch("afk.candidate_gate.reconcile_gate_comment"),
            ):
                resumed_outcome = complete_gate_cycle(
                    store, run_id, bead={"id": "central-test.1"}
                )
            sealed_rerun.assert_not_called()
            self.assertEqual(resumed_outcome["next_action"], "complete")
            self.assertTrue(
                (root / "state" / "runs" / run_id / gate / "manifest.json").is_file()
            )

            store.append_event(
                run_id,
                "repair.started",
                data={"checkpoint": "candidate_ready", "repair_attempts_used": 2},
            )
            seal_bundle_before_crash(3, {"id": "central-test.1"})
            store.write_evidence_text(
                run_id, "attempts/review-cycle-3-standards/prompt.md", "started\n"
            )
            with (
                mock.patch("afk.candidate_gate._execute_reviewer") as ambiguous_rerun,
                self.assertRaisesRegex(GateError, "incomplete") as raised,
            ):
                complete_gate_cycle(store, run_id, bead={"id": "central-test.1"})
            ambiguous_rerun.assert_not_called()
            self.assertEqual(raised.exception.kind, "inconclusive")

            store.append_event(
                run_id,
                "repair.started",
                data={"checkpoint": "candidate_ready", "repair_attempts_used": 3},
            )
            seal_bundle_before_crash(4, {"id": "central-test.1"})
            tampered_attempt = (
                root / "state" / "runs" / run_id / "attempts/review-cycle-4-standards"
            )
            store.write_evidence_text(
                run_id,
                "attempts/review-cycle-4-standards/report.json",
                (
                    '{"axis":"standards","findings":[],"process_status":'
                    '"succeeded","status":"passed","summary":"passed"}'
                ),
            )
            store.seal_evidence(run_id, "attempts/review-cycle-4-standards")
            report = tampered_attempt / "report.json"
            report.chmod(0o600)
            report.write_text("{}", encoding="utf-8")
            report.chmod(0o400)
            with (
                mock.patch("afk.candidate_gate._execute_reviewer") as tampered_rerun,
                self.assertRaises(EvidenceTampered),
            ):
                complete_gate_cycle(store, run_id, bead={"id": "central-test.1"})
            tampered_rerun.assert_not_called()

            store.append_event(
                run_id,
                "repair.started",
                data={"checkpoint": "candidate_ready", "repair_attempts_used": 4},
            )
            ambiguous_gate = f"gates/gate-cycle-5-{candidate_sha[:12]}"
            store.write_evidence_text(
                run_id, f"{ambiguous_gate}/unexpected.txt", "partial\n"
            )
            with (
                mock.patch("afk.candidate_gate._execute_reviewer") as unsafe_retry,
                self.assertRaisesRegex(GateError, "ambiguous"),
            ):
                complete_gate_cycle(store, run_id, bead={"id": "central-test.1"})
            unsafe_retry.assert_not_called()

            invalid_bundle = f"gates/gate-cycle-6-{candidate_sha[:12]}/review-bundle"
            store.write_evidence_text(run_id, f"{invalid_bundle}/bundle.json", "{}\n")
            store.seal_evidence(run_id, invalid_bundle)
            with (
                mock.patch("afk.candidate_gate._execute_reviewer") as unsafe_review,
                self.assertRaisesRegex(GateError, "bundle"),
            ):
                run_candidate_reviews(
                    store,
                    run_id,
                    cycle=6,
                    bead={"id": "central-test.1"},
                )
            unsafe_review.assert_not_called()

            exact_bead = {"id": "central-test.1"}
            seal_bundle_before_crash(7, exact_bead)
            valid_bundle_path = (
                root
                / "state"
                / "runs"
                / run_id
                / f"gates/gate-cycle-7-{candidate_sha[:12]}/review-bundle/bundle.json"
            )
            valid_bundle = json.loads(valid_bundle_path.read_text(encoding="utf-8"))
            contradictions = {
                "extra field": lambda value: value.update(extra=True),
                "run": lambda value: value.update(run_id="other-run"),
                "repository": lambda value: value.update(repository="other/project"),
                "base": lambda value: value.update(base_sha="f" * 40),
                "candidate crossing": lambda value: value.update(
                    candidate_sha="e" * 40
                ),
                "bead": lambda value: value.update(bead={"id": "central-other.1"}),
                "validation": lambda value: value.update(validation={}),
                "validation manifest": lambda value: value.update(
                    validation_manifest={}
                ),
                "diff": lambda value: value.update(diff="different diff"),
                "instructions": lambda value: value.update(
                    repository_instructions=[
                        {"path": "AGENTS.md", "content": "different"}
                    ]
                ),
                "prior cycles": lambda value: value.update(prior_gate_cycles=[]),
                "prior dispositions": lambda value: value.update(
                    prior_dispositions=[
                        {"finding_id": "other", "disposition": "addressed"}
                    ]
                ),
            }
            for cycle, (label, contradict) in enumerate(
                contradictions.items(), start=8
            ):
                with self.subTest(label=label):
                    contradicted = json.loads(json.dumps(valid_bundle))
                    contradict(contradicted)
                    contradicted_bundle = (
                        f"gates/gate-cycle-{cycle}-{candidate_sha[:12]}"
                        "/review-bundle"
                    )
                    store.write_evidence_text(
                        run_id,
                        f"{contradicted_bundle}/bundle.json",
                        json.dumps(contradicted),
                    )
                    store.seal_evidence(run_id, contradicted_bundle)
                    with (
                        mock.patch(
                            "afk.candidate_gate._execute_reviewer"
                        ) as contradicted_review,
                        self.assertRaisesRegex(GateError, "bundle"),
                    ):
                        run_candidate_reviews(
                            store,
                            run_id,
                            cycle=cycle,
                            bead=exact_bead,
                        )
                    contradicted_review.assert_not_called()

    def test_review_process_success_is_distinct_from_rejected_verdict(self):
        result = normalize_review_result(
            "standards",
            {
                "status": "rejected",
                "summary": "One blocking issue.",
                "findings": [
                    {
                        "id": "1",
                        "priority": "high",
                        "title": "Unsafe fallback",
                        "body": "Remove the fallback.",
                        "path": "src/app.py",
                        "line": 12,
                        "blocking": True,
                    }
                ],
            },
            process_exit_code=0,
        )

        self.assertEqual(result["process_status"], "succeeded")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["findings"][0]["id"], "standards-1")

    def test_failed_review_process_or_protocol_does_not_prevent_the_other_axis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "afk@example.test"],
                cwd=checkout,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "AFK Test"],
                cwd=checkout,
                check=True,
            )
            (checkout / "app.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.txt"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "commit", "-m", "base"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (checkout / "app.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-am", "candidate"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            candidate_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            store = RunStore(root / "state")
            run_id = store.create_run(
                bead_id="central-test.1",
                repository="owner/project",
                base_branch="main",
                base_sha=base_sha,
                start_request={"repository_root": str(checkout)},
                run_id="run-1",
            )["run_id"]
            store.write_evidence_text(run_id, "gates/validation-b/result.json", "{}\n")
            store.seal_evidence(run_id, "gates/validation-b")
            store.append_event(
                run_id,
                "candidate.ready",
                state="candidate_ready",
                data={
                    "checkpoint": "candidate_ready",
                    "candidate_sha": candidate_sha,
                    "worktree_path": str(checkout),
                    "pr_number": 7,
                    "validation": {
                        "status": "passed",
                        "candidate_sha": candidate_sha,
                        "evidence": "gates/validation-b",
                    },
                },
            )

            def reviewer(axis, bundle_path, attempt_path, worktree):
                if axis == "standards":
                    raise GateError("standards reviewer timed out", kind="inconclusive")
                return (
                    0,
                    {"status": "passed", "summary": "spec passed", "findings": []},
                    "spec events\n",
                    "",
                )

            with mock.patch(
                "afk.candidate_gate._execute_reviewer", side_effect=reviewer
            ):
                reviews = run_candidate_reviews(
                    store,
                    run_id,
                    cycle=1,
                    bead={"id": "central-test.1"},
                )

            self.assertEqual(
                reviews,
                [
                    {
                        "axis": "standards",
                        "process_status": "failed",
                        "status": "inconclusive",
                        "summary": "standards reviewer timed out",
                        "findings": [],
                    },
                    {
                        "axis": "spec",
                        "process_status": "succeeded",
                        "status": "passed",
                        "summary": "spec passed",
                        "findings": [],
                    },
                ],
            )
            standards = (
                root / "state" / "runs" / run_id / "attempts/review-cycle-1-standards"
            )
            self.assertTrue((standards / "manifest.json").is_file())
            self.assertTrue((standards / "outcome.json").is_file())

            def malformed_reviewer(axis, bundle_path, attempt_path, worktree):
                if axis == "standards":
                    return 0, {"summary": "missing fields"}, "bad report\n", ""
                return (
                    0,
                    {"status": "passed", "summary": "spec passed", "findings": []},
                    "spec events\n",
                    "",
                )

            with mock.patch(
                "afk.candidate_gate._execute_reviewer",
                side_effect=malformed_reviewer,
            ):
                protocol_reviews = run_candidate_reviews(
                    store,
                    run_id,
                    cycle=2,
                    bead={"id": "central-test.1"},
                )

            self.assertEqual(
                [review["axis"] for review in protocol_reviews],
                ["standards", "spec"],
            )
            self.assertEqual(protocol_reviews[0]["process_status"], "succeeded")
            self.assertEqual(protocol_reviews[0]["status"], "inconclusive")
            self.assertEqual(protocol_reviews[1]["status"], "passed")
            protocol_attempt = (
                root / "state" / "runs" / run_id / "attempts/review-cycle-2-standards"
            )
            self.assertTrue((protocol_attempt / "manifest.json").is_file())
            self.assertTrue((protocol_attempt / "raw-report.txt").is_file())

            store.append_event(
                run_id,
                "repair.started",
                data={"checkpoint": "candidate_ready", "repair_attempts_used": 2},
            )
            with (
                mock.patch(
                    "afk.candidate_gate.run_candidate_reviews",
                    return_value=protocol_reviews,
                ),
                mock.patch("afk.candidate_gate.reconcile_gate_comment"),
            ):
                outcome = complete_gate_cycle(
                    store, run_id, bead={"id": "central-test.1"}
                )

            self.assertEqual(outcome["next_action"], "attention")

    def test_review_rejects_malformed_or_contradictory_output(self):
        with self.assertRaisesRegex(GateError, "findings"):
            normalize_review_result(
                "spec",
                {"status": "passed", "summary": "looks good", "findings": [{}]},
                process_exit_code=0,
            )

        with self.assertRaisesRegex(GateError, "exited"):
            normalize_review_result(
                "spec",
                {"status": "rejected", "summary": "bad", "findings": []},
                process_exit_code=1,
            )

    def test_repair_brief_combines_blocking_validation_and_review_findings(self):
        brief = build_repair_brief(
            candidate_sha="a" * 40,
            cycle=2,
            validation={
                "status": "rejected",
                "summary": "tests failed",
                "checks": [
                    {"name": "unit", "status": "passed", "log_path": "unit.log"},
                    {"name": "smoke", "status": "rejected", "log_path": "smoke.log"},
                ],
                "diagnostics": [
                    {
                        "path": "afk/stderr.log",
                        "content": "Docker smoke failed: fetch failed",
                    },
                    {
                        "path": "contract/smoke.log",
                        "content": "health endpoint unavailable",
                    },
                ],
            },
            reviews=[
                {
                    "axis": "standards",
                    "status": "rejected",
                    "findings": [
                        {
                            "id": "standards-1",
                            "priority": "high",
                            "title": "Missing test",
                            "body": "Cover the failure path.",
                            "path": "tests/test_app.py",
                            "line": 10,
                            "blocking": True,
                        }
                    ],
                },
                {
                    "axis": "spec",
                    "status": "passed",
                    "findings": [],
                },
            ],
        )

        self.assertEqual(brief["candidate_sha"], "a" * 40)
        self.assertEqual(brief["repair_attempt"], 2)
        self.assertEqual(
            [finding["id"] for finding in brief["blocking_findings"]],
            ["validation-smoke", "standards-1"],
        )
        self.assertNotIn("unit", str(brief["blocking_findings"]))
        self.assertIn("fetch failed", brief["blocking_findings"][0]["body"])
        self.assertIn("health endpoint", brief["blocking_findings"][0]["body"])


if __name__ == "__main__":
    unittest.main()
