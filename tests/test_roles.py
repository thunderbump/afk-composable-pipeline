import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from afk.role_adapters import RoleAdapterRuntimeError
from afk.role_adapters import render_command
from afk.roles import execute_role_adapter, log_role_adapter_result, log_role_runtime_error


class RolesTest(unittest.TestCase):
    def test_render_command_replaces_only_exact_placeholder_arguments(self):
        command = [
            "python",
            "-c",
            'print("{request_path}")',
            "{request_path}",
            "--output={result_path}",
        ]

        self.assertEqual(
            render_command(
                command,
                {
                    "{request_path}": "/tmp/request.json",
                    "{result_path}": "/tmp/result.json",
                },
            ),
            [
                "python",
                "-c",
                'print("{request_path}")',
                "/tmp/request.json",
                "--output={result_path}",
            ],
        )

    def test_execute_role_adapter_renders_command_and_builds_environment(self):
        captured: dict[str, object] = {}

        def runner(command: list[str], cwd: Path, env: dict[str, str], timeout_seconds: float) -> dict[str, object]:
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = dict(env)
            captured["timeout_seconds"] = timeout_seconds
            return {"returncode": 0, "stdout": "ok", "stderr": ""}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = execute_role_adapter(
                {
                    "command": ["echo", "{token}"],
                    "timeout_seconds": 7,
                    "env": {"CUSTOM_ENV": "custom"},
                    "codex_home": "/tmp/codex-home",
                },
                cwd=Path(temp_dir),
                env_root=Path(temp_dir),
                env_vars={"AFK_RESULT": "result.json"},
                replacements={"{token}": "rendered"},
                runtime_failure_message="adapter command failed",
                timeout_message="adapter command timed out",
                runner=runner,
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(captured["command"], ["echo", "rendered"])
        self.assertEqual(captured["cwd"], Path(temp_dir))
        self.assertEqual(captured["timeout_seconds"], 7)
        env = captured["env"]
        assert isinstance(env, dict)
        self.assertEqual(env["CUSTOM_ENV"], "custom")
        self.assertEqual(env["AFK_RESULT"], "result.json")
        self.assertEqual(env["CODEX_HOME"], "/tmp/codex-home")
        self.assertIn("HOME", env)
        self.assertIn("XDG_CONFIG_HOME", env)

    def test_role_log_helpers_redact_runtime_errors_and_results(self):
        runtime_error = RoleAdapterRuntimeError(
            "adapter command failed",
            stdout="stdout super-secret",
            stderr="stderr super-secret",
        )

        with io.StringIO() as captured_stdout, io.StringIO() as captured_stderr:
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                runtime_stdout, runtime_stderr = log_role_runtime_error(
                    runtime_error,
                    exact_secrets={"super-secret"},
                )
                result_stdout, result_stderr = log_role_adapter_result(
                    {"stdout": "adapter super-secret", "stderr": "result super-secret"},
                    exact_secrets={"super-secret"},
                )
            stdout_log = captured_stdout.getvalue()
            stderr_log = captured_stderr.getvalue()

        for value in (
            runtime_stdout,
            runtime_stderr,
            result_stdout,
            result_stderr,
            stdout_log,
            stderr_log,
        ):
            self.assertNotIn("super-secret", value)
