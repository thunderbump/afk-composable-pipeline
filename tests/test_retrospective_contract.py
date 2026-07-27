import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.jsonutil import sha256_json  # noqa: E402
from afk.retrospective_contract import (  # noqa: E402
    ARTIFACT_INVENTORY_LIMIT,
    TEXT_CHARACTER_LIMIT,
    TRUNCATION_SUFFIX,
    capture_inventory,
    decode_inventory,
)


class RetrospectiveContractTest(unittest.TestCase):
    def test_capture_is_ordered_bounded_and_validates_as_stored(self):
        effects = [
            {
                "effect_id": f"effect-{index:02d}",
                "kind": "x" * (TEXT_CHARACTER_LIMIT + 1),
                "status": "confirmed",
            }
            for index in reversed(range(ARTIFACT_INVENTORY_LIMIT + 2))
        ]
        evidence = [
            {
                "unit": f"attempts/unit-{index:02d}",
                "manifest": {"schema_version": 1, "unit": index},
            }
            for index in reversed(range(ARTIFACT_INVENTORY_LIMIT + 2))
        ]

        inventory = capture_inventory(
            through_sequence=7,
            effects=effects,
            evidence=evidence,
        )

        self.assertIs(
            decode_inventory(
                inventory,
                sequence=7,
                evidence_roots={"attempts", "gates", "retrospective"},
            ),
            inventory,
        )
        self.assertEqual(len(inventory["effects"]), ARTIFACT_INVENTORY_LIMIT)
        self.assertEqual(inventory["effects"][0]["effect_id"], "effect-00")
        self.assertEqual(inventory["effects"][-1]["effect_id"], "effect-31")
        self.assertTrue(inventory["effects"][0]["kind"].endswith(TRUNCATION_SUFFIX))
        self.assertEqual(len(inventory["evidence"]), ARTIFACT_INVENTORY_LIMIT)
        self.assertEqual(inventory["evidence"][0]["unit"], "attempts/unit-00")
        self.assertEqual(
            inventory["evidence"][0]["manifest_sha256"],
            sha256_json({"schema_version": 1, "unit": 0}),
        )
        self.assertEqual(
            inventory["omitted"],
            {"effects": 2, "evidence_units": 2},
        )


if __name__ == "__main__":
    unittest.main()
