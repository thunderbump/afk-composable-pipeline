import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.redaction import redact_artifact_value, redact_text, redact_url  # noqa: E402


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

    def test_redacts_command_list_credential_flags(self):
        payload = {
            "agent": {
                "command": [
                    "pi",
                    "--auth-file",
                    "/tmp/pi-auth-secret-token",
                    "--token=pi-secret-token",
                ]
            }
        }

        redacted = redact_artifact_value(payload)
        text = repr(redacted)

        self.assertNotIn("pi-auth-secret-token", text)
        self.assertNotIn("pi-secret-token", text)
        self.assertEqual(
            redacted["agent"]["command"],
            ["pi", "--auth-file", "[REDACTED]", "--token=[REDACTED]"],
        )


if __name__ == "__main__":
    unittest.main()
