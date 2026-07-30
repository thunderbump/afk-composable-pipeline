import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "a" * 40


class AfkCliFixture:
    """Filesystem and process environment for AFK CLI integration tests."""

    def __init__(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.temp = Path(self._temporary_directory.name)
        self.project = self.temp / "beads-webui"
        self.project.mkdir()
        (self.project / "afk.toml").write_text(
            textwrap.dedent(
                """
                schema_version = 1

                [validation]
                command = ["./scripts/validation-worker.sh", "run"]
                timeout_seconds = 2700
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.state_home = self.temp / "state"
        self.state_home.mkdir()
        self.home = self.temp / "home"
        self.home.mkdir()
        self.secret_value = "dogfood-password-value"
        self.secret_path = self.temp / "secrets" / "beads-password.txt"
        self.secret_path.parent.mkdir(mode=0o700)
        self.secret_path.write_text(self.secret_value + "\n", encoding="utf-8")
        self.secret_path.chmod(0o600)
        self.config_home = self.temp / "config"
        config_dir = self.config_home / "afk"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.toml"
        config_path.write_text(
            "schema_version = 1\n"
            "[beads]\n"
            f'password_file = "{self.secret_path}"\n',
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        self.beads_workspace = self.temp / "beads"
        self.beads_workspace.mkdir()
        self.fake_bin = self.temp / "bin"
        self.fake_bin.mkdir()
        self.command_log = self.temp / "commands.jsonl"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self._temporary_directory.cleanup()

    def environment(self, **overrides):
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "PATH": f"{self.fake_bin}:{environment['PATH']}",
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "AFK_BEADS_WORKSPACE": str(self.beads_workspace),
                "AFK_FAKE_LOG": str(self.command_log),
                "AFK_FAKE_PROJECT": str(self.project),
                "AFK_FAKE_SHA": BASE_SHA,
                "AFK_FAKE_BEAD": "central-bnkl.1.1",
                "AFK_FAKE_BEAD_STATUS": "open",
                "AFK_FAKE_ASSIGNEE": "",
                "AFK_FAKE_BEAD_DESCRIPTION": "Implement one candidate.",
                "AFK_FAKE_BEAD_COMMENTS": "[]",
                "AFK_FAKE_PINNED_CONTRACT": "present",
                "AFK_FAKE_EXPECTED_PASSWORD": self.secret_value,
                "HOME": str(self.home),
                "USER": "bump",
            }
        )
        environment.update(overrides)
        return environment

    def run_afk(self, *arguments, **environment_overrides):
        return subprocess.run(
            [sys.executable, "-m", "afk", *arguments],
            cwd=self.project,
            env=self.environment(**environment_overrides),
            text=True,
            capture_output=True,
            check=False,
        )

    def write_fake_executable(self, name, source):
        executable = self.fake_bin / name
        executable.write_text(source, encoding="utf-8")
        executable.chmod(0o700)
        return executable

    def replace_fake_command(self, name, executable):
        command = self.fake_bin / name
        command.unlink()
        command.symlink_to(executable)

    def install_public_start_fakes(self):
        script = self.write_fake_executable(
            "fake-public-start-command",
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import json
                import os
                import sys
                from pathlib import Path

                command = Path(sys.argv[0]).name
                args = sys.argv[1:]
                log_path = Path(os.environ["AFK_FAKE_LOG"])
                record = {{"command": command, "args": args}}
                if command == "bd":
                    record["credential_present"] = (
                        os.environ.get("BEADS_DOLT_PASSWORD")
                        == os.environ["AFK_FAKE_EXPECTED_PASSWORD"]
                    )
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(record, separators=(",", ":")) + "\\n"
                    )

                sha = os.environ["AFK_FAKE_SHA"]
                if command == "git":
                    if args == ["rev-parse", "--show-toplevel"]:
                        print(Path.cwd())
                    elif args == ["rev-parse", "--git-common-dir"]:
                        common_dir = (
                            Path(os.environ["XDG_STATE_HOME"]) / "fake-git"
                        )
                        common_dir.mkdir(parents=True, exist_ok=True)
                        print(common_dir)
                    elif args == [
                        "ls-remote",
                        "--exit-code",
                        "origin",
                        "refs/heads/main",
                    ]:
                        print(sha + "\\trefs/heads/main")
                    elif args[:1] == ["fetch"]:
                        pass
                    elif args == ["rev-parse", "FETCH_HEAD"]:
                        print(sha)
                    elif args == ["ls-tree", sha, "--", "afk.toml"]:
                        print("100644 blob " + "c" * 40 + "\\tafk.toml")
                    elif args == ["cat-file", "blob", sha + ":afk.toml"]:
                        print("schema_version = 1")
                        print("[validation]")
                        print(
                            'command = ["./scripts/validation-worker.sh", "run"]'
                        )
                        print("timeout_seconds = 2700")
                    else:
                        raise SystemExit(f"unexpected git args: {{args}}")
                elif command == "gh":
                    if args == [
                        "repo",
                        "view",
                        "--json",
                        "nameWithOwner,defaultBranchRef",
                    ]:
                        print(json.dumps({{
                            "nameWithOwner": "thunderbump/beads-webui",
                            "defaultBranchRef": {{"name": "main"}},
                        }}))
                    else:
                        raise SystemExit(f"unexpected gh args: {{args}}")
                elif command == "bd":
                    if args[:1] == ["show"]:
                        print(json.dumps([{{
                            "id": os.environ["AFK_FAKE_BEAD"],
                            "title": "Create the first slice",
                            "description": os.environ[
                                "AFK_FAKE_BEAD_DESCRIPTION"
                            ],
                            "acceptance_criteria": "Candidate is committed.",
                            "status": os.environ["AFK_FAKE_BEAD_STATUS"],
                            "close_reason": "",
                            "assignee": os.environ["AFK_FAKE_ASSIGNEE"],
                            "labels": ["project:beads-webui"],
                        }}]))
                    elif args[:1] == ["comments"]:
                        print(os.environ["AFK_FAKE_BEAD_COMMENTS"])
                    elif args[:1] == ["update"] and os.environ.get(
                        "AFK_FAKE_CLAIM_FAILURE"
                    ):
                        print("claim failed", file=sys.stderr)
                        raise SystemExit(1)
                    else:
                        raise SystemExit(f"unexpected bd args: {{args}}")
                elif command == "loginctl":
                    if args == [
                        "show-user",
                        os.environ["USER"],
                        "--property=Linger",
                        "--value",
                    ]:
                        print("yes")
                    else:
                        raise SystemExit(f"unexpected loginctl args: {{args}}")
                else:
                    raise SystemExit(f"unexpected command: {{command}}")
                """
            ).lstrip(),
        )
        for name in ("git", "gh", "bd", "loginctl", "systemd-run"):
            (self.fake_bin / name).symlink_to(script)
