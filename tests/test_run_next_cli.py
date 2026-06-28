import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.run_next import choose_candidate, selector_result


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


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


class RunNextCliTest(unittest.TestCase):
    def test_run_next_builds_project_scoped_selection_request_and_handles_no_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            beads_workspace.mkdir()
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
import sys

if sys.argv[1:3] == ["auth", "status"]:
    sys.exit(1)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
raise SystemExit(9)
""",
            )

            completed = run_afk(
                "run-next",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--ledger",
                str(temp_path / "ledger"),
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)

            self.assertEqual(
                payload["selection_request"]["required_labels"],
                ["project:bump-eqemu", "ready-for-agent"],
            )
            self.assertEqual(
                [source["type"] for source in payload["selection_request"]["sources"]],
                ["beads", "github_issues"],
            )
            self.assertEqual(
                payload["selection_request"]["sources"][0]["labels"],
                ["project:bump-eqemu", "ready-for-agent"],
            )
            self.assertEqual(
                payload["selection_request"]["sources"][1]["labels"],
                ["project:bump-eqemu", "ready-for-agent"],
            )
            self.assertEqual(payload["selection_result"]["selected_work"], [])
            self.assertEqual(
                [status["status"] for status in payload["selection_result"]["source_statuses"]],
                ["skipped_no_auth", "skipped_no_auth"],
            )

    def test_deterministic_selector_prefers_beads_then_stable_ids(self):
        candidates = [
            {
                "source_id": "github",
                "source_type": "github_issues",
                "external_id": "thunderbump/bump-EQEmu#9",
                "workstream": "central-zzz",
                "title": "Later issue",
            },
            {
                "source_id": "central-beads",
                "source_type": "beads",
                "external_id": "central-aaa.2",
                "workstream": "central-aaa",
                "title": "Earlier bead",
            },
            {
                "source_id": "central-beads",
                "source_type": "beads",
                "external_id": "central-aaa.1",
                "workstream": "central-aaa",
                "title": "Earlier bead",
            },
        ]

        chosen = choose_candidate(
            candidates,
            selector_mode="deterministic",
            selector_model=None,
            selector_choice_json=None,
        )

        self.assertEqual(chosen["external_id"], "central-aaa.1")
        self.assertEqual(
            selector_result(chosen, selector_mode="deterministic", selector_model=None),
            {
                "mode": "deterministic",
                "model": None,
                "selected": {
                    "source_id": "central-beads",
                    "source_type": "beads",
                    "external_id": "central-aaa.1",
                    "rationale": "deterministic default",
                },
            },
        )

    def test_model_selector_rejects_disallowed_models(self):
        with self.assertRaisesRegex(
            ValueError,
            "selector model must be one of: gpt-5.3-codex-spark, gpt-5.4-mini",
        ):
            choose_candidate(
                [],
                selector_mode="model",
                selector_model="gpt-4o",
                selector_choice_json=None,
            )

    def test_model_selector_falls_back_when_choice_json_is_invalid(self):
        candidates = [
            {
                "source_id": "github",
                "source_type": "github_issues",
                "external_id": "thunderbump/bump-EQEmu#9",
                "workstream": "central-zzz",
                "title": "Later issue",
            },
            {
                "source_id": "central-beads",
                "source_type": "beads",
                "external_id": "central-aaa.1",
                "workstream": "central-aaa",
                "title": "Earlier bead",
            },
        ]

        chosen = choose_candidate(
            candidates,
            selector_mode="model",
            selector_model="gpt-5.4-mini",
            selector_choice_json="{not-json}",
        )

        self.assertEqual(chosen["external_id"], "central-aaa.1")

    def test_run_next_emits_recipe_for_selected_beads_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            secret_dir = beads_workspace / "secrets"
            secret_dir.mkdir(parents=True)
            secret_dir.joinpath("dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
import sys

if sys.argv[1:3] == ["auth", "status"]:
    sys.exit(1)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
import sys

if sys.argv[1:2] == ["list"]:
    print(json.dumps([{{"id": "central-next.1"}}]))
elif sys.argv[1:3] == ["show", "central-next.1"]:
    print(json.dumps({{
        "id": "central-next.1",
        "title": "Autonomous next item",
        "status": "open",
        "labels": ["project:bump-eqemu", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-next"}},
        "acceptance_criteria": ["ready to run"],
        "dependencies": [],
    }}))
else:
    raise SystemExit(9)
""",
            )

            completed = run_afk(
                "run-next",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)

            self.assertEqual(payload["selector"]["selected"]["external_id"], "central-next.1")
            self.assertIsNotNone(payload["recipe"])
            self.assertEqual(payload["recipe"]["workstream_id"], "central-next.1")
            self.assertEqual(payload["recipe"]["steps"][0]["input"]["target_ids"], ["central-next.1"])
            self.assertEqual(
                [source["type"] for source in payload["recipe"]["steps"][0]["input"]["sources"]],
                ["beads", "github_issues"],
            )
