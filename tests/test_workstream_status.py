import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.workstream import tracker_record, workstream_status_from_publication  # noqa: E402


def tracker_state(*, terminal_decision=None):
    return {
        "selected_work": [{"external_id": "central-afk-pr.17", "title": "Delay tracker closure"}],
        "implementation": {
            "status": "implemented",
            "git": {"after_commit": "abc123"},
        },
        "validations": [
            {
                "output": {
                    "status": "validated",
                    "checkout": {"start_commit": "abc123"},
                }
            }
        ],
        "review": {
            "status": "passed",
            "summary": "ready for human review",
            "reviewer_result": {"findings": []},
        },
        "tracker": {
            "terminal_decision": terminal_decision
            or {
                "status": "",
                "merge_commit": "",
                "reason": "",
                "pr_url": "",
                "review_feedback_status": "",
            }
        },
    }


class WorkstreamStatusMappingTest(unittest.TestCase):
    def test_workstream_status_from_publication_explicit_terminal_states(self):
        self.assertEqual(
            workstream_status_from_publication({"status": "published"}),
            "published",
        )
        self.assertEqual(
            workstream_status_from_publication(
                {"status": "validated-unpublished"},
            ),
            "validated-unpublished",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "tracker-close-blocked"}),
            "review-findings-open",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "blocked"}),
            "blocked",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "tracker-closed"}),
            "closed",
        )

    def test_workstream_status_from_publication_unknown_status_defaults(
        self,
    ):
        self.assertEqual(
            workstream_status_from_publication({"status": "mystery_status"}),
            "failed-needs-human",
        )

    def test_tracker_record_keeps_published_pr_backed_work_open(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
            },
            tracker_state(),
            {"status": "published", "url": "https://github.example/pr/17"},
        )

        self.assertEqual(record["status"], "awaiting-review")
        self.assertFalse(record["close_source_item"])
        self.assertEqual(record["close_reason"], "")
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")

    def test_tracker_record_closes_only_after_merge_commit_is_recorded(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
            },
            tracker_state(
                terminal_decision={
                    "status": "merged",
                    "merge_commit": "deadbeef",
                    "reason": "",
                    "pr_url": "https://github.example/pr/17",
                    "review_feedback_status": "",
                }
            ),
            {"status": "published", "url": "https://github.example/pr/17"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(record["merge_commit"], "deadbeef")
        self.assertEqual(record["close_reason"], "merged via deadbeef")
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")

    def test_tracker_record_closes_after_explicit_no_merge_decision(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "no-merge",
                        "merge_commit": "",
                        "reason": "Superseded by follow-up PR",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
            },
            tracker_state(
                terminal_decision={
                    "status": "no-merge",
                    "merge_commit": "",
                    "reason": "Superseded by follow-up PR",
                    "pr_url": "https://github.example/pr/17",
                    "review_feedback_status": "",
                }
            ),
            {"status": "published", "url": "https://github.example/pr/17"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(
            record["close_reason"],
            "Superseded by follow-up PR",
        )
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")

    def test_tracker_record_surfaces_open_review_cycle_findings(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
                "review_cycles": [
                    {
                        "cycle": 1,
                        "status": "findings-open",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "findings-open",
                                "summary": "Needs response",
                                "requires_response": True,
                            }
                        ],
                    }
                ],
            },
            tracker_state(),
            {"status": "validated-unpublished"},
        )

        self.assertEqual(record["status"], "review-findings-open")
        self.assertIn("response-required review findings", record["comment"])

    def test_tracker_record_keeps_request_changes_open_until_response_is_addressed(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
                "review_cycles": [
                    {
                        "cycle": 1,
                        "status": "request-changes",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "request-changes",
                                "summary": "Please fix the tracker semantics.",
                                "requires_response": True,
                                "response": {"status": "wip", "summary": "Investigating"},
                            }
                        ],
                    }
                ],
            },
            tracker_state(),
            {"status": "validated-unpublished"},
        )

        self.assertEqual(record["status"], "review-findings-open")
        self.assertIn("response-required review findings", record["comment"])

    def test_tracker_record_accepts_freeform_response_string_as_addressed_evidence(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {"terminal_decision": {"status": "", "merge_commit": "", "reason": ""}},
                "review_cycles": [
                    {
                        "cycle": 1,
                        "status": "request-changes",
                        "reviews": [
                            {
                                "role": "correctness",
                                "status": "request-changes",
                                "summary": "Please fix the tracker semantics.",
                                "requires_response": True,
                                "response": "Patched in follow-up commit abc123.",
                            }
                        ],
                    }
                ],
            },
            tracker_state(),
            {"status": "validated-unpublished"},
        )

        self.assertEqual(record["status"], "review-feedback-addressed")
        self.assertNotIn("response-required review findings", record["comment"])

    def test_tracker_record_keeps_terminal_merge_open_until_feedback_resolution_is_recorded(self):
        review_cycles = [
            {
                "cycle": 1,
                "status": "request-changes",
                "reviews": [
                    {
                        "role": "correctness",
                        "status": "request-changes",
                        "summary": "Please fix the tracker semantics.",
                        "requires_response": True,
                    }
                ],
            }
        ]
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
                "review_cycles": review_cycles,
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "review-findings-open")
        self.assertFalse(record["close_source_item"])
        self.assertEqual(record["pr_url"], "https://github.example/pr/17")
        self.assertIn("terminal decision is recorded", record["comment"])

    def test_tracker_record_closes_terminal_merge_when_feedback_is_explicitly_resolved(self):
        review_cycles = [
            {
                "cycle": 1,
                "status": "request-changes",
                "reviews": [
                    {
                        "role": "correctness",
                        "status": "request-changes",
                        "summary": "Please fix the tracker semantics.",
                        "requires_response": True,
                    }
                ],
            }
        ]
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "resolved",
                    }
                },
                "review_cycles": review_cycles,
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(
            record["close_reason"],
            "merged via deadbeef",
        )
        self.assertIn("resolved before closure", record["comment"])

    def test_tracker_record_ignores_feedback_resolution_text_when_no_review_cycles_require_response(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "merged",
                        "merge_commit": "deadbeef",
                        "reason": "",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "resolved",
                    }
                },
                "review_cycles": [],
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(record["close_reason"], "merged via deadbeef")
        self.assertEqual(
            record["comment"],
            "PR merged; close the source Beads item with the recorded merge commit.",
        )

    def test_tracker_record_closes_terminal_no_merge_when_feedback_is_explicitly_waived(self):
        review_cycles = [
            {
                "cycle": 1,
                "status": "findings-open",
                "reviews": [
                    {
                        "role": "bug-risk",
                        "status": "findings-open",
                        "summary": "One follow-up remains.",
                        "requires_response": True,
                    }
                ],
            }
        ]
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "no-merge",
                        "merge_commit": "",
                        "reason": "Superseded by follow-up PR",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "waived",
                    }
                },
                "review_cycles": review_cycles,
            },
            tracker_state(),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertTrue(record["close_source_item"])
        self.assertEqual(
            record["close_reason"],
            "Superseded by follow-up PR",
        )
        self.assertIn("waived before closure", record["comment"])

    def test_tracker_record_includes_redacted_retrospective_for_terminal_no_merge(self):
        record = tracker_record(
            {
                "workstream_id": "central-afk-pr.17",
                "tracker": {
                    "terminal_decision": {
                        "status": "no-merge",
                        "merge_commit": "",
                        "reason": "Superseded by follow-up PR",
                        "pr_url": "https://github.example/pr/17",
                        "review_feedback_status": "",
                    }
                },
                "review_cycles": [],
                "retrospective": {
                    "summary": "No-merge after token=ghp_no_merge_retrospective_secret_1234567890 follow-up.",
                    "changes": ["Documented why the branch will not merge."],
                    "validation": ["Validation remained green before the no-merge decision."],
                    "review": ["Final review passed; publication was intentionally skipped."],
                    "unresolved_risks": ["Follow-up work still needs manual tracking."],
                    "process_findings": ["Terminal no-merge decisions still need retrospective evidence."],
                    "follow_up": {
                        "recommended": [
                            {
                                "id": "central-3x6.7",
                                "summary": "Track the superseding workstream.",
                                "labels": ["project:afk-composable-pipeline"],
                            }
                        ],
                        "created": [],
                    },
                    "notes": {
                        "personal_work": [
                            "~/Documents/rmd/Ceremonies/Personal Work/work/2026-06-27-personal.md",
                        ],
                        "spikes": [],
                    },
                },
            },
            tracker_state(
                terminal_decision={
                    "status": "no-merge",
                    "merge_commit": "",
                    "reason": "Superseded by follow-up PR",
                    "pr_url": "https://github.example/pr/17",
                    "review_feedback_status": "",
                }
            ),
            {"status": "tracker-closed"},
        )

        self.assertEqual(record["status"], "closed")
        self.assertIn("[REDACTED]", record["retrospective"]["summary"])
        self.assertEqual(
            record["retrospective"]["follow_up"]["recommended"][0]["labels"],
            ["project:afk-composable-pipeline"],
        )
        self.assertEqual(
            record["retrospective"]["notes"]["personal_work"],
            ["~/Documents/rmd/Ceremonies/Personal Work/work/2026-06-27-personal.md"],
        )
