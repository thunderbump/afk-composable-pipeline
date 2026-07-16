import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk import candidate_validation  # noqa: E402
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
        self.assertEqual(status["validation"]["exit_code"], 0)
        evidence = (
            self.state_home / "afk" / "runs" / run_id / status["validation"]["evidence"]
        )
        self.assertTrue((evidence / "manifest.json").is_file())
        self.assertEqual(
            json.loads((evidence / "afk" / "outcome.json").read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": candidate_sha,
                "exit_code": 0,
                "status": "passed",
                "summary": "validation passed",
            },
        )
        manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn(
            "afk/outcome.json", {entry["path"] for entry in manifest["files"]}
        )
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o500)

    def test_contract_evidence_names_do_not_collide_with_afk_metadata(self):
        formerly_reserved = ("request.json", "stdout.log", "stderr.log", "outcome.json")
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[
                {"name": name, "status": "passed", "log_path": name}
                for name in formerly_reserved
            ],
            evidence_line="; ".join(
                f'evidence.joinpath({name!r}).write_text("contract {name}\\n", '
                'encoding="utf-8")'
                for name in formerly_reserved
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        gate = (
            self.state_home / "afk" / "runs" / run_id / status["validation"]["evidence"]
        )
        for name in formerly_reserved:
            self.assertEqual(
                (gate / "contract" / name).read_text(encoding="utf-8"),
                f"contract {name}\n",
            )
            self.assertTrue((gate / "afk" / name).is_file())
        manifest = json.loads((gate / "manifest.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        for name in formerly_reserved:
            self.assertIn(f"contract/{name}", paths)
            self.assertIn(f"afk/{name}", paths)

    def test_contract_manifest_name_is_bound_by_gate_root_manifest(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[
                {
                    "name": "contract-manifest",
                    "status": "passed",
                    "log_path": "manifest.json",
                }
            ],
            evidence_line=(
                'evidence.joinpath("manifest.json").write_text('
                '"contract manifest\\n", encoding="utf-8")'
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        gate = (
            self.state_home / "afk" / "runs" / run_id / status["validation"]["evidence"]
        )
        self.assertEqual(
            (gate / "contract" / "manifest.json").read_text(encoding="utf-8"),
            "contract manifest\n",
        )
        root_manifest = json.loads((gate / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn(
            "contract/manifest.json",
            {entry["path"] for entry in root_manifest["files"]},
        )

    def test_rejected_validation_preserves_evidence_and_prepares_repair(self):
        self.write_contract_worker(
            status="rejected",
            exit_code=1,
            checks=[{"name": "tests", "status": "rejected", "log_path": "tests.log"}],
        )
        run_id, candidate_sha = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["state"], "candidate_ready")
        self.assertEqual(status["checkpoint"], "candidate_ready")
        self.assertEqual(status["last_event"], "validation.rejected")
        self.assertEqual(status["attention"], {})
        self.assertEqual(status["validation"]["status"], "rejected")
        self.assertEqual(status["validation"]["candidate_sha"], candidate_sha)
        self.assertEqual(status["validation"]["next_action"], "repair")
        evidence = (
            self.state_home / "afk" / "runs" / run_id / status["validation"]["evidence"]
        )
        self.assertTrue((evidence / "manifest.json").is_file())

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.status(run_id)["last_sequence"], status["last_sequence"])

    def test_rejected_validation_accepts_mixed_check_outcomes(self):
        self.write_contract_worker(
            status="rejected",
            exit_code=1,
            checks=[
                {"name": "preflight", "status": "passed", "log_path": "tests.log"},
                {"name": "tier1", "status": "rejected", "log_path": "tests.log"},
                {
                    "name": "cleanup",
                    "status": "inconclusive",
                    "log_path": "tests.log",
                },
                {"name": "tier2", "status": "not_run", "log_path": "tests.log"},
            ],
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["last_event"], "validation.rejected")
        self.assertEqual(status["validation"]["status"], "rejected")

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
        first_attempt = status["validation_attempt"]
        first_gate = status["validation"]["evidence"]

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 2, resumed.stderr)
        retried = self.status(run_id)
        self.assertEqual(retried["attention"]["kind"], "inconclusive")
        self.assertNotEqual(
            retried["validation_attempt"]["attempt_id"], first_attempt["attempt_id"]
        )
        self.assertNotEqual(
            retried["validation_attempt"]["evidence"], first_attempt["evidence"]
        )
        self.assertNotEqual(retried["validation"]["evidence"], first_gate)
        run = self.state_home / "afk" / "runs" / run_id
        for relative in (
            first_attempt["evidence"],
            first_gate,
            retried["validation_attempt"]["evidence"],
            retried["validation"]["evidence"],
        ):
            self.assertTrue((run / relative / "manifest.json").is_file())

    def test_inconclusive_validation_requires_an_inconclusive_check(self):
        self.write_contract_worker(
            status="inconclusive",
            exit_code=2,
            checks=[{"name": "tier1", "status": "not_run", "log_path": "tests.log"}],
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("disagree", status["attention"]["summary"])

    def test_resume_seals_an_open_validation_attempt_as_interrupted(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        run_id, candidate_sha = self.candidate_ready_run()
        store = RunStore(self.state_home / "afk")
        attempt_id = f"validation-{candidate_sha[:12]}"
        attempt = {
            "attempt_id": attempt_id,
            "candidate_sha": candidate_sha,
            "status": "started",
            "evidence": f"attempts/{attempt_id}",
        }
        store.append_event(
            run_id,
            "validation.attempt_started",
            data={"checkpoint": "candidate_ready", "validation_attempt": attempt},
        )
        store.write_evidence_text(
            run_id,
            f"{attempt['evidence']}/contract/partial.log",
            "password=crash-secret\n",
        )

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "interrupted")
        self.assertEqual(status["validation_attempt"]["status"], "interrupted")
        evidence = (
            self.state_home
            / "afk"
            / "runs"
            / run_id
            / status["validation_attempt"]["evidence"]
        )
        self.assertTrue((evidence / "manifest.json").is_file())
        self.assertEqual(
            (evidence / "contract" / "partial.log").read_text(encoding="utf-8"),
            "password=[REDACTED]\n",
        )
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o500)
        sequence = status["last_sequence"]

        resumed = self.run_afk("resume")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        retried = self.status(run_id)
        self.assertEqual(retried["checkpoint"], "validated")
        self.assertGreater(retried["last_sequence"], sequence)
        self.assertNotEqual(
            retried["validation_attempt"]["attempt_id"], attempt["attempt_id"]
        )
        self.assertNotEqual(
            retried["validation_attempt"]["evidence"], attempt["evidence"]
        )
        retry_evidence = (
            self.state_home
            / "afk"
            / "runs"
            / run_id
            / retried["validation_attempt"]["evidence"]
        )
        self.assertTrue((retry_evidence / "manifest.json").is_file())

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

    def test_boolean_result_schema_version_is_invalid(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        worker = (self.repository / "validate.py").read_text(encoding="utf-8")
        (self.repository / "validate.py").write_text(
            worker.replace('"schema_version": 1,', '"schema_version": True,'),
            encoding="utf-8",
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["checkpoint"], "candidate_ready")
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

    def test_pinned_relative_candidate_harness_change_is_not_executed(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        scripts = self.repository / "scripts"
        scripts.mkdir()
        (self.repository / "validate.py").replace(scripts / "validation.py")
        (self.repository / "afk.toml").write_text(
            "schema_version = 1\n\n[validation]\n"
            'command = ["python3", "scripts/validation.py"]\n'
            "timeout_seconds = 5\n",
            encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-m", "trusted relative harness")
        base_sha = self.git("rev-parse", "HEAD")
        blob_sha = self.git("rev-parse", "HEAD:afk.toml")

        marker = self.temp / "untrusted-relative-harness-ran"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f'Path({str(marker)!r}).write_text("ran", encoding="utf-8"); '
                + WRITE_PASSED_LOG
            ),
        )
        (self.repository / "validate.py").replace(scripts / "validation.py")
        self.git("add", ".")
        self.git("commit", "-m", "propose relative harness change")
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

    def test_pinned_symlink_cannot_load_candidate_modified_harness(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        (self.repository / "validate.py").rename(self.repository / "runner.py")
        (self.repository / "validate.py").symlink_to("runner.py")
        self.git("add", ".")
        self.git("commit", "-m", "trusted symlink harness")
        base_sha = self.git("rev-parse", "HEAD")
        blob_sha = self.git("rev-parse", "HEAD:afk.toml")

        marker = self.temp / "untrusted-symlink-target-ran"
        runner = (self.repository / "runner.py").read_text(encoding="utf-8")
        (self.repository / "runner.py").write_text(
            runner.replace(
                WRITE_SAFE_LOG,
                f'Path({str(marker)!r}).write_text("ran", encoding="utf-8"); '
                + WRITE_PASSED_LOG,
            ),
            encoding="utf-8",
        )
        self.git("add", ".")
        self.git("commit", "-m", "propose symlink target change")
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
        self.assertIn("regular tracked file", status["attention"]["summary"])
        self.assertFalse(marker.exists())

    def test_pinned_python_module_harness_change_is_not_executed(self):
        self.assert_pinned_indirect_harness_is_rejected(["python3", "-m", "validate"])

    def test_pinned_python_inline_harness_change_is_not_executed(self):
        self.assert_pinned_indirect_harness_is_rejected(
            ["python3", "-c", "import validate"]
        )

    def test_pinned_shell_inline_harness_change_is_not_executed(self):
        self.assert_pinned_indirect_harness_is_rejected(
            ["sh", "-c", './validate.py "$@"', "validation"]
        )

    def test_bootstrap_ignores_candidate_selected_validation_policy(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        scripts = self.repository / "scripts"
        scripts.mkdir()
        (self.repository / "validate.py").replace(scripts / "validation-worker.sh")
        (self.repository / "afk.toml").unlink()
        self.git("add", ".")
        self.git("commit", "-m", "trusted bootstrap harness")
        base_sha = self.git("rev-parse", "HEAD")

        marker = self.temp / "candidate-policy-ran"
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
        self.git("commit", "-m", "candidate validation policy proposal")
        candidate_sha = self.git("rev-parse", "HEAD")
        run_id = self.create_ready_run(
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            validation_contract={
                "source": "approved_bootstrap",
                "base_sha": base_sha,
                "adapter_id": "afk.builtin.bootstrap-validation/v1",
            },
        )

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.status(run_id)["checkpoint"], "validated")
        self.assertFalse(marker.exists())

    def test_bootstrap_fails_closed_without_a_preserved_harness(self):
        self.git("commit", "--allow-empty", "-m", "base without validation harness")
        base_sha = self.git("rev-parse", "HEAD")
        marker = self.temp / "untrusted-bootstrap-policy-ran"
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
        self.git("commit", "-m", "candidate bootstrap policy proposal")
        candidate_sha = self.git("rev-parse", "HEAD")
        run_id = self.create_ready_run(
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            validation_contract={
                "source": "approved_bootstrap",
                "base_sha": base_sha,
                "adapter_id": "afk.builtin.bootstrap-validation/v1",
            },
        )

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("harness", status["attention"]["summary"])
        self.assertFalse(marker.exists())

    def test_contract_rejects_fields_outside_version_one(self):
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

    def test_evidence_root_cannot_be_replaced_by_an_external_symlink(self):
        outside = self.temp / "outside-evidence"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f"outside = Path({str(outside)!r}); "
                "outside.mkdir(); evidence.rmdir(); "
                "evidence.symlink_to(outside, target_is_directory=True); "
                "evidence = outside; " + WRITE_PASSED_LOG
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("evidence directory", status["attention"]["summary"])
        gate = (
            self.state_home
            / "afk"
            / "runs"
            / run_id
            / "gates"
            / status["validation_attempt"]["attempt_id"]
        )
        self.assertFalse(gate.exists())

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

    def test_gate_evidence_uses_one_authoritative_total_limit(self):
        evidence = self.temp / "gate-boundary"
        evidence.mkdir()
        (evidence / "first.log").write_text("a" * 8, encoding="utf-8")
        (evidence / "second.log").write_text("b" * 8, encoding="utf-8")

        with patch.object(candidate_validation, "GATE_BYTE_LIMIT", 16):
            files, total = candidate_validation._require_evidence_tree(evidence)
            self.assertEqual(files, {"first.log", "second.log"})
            self.assertEqual(total, 16)

            (evidence / "third.log").write_text("c", encoding="utf-8")
            with self.assertRaises(candidate_validation.CandidateValidationError):
                candidate_validation._require_evidence_tree(evidence)

    def test_validation_output_limits_are_independent_per_stream(self):
        command = [
            sys.executable,
            "-c",
            "import os; os.write(1, b'a' * 16); os.write(2, b'b' * 16)",
        ]

        with patch.object(candidate_validation, "OUTPUT_BYTE_LIMIT", 16):
            completed = candidate_validation._run_contract(
                command,
                cwd=self.repository,
                environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                timeout_seconds=2,
            )

        self.assertEqual(completed.stdout, "a" * 16)
        self.assertEqual(completed.stderr, "b" * 16)

    def test_validation_output_size_is_bounded(self):
        completed_marker = self.temp / "oversized-output-completed"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'exec("for _ in range(1280):\\n" '
                "\"    os.write(1, b'x' * 65536)\"); "
                f"Path({str(completed_marker)!r}).write_text("
                '"completed", encoding="utf-8"); ' + WRITE_PASSED_LOG
            ),
            timeout_seconds=10,
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        self.assertIn("output", status["attention"]["summary"])
        self.assertIn("size", status["attention"]["summary"])
        self.assertFalse(completed_marker.exists())

    def test_validation_output_is_redacted_without_a_raw_temporary_copy(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'sys.stdout.write("password=hunter2\\n"); sys.stdout.flush(); '
                'raw = request_path.parent / "stdout.raw"; '
                'evidence.joinpath("tests.log").write_text(json.dumps({'
                '"raw_output_exists": raw.exists(), '
                '"raw_secret_present": raw.exists() and "hunter2" in '
                'raw.read_text(encoding="utf-8")}), encoding="utf-8")'
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.status(run_id)
        evidence = (
            self.state_home / "afk" / "runs" / run_id / status["validation"]["evidence"]
        )
        observed = json.loads(
            (evidence / "contract" / "tests.log").read_text(encoding="utf-8")
        )
        self.assertEqual(
            observed, {"raw_output_exists": False, "raw_secret_present": False}
        )
        self.assertEqual(
            (evidence / "afk" / "stdout.log").read_text(encoding="utf-8"),
            "password=[REDACTED]\n",
        )
        for path in (self.state_home / "afk" / "runs" / run_id).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"hunter2", path.read_bytes())

    def test_timeout_terminates_the_validation_process_group(self):
        child_pid_path = self.temp / "child.pid"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'sys.stdout.write("password=timeout-secret\\n"); '
                'sys.stdout.flush(); child = subprocess.Popen(["sleep", "60"]); '
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
        attempt = status["validation_attempt"]
        self.assertEqual(attempt["status"], "interrupted")
        evidence = self.state_home / "afk" / "runs" / run_id / attempt["evidence"]
        self.assertTrue((evidence / "manifest.json").is_file())
        self.assertEqual(
            (evidence / "afk" / "stdout.log").read_text(encoding="utf-8"),
            "password=[REDACTED]\n",
        )
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o500)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_timeout_kills_term_resistant_validation_descendant(self):
        child_pid_path = self.temp / "resistant-timeout-child.pid"
        child_program = textwrap.dedent(
            """
            import os
            import signal
            import sys
            import time
            from pathlib import Path

            pid_path = Path(sys.argv[1])
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(60)
            """
        ).lstrip()
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f'child = subprocess.Popen([sys.executable, "-c", {child_program!r}, '
                f"{str(child_pid_path)!r}]); "
                f'exec("while not Path({str(child_pid_path)!r}).exists():\\n" '
                '"    time.sleep(0.001)"); '
                "time.sleep(60)"
            ),
            timeout_seconds=1,
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        try:
            self.assertEqual(completed.returncode, 2)
            status = self.status(run_id)
            self.assertEqual(status["attention"]["kind"], "interrupted")
            self.assertIn("timed out", status["attention"]["summary"])
            deadline = time.monotonic() + 2
            while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

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

    def test_successful_worker_kills_detached_term_resistant_descendant(self):
        child_pid_path = self.temp / "detached-child.pid"
        child_program = textwrap.dedent(
            """
            import os
            import signal
            import sys
            import time
            from pathlib import Path

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(3)
            """
        ).lstrip()
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f"subprocess.Popen([sys.executable, '-c', {child_program!r}, "
                f"{str(child_pid_path)!r}], start_new_session=True); "
                f'exec("while not Path({str(child_pid_path)!r}).exists():\\n" '
                '"    time.sleep(0.001)"); ' + WRITE_PASSED_LOG
            ),
        )
        run_id, _ = self.candidate_ready_run()

        started = time.monotonic()
        completed = self.run_afk("resume")
        elapsed = time.monotonic() - started

        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        try:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertLess(elapsed, 2)
            deadline = time.monotonic() + 2
            while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_successful_worker_is_drained_before_evidence_is_ingested(self):
        child_pid_path = self.temp / "resistant-child.pid"
        child_ready_path = self.temp / "resistant-child.ready"
        mutation_path = self.temp / "evidence-mutated"
        child_program = textwrap.dedent(
            """
            import signal
            import sys
            import time
            from pathlib import Path

            evidence, destination, mutation, ready = map(Path, sys.argv[1:])
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            ready.write_text("ready", encoding="utf-8")
            while not destination.exists():
                time.sleep(0.001)
            mutation.write_text("mutated", encoding="utf-8")
            evidence.joinpath("tests.log").write_text("mutated\\n", encoding="utf-8")
            time.sleep(60)
            """
        ).lstrip()
        evidence_line = (
            f'destination = Path({str(self.state_home)!r}) / "afk" / "runs" / '
            'request["run_id"] / f"gates/validation-{request[\'candidate_sha\']}" / '
            '"afk/request.json"; '
            f'child = subprocess.Popen([sys.executable, "-c", {child_program!r}, '
            f"str(evidence), str(destination), {str(mutation_path)!r}, "
            f"{str(child_ready_path)!r}]); "
            f"Path({str(child_pid_path)!r}).write_text("
            'str(child.pid), encoding="utf-8"); '
            f'exec("while not Path({str(child_ready_path)!r}).exists():\\n" '
            '"    time.sleep(0.001)"); ' + WRITE_PASSED_LOG
        )
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=evidence_line,
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        try:
            deadline = time.monotonic() + 2
            while not mutation_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(mutation_path.exists())
            status = self.status(run_id)
            evidence = (
                self.state_home
                / "afk"
                / "runs"
                / run_id
                / status["validation"]["evidence"]
            )
            self.assertEqual(
                (evidence / "contract" / "tests.log").read_text(encoding="utf-8"),
                "passed\n",
            )
        finally:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

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

    def test_malformed_result_retains_sealed_redacted_attempt_diagnostics(self):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                'sys.stdout.write("password=malformed-secret\\n"); '
                "sys.stdout.flush(); raise SystemExit(0)"
            ),
        )
        run_id, _ = self.candidate_ready_run()

        completed = self.run_afk("resume")

        self.assertEqual(completed.returncode, 2)
        status = self.status(run_id)
        self.assertEqual(status["attention"]["kind"], "invalid")
        attempt = status["validation_attempt"]
        self.assertEqual(attempt["status"], "invalid")
        evidence = self.state_home / "afk" / "runs" / run_id / attempt["evidence"]
        self.assertTrue((evidence / "manifest.json").is_file())
        self.assertEqual(
            (evidence / "afk" / "stdout.log").read_text(encoding="utf-8"),
            "password=[REDACTED]\n",
        )
        outcome = json.loads(
            (evidence / "afk" / "outcome.json").read_text(encoding="utf-8")
        )
        self.assertEqual(outcome["status"], "invalid")
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o500)

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
        log = json.loads(
            (evidence / "contract" / "tests.log").read_text(encoding="utf-8")
        )
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

    def assert_pinned_indirect_harness_is_rejected(self, command):
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
        )
        self.write_contract_command(command)
        self.git("add", ".")
        self.git("commit", "-m", "trusted indirect harness")
        base_sha = self.git("rev-parse", "HEAD")
        blob_sha = self.git("rev-parse", "HEAD:afk.toml")

        marker = self.temp / "untrusted-indirect-harness-ran"
        self.write_contract_worker(
            status="passed",
            exit_code=0,
            checks=[{"name": "tests", "status": "passed", "log_path": "tests.log"}],
            evidence_line=(
                f'Path({str(marker)!r}).write_text("ran", encoding="utf-8"); '
                + WRITE_PASSED_LOG
            ),
        )
        self.write_contract_command(command)
        self.git("add", ".")
        self.git("commit", "-m", "propose indirect harness change")
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
        self.assertIn("command grammar", status["attention"]["summary"])
        self.assertFalse(marker.exists())

    def write_contract_command(self, command):
        (self.repository / "afk.toml").write_text(
            "schema_version = 1\n\n[validation]\n"
            f"command = {json.dumps(command)}\n"
            "timeout_seconds = 5\n",
            encoding="utf-8",
        )

    def candidate_ready_run(self):
        self.git("add", ".")
        self.git("commit", "-m", "trusted validation base")
        base_sha = self.git("rev-parse", "HEAD")
        blob_sha = self.git("rev-parse", "HEAD:afk.toml")
        self.git("commit", "--allow-empty", "-m", "candidate")
        candidate_sha = self.git("rev-parse", "HEAD")
        return (
            self.create_ready_run(
                candidate_sha=candidate_sha,
                base_sha=base_sha,
                validation_contract={
                    "source": "pinned_base",
                    "base_sha": base_sha,
                    "blob_sha": blob_sha,
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
