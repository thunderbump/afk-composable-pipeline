import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from afk.pi_workers import PONYTAIL_EXTENSION_SOURCE, build_pi_print_command


ROOT = Path(__file__).resolve().parents[1]


def run_afk(*args, env=None):
    run_env = os.environ.copy()
    run_env["PYTHONPATH"] = str(ROOT / "src")
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "afk", *args],
        cwd=ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )


def git(cwd, *args):
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "AFK Test",
            "GIT_AUTHOR_EMAIL": "afk-test@example.test",
            "GIT_COMMITTER_NAME": "AFK Test",
            "GIT_COMMITTER_EMAIL": "afk-test@example.test",
            "GIT_ALLOW_PROTOCOL": "file",
        }
    )
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout.strip()


def init_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "--initial-branch", "main")
    git(path, "config", "user.name", "AFK Test")
    git(path, "config", "user.email", "afk-test@example.test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "seed")


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def write_contract(path, *, project_slug, repo_url):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_slug": project_slug,
                "repo_url": repo_url,
                "base_branch": "main",
                "beads_labels": [f"project:{project_slug}"],
                "validation_profiles": ["tier1"],
                "validation_profile_requests": {"tier1": {"profile": "tier1"}},
                "artifact_retention": {"ledger_days": 30, "log_days": 30},
                "pr_target": {"remote": "origin", "branch": "main"},
            }
        ),
        encoding="utf-8",
    )


class GenerateRecipeCliTest(unittest.TestCase):
    def test_generate_recipe_help_mentions_validation_stack_override(self):
        completed = run_afk("generate-recipe", "--help")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--validation-stack-path", completed.stdout)
        self.assertIn("Overrides the default host sibling contract", completed.stdout)

    def test_generate_recipe_writes_complete_single_item_workstream_recipe_from_project_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            beads_workspace.mkdir()
            checkout_root.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-afk-pr.1",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--output",
                str(output),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["command"], "generate-recipe")
            self.assertEqual(summary["workstream_id"], "central-afk-pr.1")
            self.assertEqual(summary["output_path"], str(output))

            recipe = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(recipe["schema_version"], 1)
            self.assertEqual(recipe["workstream_id"], "central-afk-pr.1")
            self.assertEqual(recipe["parent"], "central-afk-pr")
            self.assertEqual(recipe["review_branch"], "afk/central-afk-pr-1")
            self.assertEqual(
                [step["name"] for step in recipe["steps"]],
                ["select-work", "prepare-checkout", "implement", "validate", "review"],
            )

            select_input = recipe["steps"][0]["input"]
            self.assertEqual(select_input["target_ids"], ["central-afk-pr.1"])
            self.assertEqual(select_input["required_labels"], ["project:bump-eqemu"])
            self.assertEqual(select_input["allowed_statuses"], ["open", "in_progress"])
            self.assertEqual(
                select_input["sources"],
                [
                    {
                        "type": "beads",
                        "id": "central-beads",
                        "workspace": str(beads_workspace),
                        "workspace_kind": "central",
                        "labels": ["project:bump-eqemu"],
                        "status": "open",
                    }
                ],
            )

            checkout_input = recipe["steps"][1]["input"]
            self.assertEqual(checkout_input["repo_url"], "git@github.com:thunderbump/bump-EQEmu.git")
            self.assertEqual(checkout_input["base_ref"], "master")
            self.assertEqual(checkout_input["checkout_root"], str(checkout_root))
            self.assertEqual(checkout_input["checkout_path"], str(checkout_path))

            self.assertEqual(recipe["steps"][3]["profile"], "tier1")
            self.assertEqual(recipe["steps"][3]["input"]["validation"]["profile"], "tier1")
            self.assertEqual(recipe["publisher"], {"enabled": False})
            reviewer = recipe["steps"][4]["input"]["reviewer"]
            self.assertEqual(reviewer["type"], "fake-reviewer-command")
            self.assertEqual(reviewer["timeout_seconds"], 30)
            self.assertNotIn("retrospective_judge", recipe)

    def test_generate_recipe_writes_real_local_agent_and_enabled_publisher_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            gh_config_dir = temp_path / "gh-config"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            gh_config_dir.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--agent-mode",
                "real-local",
                "--agent-command-json",
                json.dumps(["codex", "exec", "implement"]),
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--agent-timeout-seconds",
                "600",
                "--publisher-mode",
                "create",
                "--publisher-repo",
                "thunderbump/beads",
                "--publisher-base",
                "main",
                "--publisher-gh-config-dir",
                str(gh_config_dir),
                "--output",
                str(output),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            recipe = json.loads(output.read_text(encoding="utf-8"))

            implement_input = recipe["steps"][2]["input"]
            self.assertEqual(implement_input["agent"]["type"], "real-agent-command")
            self.assertEqual(implement_input["agent"]["command"], ["codex", "exec", "implement"])
            self.assertEqual(implement_input["agent"]["result_path"], "agent-result.json")
            self.assertEqual(implement_input["agent"]["timeout_seconds"], 600)
            self.assertEqual(implement_input["agent"]["codex_home"], str(codex_home))
            self.assertEqual(implement_input["agent"]["config_home"], str(config_home))
            self.assertEqual(implement_input["agent"]["env"], {"PI_CONFIG_HOME": str(pi_config_home)})

            self.assertEqual(
                recipe["publisher"],
                {
                    "enabled": True,
                    "mode": "create",
                    "repo": "thunderbump/beads",
                    "base": "main",
                    "head": "afk/central-anh-6",
                    "git": {"push": True, "remote": "origin"},
                    "gh": {"auth": {"config_dir": str(gh_config_dir)}},
                },
            )

    def test_generate_recipe_writes_pi_agent_and_ponytail_extension_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            wrapper_secret = temp_path / "agent-wrapper-secret.txt"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            wrapper_secret.write_text("super-secret-token\n", encoding="utf-8")

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
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
                "gpt-5.4",
                "--agent-pi-thinking",
                "high",
                "--agent-ponytail",
                "--agent-wrapper-secret-file",
                str(wrapper_secret),
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--agent-timeout-seconds",
                "120",
                "--output",
                str(output),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            recipe = json.loads(output.read_text(encoding="utf-8"))
            implement_input = recipe["steps"][2]["input"]

            self.assertEqual(implement_input["agent"]["type"], "real-agent-command")
            self.assertEqual(
                implement_input["agent"]["command"],
                [
                    "/opt/pi/bin/pi",
                    "-p",
                    "{prompt}",
                    "--provider",
                    "openai-codex",
                    "--model",
                    "gpt-5.4",
                    "--thinking",
                    "high",
                    "--extension",
                    "git:github.com/DietrichGebert/ponytail",
                ],
            )
            self.assertEqual(implement_input["agent"]["result_path"], "agent-result.json")
            self.assertEqual(implement_input["agent"]["timeout_seconds"], 120)
            self.assertEqual(implement_input["agent"]["codex_home"], str(codex_home))
            self.assertEqual(implement_input["agent"]["config_home"], str(config_home))
            self.assertEqual(implement_input["agent"]["env"], {"PI_CONFIG_HOME": str(pi_config_home)})
            self.assertEqual(implement_input["agent"]["wrapper_secret_files"], {"primary": str(wrapper_secret)})
            recipe_text = output.read_text(encoding="utf-8")
            self.assertNotIn("super-secret-token", recipe_text)

    def test_generate_recipe_rejects_pi_agent_disallowed_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
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
                "gpt-5.5",
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--output",
                str(output),
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("Pi worker model must be gpt-5.4 or lower", completed.stderr)
            self.assertFalse(output.exists())

    def test_generate_recipe_writes_pi_reviewer_and_pi_retrospective_judge_modes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

            reviewer_mode = "pi"
            reviewer_pi_bin = "/opt/pi/bin/pi"
            reviewer_pi_model = "gpt-5.4-mini"
            reviewer_ponytail = PONYTAIL_EXTENSION_SOURCE
            reviewer_timeout = 90
            judge_pi_model = "gpt-5.4"
            judge_timeout = 130
            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
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
                reviewer_mode,
                "--reviewer-pi-bin",
                reviewer_pi_bin,
                "--reviewer-pi-provider",
                "openai-codex",
                "--reviewer-pi-model",
                reviewer_pi_model,
                "--reviewer-timeout-seconds",
                str(reviewer_timeout),
                "--reviewer-ponytail-extension-source",
                reviewer_ponytail,
                "--retrospective-judge-mode",
                "pi",
                "--retrospective-judge-pi-bin",
                reviewer_pi_bin,
                "--retrospective-judge-pi-provider",
                "openai-codex",
                "--retrospective-judge-pi-model",
                judge_pi_model,
                "--retrospective-judge-timeout-seconds",
                str(judge_timeout),
                "--retrospective-judge-ponytail-extension-source",
                reviewer_ponytail,
                "--output",
                str(output),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            recipe = json.loads(output.read_text(encoding="utf-8"))
            reviewer = recipe["steps"][4]["input"]["reviewer"]
            self.assertEqual(reviewer["type"], "fake-reviewer-command")
            self.assertEqual(
                reviewer["command"],
                build_pi_print_command(
                    pi_bin=reviewer_pi_bin,
                    provider="openai-codex",
                    model=reviewer_pi_model,
                    ponytail_extension_source=reviewer_ponytail,
                ),
            )
            self.assertEqual(reviewer["timeout_seconds"], reviewer_timeout)
            retrospective_judge = recipe["retrospective_judge"]
            self.assertEqual(retrospective_judge["enabled"], True)
            self.assertEqual(
                retrospective_judge["command"],
                build_pi_print_command(
                    pi_bin=reviewer_pi_bin,
                    provider="openai-codex",
                    model=judge_pi_model,
                    ponytail_extension_source=reviewer_ponytail,
                ),
            )
            self.assertEqual(retrospective_judge["type"], "local-command")
            self.assertEqual(retrospective_judge["timeout_seconds"], judge_timeout)

    def test_generate_recipe_rejects_disallowed_pi_reviewer_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
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
                "gpt-6.0",
                "--agent-mode",
                "fake",
                "--reviewer-pi-bin",
                "/opt/pi/bin/pi",
                "--reviewer-pi-provider",
                "openai-codex",
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--output",
                str(output),
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("Pi worker model must be gpt-5.4 or lower", completed.stderr)
            self.assertFalse(output.exists())

    def test_generate_recipe_rejects_disallowed_pi_retrospective_judge_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
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
                "gpt-6.0",
                "--output",
                str(output),
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("Pi worker model must be gpt-5.4 or lower", completed.stderr)
            self.assertFalse(output.exists())

    def test_generate_recipe_writes_project_worker_validation_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            beads_workspace.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--validation-mode",
                "project-worker",
                "--output",
                str(output),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            recipe = json.loads(output.read_text(encoding="utf-8"))

            validate_step = recipe["steps"][3]
            self.assertEqual(validate_step["profile"], "tier1")
            self.assertEqual(
                validate_step["input"]["validation"],
                {
                    "profile": "tier1",
                    "dry_run": False,
                    "timeout_seconds": 3600,
                    "worker_home": str(checkout_root / ".validation-worker" / "demo"),
                    "stack": {
                        "role": "validation",
                        "path": str(checkout_root / "bump-akk-stack-validation"),
                    },
                },
            )
            self.assertNotIn("worker", validate_step["input"])

    def test_generate_recipe_derives_project_worker_stack_from_checkout_parent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "work"
            checkout_path = checkout_root / "bump-EQEmu"
            beads_workspace.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier3-harness",
                "--validation-mode",
                "project-worker",
                "--output",
                str(output),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            recipe = json.loads(output.read_text(encoding="utf-8"))
            validation = recipe["steps"][3]["input"]["validation"]

            self.assertEqual(
                validation["stack"],
                {
                    "role": "validation",
                    "path": str(checkout_root / "bump-akk-stack-validation"),
                },
            )

    def test_generate_recipe_project_worker_accepts_explicit_validation_stack_path_for_nested_checkout_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "mounts" / "worktrees" / "bump-eqemu"
            checkout_path = checkout_root / "bump-EQEmu"
            validation_stack_path = temp_path / "mounts" / "bump-akk-stack-validation"
            beads_workspace.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "bump-eqemu",
                "--contracts-dir",
                "project-contracts",
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier3-harness",
                "--validation-mode",
                "project-worker",
                "--validation-stack-path",
                str(validation_stack_path),
                "--output",
                str(output),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            recipe = json.loads(output.read_text(encoding="utf-8"))
            validation = recipe["steps"][3]["input"]["validation"]

            self.assertEqual(
                validation["stack"],
                {
                    "role": "validation",
                    "path": str(validation_stack_path),
                },
            )

    def test_generate_recipe_rejects_project_worker_without_default_worker_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--validation-mode",
                "project-worker",
                "--output",
                str(output),
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn(
                "--validation-mode=project-worker requires a project contract with a default validation worker",
                completed.stderr,
            )
            self.assertFalse(output.exists())

    def test_generate_recipe_rejects_non_positive_validation_timeout_without_writing_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()

            for mode in ("fake", "project-worker"):
                with self.subTest(mode=mode):
                    completed = run_afk(
                        "generate-recipe",
                        "--workstream-id",
                        "central-anh.6",
                        "--project",
                        "demo",
                        "--contracts-dir",
                        str(contracts_dir),
                        "--ledger",
                        str(ledger),
                        "--beads-workspace",
                        str(beads_workspace),
                        "--checkout-root",
                        str(checkout_root),
                        "--checkout-path",
                        str(checkout_path),
                        "--validation-profile",
                        "tier1",
                        "--validation-mode",
                        mode,
                        "--validation-timeout-seconds",
                        "0",
                        "--output",
                        str(output),
                    )

                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn("--validation-timeout-seconds must be greater than 0", completed.stderr)
                    self.assertFalse(output.exists())

    def test_generate_recipe_rejects_real_local_agent_secret_command_without_writing_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-anh.6",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--agent-mode",
                "real-local",
                "--agent-command-json",
                json.dumps(["codex", "exec", "--api-key", "ghp_secret_value_1234567890"]),
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--output",
                str(output),
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("agent.command must not include credential flag --api-key", completed.stderr)
            self.assertFalse(output.exists())

    def test_generate_recipe_rejects_relative_checkout_paths_without_writing_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()

            cases = [
                ("checkout-root", "relative-root", str(temp_path / "checkouts" / "demo"), "checkout_root must be absolute"),
                ("checkout-path", str(temp_path / "checkouts"), "relative-checkout", "checkout_path must be absolute"),
            ]
            for name, checkout_root, checkout_path, expected_error in cases:
                with self.subTest(name=name):
                    completed = run_afk(
                        "generate-recipe",
                        "--workstream-id",
                        "central-afk-pr.1",
                        "--project",
                        "demo",
                        "--contracts-dir",
                        str(contracts_dir),
                        "--ledger",
                        str(ledger),
                        "--beads-workspace",
                        str(beads_workspace),
                        "--checkout-root",
                        checkout_root,
                        "--checkout-path",
                        checkout_path,
                        "--validation-profile",
                        "tier1",
                        "--output",
                        str(output),
                    )

                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn(expected_error, completed.stderr)
                    self.assertFalse(output.exists())

    def test_generate_recipe_rejects_checkout_path_outside_root_without_writing_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            outside_checkout = temp_path / "outside" / "demo"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-afk-pr.1",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(outside_checkout),
                "--validation-profile",
                "tier1",
                "--output",
                str(output),
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("checkout_path must be inside checkout_root", completed.stderr)
            self.assertFalse(output.exists())

    def test_generate_recipe_rejects_real_local_paths_inside_checkout_without_writing_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            contracts_dir.mkdir()
            init_repo(repo)
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            beads_workspace.mkdir()
            checkout_path.mkdir(parents=True)

            cases = [
                ("agent-codex-home", checkout_path / "codex-home", "agent.codex_home must be outside checkout"),
                ("agent-config-home", checkout_path / "xdg-config", "agent.config_home must be outside checkout"),
                (
                    "agent-pi-config-home",
                    checkout_path / "pi-config",
                    "agent.env.PI_CONFIG_HOME must be outside checkout",
                ),
                (
                    "publisher-gh-config-dir",
                    checkout_path / "gh-config",
                    "publisher.gh.auth.config_dir must be outside checkout",
                ),
            ]
            for option_name, invalid_path, expected_error in cases:
                with self.subTest(option=option_name):
                    (temp_path / "codex-home").mkdir(exist_ok=True)
                    (temp_path / "xdg-config").mkdir(exist_ok=True)
                    (temp_path / "pi-config").mkdir(exist_ok=True)
                    (temp_path / "gh-config").mkdir(exist_ok=True)
                    invalid_path.mkdir(parents=True, exist_ok=True)
                    completed = run_afk(
                        "generate-recipe",
                        "--workstream-id",
                        "central-anh.6",
                        "--project",
                        "demo",
                        "--contracts-dir",
                        str(contracts_dir),
                        "--ledger",
                        str(ledger),
                        "--beads-workspace",
                        str(beads_workspace),
                        "--checkout-root",
                        str(checkout_root),
                        "--checkout-path",
                        str(checkout_path),
                        "--validation-profile",
                        "tier1",
                        "--agent-mode",
                        "real-local",
                        "--agent-command-json",
                        json.dumps(["codex", "exec", "implement"]),
                        "--agent-codex-home",
                        str(invalid_path if option_name == "agent-codex-home" else temp_path / "codex-home"),
                        "--agent-config-home",
                        str(invalid_path if option_name == "agent-config-home" else temp_path / "xdg-config"),
                        "--agent-pi-config-home",
                        str(invalid_path if option_name == "agent-pi-config-home" else temp_path / "pi-config"),
                        "--publisher-mode",
                        "create",
                        "--publisher-repo",
                        "thunderbump/beads",
                        "--publisher-base",
                        "main",
                        "--publisher-gh-config-dir",
                        str(invalid_path if option_name == "publisher-gh-config-dir" else temp_path / "gh-config"),
                        "--output",
                        str(output),
                    )

                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn(expected_error, completed.stderr)
                    self.assertFalse(output.exists())

    def test_generate_recipe_rejects_credential_repo_url_without_writing_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            contracts_dir.mkdir()
            beads_workspace.mkdir()
            write_contract(
                contracts_dir / "demo.json",
                project_slug="demo",
                repo_url="https://user:secret-token@example.invalid/repo.git",
            )

            completed = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-afk-pr.1",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_root / "demo"),
                "--validation-profile",
                "tier1",
                "--output",
                str(output),
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("project contract repo_url must not contain embedded credentials or query parameters", completed.stderr)
            self.assertFalse(output.exists())

    def test_generated_recipe_runs_and_records_directly_selected_bead(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            contracts_dir = temp_path / "contracts"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            fake_bin = temp_path / "bin"
            init_repo(repo)
            contracts_dir.mkdir()
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("fixture-password\n", encoding="utf-8")
            fake_bin.mkdir()
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
import sys

if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-afk-pr.1"}}, {{"id": "central-afk-pr.2"}}]))
    sys.exit(0)

if sys.argv[1] == "show":
    issue_id = sys.argv[2]
    payloads = {{
        "central-afk-pr.1": {{
            "id": "central-afk-pr.1",
            "title": "Generate runnable workstream recipes",
            "status": "open",
            "labels": ["project:demo"],
            "parent": "central-afk-pr",
            "acceptance_criteria": ["Generated recipe runs"],
            "dependencies": [{{"id": "central-afk-pr.0", "status": "closed"}}],
            "metadata": {{"afk.ready": True}},
        }},
        "central-afk-pr.2": {{
            "id": "central-afk-pr.2",
            "title": "Different candidate",
            "status": "open",
            "labels": ["project:demo"],
            "parent": "central-afk-pr",
            "acceptance_criteria": ["Should not be selected"],
            "dependencies": [{{"id": "central-afk-pr.0", "status": "closed"}}],
            "metadata": {{"afk.ready": True}},
        }},
    }}
    print(json.dumps(payloads[issue_id]))
    sys.exit(0)

sys.exit(9)
""",
            )

            generated = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-afk-pr.1",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--output",
                str(output),
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            completed = run_afk(
                "run-workstream",
                "--input",
                output.read_text(encoding="utf-8"),
                "--ledger",
                str(ledger),
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                env={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "GIT_ALLOW_PROTOCOL": "file"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "validated-unpublished")
            self.assertEqual(summary["publication_status"], "validated-unpublished")

            workstream = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            select_result_path = workstream["steps"][0]["result_path"]
            select_result = json.loads((ledger / select_result_path).read_text(encoding="utf-8"))
            self.assertEqual(
                [item["external_id"] for item in select_result["output"]["selected_work"]],
                ["central-afk-pr.1"],
            )
            self.assertEqual(select_result["output"]["skipped_candidates"], [])

    def test_generated_recipe_runs_when_directly_selected_bead_is_in_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            contracts_dir = temp_path / "contracts"
            output = temp_path / "recipe.json"
            ledger = temp_path / "ledger"
            beads_workspace = temp_path / "central-beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "demo"
            fake_bin = temp_path / "bin"
            init_repo(repo)
            contracts_dir.mkdir()
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("fixture-password\n", encoding="utf-8")
            fake_bin.mkdir()
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
import sys

if sys.argv[1:3] == ["list", "--json"]:
    print(json.dumps([{{"id": "central-afk-pr.1"}}, {{"id": "central-afk-pr.2"}}]))
    sys.exit(0)

if sys.argv[1] == "show":
    issue_id = sys.argv[2]
    payloads = {{
        "central-afk-pr.1": {{
            "id": "central-afk-pr.1",
            "title": "Generate runnable workstream recipes",
            "status": "in_progress",
            "labels": ["project:demo"],
            "parent": "central-afk-pr",
            "acceptance_criteria": ["Generated recipe runs"],
            "dependencies": [{{"id": "central-afk-pr.0", "status": "closed"}}],
            "metadata": {{"afk.ready": True}},
        }},
        "central-afk-pr.2": {{
            "id": "central-afk-pr.2",
            "title": "Different candidate",
            "status": "open",
            "labels": ["project:demo"],
            "parent": "central-afk-pr",
            "acceptance_criteria": ["Should not be selected"],
            "dependencies": [{{"id": "central-afk-pr.0", "status": "closed"}}],
            "metadata": {{"afk.ready": True}},
        }},
    }}
    print(json.dumps(payloads[issue_id]))
    sys.exit(0)

sys.exit(9)
""",
            )

            generated = run_afk(
                "generate-recipe",
                "--workstream-id",
                "central-afk-pr.1",
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                "--ledger",
                str(ledger),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--output",
                str(output),
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)

            completed = run_afk(
                "run-workstream",
                "--input",
                output.read_text(encoding="utf-8"),
                "--ledger",
                str(ledger),
                "--project",
                "demo",
                "--contracts-dir",
                str(contracts_dir),
                env={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "GIT_ALLOW_PROTOCOL": "file"},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "validated-unpublished")

            workstream = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
            select_result = json.loads((ledger / workstream["steps"][0]["result_path"]).read_text(encoding="utf-8"))
            self.assertEqual(
                [item["external_id"] for item in select_result["output"]["selected_work"]],
                ["central-afk-pr.1"],
            )
            self.assertEqual(select_result["output"]["skipped_candidates"], [])

    def test_generated_recipe_records_actionable_beads_auth_and_unreachable_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo-src"
            contracts_dir = temp_path / "contracts"
            ledger = temp_path / "ledger"
            checkout_root = temp_path / "checkouts"
            fake_bin = temp_path / "bin"
            init_repo(repo)
            contracts_dir.mkdir()
            checkout_root.mkdir()
            write_contract(contracts_dir / "demo.json", project_slug="demo", repo_url=str(repo))
            fake_bin.mkdir()
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import sys
sys.exit(9)
""",
            )

            missing_auth_workspace = temp_path / "beads-without-secret"
            missing_auth_workspace.mkdir()
            unreachable_workspace = temp_path / "missing-beads-workspace"

            cases = [
                (
                    "missing-auth",
                    missing_auth_workspace,
                    "skipped_no_auth",
                    "beads credentials are not available",
                ),
                (
                    "unreachable",
                    unreachable_workspace,
                    "skipped_unreachable",
                    "beads workspace is not available",
                ),
            ]

            for name, beads_workspace, expected_status, expected_message in cases:
                with self.subTest(name=name):
                    output = temp_path / f"{name}-recipe.json"
                    generated = run_afk(
                        "generate-recipe",
                        "--workstream-id",
                        "central-afk-pr.1",
                        "--project",
                        "demo",
                        "--contracts-dir",
                        str(contracts_dir),
                        "--ledger",
                        str(ledger),
                        "--beads-workspace",
                        str(beads_workspace),
                        "--checkout-root",
                        str(checkout_root),
                        "--checkout-path",
                        str(checkout_root / name),
                        "--validation-profile",
                        "tier1",
                        "--output",
                        str(output),
                    )
                    self.assertEqual(generated.returncode, 0, generated.stderr)

                    completed = run_afk(
                        "run-workstream",
                        "--input",
                        output.read_text(encoding="utf-8"),
                        "--ledger",
                        str(ledger),
                        "--project",
                        "demo",
                        "--contracts-dir",
                        str(contracts_dir),
                        env={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
                    )

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    summary = json.loads(completed.stdout)
                    self.assertEqual(summary["status"], "blocked")
                    workstream = json.loads((ledger / summary["result_path"]).read_text(encoding="utf-8"))
                    self.assertEqual(workstream["publication"]["reason"], "select-work selected no work items")
                    select_result = json.loads((ledger / workstream["steps"][0]["result_path"]).read_text(encoding="utf-8"))
                    self.assertEqual(
                        select_result["output"]["source_statuses"],
                        [
                            {
                                "source_id": "central-beads",
                                "source_type": "beads",
                                "status": expected_status,
                                "candidate_count": 0,
                                "selected_count": 0,
                                "message": expected_message,
                            }
                        ],
                    )
