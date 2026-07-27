import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.durable_id import is_durable_id  # noqa: E402


class DurableIdTest(unittest.TestCase):
    def test_accepts_only_supported_one_to_128_character_identifiers(self):
        self.assertTrue(is_durable_id("a"))
        self.assertTrue(is_durable_id("a" + ("._-" * 42) + "x"))
        for value in ("", "-leading", "a" * 129, "path/name", None):
            with self.subTest(value=value):
                self.assertFalse(is_durable_id(value))


if __name__ == "__main__":
    unittest.main()
