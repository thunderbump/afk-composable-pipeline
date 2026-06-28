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
from afk.pi_workers import PONYTAIL_EXTENSION_SOURCE, build_pi_real_worker_agent  # noqa: E402
from afk.pi_workers import build_pi_print_command
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
    def test_run_next_help_mentions_publisher_flags(self):
        completed = run_afk("run-next", "--help")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--publisher-mode", completed.stdout)
        self.assertIn("--publisher-repo", completed.stdout)
        self.assertIn("--publisher-base", completed.stdout)
        self.assertIn("--publisher-gh-config-dir", completed.stdout)

    def test_run_next_requires_explicit_beads_workspace_flag(self):
        completed = run_afk(
            "run-next",
            "--project",
            "bump-eqemu",
            "--contracts-dir",
            "project-contracts",
            "--checkout-root",
            "/tmp/checkouts",
            "--checkout-path",
            "/tmp/checkouts/bump-EQEmu",
            "--validation-profile",
            "tier1",
            "--ledger",
            "/tmp/ledger",
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("the following arguments are required: --beads-workspace", completed.stderr)

    def test_run_next_rejects_missing_beads_workspace_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            missing_workspace = temp_path / "missing-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"

            completed = run_afk(
                "run-next",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--beads-workspace",
                str(missing_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--ledger",
                str(temp_path / "ledger"),
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("beads workspace is not available", completed.stderr)

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

    def test_run_next_targets_afk_composable_pipeline_with_first_party_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "afk-composable-pipeline"
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
                "afk-composable-pipeline",
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

            self.assertEqual(payload["project"], "afk-composable-pipeline")
            self.assertEqual(
                payload["selection_request"]["required_labels"],
                ["project:afk-composable-pipeline", "ready-for-agent"],
            )
            self.assertEqual(
                payload["selection_request"]["sources"][0]["labels"],
                ["project:afk-composable-pipeline", "ready-for-agent"],
            )
            self.assertEqual(
                payload["selection_request"]["sources"][1]["repo"],
                "thunderbump/afk-composable-pipeline",
            )
            self.assertEqual(payload["selection_result"]["selected_work"], [])

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
            with tempfile.TemporaryDirectory() as temp_dir:
                beads_workspace = Path(temp_dir) / "beads"
                beads_workspace.mkdir()
                run_next.__globals__["select_work"] = lambda request, project_contract=None: selection_result
                run_next.__globals__["generate_workstream_recipe"] = lambda **kwargs: recipe
                payload = run_next(
                    project_contract=contract,
                    beads_workspace=beads_workspace,
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

    def test_run_next_execute_mode_with_pi_agent_passes_redacted_payload_to_runner(self):
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
        agent_secret = "agent-secret-value"
        runner_calls: list[tuple[object, object, object]] = []

        def fake_workstream_runner(recipe_input, *, ledger_dir, project_contract):
            runner_calls.append((recipe_input, ledger_dir, project_contract))
            return {
                "run_id": "run-456",
                "workstream_id": "central-lve.11",
                "parent": "central-lve",
                "status": "succeeded",
                "result_path": "runs/run-456/workstream-result.json",
                "publication_status": "published",
            }

        original_select_work = run_next.__globals__["select_work"]
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                beads_workspace = temp_path / "beads"
                checkout_root = temp_path / "checkouts"
                checkout_path = checkout_root / "bump-EQEmu"
                ledger_dir = temp_path / "ledger"
                codex_home = temp_path / "codex-home"
                config_home = temp_path / "xdg-config"
                pi_config_home = temp_path / "pi-config"
                pi_coding_agent_dir = temp_path / "pi-coding-agent"
                wrapper_secret = temp_path / "agent-wrapper-secret.txt"
                beads_workspace.mkdir()
                checkout_root.mkdir(parents=True)
                checkout_path.mkdir(parents=True)
                ledger_dir.mkdir()
                codex_home.mkdir()
                config_home.mkdir()
                pi_config_home.mkdir()
                pi_coding_agent_dir.mkdir()
                wrapper_secret.write_text(agent_secret + "\n", encoding="utf-8")

                pi_agent = build_pi_real_worker_agent(
                    pi_bin="/opt/pi/bin/pi",
                    provider="openai-codex",
                    model="gpt-5.4-mini",
                    codex_home=str(codex_home),
                    config_home=str(config_home),
                    pi_config_home=str(pi_config_home),
                    pi_coding_agent_dir=str(pi_coding_agent_dir),
                    checkout_path=checkout_path,
                    ponytail_extension_source=PONYTAIL_EXTENSION_SOURCE,
                    wrapper_secret_file=str(wrapper_secret),
                )

                run_next.__globals__["select_work"] = lambda request, project_contract=None: selection_result
                payload = run_next(
                    project_contract=contract,
                    beads_workspace=beads_workspace,
                    checkout_root=checkout_root,
                    checkout_path=checkout_path,
                    validation_profile="tier1",
                    selector_mode="deterministic",
                    selector_model=None,
                    selector_choice_json=None,
                    agent=pi_agent,
                    execute=True,
                    ledger_dir=ledger_dir,
                    workstream_runner=fake_workstream_runner,
                )
        finally:
            run_next.__globals__["select_work"] = original_select_work

        self.assertEqual(len(runner_calls), 1)
        runner_payload, runner_ledger_dir, runner_contract = runner_calls[0]
        implement_agent = next(step["input"]["agent"] for step in runner_payload["steps"] if step["name"] == "implement")

        self.assertEqual(runner_ledger_dir, ledger_dir)
        self.assertEqual(runner_contract, contract)
        self.assertEqual(implement_agent["type"], "real-agent-command")
        self.assertEqual(
            implement_agent["command"],
            [
                "/opt/pi/bin/pi",
                "-p",
                "{prompt}",
                "--provider",
                "openai-codex",
                "--model",
                "gpt-5.4-mini",
                "--extension",
                PONYTAIL_EXTENSION_SOURCE,
            ],
        )
        self.assertEqual(implement_agent["wrapper_secret_files"], {"primary": str(wrapper_secret)})
        self.assertNotIn(agent_secret, json.dumps(runner_payload))
        self.assertNotIn(agent_secret, json.dumps(payload["recipe"]))
        self.assertNotIn(agent_secret, json.dumps(payload["workstream_result"]))

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
            with tempfile.TemporaryDirectory() as temp_dir:
                beads_workspace = Path(temp_dir) / "beads"
                beads_workspace.mkdir()
                run_next.__globals__["select_work"] = lambda request, project_contract=None: selection_result
                run_next.__globals__["generate_workstream_recipe"] = lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("recipe generation should not run without a selected candidate")
                )
                payload = run_next(
                    project_contract=contract,
                    beads_workspace=beads_workspace,
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
            self.assertEqual(payload["recipe"]["publisher"], {"enabled": False})
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

    def test_run_next_emits_pi_recipe_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            beads_workspace.mkdir()
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            secret_dir = beads_workspace / "secrets"
            secret_dir.mkdir(parents=True)
            secret_dir.joinpath("dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            wrapper_secret = temp_path / "agent-wrapper-secret.txt"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            wrapper_secret.write_text("agent-secret\n", encoding="utf-8")
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
                "--agent-mode",
                "pi",
                "--agent-pi-bin",
                "/opt/pi/bin/pi",
                "--agent-pi-provider",
                "openai-codex",
                "--agent-pi-model",
                "gpt-5.4-mini",
                "--agent-ponytail",
                "--agent-wrapper-secret-file",
                str(wrapper_secret),
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--agent-pi-coding-agent-dir",
                str(pi_coding_agent_dir),
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)

            implement = payload["recipe"]["steps"][2]["input"]["agent"]
            self.assertEqual(implement["type"], "real-agent-command")
            self.assertEqual(
                implement["command"],
                [
                    "/opt/pi/bin/pi",
                    "-p",
                    "{prompt}",
                    "--provider",
                    "openai-codex",
                    "--model",
                    "gpt-5.4-mini",
                    "--extension",
                    "git:github.com/DietrichGebert/ponytail",
                ],
            )
            self.assertEqual(implement["wrapper_secret_files"], {"primary": str(wrapper_secret)})
            self.assertEqual(
                implement["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )
            self.assertNotIn("agent-secret", json.dumps(payload["recipe"]))

    def test_run_next_rejects_disallowed_pi_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
import sys
sys.exit(1)
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import sys
sys.exit(0)
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
                "--agent-mode",
                "pi",
                "--agent-pi-model",
                "gpt-5.9",
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("Pi worker model must be gpt-5.4 or lower", completed.stderr)

    def test_run_next_generates_pi_reviewer_and_pi_retrospective_judge_in_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            checkout_root.mkdir()
            checkout_path.mkdir()
            beads_workspace.mkdir()
            fake_bin.mkdir()
            secret_dir = beads_workspace / "secrets"
            secret_dir.mkdir(parents=True)
            secret_dir.joinpath("dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")

            write_executable(
                fake_bin / "gh",
                "#!%s\nraise SystemExit(1)\n" % sys.executable,
            )
            write_executable(
                fake_bin / "bd",
                "#!%s\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1:2] == ['list']:\n"
                "    print(json.dumps([{'id': 'central-lve.11'}]))\n"
                "elif sys.argv[1:2] == ['show']:\n"
                "    print(json.dumps({\n"
                "        'id': 'central-lve.11',\n"
                "        'title': 'Generated pi modes',\n"
                "        'status': 'open',\n"
                "        'labels': ['project:bump-eqemu', 'ready-for-agent'],\n"
                "        'metadata': {'workstream': 'central-lve', 'afk.ready': True},\n"
                "        'acceptance_criteria': ['generated by test'],\n"
                "    }))\n"
                "else:\n"
                "    raise SystemExit(9)\n" % sys.executable,
            )

            reviewer_model = "gpt-5.4"
            judge_model = "gpt-5.4-mini"
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
                "--agent-mode",
                "fake",
                "--reviewer-mode",
                "pi",
                "--reviewer-pi-bin",
                "/opt/pi/bin/pi",
                "--reviewer-pi-provider",
                "openai-codex",
                "--reviewer-pi-model",
                reviewer_model,
                "--reviewer-timeout-seconds",
                "123",
                "--reviewer-ponytail",
                "--retrospective-judge-mode",
                "pi",
                "--retrospective-judge-pi-bin",
                "/opt/pi/bin/pi",
                "--retrospective-judge-pi-provider",
                "openai-codex",
                "--retrospective-judge-pi-model",
                judge_model,
                "--retrospective-judge-timeout-seconds",
                "321",
                "--retrospective-judge-ponytail",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(len(payload["selection_result"]["selected_work"]), 1)
            review_input = payload["recipe"]["steps"][4]["input"]["reviewer"]
            self.assertEqual(
                review_input["command"],
                build_pi_print_command(
                    pi_bin="/opt/pi/bin/pi",
                    provider="openai-codex",
                    model=reviewer_model,
                    ponytail_extension_source=PONYTAIL_EXTENSION_SOURCE,
                ),
            )
            self.assertEqual(review_input["timeout_seconds"], 123)

            retrospective_judge = payload["recipe"]["retrospective_judge"]
            self.assertEqual(
                retrospective_judge["command"],
                build_pi_print_command(
                    pi_bin="/opt/pi/bin/pi",
                    provider="openai-codex",
                    model=judge_model,
                    ponytail_extension_source=PONYTAIL_EXTENSION_SOURCE,
                ),
            )
            self.assertEqual(retrospective_judge["timeout_seconds"], 321)
            self.assertEqual(retrospective_judge["type"], "local-command")

    def test_run_next_rejects_disallowed_pi_reviewer_model(self):
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
raise SystemExit(1)
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
print(json.dumps({{"schema_version":1,"source_statuses":[],"selected_work":[],"skipped_candidates":[]}}))
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
                "--reviewer-mode",
                "pi",
                "--reviewer-pi-model",
                "gpt-5.9",
                "--reviewer-pi-bin",
                "/opt/pi/bin/pi",
                "--agent-mode",
                "fake",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("Pi worker model must be gpt-5.4 or lower", completed.stderr)

    def test_run_next_rejects_disallowed_pi_retrospective_judge_model(self):
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
raise SystemExit(1)
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
print(json.dumps({{"schema_version":1,"source_statuses":[],"selected_work":[],"skipped_candidates":[]}}))
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
                "--retrospective-judge-mode",
                "pi",
                "--retrospective-judge-pi-model",
                "gpt-5.9",
                "--retrospective-judge-pi-bin",
                "/opt/pi/bin/pi",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("Pi worker model must be gpt-5.4 or lower", completed.stderr)

    def test_run_next_generates_pi_workers_and_publisher_create_in_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            checkout_root.mkdir()
            checkout_path.mkdir()
            beads_workspace.mkdir()
            secret_dir = beads_workspace / "secrets"
            secret_dir.mkdir(parents=True)
            secret_dir.joinpath("dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")
            fake_bin.mkdir()
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            wrapper_secret = temp_path / "agent-wrapper-secret.txt"
            gh_config_dir = temp_path / "gh-config"
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            gh_config_dir.mkdir()
            wrapper_secret.write_text("agent-secret\n", encoding="utf-8")
            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
raise SystemExit(1)
""",
            )
            write_executable(
                fake_bin / "bd",
                "#!%s\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1:2] == ['list']:\n"
                "    print(json.dumps([{'id': 'central-lve.11'}]))\n"
                "elif sys.argv[1:2] == ['show']:\n"
                "    print(json.dumps({'id': 'central-lve.11', 'title': 'Generated pi and publisher', 'status': 'open', 'labels': ['project:bump-eqemu', 'ready-for-agent'], 'metadata': {'workstream': 'central-lve', 'afk.ready': True}, 'acceptance_criteria': ['generated by test'], 'dependencies': []}))\n"
                "else:\n"
                "    raise SystemExit(9)\n" % sys.executable,
            )

            reviewer_model = "gpt-5.4"
            judge_model = "gpt-5.4"
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
                "--agent-mode",
                "pi",
                "--agent-pi-bin",
                "/opt/pi/bin/pi",
                "--agent-pi-provider",
                "openai-codex",
                "--agent-pi-model",
                "gpt-5.4-mini",
                "--agent-ponytail",
                "--agent-wrapper-secret-file",
                str(wrapper_secret),
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--agent-pi-coding-agent-dir",
                str(pi_coding_agent_dir),
                "--reviewer-mode",
                "pi",
                "--reviewer-pi-bin",
                "/opt/pi/bin/pi",
                "--reviewer-pi-provider",
                "openai-codex",
                "--reviewer-pi-model",
                reviewer_model,
                "--reviewer-ponytail",
                "--reviewer-timeout-seconds",
                "111",
                "--retrospective-judge-mode",
                "pi",
                "--retrospective-judge-pi-bin",
                "/opt/pi/bin/pi",
                "--retrospective-judge-pi-provider",
                "openai-codex",
                "--retrospective-judge-pi-model",
                judge_model,
                "--retrospective-judge-ponytail",
                "--retrospective-judge-timeout-seconds",
                "222",
                "--publisher-mode",
                "create",
                "--publisher-repo",
                "thunderbump/beads",
                "--publisher-base",
                "main",
                "--publisher-gh-config-dir",
                str(gh_config_dir),
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            publisher = payload["recipe"]["publisher"]
            self.assertEqual(
                publisher,
                {
                    "enabled": True,
                    "mode": "create",
                    "repo": "thunderbump/beads",
                    "base": "main",
                    "head": "afk/central-lve-11",
                    "git": {"push": True, "remote": "origin"},
                    "gh": {"auth": {"config_dir": str(gh_config_dir)}},
                },
            )

            implement = payload["recipe"]["steps"][2]["input"]["agent"]
            self.assertEqual(
                implement["command"],
                [
                    "/opt/pi/bin/pi",
                    "-p",
                    "{prompt}",
                    "--provider",
                    "openai-codex",
                    "--model",
                    "gpt-5.4-mini",
                    "--extension",
                    PONYTAIL_EXTENSION_SOURCE,
                ],
            )
            self.assertEqual(implement["wrapper_secret_files"], {"primary": str(wrapper_secret)})
            self.assertEqual(
                implement["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )

            review_input = payload["recipe"]["steps"][4]["input"]["reviewer"]
            self.assertEqual(
                review_input["command"],
                build_pi_print_command(
                    pi_bin="/opt/pi/bin/pi",
                    provider="openai-codex",
                    model=reviewer_model,
                    ponytail_extension_source=PONYTAIL_EXTENSION_SOURCE,
                ),
            )
            self.assertEqual(review_input["timeout_seconds"], 111)

            retrospective_judge = payload["recipe"]["retrospective_judge"]
            self.assertEqual(
                retrospective_judge["command"],
                build_pi_print_command(
                    pi_bin="/opt/pi/bin/pi",
                    provider="openai-codex",
                    model=judge_model,
                    ponytail_extension_source=PONYTAIL_EXTENSION_SOURCE,
                ),
            )
            self.assertEqual(retrospective_judge["timeout_seconds"], 222)
            self.assertEqual(retrospective_judge["type"], "local-command")

    def test_run_next_rejects_publisher_create_without_required_arguments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            gh_config_dir = temp_path / "gh-config"
            gh_config_dir.mkdir(parents=True)
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
import sys

if sys.argv[1:3] == ['auth', 'status']:
    sys.exit(1)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bin / "bd",
                "#!%s\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1:2] == ['list']:\n"
                "    print(json.dumps([{'id':'central-next.1'}]))\n"
                "elif sys.argv[1:3] == ['show', 'central-next.1']:\n"
                "    print(json.dumps({'id':'central-next.1','title':'Autonomous next item','status':'open','labels':['project:bump-eqemu','ready-for-agent'],'metadata':{'afk.ready':True,'workstream':'central-next'},'acceptance_criteria':['ready to run'],'dependencies':[]}))\n"
                "else:\n"
                "    raise SystemExit(9)\n" % sys.executable,
            )

            cases = [
                (
                    [
                        "--publisher-mode",
                        "create",
                        "--publisher-repo",
                        "thunderbump/beads",
                        "--publisher-gh-config-dir",
                        str(gh_config_dir),
                    ],
                    "publisher.base is required for create",
                ),
                (
                    [
                        "--publisher-mode",
                        "create",
                        "--publisher-base",
                        "main",
                        "--publisher-gh-config-dir",
                        str(gh_config_dir),
                    ],
                    "publisher.repo is required",
                ),
                (
                    [
                        "--publisher-mode",
                        "create",
                        "--publisher-repo",
                        "thunderbump/beads",
                        "--publisher-base",
                        "main",
                    ],
                    "publisher.gh.auth.config_dir is required",
                ),
            ]

            for case_index, (extra_args, expected_error) in enumerate(cases):
                with self.subTest(case=case_index):
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
                        *extra_args,
                        env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
                    )

                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn(expected_error, completed.stderr)

    def test_run_next_rejects_publisher_create_without_required_arguments_before_selection_when_no_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            gh_config_dir = temp_path / "gh-config"
            fake_bin.mkdir()
            checkout_root.mkdir()
            beads_workspace.mkdir()

            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
import sys
if sys.argv[1:3] == ['auth', 'status']:
    sys.exit(1)
raise SystemExit(9)
""",
            )
            write_executable(
                fake_bin / "bd",
                "#!%s\n"
                "import json\n"
                "import sys\n"
                "if sys.argv[1:2] == ['list']:\n"
                "    print(json.dumps({'schema_version':1,'source_statuses':[],'selected_work':[],'skipped_candidates':[]}))\n"
                "    sys.exit(0)\n"
                "raise SystemExit(9)\n" % sys.executable,
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
                "--publisher-mode",
                "create",
                "--publisher-repo",
                "thunderbump/beads",
                "--publisher-gh-config-dir",
                str(gh_config_dir),
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("publisher.base is required for create", completed.stderr)
            self.assertEqual(completed.stdout, "")
