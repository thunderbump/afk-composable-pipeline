import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.contracts import load_project_contract  # noqa: E402
from afk.run_next import choose_candidate, github_repo_from_repo_url, run_next, selector_prompt, selector_result


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

    def test_deterministic_selector_prefers_lower_beads_priority_before_lexical_id(self):
        candidates = [
            {
                "source_id": "central-beads",
                "source_type": "beads",
                "external_id": "central-lve.10",
                "workstream": "central-lve",
                "priority": 5,
                "title": "Later lexical id",
            },
            {
                "source_id": "central-beads",
                "source_type": "beads",
                "external_id": "central-lve.9",
                "workstream": "central-lve",
                "priority": 1,
                "title": "Higher urgency",
            },
        ]

        chosen = choose_candidate(
            candidates,
            selector_mode="deterministic",
            selector_model=None,
            selector_choice_json=None,
        )

        self.assertEqual(chosen["external_id"], "central-lve.9")

    def test_selector_prompt_includes_beads_context_for_comparison(self):
        prompt = selector_prompt(
            [
                {
                    "source_id": "central-beads",
                    "source_type": "beads",
                    "external_id": "central-lve.11",
                    "title": "Rank work from Beads metadata",
                    "labels": ["project:afk-composable-pipeline", "afk:ready"],
                    "workstream": "central-lve",
                    "priority": 2,
                    "issue_type": "task",
                    "description": "Implement the selector context.\n\nUseful background for selection.",
                    "acceptance_criteria": [
                        "Carry priority into run-next",
                        "Parse acceptance criteria from description",
                    ],
                }
            ]
        )

        self.assertIn('"priority": 2', prompt)
        self.assertIn('"issue_type": "task"', prompt)
        self.assertIn("Implement the selector context.", prompt)
        self.assertIn("Carry priority into run-next", prompt)

    def test_run_next_execute_mode_runs_workstream_and_returns_summary(self):
        contract = load_project_contract("bump-eqemu", ROOT / "project-contracts", cwd=ROOT)
        selection_result = {
            "schema_version": 1,
            "source_statuses": [{"source_id": "central-beads", "source_type": "beads", "status": "selected"}],
            "selected_work": [
                {
                    "source_id": "central-beads",
                    "source_type": "beads",
                    "external_id": "central-lve.11",
                    "title": "Rank work from Beads metadata",
                    "status": "open",
                    "labels": ["project:bump-eqemu", "ready-for-agent"],
                    "workstream": "central-lve",
                    "acceptance_criteria": ["Carry priority into run-next"],
                    "priority": 2,
                    "issue_type": "task",
                    "description": "Implement the selector context.",
                    "dependencies": [],
                    "blockers": [],
                    "dependency_status": "clear",
                    "afk": {"ready": True},
                    "raw": {"beads": {"id": "central-lve.11"}},
                }
            ],
            "skipped_candidates": [],
        }
        recipe = {"schema_version": 1, "workstream_id": "central-lve.11", "steps": []}
        runner_calls: list[tuple[object, object, object]] = []

        def fake_workstream_runner(recipe_input, *, ledger_dir, project_contract):
            runner_calls.append((recipe_input, ledger_dir, project_contract))
            return {
                "run_id": "run-123",
                "workstream_id": "central-lve.11",
                "parent": "central-lve",
                "status": "succeeded",
                "result_path": "runs/run-123/workstream-result.json",
                "publication_status": "published",
            }

        original_select_work = run_next.__globals__["select_work"]
        original_generate_recipe = run_next.__globals__["generate_workstream_recipe"]
        try:
            run_next.__globals__["select_work"] = lambda request, project_contract=None: selection_result
            run_next.__globals__["generate_workstream_recipe"] = lambda **kwargs: recipe
            payload = run_next(
                project_contract=contract,
                beads_workspace=ROOT / "project-contracts",
                checkout_root=ROOT / "project-contracts",
                checkout_path=ROOT / "project-contracts",
                validation_profile="tier1",
                selector_mode="deterministic",
                selector_model=None,
                selector_choice_json=None,
                execute=True,
                ledger_dir=ROOT / "project-contracts",
                workstream_runner=fake_workstream_runner,
            )
        finally:
            run_next.__globals__["select_work"] = original_select_work
            run_next.__globals__["generate_workstream_recipe"] = original_generate_recipe

        self.assertEqual(payload["recipe"], recipe)
        self.assertEqual(payload["workstream_result"]["run_id"], "run-123")
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(runner_calls[0][0], recipe)
        self.assertEqual(runner_calls[0][1], ROOT / "project-contracts")
        self.assertEqual(runner_calls[0][2], contract)

    def test_run_next_execute_mode_does_not_run_without_candidates(self):
        contract = load_project_contract("bump-eqemu", ROOT / "project-contracts", cwd=ROOT)
        selection_result = {
            "schema_version": 1,
            "source_statuses": [{"source_id": "central-beads", "source_type": "beads", "status": "selected"}],
            "selected_work": [],
            "skipped_candidates": [],
        }
        runner_calls: list[object] = []

        def fake_workstream_runner(recipe_input, *, ledger_dir, project_contract):
            runner_calls.append(recipe_input)
            raise AssertionError("workstream runner should not be called when no candidate is selected")

        original_select_work = run_next.__globals__["select_work"]
        original_generate_recipe = run_next.__globals__["generate_workstream_recipe"]
        try:
            run_next.__globals__["select_work"] = lambda request, project_contract=None: selection_result
            run_next.__globals__["generate_workstream_recipe"] = lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("recipe generation should not run without a selected candidate")
            )
            payload = run_next(
                project_contract=contract,
                beads_workspace=ROOT / "project-contracts",
                checkout_root=ROOT / "project-contracts",
                checkout_path=ROOT / "project-contracts",
                validation_profile="tier1",
                selector_mode="deterministic",
                selector_model=None,
                selector_choice_json=None,
                execute=True,
                ledger_dir=ROOT / "project-contracts",
                workstream_runner=fake_workstream_runner,
            )
        finally:
            run_next.__globals__["select_work"] = original_select_work
            run_next.__globals__["generate_workstream_recipe"] = original_generate_recipe

        self.assertIsNone(payload["recipe"])
        self.assertIsNone(payload["workstream_result"])
        self.assertEqual(runner_calls, [])

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

    def test_model_selector_invokes_allowed_codex_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "codex",
                f"""#!{sys.executable}
import json
import sys
from pathlib import Path

args = sys.argv[1:]
assert args[:1] == ["exec"], args
assert args[args.index("--model") + 1] == "gpt-5.4-mini", args
output_path = Path(args[args.index("--output-last-message") + 1])
output_path.write_text(json.dumps({{"external_id": "thunderbump/bump-EQEmu#9", "rationale": "best fit"}}), encoding="utf-8")
""",
            )
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(fake_bin)
            try:
                candidates = [
                    {
                        "source_id": "central-beads",
                        "source_type": "beads",
                        "external_id": "central-aaa.1",
                        "workstream": "central-aaa",
                        "title": "Earlier bead",
                    },
                    {
                        "source_id": "github",
                        "source_type": "github_issues",
                        "external_id": "thunderbump/bump-EQEmu#9",
                        "workstream": "central-zzz",
                        "title": "Model choice",
                    },
                ]

                chosen = choose_candidate(
                    candidates,
                    selector_mode="model",
                    selector_model="gpt-5.4-mini",
                    selector_choice_json=None,
                )
            finally:
                os.environ["PATH"] = old_path

        self.assertEqual(chosen["external_id"], "thunderbump/bump-EQEmu#9")
        self.assertEqual(chosen["selector_rationale"], "best fit")

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

    def test_selector_rejects_unknown_mode(self):
        with self.assertRaisesRegex(ValueError, "selector mode must be deterministic or model"):
            choose_candidate(
                [],
                selector_mode="typo",
                selector_model=None,
                selector_choice_json=None,
            )

    def test_github_repo_parser_ignores_non_github_urls(self):
        self.assertEqual(
            github_repo_from_repo_url("git@github.com:thunderbump/bump-EQEmu.git"),
            "thunderbump/bump-EQEmu",
        )
        self.assertEqual(
            github_repo_from_repo_url("https://github.com/thunderbump/afk-composable-pipeline.git"),
            "thunderbump/afk-composable-pipeline",
        )
        self.assertIsNone(github_repo_from_repo_url("https://example.com/thunderbump/not-github.git"))

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
            self.assertIsNone(payload["workstream_result"])
            self.assertEqual(payload["recipe"]["workstream_id"], "central-next.1")
            self.assertEqual(payload["recipe"]["steps"][0]["input"]["target_ids"], ["central-next.1"])
            self.assertEqual(
                payload["recipe"]["steps"][0]["input"]["required_labels"],
                ["project:bump-eqemu", "ready-for-agent"],
            )
            self.assertEqual(
                [source["type"] for source in payload["recipe"]["steps"][0]["input"]["sources"]],
                ["beads", "github_issues"],
            )
