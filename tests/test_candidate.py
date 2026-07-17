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
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import afk.candidate as candidate_module  # noqa: E402
import afk.candidate_validation as candidate_validation  # noqa: E402
from afk.candidate import (  # noqa: E402
    CandidateError,
    produce_candidate,
    produce_repair_candidate,
)
from afk.run_store import EvidenceTampered, RunStore  # noqa: E402
from afk.start import resume_run  # noqa: E402


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary_directory.name)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        self.home = self.temp / "home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir()
        self.remote = self.temp / "remote.git"
        self.primary_checkout = self.temp / "primary"
        self.checkout = self.temp / "checkout"
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "init", "-b", "main", str(self.primary_checkout)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "AFK Test"],
            cwd=self.primary_checkout,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "afk@example.test"],
            cwd=self.primary_checkout,
            check=True,
        )
        (self.primary_checkout / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=self.primary_checkout, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=self.primary_checkout,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(self.remote)],
            cwd=self.primary_checkout,
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=self.primary_checkout,
            check=True,
            capture_output=True,
        )
        self.base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.primary_checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.branch = "afk/central-test-1-run-1/candidate"
        subprocess.run(
            ["git", "worktree", "add", "-b", self.branch, str(self.checkout)],
            cwd=self.primary_checkout,
            check=True,
            capture_output=True,
        )
        self.state = self.temp / "state"
        self.store = RunStore(self.state)
        self.store.create_run(
            bead_id="central-test.1",
            repository="owner/project",
            base_branch="main",
            base_sha=self.base_sha,
            start_request={
                "repository_root": str(self.checkout),
                "beads_workspace": str(self.temp),
            },
            run_id="run-1",
        )
        self.store.append_event(
            "run-1",
            "worktree.ready",
            state="worktree_ready",
            data={
                "checkpoint": "worktree_ready",
                "worktree_path": str(self.checkout),
                "branch": self.branch,
            },
        )
        self.gh_state = self.temp / "gh-state.json"
        self.codex_env = self.temp / "codex-env.json"
        self.codex_args = self.temp / "codex-args.json"
        self._write_fakes()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def produce(self, **env):
        (self.codex_home / "fake-outcome").write_text(
            env.pop("CODEX_FAKE_OUTCOME", "completed"), encoding="utf-8"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin}:{environment['PATH']}",
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "GH_FAKE_STATE": str(self.gh_state),
                "CODEX_ENV_CAPTURE": str(self.codex_env),
                "GITHUB_TOKEN": "must-not-reach-codex",
                "BEADS_DOLT_PASSWORD": "must-not-reach-codex",
            }
        )
        environment.update(env)
        with mock.patch.dict(os.environ, environment, clear=True):
            return produce_candidate(
                self.store,
                "run-1",
                bead={
                    "id": "central-test.1",
                    "title": "Implement the thing",
                    "description": "Change one file.",
                    "acceptance_criteria": "The file exists.",
                },
            )

    def test_produces_exact_committed_head_as_one_stable_draft_pr(self):
        result = self.produce()

        self.assertEqual(result["state"], "candidate_ready")
        candidate_sha = self.git("rev-parse", "HEAD")
        self.assertNotEqual(candidate_sha, self.base_sha)
        self.assertEqual(result["candidate_sha"], candidate_sha)
        self.assertEqual(
            self.git("ls-remote", "origin", f"refs/heads/{self.branch}").split()[0],
            candidate_sha,
        )
        pr = json.loads(self.gh_state.read_text(encoding="utf-8"))
        self.assertTrue(pr["isDraft"])
        self.assertEqual(pr["headRefOid"], candidate_sha)
        self.assertEqual(pr["headRefName"], self.branch)
        self.assertEqual(pr["baseRefName"], "main")
        run_dir = self.state / "runs" / "run-1"
        attempt = run_dir / "attempts" / "implementation-1"
        self.assertTrue((attempt / "manifest.json").is_file())
        prompt = (attempt / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("central-test.1", prompt)
        self.assertIn(self.base_sha, prompt)
        self.assertIn(
            "Commit after the safe checks available inside this sandbox", prompt
        )
        self.assertIn("AFK runs the full Validation Contract afterward", prompt)
        self.assertIn("Do not access Docker, the Docker socket, or systemd", prompt)
        self.assertIn(
            "Do not report blocked solely because privileged validation", prompt
        )
        captured_env = json.loads(self.codex_env.read_text(encoding="utf-8"))
        self.assertNotIn("GITHUB_TOKEN", captured_env)
        self.assertNotIn("BEADS_DOLT_PASSWORD", captured_env)
        self.assertEqual(captured_env["HOME"], str(self.home))
        self.assertEqual(captured_env["CODEX_HOME"], str(self.codex_home))
        args = json.loads(self.codex_args.read_text(encoding="utf-8"))
        self.assertNotIn("--sandbox", args)
        configs = [args[index + 1] for index, arg in enumerate(args) if arg == "-c"]
        config = "\n".join(configs)
        git_dir = Path(self.git("rev-parse", "--git-dir")).resolve()
        common_dir = Path(self.git("rev-parse", "--git-common-dir")).resolve()
        for expected in (
            'default_permissions="afk_candidate"',
            'web_search="disabled"',
            'inherit = "none"',
            f'"{self.checkout}" = "write"',
            f'"{self.checkout / ".git"}" = "read"',
            f'"{git_dir}" = "write"',
            f'"{common_dir}" = "read"',
            f'"{common_dir / "objects"}" = "write"',
            f'"{common_dir / "refs" / "heads" / "afk" / "central-test-1-run-1"}" = "write"',  # noqa: E501
            f'"{common_dir / "logs" / "refs" / "heads" / "afk" / "central-test-1-run-1"}" = "write"',  # noqa: E501
            f'"{git_dir / "afk-tmp" / "home"}"',
            f'"{git_dir / "afk-tmp"}"',
            "enabled = false",
        ):
            self.assertIn(expected, config)
        self.assertNotIn(f'"{self.home}" = "read"', config)
        self.assertNotIn(f'"{self.home}" = "write"', config)
        self.assertIn("ignore_default_excludes = false", config)
        self.assertNotIn(
            f'"{common_dir / "refs" / "heads" / self.branch}" = "write"',
            config,
        )
        for forbidden in (
            common_dir / "refs" / "heads" / "afk",
            common_dir / "logs" / "refs" / "heads" / "afk",
            common_dir / "refs" / "heads" / "afk" / "sibling-run",
            common_dir / "logs" / "refs" / "heads" / "afk" / "sibling-run",
        ):
            self.assertNotIn(f'"{forbidden}" = "write"', config)
        self.assertNotIn("CODEX_HOME", config)
        self.assertEqual(
            self.store.effect("run-1", f"branch-push-{candidate_sha}")["status"],
            "confirmed",
        )
        self.assertEqual(self.store.effect("run-1", "pr-create")["status"], "confirmed")

    def test_repair_consumes_a_slot_and_advances_the_same_candidate_branch(self):
        first = self.produce()
        brief = {
            "schema_version": 1,
            "candidate_sha": first["candidate_sha"],
            "repair_attempt": 1,
            "blocking_findings": [
                {
                    "id": "validation-smoke",
                    "source": "validation",
                    "title": "Smoke test failed",
                    "body": "Repair the smoke startup.",
                    "blocking": True,
                }
            ],
        }

        with mock.patch.dict(os.environ, self._candidate_environment(), clear=True):
            result = produce_repair_candidate(
                self.store,
                "run-1",
                bead={
                    "id": "central-test.1",
                    "title": "Implement the thing",
                    "description": "Change one file.",
                    "acceptance_criteria": "The file exists.",
                },
                repair_brief=brief,
            )

        self.assertNotEqual(result["candidate_sha"], first["candidate_sha"])
        self.assertEqual(result["repair_attempts_used"], 1)
        self.assertEqual(result["previous_candidate_sha"], first["candidate_sha"])
        attempt = self.state / "runs" / "run-1" / "attempts" / "repair-1"
        report = json.loads((attempt / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(
            report["dispositions"],
            [{"finding_id": "validation-smoke", "disposition": "addressed"}],
        )
        pr = json.loads(self.gh_state.read_text(encoding="utf-8"))
        self.assertEqual(pr["number"], 7)
        self.assertEqual(pr["headRefOid"], result["candidate_sha"])

    def test_malformed_repair_output_is_sealed_and_consumes_its_slot(self):
        first = self.produce()
        brief = {
            "schema_version": 1,
            "candidate_sha": first["candidate_sha"],
            "repair_attempt": 1,
            "blocking_findings": [
                {
                    "id": "validation-smoke",
                    "source": "validation",
                    "title": "Smoke test failed",
                    "body": "Repair it.",
                    "blocking": True,
                }
            ],
        }
        (self.codex_home / "fake-outcome").write_text("malformed", encoding="utf-8")

        with mock.patch.dict(os.environ, self._candidate_environment(), clear=True):
            with self.assertRaisesRegex(CandidateError, "report"):
                produce_repair_candidate(
                    self.store,
                    "run-1",
                    bead={
                        "id": "central-test.1",
                        "title": "Implement the thing",
                        "description": "Change one file.",
                        "acceptance_criteria": "The file exists.",
                    },
                    repair_brief=brief,
                )

        attempt = self.state / "runs" / "run-1" / "attempts" / "repair-1"
        self.assertTrue((attempt / "manifest.json").is_file())
        self.assertEqual(self.store.status("run-1")["repair_attempts_used"], 1)

    def test_timed_out_repair_seals_evidence_and_leaves_no_descendant_mutation(self):
        first = self.produce()
        brief = {
            "schema_version": 1,
            "candidate_sha": first["candidate_sha"],
            "repair_attempt": 1,
            "blocking_findings": [
                {
                    "id": "validation-smoke",
                    "source": "validation",
                    "title": "Smoke test failed",
                    "body": "Repair it.",
                    "blocking": True,
                }
            ],
        }
        self.store.append_event(
            "run-1",
            "gate.cycle_completed",
            state="validated",
            data={
                "checkpoint": "validated",
                "repair_brief": brief,
            },
        )
        (self.codex_home / "fake-outcome").write_text("timeout", encoding="utf-8")

        with (
            mock.patch.dict(os.environ, self._candidate_environment(), clear=True),
            mock.patch.object(candidate_module, "COMMAND_TIMEOUT_SECONDS", 0.1),
            mock.patch.object(candidate_validation, "PROCESS_CLEANUP_SECONDS", 0.1),
            self.assertRaisesRegex(CandidateError, "timed out"),
        ):
            produce_repair_candidate(
                self.store,
                "run-1",
                bead={
                    "id": "central-test.1",
                    "title": "Implement the thing",
                    "description": "Change one file.",
                    "acceptance_criteria": "The file exists.",
                },
                repair_brief=brief,
            )

        time.sleep(0.7)
        self.assertFalse((self.checkout / "late-repair-mutation").exists())
        attempt = self.state / "runs/run-1/attempts/repair-1"
        self.assertTrue((attempt / "manifest.json").is_file())
        outcome = json.loads((attempt / "outcome.json").read_text(encoding="utf-8"))
        self.assertEqual(outcome["status"], "interrupted")
        self.assertEqual(self.store.status("run-1")["checkpoint"], "validated")

    def test_repair_resume_reconciles_crash_windows_without_rerunning_codex(self):
        first = self.produce()
        brief = {
            "schema_version": 1,
            "candidate_sha": first["candidate_sha"],
            "repair_attempt": 1,
            "blocking_findings": [
                {
                    "id": "validation-smoke",
                    "source": "validation",
                    "title": "Smoke test failed",
                    "body": "Repair it.",
                    "blocking": True,
                }
            ],
        }
        bead = {
            "id": "central-test.1",
            "title": "Implement the thing",
            "description": "Change one file.",
            "acceptance_criteria": "The file exists.",
        }
        gate_outcome = {"next_action": "repair", "repair_brief": brief}
        self.store.append_event(
            "run-1",
            "gate.cycle_completed",
            state="candidate_ready",
            data={
                "checkpoint": "candidate_ready",
                "gate_cycles": [gate_outcome],
            },
        )

        with (
            mock.patch.dict(os.environ, self._candidate_environment(), clear=True),
            mock.patch(
                "afk.candidate._verify_candidate",
                side_effect=RuntimeError("crash after sealed report"),
            ),
            self.assertRaisesRegex(RuntimeError, "crash after sealed report"),
        ):
            produce_repair_candidate(
                self.store,
                "run-1",
                bead=bead,
                repair_brief=brief,
            )

        attempt = self.state / "runs" / "run-1" / "attempts" / "repair-1"
        self.assertTrue((attempt / "manifest.json").is_file())
        codex = self.bin / "codex"
        disabled_codex = self.bin / "codex.disabled"
        codex.rename(disabled_codex)
        with (
            mock.patch.dict(os.environ, self._candidate_environment(), clear=True),
            mock.patch("afk.start.RunStore", return_value=self.store),
            mock.patch("afk.start._show_bead", return_value=bead),
            mock.patch("afk.start._advance_validation", return_value=0),
        ):
            resumed_run_id, resumed_exit = resume_run()
        self.assertEqual((resumed_run_id, resumed_exit), ("run-1", 0))
        resumed = self.store.status("run-1")

        self.assertEqual(resumed["repair_attempts_used"], 1)
        self.assertEqual(resumed["previous_candidate_sha"], first["candidate_sha"])
        disabled_codex.rename(codex)

        second_brief = {
            **brief,
            "candidate_sha": resumed["candidate_sha"],
            "repair_attempt": 2,
        }
        original_confirm = self.store.confirm_effect

        def crash_before_push_confirmation(run_id, effect_id, *, observed):
            if effect_id.startswith("branch-push-"):
                raise RuntimeError("crash after push before confirmation")
            return original_confirm(run_id, effect_id, observed=observed)

        with (
            mock.patch.dict(os.environ, self._candidate_environment(), clear=True),
            mock.patch.object(
                self.store,
                "confirm_effect",
                side_effect=crash_before_push_confirmation,
            ),
            self.assertRaisesRegex(RuntimeError, "after push before confirmation"),
        ):
            produce_repair_candidate(
                self.store,
                "run-1",
                bead=bead,
                repair_brief=second_brief,
            )

        second_sha = self.git("rev-parse", "HEAD")
        self.assertEqual(
            self.store.effect("run-1", f"branch-push-{second_sha}")["status"],
            "prepared",
        )
        codex.rename(disabled_codex)
        with mock.patch.dict(os.environ, self._candidate_environment(), clear=True):
            second = produce_repair_candidate(
                self.store,
                "run-1",
                bead=bead,
                repair_brief=second_brief,
            )
        self.assertEqual(second["repair_attempts_used"], 2)
        self.assertEqual(
            self.store.effect("run-1", f"branch-push-{second_sha}")["status"],
            "confirmed",
        )
        disabled_codex.rename(codex)

        third_brief = {
            **brief,
            "candidate_sha": second["candidate_sha"],
            "repair_attempt": 3,
        }
        original_append = self.store.append_event

        def crash_before_candidate_event(run_id, event, **kwargs):
            if event == "candidate.repaired":
                raise RuntimeError("crash before candidate repaired event")
            return original_append(run_id, event, **kwargs)

        with (
            mock.patch.dict(os.environ, self._candidate_environment(), clear=True),
            mock.patch.object(
                self.store, "append_event", side_effect=crash_before_candidate_event
            ),
            self.assertRaisesRegex(RuntimeError, "before candidate repaired event"),
        ):
            produce_repair_candidate(
                self.store,
                "run-1",
                bead=bead,
                repair_brief=third_brief,
            )

        third_sha = self.git("rev-parse", "HEAD")
        self.assertEqual(
            self.store.effect("run-1", f"branch-push-{third_sha}")["status"],
            "confirmed",
        )
        codex.rename(disabled_codex)
        with mock.patch.dict(os.environ, self._candidate_environment(), clear=True):
            third = produce_repair_candidate(
                self.store,
                "run-1",
                bead=bead,
                repair_brief=third_brief,
            )
        self.assertEqual(third["repair_attempts_used"], 3)
        events = [
            json.loads(line)
            for line in (self.state / "runs" / "run-1" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            [event["event"] for event in events].count("candidate.repaired"), 3
        )

    def _candidate_environment(self):
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin}:{environment['PATH']}",
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "GH_FAKE_STATE": str(self.gh_state),
                "CODEX_ENV_CAPTURE": str(self.codex_env),
            }
        )
        return environment

    def test_repair_resume_rejects_unsealed_tampered_or_invalid_evidence(self):
        first = self.produce()
        candidate_sha = first["candidate_sha"]
        codex = self.bin / "codex"
        codex.unlink()

        def brief(attempt):
            return {
                "schema_version": 1,
                "candidate_sha": candidate_sha,
                "repair_attempt": attempt,
                "blocking_findings": [
                    {
                        "id": "validation-smoke",
                        "source": "validation",
                        "title": "Smoke failed",
                        "body": "Repair it.",
                        "blocking": True,
                    }
                ],
            }

        def start_attempt(attempt, repair_brief):
            self.store.append_event(
                "run-1",
                "repair.started",
                data={
                    "checkpoint": "candidate_ready",
                    "repair_attempts_used": attempt,
                    "repair_brief": repair_brief,
                },
            )

        bead = {
            "id": "central-test.1",
            "title": "Implement the thing",
            "description": "Change one file.",
            "acceptance_criteria": "The file exists.",
        }
        first_brief = brief(1)
        start_attempt(1, first_brief)
        self.store.write_evidence_text(
            "run-1", "attempts/repair-1/prompt.md", "started\n"
        )
        with self.assertRaisesRegex(CandidateError, "incomplete"):
            produce_repair_candidate(
                self.store, "run-1", bead=bead, repair_brief=first_brief
            )

        valid_report = {
            "status": "completed",
            "starting_sha": candidate_sha,
            "ending_sha": candidate_sha,
            "summary": "repaired",
            "checks": [],
            "changed_areas": ["app"],
            "dispositions": [
                {"finding_id": "validation-smoke", "disposition": "addressed"}
            ],
        }
        second_brief = brief(2)
        start_attempt(2, second_brief)
        self.store.write_evidence_text(
            "run-1",
            "attempts/repair-2/report.json",
            json.dumps(valid_report),
        )
        self.store.seal_evidence("run-1", "attempts/repair-2")
        tampered = self.state / "runs/run-1/attempts/repair-2/report.json"
        tampered.chmod(0o600)
        tampered.write_text("{}", encoding="utf-8")
        tampered.chmod(0o400)
        with self.assertRaises(EvidenceTampered):
            produce_repair_candidate(
                self.store, "run-1", bead=bead, repair_brief=second_brief
            )

        third_brief = brief(3)
        start_attempt(3, third_brief)
        invalid_report = {
            **valid_report,
            "dispositions": [
                {"finding_id": "wrong-finding", "disposition": "addressed"}
            ],
        }
        self.store.write_evidence_text(
            "run-1",
            "attempts/repair-3/report.json",
            json.dumps(invalid_report),
        )
        self.store.seal_evidence("run-1", "attempts/repair-3")
        with self.assertRaisesRegex(CandidateError, "dispositions"):
            produce_repair_candidate(
                self.store, "run-1", bead=bead, repair_brief=third_brief
            )

    def test_allows_only_codex_package_when_installed_beneath_home(self):
        package = self.home / ".local/lib/node_modules/@openai/codex"
        wrapper = package / "bin/codex.js"
        wrapper.parent.mkdir(parents=True)
        (self.bin / "codex").replace(wrapper)
        (self.bin / "codex").symlink_to(wrapper)

        self.produce()

        args = json.loads(self.codex_args.read_text(encoding="utf-8"))
        config = "\n".join(
            args[index + 1] for index, arg in enumerate(args) if arg == "-c"
        )
        self.assertIn(f'"{package}" = "read"', config)
        self.assertNotIn(f'"{self.home}" = "read"', config)
        self.assertNotIn(f'"{self.home}" = "write"', config)
        self.assertIn("ignore_default_excludes = false", config)

    def test_does_not_allow_a_home_lookalike_codex_package(self):
        lookalike = self.home / "private"
        wrapper = lookalike / "bin/codex.js"
        wrapper.parent.mkdir(parents=True)
        (self.bin / "codex").replace(wrapper)
        (self.bin / "codex").symlink_to(wrapper)

        self.produce()

        args = json.loads(self.codex_args.read_text(encoding="utf-8"))
        config = "\n".join(
            args[index + 1] for index, arg in enumerate(args) if arg == "-c"
        )
        self.assertNotIn(f'"{lookalike}" = "read"', config)

    def test_no_change_and_dirty_results_require_attention(self):
        with self.subTest("no change"):
            with self.assertRaisesRegex(CandidateError, "no_change"):
                self.produce(CODEX_FAKE_OUTCOME="no_change")

        self.tearDown()
        self.setUp()
        with self.subTest("dirty"):
            with self.assertRaisesRegex(CandidateError, "dirty"):
                self.produce(CODEX_FAKE_OUTCOME="dirty")

    def test_legacy_flat_candidate_branch_fails_closed(self):
        with self.assertRaisesRegex(CandidateError, "per-Run namespace"):
            candidate_module._codex_permission_args(
                self.checkout, "afk/central-test-1-run-1"
            )

    def test_rejects_nonzero_malformed_and_merge_results(self):
        for outcome, message in (
            ("nonzero", "exited"),
            ("malformed", "report"),
            ("merge", "merge commit"),
        ):
            with self.subTest(outcome):
                with self.assertRaisesRegex(CandidateError, message):
                    self.produce(CODEX_FAKE_OUTCOME=outcome)
            self.tearDown()
            self.setUp()

    def test_reconciles_a_push_and_pr_created_before_effect_confirmation(self):
        first = self.produce()
        candidate_sha = first["candidate_sha"]
        push_path = (
            self.state
            / "runs"
            / "run-1"
            / "effects"
            / f"branch-push-{candidate_sha}.json"
        )
        pr_path = self.state / "runs" / "run-1" / "effects" / "pr-create.json"
        for path in (push_path, pr_path):
            value = json.loads(path.read_text(encoding="utf-8"))
            value.pop("observed")
            value["status"] = "prepared"
            path.chmod(0o600)
            path.write_text(json.dumps(value), encoding="utf-8")

        reconciled = self.produce()

        self.assertEqual(reconciled["candidate_sha"], candidate_sha)
        self.assertEqual(self.store.effect("run-1", "pr-create")["status"], "confirmed")

    def test_pushes_verified_candidate_sha_when_local_head_moves(self):
        original_remote_sha = candidate_module._remote_sha
        observed = {}

        def move_head_after_candidate_verification(worktree, branch):
            remote_sha = original_remote_sha(worktree, branch)
            if "candidate_sha" not in observed and not remote_sha:
                observed["candidate_sha"] = self.git("rev-parse", "HEAD")
                (self.checkout / "later.txt").write_text("later\n", encoding="utf-8")
                subprocess.run(
                    ["git", "add", "later.txt"], cwd=self.checkout, check=True
                )
                subprocess.run(
                    ["git", "commit", "-m", "later"],
                    cwd=self.checkout,
                    check=True,
                    capture_output=True,
                )
                observed["moved_sha"] = self.git("rev-parse", "HEAD")
            return remote_sha

        with mock.patch(
            "afk.candidate._remote_sha",
            side_effect=move_head_after_candidate_verification,
        ):
            with self.assertRaises(CandidateError):
                self.produce()

        remote_sha = self.git(
            "ls-remote", "origin", f"refs/heads/{self.branch}"
        ).split()[0]
        self.assertEqual(remote_sha, observed["candidate_sha"])
        self.assertNotEqual(remote_sha, observed["moved_sha"])

    def _write_fakes(self):
        codex = self.bin / "codex"
        codex.write_text(
            textwrap.dedent(
                f"""
                #!/usr/bin/env python3
                import json, os, signal, subprocess, sys, time
                from pathlib import Path
                args = sys.argv[1:]
                prompt = sys.stdin.read()
                cwd = Path(args[args.index("--cd") + 1])
                report = Path(args[args.index("--output-last-message") + 1])
                Path({str(self.codex_env)!r}).write_text(json.dumps(dict(os.environ)), encoding="utf-8")  # noqa: E501
                Path({str(self.codex_args)!r}).write_text(json.dumps(args), encoding="utf-8")  # noqa: E501
                outcome = (Path(os.environ["CODEX_HOME"]) / "fake-outcome").read_text()
                start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()  # noqa: E501
                repair = "# AFK repair attempt" in prompt
                repair_attempt = prompt.split("Attempt: repair-")[1].splitlines()[0] if repair else ""  # noqa: E501
                changed = f"repair-{{repair_attempt}}.txt" if repair else "candidate.txt"
                if outcome == "timeout":
                    child = "import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(0.5);open('late-repair-mutation','w').write('mutated')"  # noqa: E501
                    subprocess.Popen([sys.executable, "-c", child], cwd=cwd)
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    time.sleep(30)
                if outcome not in {"no_change", "nonzero", "malformed"}:
                    (cwd / changed).write_text("candidate\\n", encoding="utf-8")
                    subprocess.run(["git", "add", changed], cwd=cwd, check=True)
                    subprocess.run(["git", "commit", "-m", "repair" if repair else "candidate"], cwd=cwd, check=True, capture_output=True)  # noqa: E501
                if outcome == "merge":
                    subprocess.run(["git", "commit", "--allow-empty", "-m", "side"], cwd=cwd, check=True, capture_output=True)  # noqa: E501
                    side = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()  # noqa: E501
                    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=cwd, check=True, capture_output=True)  # noqa: E501
                    subprocess.run(["git", "merge", "--no-ff", side, "-m", "merge"], cwd=cwd, check=True, capture_output=True)  # noqa: E501
                if outcome == "dirty":
                    (cwd / "dirty.txt").write_text("dirty\\n", encoding="utf-8")
                end = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()  # noqa: E501
                if outcome == "malformed":
                    report.write_text("not json", encoding="utf-8")
                else:
                    value = {{
                        "status": "no_change" if outcome == "no_change" else "completed",  # noqa: E501
                        "starting_sha": start,
                        "ending_sha": end,
                        "summary": "implemented",
                        "checks": [],
                        "changed_areas": [changed],
                    }}
                    if repair:
                        value["dispositions"] = [{{"finding_id": "validation-smoke", "disposition": "addressed"}}]
                    report.write_text(json.dumps(value), encoding="utf-8")
                print(json.dumps({{"type": "result"}}))
                raise SystemExit(1 if outcome == "nonzero" else 0)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        gh = self.bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env python3
                import json, os, subprocess, sys
                from pathlib import Path
                args = sys.argv[1:]
                state = Path(os.environ["GH_FAKE_STATE"])
                if args[:2] == ["pr", "list"]:
                    if state.exists():
                        value = json.loads(state.read_text())
                        value["headRefOid"] = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()  # noqa: E501
                        state.write_text(json.dumps(value), encoding="utf-8")
                        print(json.dumps([value]))
                    else:
                        print("[]")
                elif args[:2] == ["pr", "create"]:
                    branch = args[args.index("--head") + 1]
                    base = args[args.index("--base") + 1]
                    oid = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()  # noqa: E501
                    value = {"number": 7, "url": "https://example.test/pr/7", "state": "OPEN", "isDraft": True, "headRefOid": oid, "headRefName": branch, "baseRefName": base}  # noqa: E501
                    state.write_text(json.dumps(value), encoding="utf-8")
                    print(value["url"])
                else:
                    raise SystemExit(f"unexpected gh args: {args}")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        for path in (codex, gh):
            path.chmod(path.stat().st_mode | stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
