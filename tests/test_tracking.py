import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.tracking import TrackerContext, build_tracker_record  # noqa: E402


def tracker_state():
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
            "terminal_decision": {
                "status": "",
                "merge_commit": "",
                "reason": "",
                "pr_url": "",
                "review_feedback_status": "",
            }
        },
    }


class TrackingModuleTest(unittest.TestCase):
    def test_build_tracker_record_marks_blocked_publication_and_preserves_implementation_commit(self):
        record = build_tracker_record(
            TrackerContext(
                schema_version=1,
                normalized={"workstream_id": "central-afk-pr.17", "tracker": {"terminal_decision": {}}},
                state=tracker_state(),
                publication={
                    "status": "blocked",
                    "reason": "stuck_same_finding: validation repairs exhausted for the selected work item",
                },
                retrospective={},
            )
        )

        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["implementation_commit"], "abc123")
        self.assertFalse(record["close_source_item"])
        self.assertIn("keep the source Beads item open", record["comment"])

    def test_build_tracker_record_includes_repair_stop_evidence(self):
        state = tracker_state()
        state["implementation_result_path"] = "runs/implement/step-result.json"
        state["review_result_path"] = "runs/review/step-result.json"
        state["review"] = {
            "status": "request_revision",
            "summary": "review requested changes",
            "reviewer_result": {
                "findings": [
                    {
                        "classification": "correctness",
                        "summary": "Guard terminal publish when the cycle list is empty.",
                    }
                ]
            },
        }
        record = build_tracker_record(
            TrackerContext(
                schema_version=1,
                normalized={"workstream_id": "central-afk-pr.17", "tracker": {"terminal_decision": {}}},
                state=state,
                publication={
                    "status": "blocked",
                    "reason": (
                        "stuck_same_finding: correctness src/demo.py:41: "
                        "Guard terminal publish when the cycle list is empty."
                    ),
                },
                retrospective={},
            )
        )

        self.assertEqual(record["repair_stop"]["classification"], "stuck_same_finding")
        self.assertEqual(record["repair_stop"]["scope"], "target-work")
        self.assertEqual(
            record["repair_stop"]["evidence_paths"],
            [
                "runs/implement/step-result.json",
                "runs/review/step-result.json",
            ],
        )

    def test_build_tracker_record_preserves_addressed_review_cycle_status(self):
        record = build_tracker_record(
            TrackerContext(
                schema_version=1,
                normalized={
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
                state=tracker_state(),
                publication={"status": "validated-unpublished"},
                retrospective={},
            )
        )

        self.assertEqual(record["status"], "review-feedback-addressed")
        self.assertEqual(record["source_item_external_id"], "central-afk-pr.17")
        self.assertEqual(len(record["review_cycles"]), 1)
        self.assertFalse(record["close_source_item"])


if __name__ == "__main__":
    unittest.main()
