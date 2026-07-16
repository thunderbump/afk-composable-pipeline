import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.run_store import RunStore  # noqa: E402


WRITE_PASSED_LOG = (
    'evidence.joinpath("tests.log").write_text(' '"passed\\n", encoding="utf-8")'
)
WRITE_SAFE_LOG = (
    'evidence.joinpath("tests.log").write_text('
    '"safe validation log\\n", encoding="utf-8")'
)


class CandidateValidationCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary_directory.name)
        self.repository = self.temp / "repository"
        self.repository.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "afk@example.invalid")
        self.git("config", "user.name", "AFK Test")
        self.state_home = self.temp / "state"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_resume_advances_only_after_exact_candidate_validation_passes(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        run_id, candidate_sha = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["state"], "validated")
        self.assertEqual(status["checkpoint"], "validated")
        self.assertEqual(status["validation"]["status"], "passed")
        self.assertEqual(status["validation"]["candidate_sha"], candidate_sha)
        evidence = (
            self.state_home / "afk" / "runs" / run_id / status["validation"]["evidence"]
        )
        self.assertTrue((evidence / "manifest.json").is_file())
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o500)

    def test_rejected_validation_preserves_evidence_and_prepares_repair(self):
        self.write_contract_worker(
            status="rejected",
            exit_code=1,
            checks=[{"name": "tests", "status": "rejected", "log_path": "tests.log"}],
        )
        run_id, candidate_sha = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["checkpoint"], "candidate_ready")
        self.assertEqual(status["attention"]["kind"], "rejected")
        self.assertEqual(status["validation"]["status"], "rejected")
        self.assertEqual(status["validation"]["candidate_sha"], candidate_sha)
        self.assertEqual(status["validation"]["next_action"], "repair")
        evidence = (
            self.state_home / "afk" / "runs" / run_id / status["validation"]["evidence"]
        )
        self.assertTrue((evidence / "manifest.json").is_file())

    def test_inconclusive_validation_requires_attention(self):
        self.write_contract_worker(
            status="inconclusive",
            exit_code=2,
            checks=[
                {
                    "name": "docker",
                    "status": "inconclusive",
                    "log_path": "tests.log",
                }
            ],
        )
        run_id, candidate_sha = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["checkpoint"], "candidate_ready")
        self.assertEqual(status["attention"]["kind"], "inconclusive")
        self.assertEqual(status["validation"]["status"], "inconclusive")
        self.assertEqual(status["validation"]["candidate_sha"], candidate_sha)
        self.assertEqual(status["validation"]["next_action"], "attention")

    def test_boolean_contract_schema_version_is_invalid(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        contract = (self.repository / "afk.toml").read_text(encoding="utf-8")
        (self.repository / "afk.toml").write_text(
            contract.replace("schema_version = 1", "schema_version = true"),
            encoding="utf-8",
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")

    def test_boolean_contract_timeout_is_invalid(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        contract = (self.repository / "afk.toml").read_text(encoding="utf-8")
        (self.repository / "afk.toml").write_text(
            contract.replace("timeout_seconds = 5", "timeout_seconds = true"),
            encoding="utf-8",
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")

    def test_pinned_contract_content_ignores_candidate_afk_toml_changes(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        self.git("add", ".")
        self.git("commit", "-m", "trusted contract")
        base_sha = self.git("rev-parse", "HEAD")
        blob_sha = self.git("rev-parse", "HEAD:afk.toml")
        (self.repository / "afk.toml").write_text(
            "candidate contract proposal\n", encoding="utf-8"
        )
        self.git("add", "afk.toml")
        self.git("commit", "-m", "propose contract change")
        candidate_sha = self.git("rev-parse", "HEAD")
        run_id = self.create_ready_run(
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            validation_contract={
                "source": "pinned_base",
                "base_sha": base_sha,
                "blob_sha": blob_sha,
            },
        )

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["checkpoint"], "validated")
        self.assertEqual(status["validation"]["contract"]["blob_sha"], blob_sha)

    def test_pinned_candidate_harness_change_is_not_executed(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        self.git("add", ".")
        self.git("commit", "-m", "trusted harness")
        base_sha = self.git("rev-parse", "HEAD")
        blob_sha = self.git("rev-parse", "HEAD:afk.toml")
        marker = self.temp / "untrusted-harness-ran"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f'Path({str(marker)!r}).write_text("ran", encoding="utf-8"); '
                + WRITE_PASSED_LOG
            ),
        )
        self.git("add", ".")
        self.git("commit", "-m", "propose harness change")
        candidate_sha = self.git("rev-parse", "HEAD")
        run_id = self.create_ready_run(
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            validation_contract={
                "source": "pinned_base",
                "base_sha": base_sha,
                "blob_sha": blob_sha,
            },
        )

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("harness", status["attention"]["summary"])
        self.assertFalse(marker.exists())

    def test_bootstrap_rejects_contract_fields_outside_version_one(self):
        marker = self.temp / "invalid-contract-ran"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f'Path({str(marker)!r}).write_text("ran", encoding="utf-8"); '
                + WRITE_PASSED_LOG
            ),
        )
        (self.repository / "afk.toml").write_text(
            'version = 1\n\n[validation]\ncommand = "./validate.py"\n'
            "candidate_argument = true\ntimeout_seconds = 5\n",
            encoding="utf-8",
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("contract", status["attention"]["summary"])
        self.assertFalse(marker.exists())

    def test_evidence_log_path_cannot_escape_the_evidence_directory(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[
                {"name": "tests", "status": "passed", "log_path": "../outside.log"}
            ],
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["checkpoint"], "candidate_ready")
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("evidence", status["attention"]["summary"])

    def test_declared_log_must_be_in_the_validated_evidence_tree(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line='evidence.joinpath("other.log").write_text("passed\\n")',
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("regular", status["attention"]["summary"])

    def test_evidence_log_must_not_be_a_symlink(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line='evidence.joinpath("tests.log").symlink_to(Path(__file__))',
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("regular", status["attention"]["summary"])

    def test_validation_result_must_not_be_a_symlink(self):
        outside = self.temp / "outside-result.json"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f"outside = Path({str(outside)!r}); "
                'outside.write_text("{}", encoding="utf-8"); '
                'evidence.joinpath("result.json").symlink_to(outside); '
                + WRITE_PASSED_LOG
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("result", status["attention"]["summary"])
        self.assertIn("regular", status["attention"]["summary"])

    def test_evidence_log_must_be_utf8_text(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line='evidence.joinpath("tests.log").write_bytes(bytes([255]))',
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("UTF-8", status["attention"]["summary"])

    def test_unreferenced_evidence_must_also_be_utf8_text(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                WRITE_PASSED_LOG + "; "
                'evidence.joinpath("extra.bin").write_bytes(bytes([255]))'
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("UTF-8", status["attention"]["summary"])

    def test_evidence_log_size_is_bounded(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'evidence.joinpath("tests.log").write_text('
                '"x" * (16 * 1024 * 1024 + 1), encoding="utf-8")'
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("size", status["attention"]["summary"])

    def test_validation_output_size_is_bounded(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'sys.stdout.write("x" * (1024 * 1024 + 1)); ' + WRITE_PASSED_LOG
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("output", status["attention"]["summary"])
        self.assertIn("size", status["attention"]["summary"])

    def test_timeout_terminates_the_validation_process_group(self):
        child_pid_path = self.temp / "child.pid"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'child = subprocess.Popen(["sleep", "60"]); '
                f"Path({str(child_pid_path)!r}).write_text("
                'str(child.pid), encoding="utf-8"); '
                "time.sleep(60)"
            ),
            timeout_seconds=1,
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "interrupted")
        self.assertIn("timed out", status["attention"]["summary"])
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_successful_worker_cannot_leave_descendants_running(self):
        child_pid_path = self.temp / "successful-child.pid"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'child = subprocess.Popen(["sleep", "60"]); '
                f"Path({str(child_pid_path)!r}).write_text("
                'str(child.pid), encoding="utf-8"); ' + WRITE_PASSED_LOG
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_validation_signal_requires_interrupted_attention(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line="os.kill(os.getpid(), signal.SIGTERM)",
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "interrupted")
        self.assertIn("signal", status["attention"]["summary"])

    def test_exit_and_result_status_must_agree(self):
        self.write_contract_worker(
            status="passed",
            exit_code=1,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("disagree", status["attention"]["summary"])

    def test_candidate_mutation_invalidates_validation(self):
        (self.repository / "README.md").write_text("original\n", encoding="utf-8")
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                WRITE_PASSED_LOG + "; "
                'Path("README.md").write_text("mutated\\n", encoding="utf-8")'
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "head_mismatch")
        self.assertIn("changed", status["attention"]["summary"])

    def test_validation_environment_is_allowlisted_and_evidence_is_redacted(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'evidence.joinpath("tests.log").write_text('
                'json.dumps({"environment": sorted(os.environ), '
                '"password": "hunter2"}), '
                'encoding="utf-8")'
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume", UNRELATED_SECRET="must-not-cross")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        evidence_relative = status["validation"]["evidence"]
        evidence = self.state_home / "afk" / "runs" / run_id / evidence_relative
        log = json.loads((evidence / "tests.log").read_text(encoding="utf-8"))
        self.assertNotIn("UNRELATED_SECRET", log["environment"])
        self.assertEqual(log["password"], "[REDACTED]")

    def write_contract_worker(
        self,
        *,
        status,
        exit_code,
        checks,
        evidence_line=WRITE_SAFE_LOG,
        timeout_seconds=5,
    ):
        worker = self.repository / "validate.py"
        worker.write_text(
            textwrap.dedent(
                f"""
                #!/usr/bin/env python3
                import json
                import os
                import signal
                import subprocess
                import sys
                import time
                from pathlib import Path

                request_path = Path(sys.argv[sys.argv.index("--request") + 1])
                request = json.loads(request_path.read_text(encoding="utf-8"))
                evidence = Path(request["evidence_dir"])
                {evidence_line}
                evidence.joinpath("result.json").write_text(json.dumps({{
                    "schema_version": 1,
                    "candidate_sha": request["candidate_sha"],
                    "status": {status!r},
                    "summary": "validation {status}",
                    "checks": {checks!r},
                }}), encoding="utf-8")
                raise SystemExit({exit_code})
                """
            ).lstrip(),
            encoding="utf-8",
        )
        worker.chmod(worker.stat().st_mode | stat.S_IXUSR)
        (self.repository / "afk.toml").write_text(
            'schema_version = 1\n\n[validation]\ncommand = ["./validate.py"]\n'
            f"timeout_seconds = {timeout_seconds}\n",
            encoding="utf-8",
        )

    def candidate_ready_run(self):
        self.git("add", ".")
        self.git("commit", "-m", "candidate")
        candidate_sha = self.git("rev-parse", "HEAD")
        return (
            self.create_ready_run(
                candidate_sha=candidate_sha,
                base_sha=candidate_sha,
                validation_contract={
                    "source": "approved_bootstrap",
                    "base_sha": candidate_sha,
                    "adapter_id": "afk.builtin.bootstrap-validation/v1",
                },
            ),
            candidate_sha,
        )

    def create_ready_run(self, *, candidate_sha, base_sha, validation_contract):
        store = RunStore(self.state_home / "afk")
        run_id = store.create_run(
            bead_id="central-test.1",
            repository="thunderbump/test",
            base_branch="main",
            base_sha=base_sha,
            start_request={
                "repository_root": str(self.repository),
                "validation_contract": validation_contract,
            },
        )["run_id"]
        store.append_event(
            run_id,
            "worktree.ready",
            state="worktree_ready",
            data={
                "checkpoint": "worktree_ready",
                "worktree_path": str(self.repository),
            },
        )
        store.append_event(
            run_id,
            "candidate.ready",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "candidate_sha": candidate_sha,
                "pr_head_sha": candidate_sha,
                "validation_contract": store.identity(run_id)["start_request"][
                    "validation_contract"
                ],
            },
        )
        store.append_event(
            run_id,
            "run.attention_required",
            state="attention_required",
            data={
                "checkpoint": "candidate_ready",
                "attention": {
                    "scope": "validation",
                    "kind": "unavailable",
                    "summary": "validation is not available in this AFK slice",
                },
            },
        )
        store.append_event(
            run_id,
            "worker.terminal",
            data={
                "checkpoint": "candidate_ready",
                "worker_exit_code": 2,
                "worker_result": "attention_required",
            },
        )
        return run_id

    def run_afk(self, *args, **overrides):
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "XDG_STATE_HOME": str(self.state_home),
            }
        )
        environment.update(overrides)
        return subprocess.run(
            [sys.executable, "-m", "afk", *args],
            cwd=self.repository,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def status(self, run_id):
        completed = self.run_afk("status", run_id, "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def git(self, *args):
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
