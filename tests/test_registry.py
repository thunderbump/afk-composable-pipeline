import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.registry import StepContext, default_step_registry


class RegistryTest(unittest.TestCase):
    def test_default_registry_dispatches_noop_step(self):
        registry = default_step_registry()

        result = registry.run(
            "noop",
            StepContext(input_data={"message": "hello"}, run_id="test-run"),
        )

        self.assertEqual(
            registry.step_names,
            ("implement", "noop", "prepare-checkout", "select-work", "validate"),
        )
        self.assertEqual(result.run_id, "test-run")
        self.assertEqual(result.step, "noop")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.output, {"message": "hello"})
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertRegex(result.result_sha256, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
