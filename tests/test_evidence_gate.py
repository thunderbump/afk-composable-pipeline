import unittest

import afk.evidence_gate as evidence_gate


class EvidenceGateTest(unittest.TestCase):
    def test_required_validation_gate_rejects_unvalidated_required_artifact(self):
        gate = evidence_gate.required_validation_gate(
            [
                {
                    "name": "tier1",
                    "status": "failed_validation",
                    "worker_status": "failed_validation",
                    "evidence_status": "valid",
                    "summary": "unit failed",
                    "worker_summary": "unit failed",
                    "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                    "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
                }
            ]
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(
            gate["reason"],
            "required final validation evidence is not validated: tier1 (failed_validation)",
        )
        self.assertEqual(gate["artifacts"][0]["name"], "tier1")
        self.assertEqual(gate["failures"][0]["validation"]["name"], "tier1")

    def test_publication_gate_rejects_stale_validation_for_implemented_head(self):
        gate = evidence_gate.publication_gate(
            validations=[
                {
                    "name": "tier1",
                    "status": "validated",
                    "worker_status": "validated",
                    "evidence_status": "valid",
                    "checkout_commit": "old-head",
                    "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                    "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
                }
            ],
            review={"status": "passed", "checkout_commit": "new-head"},
            implemented_commit="new-head",
            incomplete_selected_work=[],
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(
            gate["reason"],
            "required final validation evidence is stale for implemented HEAD: tier1",
        )

    def test_required_validation_gate_rejects_missing_checkout_commit_for_implemented_head(self):
        gate = evidence_gate.required_validation_gate(
            [
                {
                    "name": "tier1",
                    "status": "validated",
                    "worker_status": "validated",
                    "evidence_status": "valid",
                    "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                    "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
                }
            ],
            implemented_commit="new-head",
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(
            gate["reason"],
            "required final validation evidence is stale for implemented HEAD: tier1",
        )
        self.assertEqual(
            gate["failures"][0]["summary"],
            "tier1 was validated for missing checkout_commit, not new-head",
        )

    def test_publication_gate_rejects_missing_review_checkout_commit_for_implemented_head(self):
        gate = evidence_gate.publication_gate(
            validations=[
                {
                    "name": "tier1",
                    "status": "validated",
                    "worker_status": "validated",
                    "evidence_status": "valid",
                    "checkout_commit": "new-head",
                    "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                    "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
                }
            ],
            review={"status": "passed"},
            implemented_commit="new-head",
            incomplete_selected_work=[],
        )

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "final review evidence is stale for implemented HEAD")

    def test_validation_summary_lines_preserve_pr_body_contract(self):
        lines = evidence_gate.validation_summary_lines(
            [
                {
                    "name": "tier1",
                    "status": "validated",
                    "summary": "validated",
                    "worker_result": {
                        "raw": {"steps": [{"name": "unit", "status": "pass"}]},
                        "normalized": {"adapter": {"command": ["python3", "worker.py"]}},
                    },
                    "step_result_path": "/tmp/ledger/runs/validate/step-result.json",
                    "worker_result_path": "/tmp/ledger/runs/validate/worker-result.json",
                }
            ]
        )

        self.assertEqual(
            lines,
            [
                "- tier1: validated - result: unit=pass - command: python3 worker.py - summary: validated - evidence: runs/validate/step-result.json; runs/validate/worker-result.json"
            ],
        )


if __name__ == "__main__":
    unittest.main()
