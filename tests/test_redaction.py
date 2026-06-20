import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.redaction import redact_text, redact_url  # noqa: E402


class RedactionTest(unittest.TestCase):
    def test_redacts_url_userinfo_query_and_fragment(self):
        self.assertEqual(
            redact_url("https://user:secret@example.invalid/repo.git?token=hidden#frag"),
            "https://example.invalid/repo.git",
        )

    def test_redacts_quote_wrapped_urls_in_git_stderr(self):
        text = (
            "fatal: unable to access "
            "'https://example.invalid/private.git?token=hidden-secret': "
            "repository not found"
        )

        redacted = redact_text(text)

        self.assertNotIn("hidden-secret", redacted)
        self.assertNotIn("token=", redacted)
        self.assertIn("'https://example.invalid/private.git':", redacted)


if __name__ == "__main__":
    unittest.main()
