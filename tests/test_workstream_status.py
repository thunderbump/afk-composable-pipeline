import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.workstream import workstream_status_from_publication  # noqa: E402


class WorkstreamStatusMappingTest(unittest.TestCase):
    def test_workstream_status_from_publication_uses_explicit_terminal_vocabulary(self):
        self.assertEqual(
            workstream_status_from_publication({"status": "published"}), "published"
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "validated-unpublished"}),
            "validated-unpublished",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "blocked"}), "blocked"
        )

    def test_workstream_status_from_publication_rejects_legacy_terminal_strings(self):
        self.assertEqual(
            workstream_status_from_publication({"status": "failed_publication"}),
            "failed-needs-human",
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "completed"}), "failed-needs-human"
        )
        self.assertEqual(
            workstream_status_from_publication({"status": "needs_human"}),
            "failed-needs-human",
        )
