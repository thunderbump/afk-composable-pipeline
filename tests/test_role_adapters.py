import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from afk.role_adapters import (
    RoleAdapterRuntimeError,
    execute_role_command,
    read_json_result_file,
    redact_adapter_streams,
    write_adapter_logs,
)


class RoleAdaptersTest(unittest.TestCase):
    def test_execute_role_command_raises_timeout_with_elapsed_seconds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            started_at = time.monotonic()
            with self.assertRaises(RoleAdapterRuntimeError) as raised:
                execute_role_command(
                    command=[sys.executable, "-c", "import time; time.sleep(0.3)"],
                    cwd=Path(temp_dir),
                    env=os.environ.copy(),
                    timeout_seconds=0.1,
                    runtime_failure_message="adapter command failed",
                    timeout_message="adapter command timed out",
                )
            elapsed = time.monotonic() - started_at

        self.assertTrue(raised.exception.timed_out)
        self.assertEqual(raised.exception.message, "adapter command timed out")
        self.assertEqual(raised.exception.configured_timeout_seconds, 0.1)
        self.assertGreaterEqual(raised.exception.elapsed_seconds or 0.0, 0.1)
        self.assertGreaterEqual(elapsed, 0.1)

    def test_read_json_result_file_reports_missing_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = read_json_result_file(
                Path(temp_dir) / "missing.json",
                missing_message="result file was not produced",
                invalid_json_message="result file is not valid JSON",
                invalid_type_message="result file must contain an object",
            )

        self.assertEqual(
            result,
            {
                "status": "missing",
                "message": "result file was not produced",
                "result_file_present": False,
            },
        )

    def test_read_json_result_file_reports_malformed_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            result_path.write_text("{not json", encoding="utf-8")

            result = read_json_result_file(
                result_path,
                missing_message="result file was not produced",
                invalid_json_message="result file is not valid JSON",
                invalid_type_message="result file must contain an object",
            )

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["message"], "result file is not valid JSON")
        self.assertTrue(result["result_file_present"])

    def test_redaction_helpers_scrub_payload_and_logs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            result_path.write_text(
                json.dumps({"status": "completed", "summary": "token super-secret-value"}),
                encoding="utf-8",
            )

            payload_result = read_json_result_file(
                result_path,
                missing_message="result file was not produced",
                invalid_json_message="result file is not valid JSON",
                invalid_type_message="result file must contain an object",
                exact_secrets={"super-secret-value"},
            )
            stdout, stderr = redact_adapter_streams(
                stdout="token super-secret-value",
                stderr="stderr super-secret-value",
                exact_secrets={"super-secret-value"},
            )

            with io.StringIO() as captured_stdout, io.StringIO() as captured_stderr:
                with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                    write_adapter_logs(stdout, stderr)
                stdout_log = captured_stdout.getvalue()
                stderr_log = captured_stderr.getvalue()

        self.assertEqual(payload_result["status"], "valid")
        self.assertNotIn("super-secret-value", json.dumps(payload_result["payload"]))
        self.assertNotIn("super-secret-value", stdout)
        self.assertNotIn("super-secret-value", stderr)
        self.assertNotIn("super-secret-value", stdout_log)
        self.assertNotIn("super-secret-value", stderr_log)

