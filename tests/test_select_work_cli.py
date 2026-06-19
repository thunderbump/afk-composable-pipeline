import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_afk(*args, env=None):
    run_env = os.environ.copy()
    run_env["PYTHONPATH"] = str(ROOT / "src")
    if env:
        for key, value in env.items():
            if value is None:
                run_env.pop(key, None)
            else:
                run_env[key] = value
    return subprocess.run(
        [sys.executable, "-m", "afk", *args],
        cwd=ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


class SelectWorkCliTest(unittest.TestCase):
    def test_fixture_source_writes_normalized_selection(self):
        request = {
            "required_labels": ["afk:ready"],
            "sources": [
                {
                    "type": "fixture",
                    "id": "fixture",
                    "items": [
                        {
                            "external_id": "central-lve.3",
                            "url": "https://tracker.example/central-lve.3",
                            "title": "Implement WorkSource selection",
                            "status": "open",
                            "labels": ["project:afk-composable-pipeline", "afk:ready"],
                            "parent": "central-lve",
                            "workstream": "central-lve",
                            "acceptance_criteria": ["Fixture selection is normalized"],
                            "dependencies": [{"id": "central-lve.2", "status": "closed"}],
                            "blockers": [],
                            "afk": {"ready": True},
                            "raw": {"bead_id": "central-lve.3"},
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["step"], "select-work")
            self.assertEqual(result["output"]["schema_version"], 1)
            self.assertEqual(
                result["output"]["source_statuses"],
                [
                    {
                        "source_id": "fixture",
                        "source_type": "fixture",
                        "status": "selected",
                        "candidate_count": 1,
                        "selected_count": 1,
                        "message": "selected 1 candidate",
                    }
                ],
            )
            self.assertEqual(result["output"]["skipped_candidates"], [])

            selected = result["output"]["selected_work"]
            self.assertEqual(len(selected), 1)
            self.assertEqual(
                selected[0],
                {
                    "source_id": "fixture",
                    "source_type": "fixture",
                    "external_id": "central-lve.3",
                    "url": "https://tracker.example/central-lve.3",
                    "title": "Implement WorkSource selection",
                    "status": "open",
                    "labels": ["project:afk-composable-pipeline", "afk:ready"],
                    "parent": "central-lve",
                    "workstream": "central-lve",
                    "acceptance_criteria": ["Fixture selection is normalized"],
                    "dependencies": [{"id": "central-lve.2", "status": "closed"}],
                    "blockers": [],
                    "dependency_status": "clear",
                    "afk": {"ready": True},
                    "raw": {"bead_id": "central-lve.3"},
                },
            )

            events = [
                json.loads(line)
                for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["event"] for event in events],
                ["run.started", "step.started", "step.completed", "run.completed"],
            )

    def test_github_and_beads_sources_skip_without_auth_or_workspace(self):
        request = {
            "required_labels": ["afk:ready"],
            "sources": [
                {
                    "type": "github_issues",
                    "id": "github",
                    "repo": "thunderbump/afk-composable-pipeline",
                    "labels": ["afk:ready"],
                    "query": "label:afk:ready is:open",
                },
                {
                    "type": "beads",
                    "id": "central-beads",
                    "workspace": "/definitely/missing/beads/workspace",
                    "labels": ["project:afk-composable-pipeline", "afk:ready"],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["selected_work"], [])
            self.assertEqual(result["output"]["skipped_candidates"], [])
            self.assertEqual(
                result["output"]["source_statuses"],
                [
                    {
                        "source_id": "github",
                        "source_type": "github_issues",
                        "status": "skipped_no_auth",
                        "candidate_count": 0,
                        "selected_count": 0,
                        "message": "GH_TOKEN or GITHUB_TOKEN is required",
                    },
                    {
                        "source_id": "central-beads",
                        "source_type": "beads",
                        "status": "skipped_unreachable",
                        "candidate_count": 0,
                        "selected_count": 0,
                        "message": "beads workspace is not available",
                    },
                ],
            )

    def test_invalid_fixture_payload_records_source_failure(self):
        request = {"sources": [{"type": "fixture", "id": "fixture", "items": {"not": "a list"}}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["selected_work"], [])
            self.assertEqual(
                result["output"]["source_statuses"],
                [
                    {
                        "source_id": "fixture",
                        "source_type": "fixture",
                        "status": "failed_invalid_payload",
                        "candidate_count": 0,
                        "selected_count": 0,
                        "message": "fixture items must be a list",
                    }
                ],
            )

    def test_malformed_fixture_candidate_is_reported_without_crashing(self):
        request = {
            "sources": [
                {
                    "type": "fixture",
                    "id": "fixture",
                    "items": [
                        {
                            "external_id": "bad",
                            "title": "Malformed candidate",
                            "status": "open",
                            "labels": "afk:ready",
                            "afk": ["bad"],
                            "raw": ["bad"],
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["selected_work"], [])
            self.assertEqual(
                [
                    (skipped["candidate"]["external_id"], skipped["reason"])
                    for skipped in result["output"]["skipped_candidates"]
                ],
                [("bad", "invalid_candidate_payload")],
            )
            self.assertEqual(result["output"]["source_statuses"][0]["status"], "skipped_empty")

    def test_fixture_filtering_rejects_blocked_active_and_missing_metadata_candidates(self):
        request = {
            "required_labels": ["afk:ready"],
            "required_metadata": ["workstream", "acceptance_criteria", "afk.ready"],
            "sources": [
                {
                    "type": "fixture",
                    "id": "fixture",
                    "items": [
                        {
                            "external_id": "blocked",
                            "title": "Blocked work",
                            "status": "open",
                            "labels": ["afk:ready"],
                            "workstream": "central-lve",
                            "acceptance_criteria": ["blocked candidate is rejected"],
                            "dependencies": [{"id": "central-lve.99", "status": "open"}],
                            "afk": {"ready": True},
                        },
                        {
                            "external_id": "active-run",
                            "title": "Already running",
                            "status": "open",
                            "labels": ["afk:ready"],
                            "workstream": "central-lve",
                            "acceptance_criteria": ["active run is rejected"],
                            "afk": {"ready": True, "active_run_id": "run-123"},
                        },
                        {
                            "external_id": "missing-metadata",
                            "title": "Missing workstream",
                            "status": "open",
                            "labels": ["afk:ready"],
                            "acceptance_criteria": ["metadata is required"],
                            "afk": {"ready": True},
                        },
                        {
                            "external_id": "runnable",
                            "title": "Runnable work",
                            "status": "open",
                            "labels": ["afk:ready"],
                            "workstream": "central-lve",
                            "acceptance_criteria": ["runnable candidate is selected"],
                            "dependencies": [{"id": "central-lve.2", "status": "closed"}],
                            "afk": {"ready": True},
                        },
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(
                [candidate["external_id"] for candidate in result["output"]["selected_work"]],
                ["runnable"],
            )
            self.assertEqual(
                [
                    (skipped["candidate"]["external_id"], skipped["reason"])
                    for skipped in result["output"]["skipped_candidates"]
                ],
                [
                    ("blocked", "blocked"),
                    ("active-run", "active_run_exists"),
                    ("missing-metadata", "missing_metadata:workstream"),
                ],
            )
            self.assertEqual(result["output"]["source_statuses"][0]["status"], "selected")
            self.assertEqual(result["output"]["source_statuses"][0]["candidate_count"], 4)
            self.assertEqual(result["output"]["source_statuses"][0]["selected_count"], 1)

    def test_candidate_status_is_normalized_before_filtering(self):
        request = {
            "sources": [
                {
                    "type": "fixture",
                    "id": "fixture",
                    "items": [
                        {
                            "external_id": "uppercase-open",
                            "title": "Uppercase open status",
                            "status": "OPEN",
                            "labels": [],
                            "afk": {"ready": True},
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [candidate["external_id"] for candidate in result["output"]["selected_work"]],
                ["uppercase-open"],
            )
            self.assertEqual(result["output"]["selected_work"][0]["status"], "open")

    def test_duplicate_candidates_are_selected_once(self):
        request = {
            "required_labels": ["afk:ready"],
            "sources": [
                {
                    "type": "fixture",
                    "id": "fixture-a",
                    "items": [
                        {
                            "external_id": "issue-7",
                            "url": "https://github.com/example/repo/issues/7",
                            "title": "Shared work item",
                            "status": "open",
                            "labels": ["afk:ready"],
                            "workstream": "central-lve",
                            "acceptance_criteria": ["only one copy is selected"],
                            "afk": {"ready": True},
                        }
                    ],
                },
                {
                    "type": "fixture",
                    "id": "fixture-b",
                    "items": [
                        {
                            "external_id": "bead-7",
                            "url": "https://github.com/example/repo/issues/7",
                            "title": "Same work from another source",
                            "status": "open",
                            "labels": ["afk:ready"],
                            "workstream": "central-lve",
                            "acceptance_criteria": ["duplicate is skipped"],
                            "afk": {"ready": True},
                        }
                    ],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(
                [candidate["external_id"] for candidate in result["output"]["selected_work"]],
                ["issue-7"],
            )
            self.assertEqual(
                [
                    (skipped["candidate"]["source_id"], skipped["candidate"]["external_id"], skipped["reason"])
                    for skipped in result["output"]["skipped_candidates"]
                ],
                [("fixture-b", "bead-7", "duplicate:https://github.com/example/repo/issues/7")],
            )
            self.assertEqual(
                [status["status"] for status in result["output"]["source_statuses"]],
                ["selected", "skipped_empty"],
            )

    def test_environment_auth_values_are_not_written_to_artifacts(self):
        secret = "ghp_this_secret_must_not_be_written"
        request = {
            "sources": [
                {
                    "type": "github_issues",
                    "id": "github",
                    "repo": "thunderbump/afk-composable-pipeline",
                    "labels": ["afk:ready"],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
                env={"GH_TOKEN": secret, "GITHUB_TOKEN": None, "PATH": ""},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]

            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in [
                    run_dir / "command.json",
                    run_dir / "ledger.jsonl",
                    run_dir / "step-result.json",
                    run_dir / "stdout.log",
                    run_dir / "stderr.log",
                ]
            )
            self.assertNotIn(secret, artifact_text)
            self.assertIn("gh command is not available", artifact_text)

    def test_github_source_normalizes_fake_cli_issues_and_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
import json
import sys

if sys.argv[1:3] == ["issue", "list"]:
    print(json.dumps([
        {{
            "number": 7,
            "title": "Fake GitHub issue",
            "state": "OPEN",
            "url": "https://github.com/example/repo/issues/7",
            "body": "## Acceptance Criteria\\n- [ ] GitHub issue is normalized",
            "labels": [{{"name": "afk:ready"}}, {{"name": "workstream:central-lve"}}],
        }}
    ]))
elif len(sys.argv) >= 3 and sys.argv[1] == "api" and sys.argv[2].endswith("/dependencies/blocked_by"):
    print("[]")
else:
    print("unexpected gh args: " + " ".join(sys.argv[1:]), file=sys.stderr)
    sys.exit(9)
""",
            )

            request = {
                "required_labels": ["afk:ready"],
                "required_metadata": ["workstream", "acceptance_criteria", "afk.ready"],
                "sources": [
                    {
                        "type": "github_issues",
                        "id": "github",
                        "repo": "example/repo",
                        "labels": ["afk:ready"],
                        "query": "label:afk:ready is:open",
                    }
                ],
            }
            ledger = temp_path / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
                env={"GH_TOKEN": "fake-token", "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["source_statuses"][0]["status"], "selected")
            self.assertEqual(result["output"]["skipped_candidates"], [])
            self.assertEqual(
                result["output"]["selected_work"],
                [
                    {
                        "source_id": "github",
                        "source_type": "github_issues",
                        "external_id": "example/repo#7",
                        "url": "https://github.com/example/repo/issues/7",
                        "title": "Fake GitHub issue",
                        "status": "open",
                        "labels": ["afk:ready", "workstream:central-lve"],
                        "parent": None,
                        "workstream": "central-lve",
                        "acceptance_criteria": ["GitHub issue is normalized"],
                        "dependencies": [],
                        "blockers": [],
                        "dependency_status": "clear",
                        "afk": {"ready": True},
                        "raw": {"github": {"repo": "example/repo", "number": 7}},
                    }
                ],
            )

    def test_beads_source_normalizes_fake_central_workspace(self):
        secret = "beads-secret-value"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            workspace = temp_path / "beads"
            (workspace / "secrets").mkdir(parents=True)
            (workspace / "secrets" / "dolt_beads_password.txt").write_text(secret, encoding="utf-8")
            fake_bin.mkdir()
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
import os
import sys

if os.environ.get("BEADS_DOLT_PASSWORD") != "{secret}":
    print("missing password", file=sys.stderr)
    sys.exit(8)

if len(sys.argv) > 1 and sys.argv[1] == "list":
    print(json.dumps([{{"id": "central-lve.4"}}]))
elif len(sys.argv) > 2 and sys.argv[1] == "show" and sys.argv[2] == "central-lve.4":
    print(json.dumps([
        {{
            "id": "central-lve.4",
            "title": "Prepare checkout",
            "description": "body",
            "acceptance_criteria": "- [ ] Beads item is normalized",
            "status": "open",
            "labels": ["project:afk-composable-pipeline", "afk:ready"],
            "parent": "central-lve",
            "metadata": {{"workstream": "central-lve", "afk_ready": True}},
            "dependencies": [
                {{"id": "central-lve.3", "status": "closed", "dependency_type": "blocks"}}
            ],
        }}
    ]))
else:
    print("unexpected bd args: " + " ".join(sys.argv[1:]), file=sys.stderr)
    sys.exit(9)
""",
            )

            request = {
                "required_labels": ["project:afk-composable-pipeline", "afk:ready"],
                "required_metadata": ["workstream", "acceptance_criteria", "afk.ready"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(workspace),
                        "labels": ["project:afk-composable-pipeline", "afk:ready"],
                    }
                ],
            }
            ledger = temp_path / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
                env={"PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result["output"]["source_statuses"][0]["status"], "selected")
            self.assertEqual(result["output"]["skipped_candidates"], [])
            self.assertEqual(
                result["output"]["selected_work"][0],
                {
                    "source_id": "central-beads",
                    "source_type": "beads",
                    "external_id": "central-lve.4",
                    "url": "",
                    "title": "Prepare checkout",
                    "status": "open",
                    "labels": ["project:afk-composable-pipeline", "afk:ready"],
                    "parent": "central-lve",
                    "workstream": "central-lve",
                    "acceptance_criteria": ["Beads item is normalized"],
                    "dependencies": [{"id": "central-lve.3", "status": "closed", "type": "blocks"}],
                    "blockers": [],
                    "dependency_status": "clear",
                    "afk": {"ready": True},
                    "raw": {"beads": {"id": "central-lve.4"}},
                },
            )
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in [
                    run_dir / "command.json",
                    run_dir / "ledger.jsonl",
                    run_dir / "step-result.json",
                    run_dir / "stdout.log",
                    run_dir / "stderr.log",
                ]
            )
            self.assertNotIn(secret, artifact_text)

    def test_beads_source_rejects_project_local_beads_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            workspace = temp_path / "repo" / ".beads"
            (workspace / "secrets").mkdir(parents=True)
            (workspace / "secrets" / "dolt_beads_password.txt").write_text(
                "secret",
                encoding="utf-8",
            )
            fake_bin.mkdir()
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
raise SystemExit("bd should not be called for project-local .beads")
""",
            )

            request = {
                "sources": [
                    {
                        "type": "beads",
                        "id": "project-local",
                        "workspace": str(workspace),
                        "labels": ["project:afk-composable-pipeline"],
                    }
                ],
            }
            ledger = temp_path / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
                env={"PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            self.assertEqual(
                result["output"]["source_statuses"],
                [
                    {
                        "source_id": "project-local",
                        "source_type": "beads",
                        "status": "skipped_unconfigured",
                        "candidate_count": 0,
                        "selected_count": 0,
                        "message": "project-local .beads workspace is not allowed",
                    }
                ],
            )

    def test_beads_false_ready_metadata_does_not_select_candidate(self):
        secret = "beads-secret-value"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            workspace = temp_path / "beads"
            (workspace / "secrets").mkdir(parents=True)
            (workspace / "secrets" / "dolt_beads_password.txt").write_text(secret, encoding="utf-8")
            fake_bin.mkdir()
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
import sys

if len(sys.argv) > 1 and sys.argv[1] == "list":
    print(json.dumps([{{"id": "central-lve.false"}}]))
elif len(sys.argv) > 2 and sys.argv[1] == "show":
    print(json.dumps([
        {{
            "id": "central-lve.false",
            "title": "Not ready",
            "acceptance_criteria": "- [ ] not selected",
            "status": "open",
            "labels": ["project:afk-composable-pipeline"],
            "metadata": {{"workstream": "central-lve", "afk_ready": "false"}},
            "dependencies": [],
        }}
    ]))
else:
    sys.exit(9)
""",
            )

            request = {
                "required_metadata": ["afk.ready"],
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(workspace),
                        "labels": ["project:afk-composable-pipeline"],
                    }
                ],
            }
            ledger = temp_path / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
                env={"PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["output"]["selected_work"], [])
            self.assertEqual(result["output"]["skipped_candidates"][0]["reason"], "missing_metadata:afk.ready")
            self.assertEqual(result["output"]["skipped_candidates"][0]["candidate"]["afk"], {"ready": False})

    def test_beads_source_skips_empty_credentials_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            workspace = temp_path / "beads"
            (workspace / "secrets").mkdir(parents=True)
            (workspace / "secrets" / "dolt_beads_password.txt").write_text("", encoding="utf-8")
            fake_bin.mkdir()
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
raise SystemExit("bd should not be called without credentials")
""",
            )

            request = {
                "sources": [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(workspace),
                        "labels": ["project:afk-composable-pipeline"],
                    }
                ],
            }
            ledger = temp_path / "ledger"
            completed = run_afk(
                "run-step",
                "select-work",
                "--input",
                json.dumps(request),
                "--ledger",
                str(ledger),
                env={"PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            run_dir = ledger / "runs" / summary["run_id"]
            result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
            self.assertEqual(
                result["output"]["source_statuses"],
                [
                    {
                        "source_id": "central-beads",
                        "source_type": "beads",
                        "status": "skipped_no_auth",
                        "candidate_count": 0,
                        "selected_count": 0,
                        "message": "beads credentials are not available",
                    }
                ],
            )


def write_executable(path, contents):
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
