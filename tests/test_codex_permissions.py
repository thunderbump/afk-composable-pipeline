import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.codex_permissions import (  # noqa: E402
    codex_environment,
    codex_permission_args,
)


class CodexPermissionsTest(unittest.TestCase):
    def test_shared_policy_builds_a_contained_profile_and_environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/operator/home",
                "PATH": "/usr/bin",
                "CODEX_HOME": "/operator/codex",
                "OPENAI_API_KEY": "must-not-cross",
            },
            clear=True,
        ):
            self.assertEqual(
                codex_environment(),
                {
                    "HOME": "/operator/home",
                    "PATH": "/usr/bin",
                    "CODEX_HOME": "/operator/codex",
                },
            )

        self.assertEqual(
            codex_permission_args(
                profile_name="afk_test",
                description="AFK test profile",
                filesystem={"/checkout": "read", "/output": "write"},
                shell_environment={"PATH": "/usr/bin", "HOME": "/temporary/home"},
            ),
            [
                "-c",
                'default_permissions="afk_test"',
                "-c",
                'permissions.afk_test={ description = "AFK test profile", '
                'filesystem = { "/checkout" = "read", "/output" = "write" }, '
                "network = { enabled = false } }",
                "-c",
                'approval_policy="never"',
                "-c",
                'web_search="disabled"',
                "-c",
                'shell_environment_policy={ inherit = "none", '
                'ignore_default_excludes = false, set = { "PATH" = "/usr/bin", '
                '"HOME" = "/temporary/home" } }',
            ],
        )


if __name__ == "__main__":
    unittest.main()
