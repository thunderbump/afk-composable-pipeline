import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.review_cycles import (  # noqa: E402
    aggregate_runtime_review_cycle_status,
    finalized_runtime_review_cycle_status,
    normalize_review_cycles,
)
from afk.schema_helpers import (  # noqa: E402
    build_selected_work_record,
    copy_selected_work_items,
    first_selected_work_external_id,
    normalize_prepared_checkout,
    scrub_selected_work_value,
    validation_artifact_ref,
)


class SchemaHelpersTest(unittest.TestCase):
    def test_normalize_prepared_checkout_supports_optional_repo_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir) / "checkout"
            subprocess.run(
                ["git", "init", "--quiet", str(checkout)],
                check=True,
                capture_output=True,
            )

            normalized = normalize_prepared_checkout(
                {
                    "status": "prepared",
                    "repo_url": "git@github.com:thunderbump/example.git",
                    "checkout_path": str(checkout),
                    "review_branch": "afk/example",
                    "requested_ref": "main",
                    "start_commit": "abc123",
                },
                include_repo_url=True,
                redact_repo_url=lambda value: value.replace("github.com", "redacted.example"),
            )

        self.assertEqual(normalized["status"], "valid")
        self.assertEqual(normalized["checkout"]["path"], str(checkout))
        self.assertEqual(normalized["checkout"]["review_branch"], "afk/example")
        self.assertEqual(normalized["checkout"]["requested_ref"], "main")
        self.assertEqual(normalized["checkout"]["start_commit"], "abc123")
        self.assertEqual(
            normalized["checkout"]["repo_url"],
            "git@redacted.example:thunderbump/example.git",
        )

    def test_selected_work_helpers_preserve_record_shape_and_scrub_selector_fields(self):
        selected = build_selected_work_record(
            {
                "parent": "central",
                "workstream": "central",
                "afk": {"ready": True},
                "selector_rationale": "prefer this",
            },
            external_id="central-1dwd",
            source_id="fixture",
            source_type="fixture",
            labels=["project:afk-composable-pipeline", "afk:ready"],
            acceptance_criteria=["collapse duplicated schema normalizers"],
            dependencies=[{"id": "central-1dvc", "status": "closed"}],
            blockers=[],
        )

        self.assertEqual(selected["external_id"], "central-1dwd")
        self.assertEqual(first_selected_work_external_id([selected]), "central-1dwd")
        self.assertEqual(copy_selected_work_items([selected]), [selected])

        scrubbed = scrub_selected_work_value(
            [
                {
                    **selected,
                    "selector_rationale": "prefer this",
                    "raw": {"afk": {"selector_mode": "ranked", "ready": True}},
                }
            ]
        )

        self.assertNotIn("selector_rationale", scrubbed[0])
        self.assertEqual(scrubbed[0]["raw"]["afk"]["selector_mode"], "ranked")

    def test_validation_artifact_ref_and_review_cycles_keep_public_shape(self):
        artifact = validation_artifact_ref(
            index=0,
            name="tier3-harness",
            step_result_path="/tmp/ledger/runs/validate/step-result.json",
            worker_result_path="/tmp/ledger/runs/validate/worker-result.json",
        )
        cycles = normalize_review_cycles(
            [
                {
                    "status": "request-changes",
                    "reviews": [
                        {
                            "role": "correctness",
                            "status": "request-changes",
                            "summary": "Needs follow-up.",
                            "requires_response": True,
                            "response": {"status": "addressed", "summary": "Patched."},
                        }
                    ],
                }
            ]
        )

        self.assertEqual(
            artifact,
            {
                "name": "tier3-harness",
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            },
        )
        self.assertEqual(cycles[0]["cycle"], 1)
        self.assertEqual(cycles[0]["reviews"][0]["response"]["status"], "addressed")
        self.assertEqual(finalized_runtime_review_cycle_status(cycles[0]["reviews"]), "findings-addressed")
        self.assertEqual(aggregate_runtime_review_cycle_status(cycles[0]["reviews"]), "request-changes")


if __name__ == "__main__":
    unittest.main()
