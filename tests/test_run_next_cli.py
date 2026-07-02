import json
import os
import subprocess
import sys
import tempfile
import textwrap
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


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "--initial-branch", "main")
    git(path, "config", "user.name", "AFK Test")
    git(path, "config", "user.email", "afk-test@example.test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "seed")


def write_contract(path: Path, *, project_slug: str, repo_url: str) -> None:
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
                "--role-profile",
                "fake-local",
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
                "--role-profile",
                "fake-local",
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
                "--role-profile",
                "fake-local",
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
                payload["selection_request"]["required_metadata"],
                ["afk.ready"],
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

    def test_run_next_production_preview_preserves_no_candidate_selection_output_without_pi_auth_mounts(self):
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
            self.assertEqual(payload["selector"]["selected"], None)
            self.assertEqual(payload["selector"]["rationale"], "no candidates")
            self.assertEqual(payload["selection_result"]["selected_work"], [])
            self.assertEqual(
                [status["status"] for status in payload["selection_result"]["source_statuses"]],
                ["skipped_no_auth", "skipped_no_auth"],
            )
            self.assertIsNone(payload["recipe"])

    def test_run_next_targets_afk_composable_pipeline_with_first_party_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "afk-composable-pipeline"
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
    print(json.dumps([{{"id": "central-lhx"}}]))
elif sys.argv[1:3] == ["show", "central-lhx"]:
    print(json.dumps({{
        "id": "central-lhx",
        "title": "Add afk-composable-pipeline project contract for self-dogfood",
        "status": "open",
        "labels": ["project:afk-composable-pipeline", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-lhx"}},
        "acceptance_criteria": ["run-next can target this repo"],
        "dependencies": [],
    }}))
else:
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
                "--role-profile",
                "fake-local",
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
            self.assertEqual(payload["selector"]["selected"]["external_id"], "central-lhx")
            self.assertEqual(payload["selection_result"]["selected_work"][0]["external_id"], "central-lhx")
            self.assertIsNone(payload["workstream_result"])
            recipe = payload["recipe"]
            self.assertIsNotNone(recipe)
            self.assertEqual(recipe["workstream_id"], "central-lhx")
            prepare_checkout = next(step for step in recipe["steps"] if step["name"] == "prepare-checkout")
            self.assertEqual(
                prepare_checkout["input"],
                {
                    "repo_url": "git@github.com:thunderbump/afk-composable-pipeline.git",
                    "base_ref": "main",
                    "checkout_root": str(checkout_root),
                    "checkout_path": str(checkout_path),
                },
            )

    def test_run_next_preview_can_enable_beads_retrospective_follow_up_recipe_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            contracts_dir.mkdir()
            repo = temp_path / "repo-src"
            init_repo(repo)
            write_contract(
                contracts_dir / "dogfood.json",
                project_slug="dogfood",
                repo_url=repo.as_uri(),
            )
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "dogfood"
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")
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
    print(json.dumps([{{"id": "central-df.1"}}]))
elif sys.argv[1:3] == ["show", "central-df.1"]:
    print(json.dumps({{
        "id": "central-df.1",
        "title": "Add retrospective follow-up wiring",
        "status": "open",
        "labels": ["project:dogfood", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-df.1"}},
        "acceptance_criteria": ["run-next can emit retrospective follow-up config"],
        "dependencies": [],
    }}))
else:
    raise SystemExit(9)
""",
            )

            completed = run_afk(
                "run-next",
                "--project",
                "dogfood",
                "--contracts-dir",
                str(contracts_dir),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--role-profile",
                "fake-local",
                "--retrospective-follow-up-mode",
                "beads",
                "--retrospective-follow-up-label",
                "area:retrospective",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            recipe = payload["recipe"]

            self.assertEqual(
                recipe["retrospective_follow_up"],
                {
                    "enabled": True,
                    "creator": "beads",
                    "beads_workspace": str(beads_workspace),
                    "labels": ["project:dogfood", "ready-for-agent", "area:retrospective"],
                },
            )
            self.assertNotIn("beads-secret", completed.stdout)

    def test_run_next_rejects_project_local_beads_workspace_before_github_fallback_recipe_emission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            contracts_dir.mkdir()
            repo = temp_path / "repo-src"
            init_repo(repo)
            project_local_beads = repo / ".beads"
            project_local_beads.mkdir()
            write_contract(
                contracts_dir / "dogfood.json",
                project_slug="dogfood",
                repo_url="https://github.com/example/dogfood",
            )
            fake_bin = temp_path / "bin"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "dogfood"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gh",
                f"""#!{sys.executable}
raise SystemExit("gh should not be called when project-local .beads is rejected early")
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
raise SystemExit("bd should not be called when project-local .beads is rejected early")
""",
            )

            completed = run_afk(
                "run-next",
                "--project",
                "dogfood",
                "--contracts-dir",
                str(contracts_dir),
                "--beads-workspace",
                str(project_local_beads),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--role-profile",
                "fake-local",
                "--retrospective-follow-up-mode",
                "beads",
                env={"GH_TOKEN": "fixture-token", "PATH": str(fake_bin)},
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("project-local .beads workspace is not allowed", completed.stderr)

    def test_run_next_preview_leaves_retrospective_follow_up_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            contracts_dir.mkdir()
            repo = temp_path / "repo-src"
            init_repo(repo)
            write_contract(
                contracts_dir / "dogfood.json",
                project_slug="dogfood",
                repo_url=repo.as_uri(),
            )
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "dogfood"
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")
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
    print(json.dumps([{{"id": "central-df.2"}}]))
elif sys.argv[1:3] == ["show", "central-df.2"]:
    print(json.dumps({{
        "id": "central-df.2",
        "title": "Leave follow-up disabled without explicit opt-in",
        "status": "open",
        "labels": ["project:dogfood", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-df.2"}},
        "acceptance_criteria": ["safe preview stays recommendation-only"],
        "dependencies": [],
    }}))
else:
    raise SystemExit(9)
""",
            )

            completed = run_afk(
                "run-next",
                "--project",
                "dogfood",
                "--contracts-dir",
                str(contracts_dir),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--role-profile",
                "fake-local",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertNotIn("retrospective_follow_up", payload["recipe"])

    def test_run_next_execute_can_create_beads_retrospective_follow_up_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            contracts_dir.mkdir()
            repo = temp_path / "repo-src"
            init_repo(repo)
            write_contract(
                contracts_dir / "dogfood.json",
                project_slug="dogfood",
                repo_url=repo.as_uri(),
            )
            fake_calls = temp_path / "fake-calls.jsonl"
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "dogfood"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            for path in (fake_bin, codex_home, config_home, pi_config_home, pi_coding_agent_dir):
                path.mkdir(parents=True, exist_ok=True)
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
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
                fake_bin / "pi",
                f"""#!{sys.executable}
import sys
print("pi auth failed", file=sys.stderr)
raise SystemExit(7)
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "password": os.environ.get("BEADS_DOLT_PASSWORD", ""),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:2] == ["list"]:
    print(json.dumps([{{"id": "central-df.3"}}]))
    raise SystemExit(0)
if sys.argv[1:3] == ["show", "central-df.3"]:
    print(json.dumps({{
        "id": "central-df.3",
        "title": "Create auth-preflight follow-up bead",
        "status": "open",
        "labels": ["project:dogfood", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-df.3"}},
        "acceptance_criteria": ["run-next execute creates follow-up bead"],
        "dependencies": [],
    }}))
    raise SystemExit(0)
if sys.argv[1] == "create":
    print("central-new")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )

            completed = run_afk(
                "run-next",
                "--project",
                "dogfood",
                "--contracts-dir",
                str(contracts_dir),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--execute",
                "--ledger",
                str(temp_path / "ledger"),
                "--retrospective-follow-up-mode",
                "beads",
                "--retrospective-follow-up-label",
                "area:retrospective",
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--agent-pi-coding-agent-dir",
                str(pi_coding_agent_dir),
                env={
                    "GH_TOKEN": None,
                    "GITHUB_TOKEN": None,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "GIT_ALLOW_PROTOCOL": "file",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            workstream_result = payload["workstream_result"]
            self.assertIsNotNone(workstream_result)

            result_path = ROOT / (temp_path / "ledger") / workstream_result["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            creation = result["pipeline_retrospective"]["follow_up"]["creation"]
            created = result["pipeline_retrospective"]["follow_up"]["created"]
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            create_call = next(call for call in calls if call["argv"][0] == "create")
            metadata = json.loads(create_call["argv"][create_call["argv"].index("--metadata") + 1])
            description = create_call["argv"][create_call["argv"].index("--description") + 1]

            self.assertEqual(creation["status"], "created")
            self.assertEqual(created[0]["id"], "central-new")
            self.assertIn("afk.retrospective_follow_up.fingerprint", metadata)
            self.assertIn("pi-auth-preflight.json", description)
            self.assertEqual(create_call["password"], "test-password")
            self.assertNotIn("test-password", completed.stdout)

    def test_run_next_execute_resolves_relative_beads_workspace_for_retrospective_follow_up(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            contracts_dir.mkdir()
            repo = temp_path / "repo-src"
            init_repo(repo)
            write_contract(
                contracts_dir / "dogfood.json",
                project_slug="dogfood",
                repo_url=repo.as_uri(),
            )
            fake_calls = temp_path / "fake-calls.jsonl"
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            relative_beads_workspace = os.path.relpath(beads_workspace, ROOT)
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "dogfood"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            for path in (fake_bin, codex_home, config_home, pi_config_home, pi_coding_agent_dir):
                path.mkdir(parents=True, exist_ok=True)
            (beads_workspace / "secrets").mkdir(parents=True)
            (beads_workspace / "secrets" / "dolt_beads_password.txt").write_text("test-password\n", encoding="utf-8")
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
                fake_bin / "pi",
                f"""#!{sys.executable}
import sys
print("pi auth failed", file=sys.stderr)
raise SystemExit(7)
""",
            )
            write_executable(
                fake_bin / "bd",
                f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

record = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "password": os.environ.get("BEADS_DOLT_PASSWORD", ""),
}}
Path({str(fake_calls)!r}).open("a", encoding="utf-8").write(json.dumps(record) + "\\n")
if sys.argv[1:2] == ["list"]:
    print(json.dumps([{{"id": "central-df.4"}}]))
    raise SystemExit(0)
if sys.argv[1:3] == ["show", "central-df.4"]:
    print(json.dumps({{
        "id": "central-df.4",
        "title": "Resolve relative follow-up workspace",
        "status": "open",
        "labels": ["project:dogfood", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-df.4"}},
        "acceptance_criteria": ["run-next resolves retrospective follow-up workspace"],
        "dependencies": [],
    }}))
    raise SystemExit(0)
if sys.argv[1] == "create":
    print("central-new")
    raise SystemExit(0)
raise SystemExit(9)
""",
            )

            completed = run_afk(
                "run-next",
                "--project",
                "dogfood",
                "--contracts-dir",
                str(contracts_dir),
                "--beads-workspace",
                relative_beads_workspace,
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--execute",
                "--ledger",
                str(temp_path / "ledger"),
                "--retrospective-follow-up-mode",
                "beads",
                "--retrospective-follow-up-label",
                "area:retrospective",
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--agent-pi-coding-agent-dir",
                str(pi_coding_agent_dir),
                env={
                    "GH_TOKEN": None,
                    "GITHUB_TOKEN": None,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "GIT_ALLOW_PROTOCOL": "file",
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            recipe = payload["recipe"]
            self.assertEqual(recipe["retrospective_follow_up"]["beads_workspace"], str(beads_workspace.resolve()))

            workstream_result = payload["workstream_result"]
            self.assertIsNotNone(workstream_result)

            result_path = ROOT / (temp_path / "ledger") / workstream_result["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            creation = result["pipeline_retrospective"]["follow_up"]["creation"]
            calls = [json.loads(line) for line in fake_calls.read_text(encoding="utf-8").splitlines()]
            create_call = next(call for call in calls if call["argv"][0] == "create")

            self.assertEqual(creation["status"], "created")
            self.assertEqual(creation["creator"]["workspace"], str(beads_workspace.resolve()))
            self.assertEqual(create_call["cwd"], str(beads_workspace.resolve()))

    def test_run_next_defaults_to_production_pi_roles_when_mounts_are_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            checkout_root.mkdir()
            checkout_path.mkdir()
            beads_workspace.mkdir()
            fake_bin.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
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
                "        'title': 'Generated pi defaults',\n"
                "        'status': 'open',\n"
                "        'labels': ['project:bump-eqemu', 'ready-for-agent'],\n"
                "        'metadata': {'workstream': 'central-lve', 'afk.ready': True},\n"
                "        'acceptance_criteria': ['generated by test'],\n"
                "    }))\n"
                "else:\n"
                "    raise SystemExit(9)\n" % sys.executable,
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

            implement_agent = payload["recipe"]["steps"][2]["input"]["agent"]
            self.assertEqual(implement_agent["type"], "real-agent-command")
            self.assertEqual(
                implement_agent["command"],
                ["pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4"],
            )
            self.assertEqual(
                implement_agent["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )
            self.assertEqual(implement_agent["timeout_seconds"], 3600)

            reviewer = payload["recipe"]["steps"][4]["input"]["reviewer"]
            self.assertEqual(
                reviewer["command"],
                build_pi_print_command(
                    pi_bin="pi",
                    provider="openai-codex",
                    model="gpt-5.4",
                ),
            )
            self.assertEqual(reviewer["timeout_seconds"], 300)

            retrospective_judge = payload["recipe"]["retrospective_judge"]
            self.assertEqual(
                retrospective_judge["command"],
                build_pi_print_command(
                    pi_bin="pi",
                    provider="openai-codex",
                    model="gpt-5.4",
                ),
            )
            self.assertEqual(retrospective_judge["timeout_seconds"], 120)
            self.assertEqual(payload["recipe"]["review_feedback"], {"enabled": True})

    def test_run_next_preview_preserves_production_recipe_without_pi_auth_mounts(self):
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
                "        'title': 'Generated pi defaults without mounts',\n"
                "        'status': 'open',\n"
                "        'labels': ['project:bump-eqemu', 'ready-for-agent'],\n"
                "        'metadata': {'workstream': 'central-lve', 'afk.ready': True},\n"
                "        'acceptance_criteria': ['generated by test'],\n"
                "    }))\n"
                "else:\n"
                "    raise SystemExit(9)\n" % sys.executable,
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

            self.assertEqual(payload["selector"]["selected"]["external_id"], "central-lve.11")
            self.assertIsNone(payload["workstream_result"])
            self.assertEqual(payload["selection_result"]["selected_work"][0]["external_id"], "central-lve.11")

            implement_agent = payload["recipe"]["steps"][2]["input"]["agent"]
            self.assertEqual(implement_agent["type"], "real-agent-command")
            self.assertEqual(
                implement_agent["command"],
                ["pi", "-p", "{prompt}", "--provider", "openai-codex", "--model", "gpt-5.4"],
            )
            self.assertNotIn("codex_home", implement_agent)
            self.assertNotIn("config_home", implement_agent)
            self.assertNotIn("env", implement_agent)

            reviewer = payload["recipe"]["steps"][4]["input"]["reviewer"]
            self.assertEqual(
                reviewer["command"],
                build_pi_print_command(
                    pi_bin="pi",
                    provider="openai-codex",
                    model="gpt-5.4",
                ),
            )
            self.assertNotIn("codex_home", reviewer)
            self.assertNotIn("config_home", reviewer)
            self.assertNotIn("env", reviewer)

            retrospective_judge = payload["recipe"]["retrospective_judge"]
            self.assertEqual(
                retrospective_judge["command"],
                build_pi_print_command(
                    pi_bin="pi",
                    provider="openai-codex",
                    model="gpt-5.4",
                ),
            )
            self.assertNotIn("codex_home", retrospective_judge)
            self.assertNotIn("config_home", retrospective_judge)
            self.assertNotIn("env", retrospective_judge)

    def test_run_next_execute_production_defaults_fail_without_pi_auth_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            ledger = temp_path / "ledger"
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
                "        'title': 'Generated pi defaults without mounts',\n"
                "        'status': 'open',\n"
                "        'labels': ['project:bump-eqemu', 'ready-for-agent'],\n"
                "        'metadata': {'workstream': 'central-lve', 'afk.ready': True},\n"
                "        'acceptance_criteria': ['generated by test'],\n"
                "    }))\n"
                "else:\n"
                "    raise SystemExit(9)\n" % sys.executable,
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
                str(ledger),
                "--execute",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("agent.codex_home is required", completed.stderr)

    def test_run_next_fake_local_role_profile_preserves_fake_adapters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
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
                "        'title': 'Generated fake defaults',\n"
                "        'status': 'open',\n"
                "        'labels': ['project:bump-eqemu', 'ready-for-agent'],\n"
                "        'metadata': {'workstream': 'central-lve', 'afk.ready': True},\n"
                "        'acceptance_criteria': ['generated by test'],\n"
                "    }))\n"
                "else:\n"
                "    raise SystemExit(9)\n" % sys.executable,
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
                "--role-profile",
                "fake-local",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["recipe"]["steps"][2]["input"]["agent"]["type"], "fake-pi-command")
            self.assertEqual(payload["recipe"]["steps"][4]["input"]["reviewer"]["type"], "fake-reviewer-command")
            self.assertNotIn("retrospective_judge", payload["recipe"])
            self.assertEqual(payload["recipe"]["review_feedback"], {"enabled": False})
            implement = next(step for step in payload["recipe"]["steps"] if step["name"] == "implement")
            validate = next(step for step in payload["recipe"]["steps"] if step["name"] == "validate")
            self.assertEqual(implement["input"]["validation"]["profile"], "tier1")
            self.assertEqual(validate["profile"], "tier1")
            self.assertEqual(validate["input"]["validation"]["profile"], "tier1")

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
        self.assertEqual(runner_payload["review_feedback"], {"enabled": False})
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

    def test_run_next_direct_calls_leave_review_feedback_disabled_by_default(self):
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

        original_select_work = run_next.__globals__["select_work"]
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                beads_workspace = temp_path / "beads"
                checkout_root = temp_path / "checkouts"
                checkout_path = checkout_root / "bump-EQEmu"
                beads_workspace.mkdir()
                checkout_root.mkdir(parents=True)
                checkout_path.mkdir(parents=True)

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
                )
        finally:
            run_next.__globals__["select_work"] = original_select_work

        self.assertEqual(payload["recipe"]["review_feedback"], {"enabled": False})

    def test_run_next_execute_mode_with_fake_local_recipe_keeps_review_feedback_disabled(self):
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
        runner_calls: list[tuple[object, object, object]] = []

        def fake_workstream_runner(recipe_input, *, ledger_dir, project_contract):
            runner_calls.append((recipe_input, ledger_dir, project_contract))
            return {
                "run_id": "run-789",
                "workstream_id": "central-lve.11",
                "parent": "central-lve",
                "status": "succeeded",
                "result_path": "runs/run-789/workstream-result.json",
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
                beads_workspace.mkdir()
                checkout_root.mkdir(parents=True)
                checkout_path.mkdir(parents=True)
                ledger_dir.mkdir()

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
                    enable_review_feedback=False,
                    execute=True,
                    ledger_dir=ledger_dir,
                    workstream_runner=fake_workstream_runner,
                )
        finally:
            run_next.__globals__["select_work"] = original_select_work

        self.assertEqual(len(runner_calls), 1)
        runner_payload, runner_ledger_dir, runner_contract = runner_calls[0]
        self.assertEqual(runner_payload["review_feedback"], {"enabled": False})
        self.assertEqual(payload["recipe"]["review_feedback"], {"enabled": False})
        self.assertEqual(runner_ledger_dir, ledger_dir)
        self.assertEqual(runner_contract, contract)

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
                "--role-profile",
                "fake-local",
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
                payload["recipe"]["steps"][0]["input"]["required_metadata"],
                ["afk.ready"],
            )
            self.assertEqual(
                [source["type"] for source in payload["recipe"]["steps"][0]["input"]["sources"]],
                ["beads", "github_issues"],
            )
            validate = next(step for step in payload["recipe"]["steps"] if step["name"] == "validate")
            self.assertEqual(
                validate["input"]["validation"],
                {
                    "profile": "tier1",
                    "dry_run": True,
                    "timeout_seconds": 30,
                },
            )
            self.assertEqual(validate["input"]["worker"]["type"], "local-command")

    def test_run_next_skips_beads_item_when_local_tracker_artifact_shows_open_afk_pr(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory(
            dir=ROOT,
            prefix="ledger-open-pr-skip-",
        ) as artifact_root:
            temp_path = Path(temp_dir)
            artifact_path = Path(artifact_root) / "workstreams" / "20260629T173653866624Z-89b72c97"
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            secret_dir = beads_workspace / "secrets"
            secret_dir.mkdir(parents=True)
            secret_dir.joinpath("dolt_beads_password.txt").write_text("beads-secret", encoding="utf-8")
            artifact_path.mkdir(parents=True)
            (artifact_path / "tracker-result.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "awaiting-review",
                        "source_item_external_id": "central-zwk",
                        "pr_url": "https://github.example/pr/31",
                    }
                ),
                encoding="utf-8",
            )
            (artifact_path / "workstream-result.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workstream_id": "central-zwk",
                    }
                ),
                encoding="utf-8",
            )
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
    print(json.dumps([{{"id": "central-zwk"}}, {{"id": "central-next.1"}}]))
elif sys.argv[1:3] == ["show", "central-zwk"]:
    print(json.dumps({{
        "id": "central-zwk",
        "title": "Already published source Bead",
        "status": "open",
        "labels": ["project:bump-eqemu", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-zwk"}},
        "acceptance_criteria": ["Should be skipped while PR stays open"],
        "dependencies": [],
    }}))
elif sys.argv[1:3] == ["show", "central-next.1"]:
    print(json.dumps({{
        "id": "central-next.1",
        "title": "Fresh source Bead",
        "status": "open",
        "labels": ["project:bump-eqemu", "ready-for-agent"],
        "metadata": {{"afk.ready": True, "workstream": "central-next"}},
        "acceptance_criteria": ["Should still be selected"],
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
                "--role-profile",
                "fake-local",
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)

            self.assertEqual(
                payload["selection_request"]["sources"][0]["tracker_artifact_roots"],
                [str(ROOT)],
            )
            self.assertEqual(payload["selector"]["selected"]["external_id"], "central-next.1")
            self.assertEqual(
                [
                    (item["candidate"]["external_id"], item["reason"])
                    for item in payload["selection_result"]["skipped_candidates"]
                ],
                [
                    (
                        "central-zwk",
                        "open_afk_pr_exists:workstream=central-zwk,pr_url=https://github.example/pr/31",
                    )
                ],
            )

    def test_run_next_uses_contract_root_for_tracker_artifacts_even_when_invoked_from_other_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo-root"
            launch_cwd = temp_path / "launch-cwd"
            contracts_dir = repo_root / "project-contracts"
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            contracts_dir.mkdir(parents=True)
            launch_cwd.mkdir()
            beads_workspace.mkdir()
            fake_bin.mkdir()
            write_contract(
                contracts_dir / "bump-eqemu.json",
                project_slug="bump-eqemu",
                repo_url="https://github.com/thunderbump/bump-EQEmu",
            )
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

            run_env = os.environ.copy()
            run_env["PYTHONPATH"] = str(ROOT / "src")
            run_env["PATH"] = str(fake_bin)
            run_env.pop("GH_TOKEN", None)
            run_env.pop("GITHUB_TOKEN", None)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "afk",
                    "run-next",
                    "--project",
                    "bump-eqemu",
                    "--contracts-dir",
                    str(contracts_dir),
                    "--beads-workspace",
                    str(beads_workspace),
                    "--checkout-root",
                    str(checkout_root),
                    "--checkout-path",
                    str(checkout_path),
                    "--validation-profile",
                    "tier1",
                    "--role-profile",
                    "fake-local",
                    "--ledger",
                    str(temp_path / "ledger"),
                ],
                cwd=launch_cwd,
                env=run_env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                payload["selection_request"]["sources"][0]["tracker_artifact_roots"],
                [str(repo_root)],
            )

    def test_run_next_preview_emits_project_worker_validation_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "mounts" / "worktrees" / "bump-eqemu"
            checkout_path = checkout_root / "bump-EQEmu"
            validation_stack_path = temp_path / "mounts" / "bump-akk-stack-validation"
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
                "--role-profile",
                "fake-local",
                "--validation-mode",
                "project-worker",
                "--validation-stack-path",
                str(validation_stack_path),
                env={"GH_TOKEN": None, "GITHUB_TOKEN": None, "PATH": str(fake_bin)},
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            implement = next(step for step in payload["recipe"]["steps"] if step["name"] == "implement")
            validate = next(step for step in payload["recipe"]["steps"] if step["name"] == "validate")

            self.assertEqual(
                implement["input"]["validation"],
                {
                    "profile": "tier1",
                    "commands": [],
                    "run_commands_during_implementation": False,
                    "worker_home": str(checkout_root / ".validation-worker" / "bump-EQEmu"),
                    "stack": {
                        "role": "validation",
                        "path": str(validation_stack_path),
                    },
                },
            )
            self.assertEqual(
                validate["input"]["validation"],
                {
                    "profile": "tier1",
                    "dry_run": False,
                    "timeout_seconds": 3600,
                    "worker_home": str(checkout_root / ".validation-worker" / "bump-EQEmu"),
                    "stack": {
                        "role": "validation",
                        "path": str(validation_stack_path),
                    },
                },
            )
            self.assertNotIn("worker", validate["input"])

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
                "--role-profile",
                "fake-local",
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
            self.assertNotIn("timeout_seconds", implement)
            self.assertNotIn("agent-secret", json.dumps(payload["recipe"]))

    def test_run_next_execute_uses_project_worker_validation_input_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            contracts_dir = temp_path / "contracts"
            repo = temp_path / "repo-src"
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            validation_stack_path = temp_path / "external" / "bump-akk-stack-validation"
            ledger = temp_path / "ledger"

            contracts_dir.mkdir()
            init_repo(repo)
            worker_script = repo / "scripts" / "validation-worker.sh"
            worker_script.parent.mkdir()
            worker_script.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    [[ "${{1:-}}" == "run" ]]
                    [[ "${{2:-}}" == "--request" ]]
                    python3 - "$3" <<'PY'
                    import json
                    import sys
                    from pathlib import Path

                    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
                    assert request["project"] == "bump-eqemu", request
                    assert request["profile"] == "tier1", request
                    assert request["worker_home"] == {str(checkout_root / ".validation-worker" / "bump-EQEmu")!r}, request
                    assert request["stack"] == {{
                        "role": "validation",
                        "path": {str(validation_stack_path)!r},
                    }}, request
                    evidence_dir = Path(request["evidence_dir"])
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    (evidence_dir / "result.json").write_text(
                        json.dumps(
                            {{
                                "profile": request["profile"],
                                "status": "pass",
                                "repo": request["repo"],
                                "checkout": {{
                                    "requestedRef": request["ref"],
                                    "requestedCommit": request["commit"],
                                    "resolvedCommit": request["commit"],
                                }},
                                "metadata": {{
                                    "workerHome": request["worker_home"],
                                    "stackDir": request["stack"]["path"],
                                }},
                                "steps": [],
                            }}
                        ),
                        encoding="utf-8",
                    )
                    print("worker_home=" + request["worker_home"])
                    print("stack_dir=" + request["stack"]["path"])
                    PY
                    """
                ),
                encoding="utf-8",
            )
            worker_script.chmod(0o755)
            git(repo, "add", "scripts/validation-worker.sh")
            git(repo, "commit", "-m", "add validation worker")
            write_contract(contracts_dir / "bump-eqemu.json", project_slug="bump-eqemu", repo_url=str(repo))

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
                str(contracts_dir),
                "--beads-workspace",
                str(beads_workspace),
                "--checkout-root",
                str(checkout_root),
                "--checkout-path",
                str(checkout_path),
                "--validation-profile",
                "tier1",
                "--role-profile",
                "fake-local",
                "--validation-mode",
                "project-worker",
                "--validation-stack-path",
                str(validation_stack_path),
                "--ledger",
                str(ledger),
                "--execute",
                env={
                    "GH_TOKEN": None,
                    "GITHUB_TOKEN": None,
                    "PATH": os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]),
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["workstream_result"]["status"], "validated-unpublished")
            validate = next(step for step in payload["recipe"]["steps"] if step["name"] == "validate")
            self.assertEqual(validate["input"]["validation"]["stack"]["path"], str(validation_stack_path))
            self.assertNotIn("worker", validate["input"])

            validate_run = next(path.parent for path in ledger.rglob("worker-request.json"))
            worker_request = json.loads((validate_run / "worker-request.json").read_text(encoding="utf-8"))
            self.assertEqual(worker_request["worker_home"], str(checkout_root / ".validation-worker" / "bump-EQEmu"))
            self.assertEqual(
                worker_request["stack"],
                {
                    "role": "validation",
                    "path": str(validation_stack_path),
                },
            )
            self.assertIn(
                f"worker_home={checkout_root / '.validation-worker' / 'bump-EQEmu'}",
                (validate_run / "stdout.log").read_text(encoding="utf-8"),
            )
            self.assertIn(f"stack_dir={validation_stack_path}", (validate_run / "stdout.log").read_text(encoding="utf-8"))

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
                "--role-profile",
                "fake-local",
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
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            checkout_root.mkdir()
            checkout_path.mkdir()
            beads_workspace.mkdir()
            fake_bin.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
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
                "--role-profile",
                "fake-local",
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
                "--agent-codex-home",
                str(codex_home),
                "--agent-config-home",
                str(config_home),
                "--agent-pi-config-home",
                str(pi_config_home),
                "--agent-pi-coding-agent-dir",
                str(pi_coding_agent_dir),
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
            self.assertEqual(review_input["type"], "real-reviewer-command")
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
            self.assertEqual(review_input["codex_home"], str(codex_home))
            self.assertEqual(review_input["config_home"], str(config_home))
            self.assertEqual(
                review_input["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )

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
            self.assertEqual(retrospective_judge["codex_home"], str(codex_home))
            self.assertEqual(retrospective_judge["config_home"], str(config_home))
            self.assertEqual(
                retrospective_judge["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )

    def test_run_next_fake_local_pi_reviewer_without_timeout_keeps_non_production_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            beads_workspace = temp_path / "beads"
            checkout_root = temp_path / "checkouts"
            checkout_path = checkout_root / "bump-EQEmu"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            checkout_root.mkdir()
            checkout_path.mkdir()
            beads_workspace.mkdir()
            fake_bin.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
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
                "--role-profile",
                "fake-local",
                "--agent-mode",
                "fake",
                "--reviewer-mode",
                "pi",
                "--reviewer-pi-bin",
                "/opt/pi/bin/pi",
                "--reviewer-pi-provider",
                "openai-codex",
                "--reviewer-pi-model",
                "gpt-5.4",
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
            review_input = payload["recipe"]["steps"][4]["input"]["reviewer"]
            self.assertEqual(review_input["type"], "real-reviewer-command")
            self.assertEqual(review_input["timeout_seconds"], 30)

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
                "--role-profile",
                "fake-local",
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
                "--role-profile",
                "fake-local",
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
                "--role-profile",
                "fake-local",
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
                "--agent-timeout-seconds",
                "777",
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
            self.assertEqual(implement["timeout_seconds"], 777)

            review_input = payload["recipe"]["steps"][4]["input"]["reviewer"]
            self.assertEqual(review_input["type"], "real-reviewer-command")
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
            self.assertEqual(review_input["codex_home"], str(codex_home))
            self.assertEqual(review_input["config_home"], str(config_home))
            self.assertEqual(
                review_input["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )

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
            self.assertEqual(retrospective_judge["codex_home"], str(codex_home))
            self.assertEqual(retrospective_judge["config_home"], str(config_home))
            self.assertEqual(
                retrospective_judge["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )

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
                "--role-profile",
                "fake-local",
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
                "--role-profile",
                "fake-local",
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
