import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.jsonutil import canonical_json  # noqa: E402
from afk.retrospective_result import (  # noqa: E402
    RetrospectiveResultError,
    normalize_retrospective_result,
)


class RetrospectiveResultTest(unittest.TestCase):
    def summary(self):
        return canonical_json(
            {
                "schema_version": 1,
                "run": {"run_id": "run-001"},
                "episode": {"state": "attention_required"},
                "events": [{"sequence": 7, "event": "validation.rejected"}],
                "projection": {
                    "attention": {"summary": "Validation could not produce a verdict."},
                    "items": [],
                },
                "citation_manifest": {
                    "events.jsonl": {
                        "kind": "event",
                        "summary_pointer": "/events",
                    },
                    "projection.json": {
                        "kind": "json",
                        "summary_pointer": "/projection",
                    },
                    "attention-summary.txt": {
                        "kind": "text",
                        "summary_pointer": "/projection/attention/summary",
                    },
                },
            }
        )

    def test_empty_result_normalizes_deterministically(self):
        result = {
            "schema_version": 1,
            "run_id": "run-001",
            "terminal_outcome": "attention_required",
            "summary": "No actionable process findings.",
            "process_findings": [],
            "improvement_proposals": [],
        }

        first = normalize_retrospective_result(self.summary(), result)
        second = normalize_retrospective_result(self.summary(), result)

        self.assertEqual(first, second)
        self.assertEqual(first, result)

    def test_populated_result_accepts_resolvable_event_json_and_text_citations(self):
        result = {
            "schema_version": 1,
            "run_id": "run-001",
            "terminal_outcome": "attention_required",
            "summary": "Validation infrastructure stopped the Run.",
            "process_findings": [
                {
                    "id": "finding-1",
                    "category": "validation",
                    "title": "Validation could not produce a verdict",
                    "evidence": [
                        {"artifact": "events.jsonl", "event_sequence": 7},
                        {
                            "artifact": "projection.json",
                            "json_pointer": "/attention/summary",
                        },
                        {
                            "artifact": "attention-summary.txt",
                            "line_start": 1,
                            "line_end": 1,
                        },
                    ],
                    "impact": "The Run required operator attention.",
                    "confidence": "high",
                }
            ],
            "improvement_proposals": [
                {
                    "id": "proposal-1",
                    "addresses": ["finding-1"],
                    "scope": "target_repository",
                    "priority": "P1",
                    "title": "Make validation readiness explicit",
                    "rationale": "Earlier detection shortens attention cycles.",
                    "suggested_change": "Add readiness to validation preflight.",
                    "requires_human_decision": True,
                }
            ],
        }

        self.assertEqual(
            normalize_retrospective_result(self.summary(), result),
            result,
        )

    def test_unknown_vocabulary_is_rejected_with_bounded_field_evidence(self):
        cases = (
            ("category", "build", "process_findings[0].category is invalid"),
            ("confidence", "certain", "process_findings[0].confidence is invalid"),
            ("scope", "repository", "improvement_proposals[0].scope is invalid"),
            ("priority", "urgent", "improvement_proposals[0].priority is invalid"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                result = self.populated_result()
                target = (
                    result["process_findings"][0]
                    if field in {"category", "confidence"}
                    else result["improvement_proposals"][0]
                )
                target[field] = value

                with self.assertRaises(RetrospectiveResultError) as raised:
                    normalize_retrospective_result(self.summary(), result)

                self.assertEqual(raised.exception.errors, (expected,))
                self.assertLessEqual(len(str(raised.exception)), 512)

    def test_unresolved_escaped_and_mismatched_citations_are_rejected(self):
        cases = (
            {"artifact": "events.jsonl", "event_sequence": 99},
            {"artifact": "projection.json", "json_pointer": "/missing"},
            {"artifact": "projection.json", "json_pointer": "/attention/~2"},
            {"artifact": "attention-summary.txt", "line_start": 2},
            {"artifact": "../projection.json", "json_pointer": ""},
            {"artifact": "unmanifested.json", "json_pointer": ""},
            {"artifact": "projection.json", "line_start": 1},
        )
        for citation in cases:
            with self.subTest(citation=citation):
                result = self.populated_result()
                result["process_findings"][0]["evidence"] = [citation]

                with self.assertRaises(RetrospectiveResultError) as raised:
                    normalize_retrospective_result(self.summary(), result)

                self.assertEqual(
                    raised.exception.errors,
                    ("process_findings[0].evidence[0] is unresolved",),
                )

    def test_duplicate_identities_and_absent_proposal_links_are_rejected(self):
        duplicate_finding = self.populated_result()
        duplicate_finding["process_findings"].append(
            dict(duplicate_finding["process_findings"][0])
        )
        duplicate_proposal = self.populated_result()
        duplicate_proposal["improvement_proposals"].append(
            dict(duplicate_proposal["improvement_proposals"][0])
        )
        absent_link = self.populated_result()
        absent_link["improvement_proposals"][0]["addresses"] = ["finding-missing"]
        duplicate_link = self.populated_result()
        duplicate_link["improvement_proposals"][0]["addresses"] = [
            "finding-1",
            "finding-1",
        ]
        cross_collection_duplicate = self.populated_result()
        cross_collection_duplicate["improvement_proposals"][0]["id"] = "finding-1"
        cases = (
            (
                duplicate_finding,
                "process_findings[1].id duplicates an existing identity",
            ),
            (
                duplicate_proposal,
                "improvement_proposals[1].id duplicates an existing identity",
            ),
            (
                absent_link,
                "improvement_proposals[0].addresses[0] is unresolved",
            ),
            (
                duplicate_link,
                "improvement_proposals[0].addresses contains a duplicate",
            ),
            (
                cross_collection_duplicate,
                "improvement_proposals[0].id duplicates an existing identity",
            ),
        )
        for result, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(RetrospectiveResultError) as raised:
                    normalize_retrospective_result(self.summary(), result)

                self.assertEqual(raised.exception.errors, (expected,))

    def test_malformed_and_oversized_untrusted_values_fail_closed(self):
        malformed_address = self.populated_result()
        malformed_address["improvement_proposals"][0]["addresses"] = [{}]
        oversized_summary = self.populated_result()
        oversized_summary["summary"] = "x" * 513
        out_of_range_pointer = self.populated_result()
        out_of_range_pointer["process_findings"][0]["evidence"] = [
            {"artifact": "projection.json", "json_pointer": "/items/0"}
        ]

        for result in (malformed_address, oversized_summary, out_of_range_pointer):
            with self.subTest(result=result):
                with self.assertRaises(RetrospectiveResultError) as raised:
                    normalize_retrospective_result(self.summary(), result)

                self.assertLessEqual(len(str(raised.exception)), 512)

    def test_oversized_run_summary_is_rejected_before_validation(self):
        summary = json.loads(self.summary())
        summary["padding"] = "x" * (64 * 1024)

        with self.assertRaisesRegex(
            RetrospectiveResultError,
            "Run Summary is invalid",
        ):
            normalize_retrospective_result(
                canonical_json(summary),
                {
                    "schema_version": 1,
                    "run_id": "run-001",
                    "terminal_outcome": "attention_required",
                    "summary": "No findings.",
                    "process_findings": [],
                    "improvement_proposals": [],
                },
            )

    def test_overlong_json_pointer_is_rejected_even_when_it_resolves(self):
        summary = json.loads(self.summary())
        long_key = "x" * 513
        summary["projection"][long_key] = "present"
        result = self.populated_result()
        result["process_findings"][0]["evidence"] = [
            {"artifact": "projection.json", "json_pointer": f"/{long_key}"}
        ]

        with self.assertRaises(RetrospectiveResultError):
            normalize_retrospective_result(canonical_json(summary), result)

    def populated_result(self):
        return {
            "schema_version": 1,
            "run_id": "run-001",
            "terminal_outcome": "attention_required",
            "summary": "Validation infrastructure stopped the Run.",
            "process_findings": [
                {
                    "id": "finding-1",
                    "category": "validation",
                    "title": "Validation could not produce a verdict",
                    "evidence": [
                        {"artifact": "events.jsonl", "event_sequence": 7},
                    ],
                    "impact": "The Run required operator attention.",
                    "confidence": "high",
                }
            ],
            "improvement_proposals": [
                {
                    "id": "proposal-1",
                    "addresses": ["finding-1"],
                    "scope": "target_repository",
                    "priority": "P1",
                    "title": "Make validation readiness explicit",
                    "rationale": "Earlier detection shortens attention cycles.",
                    "suggested_change": "Add readiness to validation preflight.",
                    "requires_human_decision": True,
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
