import sys
import unittest
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk import workstream  # noqa: E402
from afk import retrospective as retrospective_api  # noqa: E402
from afk.retrospective import RetrospectiveContext, build_pipeline_retrospective  # noqa: E402


def retrospective_state():
    return {
        "selected_work": [{"external_id": "central-afk-pr.17", "title": "Delay tracker closure"}],
        "implementation": {
            "status": "implemented",
            "git": {"after_commit": "abc123"},
        },
        "implementation_selection": [{"external_id": "central-afk-pr.17"}],
        "implementation_result_path": "/tmp/ledger/runs/impl/step-result.json",
        "validations": [
            {
                "output": {
                    "status": "validated",
                    "checkout": {"start_commit": "abc123"},
                    "validation": {"requested_profile": "tier1"},
                },
                "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
            }
        ],
        "review": {
            "status": "passed",
            "summary": "ready for human review",
            "checkout": {"start_commit": "abc123"},
            "reviewer_result": {"findings": []},
        },
        "review_selection": [{"external_id": "central-afk-pr.17"}],
        "review_result_path": "runs/review/step-result.json",
        "cleanup": {"status": "clean", "resources": []},
    }


def retrospective_tracker(status="awaiting-review"):
    return {
        "status": status,
        "close_source_item": False,
        "close_reason": "",
        "comment": "",
        "pr_url": "https://github.example/pr/17",
        "merge_commit": "",
    }


class RetrospectiveModuleTest(unittest.TestCase):
    def test_workstream_does_not_expose_private_retrospective_internals(self):
        for symbol in (
            "_apply_retrospective_judge",
            "_retrospective_follow_up_bead_description",
            "_retrospective_follow_up_bead_labels",
            "_retrospective_follow_up_fingerprint",
        ):
            with self.subTest(symbol=symbol):
                self.assertFalse(hasattr(workstream, symbol))

    def test_retrospective_module_exposes_public_retrospective_helpers(self):
        namespace = {}
        exec(
            "from afk.retrospective import effective_retrospective, pipeline_retrospective_record",
            namespace,
        )

        self.assertIs(namespace["effective_retrospective"], retrospective_api.effective_retrospective)
        self.assertIs(
            namespace["pipeline_retrospective_record"],
            retrospective_api.pipeline_retrospective_record,
        )

    def test_workstream_keeps_legacy_retrospective_helper_imports(self):
        namespace = {}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            exec(
                "from afk.workstream import effective_retrospective, pipeline_retrospective_record",
                namespace,
            )

        self.assertIs(namespace["effective_retrospective"], retrospective_api.effective_retrospective)
        self.assertIs(
            namespace["pipeline_retrospective_record"],
            retrospective_api.pipeline_retrospective_record,
        )
        self.assertEqual(len(caught), 2)
        self.assertTrue(all(issubclass(item.category, DeprecationWarning) for item in caught))

    def test_build_pipeline_retrospective_reports_clean_published_run(self):
        record = build_pipeline_retrospective(
            RetrospectiveContext(
                state=retrospective_state(),
                publication={"status": "published", "url": "https://github.example/pr/17"},
                tracker=retrospective_tracker(),
            )
        )

        self.assertEqual(record["status"], "published")
        self.assertEqual(record["health"], "healthy")
        self.assertEqual(record["publication_status"], "published")
        self.assertEqual(record["tracker_status"], "awaiting-review")
        self.assertEqual(record["signals"], [])
        self.assertEqual(record["recommended_follow_up"], [])
        self.assertEqual(record["follow_up"]["recommended"], [])
        self.assertEqual(record["follow_up"]["created"], [])
        self.assertEqual(record["judge"], {"enabled": False, "status": "disabled"})
