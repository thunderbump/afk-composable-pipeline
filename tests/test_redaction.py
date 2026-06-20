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
                    "--auth.file",
                    "/tmp/pi-dotted-auth-secret-token",
                    "--credential-file",
                    "/tmp/pi-credential-secret",
                    "--credential.file=/tmp/pi-dotted-credential-secret",
                    "--access-token",
                    "access-secret",
                    "--api.key",
                    "api-dot-secret",
                    "--api.key=api-dot-equals-secret",
                    "--token=pi-secret-token",
                    "--github-token=github-secret",
                ]
            }
        }

        redacted = redact_artifact_value(payload)
        text = repr(redacted)

        self.assertNotIn("pi-auth-secret-token", text)
        self.assertNotIn("pi-dotted-auth-secret-token", text)
        self.assertNotIn("pi-credential-secret", text)
        self.assertNotIn("pi-dotted-credential-secret", text)
        self.assertNotIn("access-secret", text)
        self.assertNotIn("api-dot-secret", text)
        self.assertNotIn("api-dot-equals-secret", text)
        self.assertNotIn("pi-secret-token", text)
        self.assertNotIn("github-secret", text)
        self.assertEqual(
            redacted["agent"]["command"],
            [
                "pi",
                "--auth-file",
                "[REDACTED]",
                "--auth.file",
                "[REDACTED]",
                "--credential-file",
                "[REDACTED]",
                "--credential.file=[REDACTED]",
                "--access-token",
                "[REDACTED]",
                "--api.key",
                "[REDACTED]",
                "--api.key=[REDACTED]",
                "--token=[REDACTED]",
                "--github-token=[REDACTED]",
            ],
        )

    def test_redacts_json_shaped_secret_key_values_in_text(self):
        text = (
            'stdout {"token":"token-secret","api_key": "api-key-secret", '
            '"password" : "password-secret"}'
        )

        redacted = redact_text(text)

        self.assertNotIn("token-secret", redacted)
        self.assertNotIn("api-key-secret", redacted)
        self.assertNotIn("password-secret", redacted)
        self.assertIn('"token":"[REDACTED]"', redacted)
        self.assertIn('"api_key": "[REDACTED]"', redacted)
        self.assertIn('"password" : "[REDACTED]"', redacted)

    def test_does_not_redact_safe_command_flags_that_contain_secret_words(self):
        payload = {
            "agent": {
                "command": [
                    "pi",
                    "--author",
                    "pipeline-bot",
                    "--authority",
                    "local",
                    "--tokenize",
                    "words",
                    "--secretary",
                    "notes",
                ]
            }
        }

        self.assertEqual(redact_artifact_value(payload), payload)


if __name__ == "__main__":
    unittest.main()
