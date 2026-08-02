import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.validation_contract import (  # noqa: E402
    ValidationContractError,
    parse_validation_contract,
)


def contract(trusted_files: str) -> str:
    return (
        "schema_version = 1\n\n"
        "[validation]\n"
        'command = ["./scripts/validation-worker.sh"]\n'
        f"trusted_files = {trusted_files}\n"
        "timeout_seconds = 30\n"
    )


class ValidationContractTest(unittest.TestCase):
    def test_parses_a_normalized_trusted_validator_closure(self):
        parsed = parse_validation_contract(
            contract('["scripts/validation-worker.sh", "scripts/validate.sh"]')
        )

        self.assertEqual(
            parsed["trusted_files"],
            ["scripts/validation-worker.sh", "scripts/validate.sh"],
        )

    def test_requires_a_nonempty_trusted_validator_closure(self):
        for trusted_files in ("[]",):
            with self.subTest(trusted_files=trusted_files):
                with self.assertRaisesRegex(ValidationContractError, "invalid"):
                    parse_validation_contract(contract(trusted_files))

    def test_rejects_ambiguous_or_escaping_trusted_paths(self):
        for path in (
            "./scripts/validate.sh",
            "scripts/../validate.sh",
            "/scripts/validate.sh",
            "scripts\\validate.sh",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValidationContractError, "invalid"):
                    parse_validation_contract(contract(f"[{path!r}]"))

    def test_rejects_duplicate_trusted_paths(self):
        with self.assertRaisesRegex(ValidationContractError, "invalid"):
            parse_validation_contract(
                contract('["scripts/validate.sh", "scripts/validate.sh"]')
            )


if __name__ == "__main__":
    unittest.main()
