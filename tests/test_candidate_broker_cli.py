import errno
import json
import os
import shutil
import socket
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


class CandidateBrokerCliTest(unittest.TestCase):
    def setUp(self):
        self.git_environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("GIT_")
        }
        self.git_environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary_directory.name)
        self.repository = self.temp / "repository"
        self.repository.mkdir()
        self.candidate = self.temp / "candidate"
        self.git("init", "-b", "main", cwd=self.repository)
        self.git("config", "user.email", "afk@example.invalid", cwd=self.repository)
        self.git("config", "user.name", "AFK Test", cwd=self.repository)
        (self.repository / "input.txt").write_text(
            "exact candidate\n", encoding="utf-8"
        )
        self.git("add", ".", cwd=self.repository)
        self.git("commit", "-m", "candidate", cwd=self.repository)
        self.git(
            "worktree",
            "add",
            "--detach",
            str(self.candidate),
            "HEAD",
            cwd=self.repository,
        )
        self.candidate_sha = self.git("rev-parse", "HEAD")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_rejects_invalid_utf8_request_without_a_traceback(self):
        request = self.temp / "invalid-utf8-request.json"
        result = self.temp / "invalid-utf8-result.json"
        request.write_bytes(b"\xff")
        no_bwrap_bin = self.temp / "invalid-utf8-bin"
        no_bwrap_bin.mkdir()

        completed = self.run_broker(request, result, env={"PATH": str(no_bwrap_bin)})

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "candidate broker request is invalid\n")
        self.assertNotIn("Traceback", completed.stderr)
        self.assertFalse(result.exists())

    def test_invalid_request_removes_stale_result_symlink_without_following_it(self):
        request = self.temp / "stale-invalid-request.json"
        result = self.temp / "stale-invalid-result.json"
        prior_result = self.temp / "prior-result.json"
        request.write_bytes(b"\xff")
        prior_result.write_text("prior success\n", encoding="utf-8")
        result.symlink_to(prior_result)

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "candidate broker request is invalid\n")
        self.assertFalse(result.exists())
        self.assertEqual(prior_result.read_text(encoding="utf-8"), "prior success\n")

    def test_rejects_nul_in_request_paths_and_commands(self):
        invalid_values = (
            (f"{self.candidate}\0escaped", ["/usr/bin/true"]),
            (str(self.candidate), ["/usr/bin/true\0escaped"]),
            (str(self.candidate), ["/usr/bin/true", "argument\0escaped"]),
        )
        for index, (candidate_path, command) in enumerate(invalid_values):
            with self.subTest(candidate_path=candidate_path, command=command):
                request = self.temp / f"nul-request-{index}.json"
                result = self.temp / f"nul-result-{index}.json"
                self.write_request(
                    request, candidate_path=candidate_path, command=command
                )

                completed = self.run_broker(request, result)

                self.assertEqual(completed.returncode, 2)
                self.assertEqual(
                    completed.stderr, "candidate broker request is invalid\n"
                )
                self.assertNotIn("Traceback", completed.stderr)
                self.assertFalse(result.exists())

    def test_rejects_unencodable_request_paths_and_commands(self):
        invalid_values = (
            (f"{self.candidate}/\ud800", ["/usr/bin/true"]),
            (str(self.candidate), ["/usr/bin/true", "\ud800"]),
        )
        for index, (candidate_path, command) in enumerate(invalid_values):
            with self.subTest(candidate_path=candidate_path, command=command):
                request = self.temp / f"surrogate-request-{index}.json"
                result = self.temp / f"surrogate-result-{index}.json"
                self.write_request(
                    request, candidate_path=candidate_path, command=command
                )

                completed = self.run_broker(request, result)

                self.assertEqual(completed.returncode, 2)
                self.assertEqual(
                    completed.stderr, "candidate broker request is invalid\n"
                )
                self.assertNotIn("Traceback", completed.stderr)
                self.assertFalse(result.exists())

    def test_rejects_unsafe_candidate_execution_bounds(self):
        invalid_values = (
            ("timeout_seconds", 0),
            ("timeout_seconds", -1),
            ("timeout_seconds", True),
            ("timeout_seconds", "1"),
            ("timeout_seconds", 3601),
            ("output_byte_limit", 0),
            ("output_byte_limit", -1),
            ("output_byte_limit", True),
            ("output_byte_limit", 1.5),
            ("output_byte_limit", 64 * 1024 * 1024 + 1),
        )
        for index, (field, value) in enumerate(invalid_values):
            with self.subTest(field=field, value=value):
                request = self.temp / f"invalid-bound-{index}-request.json"
                result = self.temp / f"invalid-bound-{index}-result.json"
                payload = {
                    "schema_version": 1,
                    "candidate_sha": self.candidate_sha,
                    "candidate_path": str(self.candidate),
                    "command": ["/usr/bin/true"],
                    field: value,
                }
                request.write_text(json.dumps(payload), encoding="utf-8")

                completed = self.run_broker(request, result)

                self.assertEqual(completed.returncode, 2)
                self.assertEqual(
                    completed.stderr, "candidate broker request is invalid\n"
                )
                self.assertFalse(result.exists())

    def test_rejects_a_nested_candidate_path_before_execution(self):
        nested = self.candidate / "nested"
        nested.mkdir()
        (nested / "input.txt").write_text("nested\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "add nested directory")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        request = self.temp / "nested-path-request.json"
        result = self.temp / "nested-path-result.json"
        result.write_text("prior success\n", encoding="utf-8")
        self.write_request(
            request, candidate_path=str(nested), command=["/usr/bin/true"]
        )
        no_bwrap_bin = self.temp / "nested-path-bin"
        no_bwrap_bin.mkdir()
        (no_bwrap_bin / "git").symlink_to(shutil.which("git"))

        completed = self.run_broker(request, result, env={"PATH": str(no_bwrap_bin)})

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            completed.stderr, "Candidate path is not the repository root\n"
        )
        self.assertFalse(result.exists())

    def test_accepts_a_symlink_alias_to_the_candidate_root(self):
        alias = self.temp / "candidate-alias"
        alias.symlink_to(self.candidate, target_is_directory=True)
        request = self.temp / "candidate-alias-request.json"
        result = self.temp / "candidate-alias-result.json"
        fake_bin = self.temp / "candidate-alias-bin"
        fake_bin.mkdir()
        (fake_bin / "bwrap").symlink_to(shutil.which("true"))
        self.write_request(
            request, candidate_path=str(alias), command=["/usr/bin/true"]
        )

        completed = self.run_broker(
            request, result, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"}
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["exit_code"], 0)

    def test_exact_candidate_pins_supplied_root_alias_for_whole_check(self):
        from afk import checkouts

        alias = self.temp / "candidate-alias"
        alias.symlink_to(self.candidate, target_is_directory=True)
        plain = self.temp / "plain-tree"
        plain.mkdir()
        (plain / "input.txt").write_text("exact candidate\n", encoding="utf-8")
        real_collect = checkouts._collect_untracked_worktree_paths
        real_open = os.open
        retargeted = False
        reopened_aliases = []

        def retarget_after_traversal(*args, **kwargs):
            nonlocal retargeted
            paths = real_collect(*args, **kwargs)
            alias.unlink()
            alias.symlink_to(plain, target_is_directory=True)
            retargeted = True
            return paths

        def record_open(path, flags, *args, **kwargs):
            if (
                retargeted
                and not isinstance(path, (bytes, int))
                and Path(path) == alias
            ):
                reopened_aliases.append(Path(path))
            return real_open(path, flags, *args, **kwargs)

        with (
            mock.patch(
                "afk.checkouts._collect_untracked_worktree_paths",
                side_effect=retarget_after_traversal,
            ),
            mock.patch("afk.checkouts.os.open", side_effect=record_open),
        ):
            self.assertTrue(checkouts.is_exact_clean_commit(alias, self.candidate_sha))

        self.assertEqual(alias.resolve(), plain)
        self.assertEqual(reopened_aliases, [])

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_candidate_stdin_is_closed_instead_of_inherited_from_the_broker(self):
        request = self.temp / "stdin-request.json"
        result = self.temp / "stdin-result.json"
        self.write_request(request, command=["/bin/cat"])

        completed = self.run_broker(request, result, input_text="host sentinel\n")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["status"], "completed")
        self.assertEqual(broker_result["exit_code"], 0, broker_result["stderr"])
        self.assertEqual(broker_result["stdout"], "")

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_invalid_utf8_candidate_output_publishes_a_bounded_result(self):
        request = self.temp / "invalid-output-request.json"
        result = self.temp / "invalid-output-result.json"
        self.write_request(
            request,
            command=[
                "/usr/bin/python3",
                "-c",
                "import os; os.write(1, b'\\xff')",
            ],
            output_byte_limit=3,
        )

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "completed",
                "exit_code": 0,
                "stdout": "\ufffd",
                "stderr": "",
            },
        )

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_timeout_publishes_a_candidate_bound_failure_result(self):
        request = self.temp / "timeout-request.json"
        result = self.temp / "timeout-result.json"
        self.write_request(
            request,
            command=[
                "/usr/bin/python3",
                "-c",
                (
                    "import sys,time; print('password=hunter2',flush=True);"
                    "time.sleep(30)"
                ),
            ],
            timeout_seconds=0.1,
        )

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "failed",
                "failure_classification": "timeout",
                "summary": (
                    "Candidate command timed out and its process tree was terminated"
                ),
                "exit_code": None,
                "stdout": "password=[REDACTED]\n",
                "stderr": "",
            },
        )

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_redacted_timeout_output_stays_within_the_requested_byte_limit(self):
        request = self.temp / "redacted-timeout-request.json"
        result = self.temp / "redacted-timeout-result.json"
        self.write_request(
            request,
            command=[
                "/usr/bin/python3",
                "-c",
                "import os,time; os.write(1,b'password=a\\n');time.sleep(30)",
            ],
            timeout_seconds=0.1,
            output_byte_limit=11,
        )

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["failure_classification"], "timeout")
        self.assertLessEqual(len(broker_result["stdout"].encode("utf-8")), 11)
        self.assertNotIn("password=a", broker_result["stdout"])

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_each_candidate_output_stream_has_an_independent_byte_limit(self):
        for stream, descriptor in (("stdout", 1), ("stderr", 2)):
            with self.subTest(stream=stream):
                request = self.temp / f"{stream}-overflow-request.json"
                result = self.temp / f"{stream}-overflow-result.json"
                self.write_request(
                    request,
                    command=[
                        "/usr/bin/python3",
                        "-c",
                        f"import os; os.write({descriptor}, b'x' * 17)",
                    ],
                    output_byte_limit=16,
                )

                completed = self.run_broker(request, result)

                self.assertEqual(completed.returncode, 0, completed.stderr)
                broker_result = json.loads(result.read_text(encoding="utf-8"))
                self.assertEqual(broker_result["candidate_sha"], self.candidate_sha)
                self.assertEqual(broker_result["status"], "failed")
                self.assertEqual(
                    broker_result["failure_classification"], "output_overflow"
                )
                self.assertIsNone(broker_result["exit_code"])
                self.assertLessEqual(len(broker_result["stdout"].encode()), 16)
                self.assertLessEqual(len(broker_result["stderr"].encode()), 16)

        request = self.temp / "independent-output-request.json"
        result = self.temp / "independent-output-result.json"
        self.write_request(
            request,
            command=[
                "/usr/bin/python3",
                "-c",
                "import os; os.write(1,b'o'*12); os.write(2,b'e'*12)",
            ],
            output_byte_limit=16,
        )

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["status"], "completed")
        self.assertEqual(broker_result["stdout"], "o" * 12)
        self.assertEqual(broker_result["stderr"], "e" * 12)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_abnormal_candidate_exits_publish_stable_failure_results(self):
        cases = (
            ("nonzero", "import sys; sys.exit(7)", 7),
            (
                "signal",
                "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
                None,
            ),
        )
        for name, program, expected_exit_code in cases:
            with self.subTest(name=name):
                request = self.temp / f"{name}-exit-request.json"
                result = self.temp / f"{name}-exit-result.json"
                self.write_request(
                    request,
                    command=["/usr/bin/python3", "-c", program],
                )

                completed = self.run_broker(request, result)

                self.assertEqual(completed.returncode, 0, completed.stderr)
                broker_result = json.loads(result.read_text(encoding="utf-8"))
                self.assertEqual(broker_result["candidate_sha"], self.candidate_sha)
                self.assertEqual(broker_result["status"], "failed")
                self.assertEqual(
                    broker_result["failure_classification"], "abnormal_exit"
                )
                self.assertIsInstance(broker_result["exit_code"], int)
                self.assertNotEqual(broker_result["exit_code"], 0)
                if expected_exit_code is not None:
                    self.assertEqual(broker_result["exit_code"], expected_exit_code)

    def test_bwrap_launch_failure_publishes_a_candidate_bound_result(self):
        request = self.temp / "launch-failure-request.json"
        result = self.temp / "launch-failure-result.json"
        fake_bin = self.temp / "launch-failure-bin"
        fake_bin.mkdir()
        bwrap = fake_bin / "bwrap"
        bwrap.write_text("#!/definitely/missing/interpreter\n", encoding="utf-8")
        bwrap.chmod(0o755)
        self.write_request(request, command=["/usr/bin/true"])

        completed = self.run_broker(
            request,
            result,
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "failed",
                "failure_classification": "launch_failure",
                "summary": "Candidate command could not be launched",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
            },
        )

    def test_container_execution_classifies_a_missing_runtime_as_unavailable(self):
        request = self.temp / "container-unavailable-request.json"
        result = self.temp / "container-unavailable-result.json"
        runtime_free_bin = self.temp / "container-unavailable-bin"
        runtime_free_bin.mkdir()
        (runtime_free_bin / "git").symlink_to(shutil.which("git"))
        self.write_request(
            request,
            command=["/bin/true"],
            execution={"type": "container", "image": "fixture:local"},
        )

        completed = self.run_broker(
            request,
            result,
            env={"PATH": str(runtime_free_bin)},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "failed",
                "failure_classification": "adapter_unavailable",
                "summary": "Container execution adapter is unavailable",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
            },
        )

    def test_container_execution_classifies_an_unusable_runtime_as_unavailable(self):
        request = self.temp / "container-unusable-request.json"
        result = self.temp / "container-unusable-result.json"
        fake_bin = self.temp / "container-unusable-bin"
        fake_bin.mkdir()
        (fake_bin / "git").symlink_to(shutil.which("git"))
        (fake_bin / "bwrap").symlink_to(shutil.which("bwrap"))
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            f"#!{sys.executable}\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        self.write_request(
            request,
            command=["/bin/true"],
            execution={"type": "container", "image": "fixture:local"},
        )

        completed = self.run_broker(
            request,
            result,
            env={"PATH": str(fake_bin)},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "failed",
                "failure_classification": "adapter_unavailable",
                "summary": "Container execution adapter is unavailable",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
            },
        )

    def test_container_execution_classifies_unavailable_supervision_as_unavailable(
        self,
    ):
        request = self.temp / "container-supervision-unavailable-request.json"
        result = self.temp / "container-supervision-unavailable-result.json"
        fake_bin = self.temp / "container-supervision-unavailable-bin"
        fake_bin.mkdir()
        (fake_bin / "git").symlink_to(shutil.which("git"))
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            f"#!{sys.executable}\nraise SystemExit(0)\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        self.write_request(
            request,
            command=["/bin/true"],
            execution={"type": "container", "image": "fixture:local"},
        )

        completed = self.run_broker(
            request,
            result,
            env={"PATH": str(fake_bin)},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "failed",
                "failure_classification": "adapter_unavailable",
                "summary": "Container execution adapter is unavailable",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
            },
        )

    def test_container_execution_receives_only_the_exact_candidate_snapshot(self):
        request = self.temp / "container-request.json"
        result = self.temp / "container-result.json"
        fake_bin = self.temp / "container-bin"
        fake_bin.mkdir()
        (fake_bin / "git").symlink_to(shutil.which("git"))
        (fake_bin / "bwrap").symlink_to(shutil.which("bwrap"))
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import sys
                from pathlib import Path

                arguments = sys.argv[1:]
                if arguments[0] == "info":
                    raise SystemExit(0)
                if arguments[0] == "rm":
                    raise SystemExit(0)
                mount = next(
                    arguments[index + 1]
                    for index, value in enumerate(arguments)
                    if value == "--mount"
                )
                source = next(
                    item.removeprefix("src=")
                    for item in mount.split(",")
                    if item.startswith("src=")
                )
                Path({str(self.candidate / "input.txt")!r}).write_text(
                    "drifted after snapshot\\n", encoding="utf-8"
                )
                print((Path(source) / "input.txt").read_text(encoding="utf-8").strip())
                one_read_only_mount = (
                    arguments.count("--mount") == 1 and "readonly" in mount
                )
                network_is_disabled = (
                    arguments[arguments.index("--network") + 1] == "none"
                )
                socket_is_hidden = not any(
                    "docker.sock" in value or "podman.sock" in value
                    for value in arguments
                )
                print("ONE_READ_ONLY_MOUNT" if one_read_only_mount else "UNSAFE_MOUNTS")
                print("NO_NETWORK" if network_is_disabled else "NETWORKED")
                print("NO_SOCKET" if socket_is_hidden else "SOCKET_EXPOSED")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        self.write_request(
            request,
            command=["/bin/fixture-check"],
            execution={"type": "container", "image": "fixture:local"},
        )

        completed = self.run_broker(
            request,
            result,
            env={"PATH": str(fake_bin)},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["status"], "completed", broker_result)
        self.assertEqual(broker_result["candidate_sha"], self.candidate_sha)
        self.assertEqual(
            broker_result["stdout"],
            "exact candidate\nONE_READ_ONLY_MOUNT\nNO_NETWORK\nNO_SOCKET\n",
        )
        self.assertEqual(broker_result["stderr"], "")

    @unittest.skipUnless(
        (shutil.which("docker") or shutil.which("podman"))
        and os.environ.get("AFK_CONTAINER_TEST_IMAGE"),
        "set AFK_CONTAINER_TEST_IMAGE to a locally available fixture image",
    )
    def test_container_execution_runs_a_fixture_on_the_local_runtime(self):
        request = self.temp / "container-runtime-request.json"
        result = self.temp / "container-runtime-result.json"
        self.write_request(
            request,
            command=[
                "/bin/sh",
                "-c",
                "cat /candidate/input.txt; "
                "printf 'scratch works\\n' > /work/result; "
                "cat /work/result",
            ],
            execution={
                "type": "container",
                "image": os.environ["AFK_CONTAINER_TEST_IMAGE"],
            },
        )

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["status"], "completed", broker_result)
        self.assertEqual(broker_result["candidate_sha"], self.candidate_sha)
        self.assertEqual(broker_result["stdout"], "exact candidate\nscratch works\n")
        self.assertEqual(broker_result["stderr"], "")

    def test_container_execution_rejects_target_specific_configuration(self):
        invalid_executions = (
            {"type": "compose", "image": "fixture:local"},
            {"type": "container", "image": "--privileged"},
            {
                "type": "container",
                "image": "fixture:local",
                "compose_file": "compose.yml",
            },
            {
                "type": "container",
                "image": "fixture:local",
                "service": "database",
            },
        )
        for index, execution in enumerate(invalid_executions):
            with self.subTest(execution=execution):
                request = self.temp / f"target-specific-{index}-request.json"
                result = self.temp / f"target-specific-{index}-result.json"
                self.write_request(
                    request,
                    command=["/bin/true"],
                    execution=execution,
                )

                completed = self.run_broker(request, result)

                self.assertEqual(completed.returncode, 2)
                self.assertEqual(
                    completed.stderr, "candidate broker request is invalid\n"
                )
                self.assertFalse(result.exists())

    def test_container_timeout_fails_closed_when_forced_cleanup_fails(self):
        request = self.temp / "container-cleanup-request.json"
        result = self.temp / "container-cleanup-result.json"
        fake_bin = self.temp / "container-cleanup-bin"
        fake_bin.mkdir()
        for executable in ("git", "bwrap"):
            (fake_bin / executable).symlink_to(shutil.which(executable))
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import sys
                import time

                if sys.argv[1] == "info":
                    raise SystemExit(0)
                if sys.argv[1] == "rm":
                    raise SystemExit(7)
                print("container started", flush=True)
                time.sleep(30)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        self.write_request(
            request,
            command=["/bin/sleep", "30"],
            timeout_seconds=0.1,
            execution={"type": "container", "image": "fixture:local"},
        )

        completed = self.run_broker(
            request,
            result,
            env={"PATH": str(fake_bin)},
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "Candidate container cleanup failed\n")
        self.assertFalse(result.exists())

    def test_container_timeout_forcibly_removes_the_runtime_container(self):
        request = self.temp / "container-timeout-request.json"
        result = self.temp / "container-timeout-result.json"
        cleanup_marker = self.temp / "container-was-removed"
        fake_bin = self.temp / "container-timeout-bin"
        fake_bin.mkdir()
        for executable in ("git", "bwrap"):
            (fake_bin / executable).symlink_to(shutil.which(executable))
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import sys
                import time
                from pathlib import Path

                if sys.argv[1] == "info":
                    raise SystemExit(0)
                if sys.argv[1] == "rm":
                    Path({str(cleanup_marker)!r}).write_text(
                        sys.argv[-1], encoding="utf-8"
                    )
                    raise SystemExit(0)
                print("container started", flush=True)
                time.sleep(30)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        self.write_request(
            request,
            command=["/bin/sleep", "30"],
            timeout_seconds=0.1,
            execution={"type": "container", "image": "fixture:local"},
        )

        completed = self.run_broker(
            request,
            result,
            env={"PATH": str(fake_bin)},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["failure_classification"], "timeout")
        self.assertTrue(
            cleanup_marker.read_text(encoding="utf-8").startswith("afk-candidate-")
        )

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_timeout_reaps_detached_candidate_descendants_before_publication(self):
        request = self.temp / "detached-timeout-request.json"
        result = self.temp / "detached-timeout-result.json"
        token = f"afk-detached-{os.getpid()}-{time.monotonic_ns()}"
        child = "import time; time.sleep(30)"
        parent = (
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-c',{child!r},{token!r}],"
            "start_new_session=True);"
            "print('detached-ready',flush=True);"
            "time.sleep(30)"
        )
        self.write_request(
            request,
            command=["/usr/bin/python3", "-c", parent],
            timeout_seconds=0.2,
        )

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["candidate_sha"], self.candidate_sha)
        self.assertEqual(broker_result["failure_classification"], "timeout")
        self.assertIn("detached-ready", broker_result["stdout"])
        survivors = []
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                command_line = (process / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if token.encode() in command_line:
                survivors.append(int(process.name))
        self.assertEqual(survivors, [])

    def test_success_atomically_replaces_a_result_symlink_without_following_it(self):
        request = self.temp / "atomic-request.json"
        result = self.temp / "atomic-result.json"
        prior_result = self.temp / "atomic-prior-result.json"
        prior_result.write_text("prior success\n", encoding="utf-8")
        self.write_request(request, command=["/usr/bin/true"])
        fake_bin = self.temp / "atomic-bin"
        fake_bin.mkdir()
        launcher = fake_bin / "bwrap"
        launcher.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                from pathlib import Path

                Path({str(result)!r}).symlink_to({str(prior_result)!r})
                """
            ).lstrip(),
            encoding="utf-8",
        )
        launcher.chmod(0o755)

        completed = self.run_broker(
            request,
            result,
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(result.is_symlink())
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["exit_code"], 0)
        self.assertEqual(prior_result.read_text(encoding="utf-8"), "prior success\n")

    def test_failed_atomic_publication_cleans_its_temporary_result(self):
        request = self.temp / "failed-publication-request.json"
        result = self.temp / "failed-publication-result.json"
        self.write_request(request, command=["/usr/bin/true"])
        fake_bin = self.temp / "failed-publication-bin"
        fake_bin.mkdir()
        launcher = fake_bin / "bwrap"
        launcher.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                from pathlib import Path

                Path({str(result)!r}).mkdir()
                """
            ).lstrip(),
            encoding="utf-8",
        )
        launcher.chmod(0o755)

        completed = self.run_broker(
            request,
            result,
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )

        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertTrue(result.is_dir())
        self.assertEqual(list(self.temp.glob(f".{result.name}.*.tmp")), [])

    def test_does_not_run_repository_fsmonitor_on_the_host(self):
        marker = self.temp / "fsmonitor-ran"
        fsmonitor = self.temp / "fsmonitor"
        fsmonitor.write_text(
            f"#!/bin/sh\ntouch {marker}\nexit 1\n",
            encoding="utf-8",
        )
        fsmonitor.chmod(0o755)
        self.git("config", "core.fsmonitor", str(fsmonitor))
        request = self.temp / "fsmonitor-request.json"
        result = self.temp / "fsmonitor-result.json"
        fake_bin = self.temp / "fsmonitor-bin"
        fake_bin.mkdir()
        (fake_bin / "bwrap").symlink_to(shutil.which("true"))
        self.write_request(request, command=["/usr/bin/true"])

        completed = self.run_broker(
            request, result, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"}
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["exit_code"], 0)
        self.assertFalse(marker.exists())

    def test_does_not_run_clean_filters_when_checking_the_candidate(self):
        (self.candidate / ".gitattributes").write_text(
            "input.txt filter=evil\n", encoding="utf-8"
        )
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "add candidate filter")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        marker = self.temp / "clean-filter-ran"
        clean_filter = self.temp / "clean-filter"
        clean_filter.write_text(
            f"#!/bin/sh\ntouch {marker}\n/bin/cat\n",
            encoding="utf-8",
        )
        clean_filter.chmod(0o755)
        self.git("config", "filter.evil.clean", str(clean_filter))
        os.utime(self.candidate / "input.txt")
        request = self.temp / "clean-filter-request.json"
        result = self.temp / "clean-filter-result.json"
        fake_bin = self.temp / "clean-filter-bin"
        fake_bin.mkdir()
        (fake_bin / "bwrap").symlink_to(shutil.which("true"))
        self.write_request(request, command=["/usr/bin/true"])

        completed = self.run_broker(
            request, result, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"}
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["exit_code"], 0)
        self.assertFalse(marker.exists())

    def test_exact_candidate_streams_regular_files_without_path_read_bytes(self):
        from afk.checkouts import is_exact_clean_commit

        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("regular files must be streamed"),
        ):
            self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_rejects_tracked_metadata_over_output_limit(self):
        from afk import checkouts

        (self.candidate / "second.txt").write_text("second\n", encoding="utf-8")
        self.git("add", "second.txt")
        self.git("commit", "-m", "add second tracked file")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        real_run_trusted_read_git = checkouts.run_trusted_read_git
        bounded_results = []

        def record_tracked_read(args, **kwargs):
            result = real_run_trusted_read_git(args, **kwargs)
            if args[0] in {"ls-tree", "ls-files"}:
                bounded_results.append(
                    (kwargs.get("output_byte_limit"), result.returncode, result.stdout)
                )
            return result

        with (
            mock.patch.object(checkouts, "EXACT_CANDIDATE_TRACKED_PATH_LIMIT", 1),
            mock.patch.object(checkouts, "EXACT_CANDIDATE_TRACKED_BYTES_LIMIT", 1),
            mock.patch.object(checkouts, "EXACT_CANDIDATE_TRACKED_DEPTH_LIMIT", 8),
            mock.patch(
                "afk.checkouts.run_trusted_read_git",
                side_effect=record_tracked_read,
            ),
        ):
            self.assertFalse(
                checkouts.is_exact_clean_commit(self.candidate, self.candidate_sha)
            )

        self.assertEqual(bounded_results, [(65, 1, b""), (65, 1, b"")])

    def test_bounded_trusted_git_cleans_child_when_io_setup_raises(self):
        from afk import checkouts

        real_popen = subprocess.Popen
        children = []

        def start_long_lived_child(_command, **kwargs):
            child = real_popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                **kwargs,
            )
            children.append(child)
            return child

        try:
            with (
                mock.patch(
                    "afk.checkouts.subprocess.Popen",
                    side_effect=start_long_lived_child,
                ),
                mock.patch(
                    "afk.checkouts.BoundedProcessIO",
                    side_effect=RuntimeError("io setup failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "io setup failed"),
            ):
                checkouts._run_bounded_trusted_git(
                    ["git", "ls-tree"],
                    cwd=self.candidate,
                    environment=self.git_environment,
                    input_data=None,
                    output_byte_limit=64,
                )

            child = children[0]
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdout.closed)
            self.assertTrue(child.stderr.closed)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_bounded_trusted_git_cleans_child_and_reraises_interrupt(self):
        from afk import checkouts

        real_popen = subprocess.Popen
        real_process_io = checkouts.BoundedProcessIO
        children = []

        def start_long_lived_child(_command, **kwargs):
            child = real_popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                **kwargs,
            )
            children.append(child)
            return child

        def interrupting_process_io(*args, **kwargs):
            process_io = real_process_io(*args, **kwargs)
            process_io.observe = mock.Mock(side_effect=KeyboardInterrupt())
            return process_io

        try:
            with (
                mock.patch(
                    "afk.checkouts.subprocess.Popen",
                    side_effect=start_long_lived_child,
                ),
                mock.patch(
                    "afk.checkouts.BoundedProcessIO",
                    side_effect=interrupting_process_io,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                checkouts._run_bounded_trusted_git(
                    ["git", "ls-tree"],
                    cwd=self.candidate,
                    environment=self.git_environment,
                    input_data=None,
                    output_byte_limit=64,
                )

            child = children[0]
            self.assertIsNotNone(child.poll())
            self.assertTrue(child.stdout.closed)
            self.assertTrue(child.stderr.closed)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_exact_candidate_accepts_normal_nested_tracked_tree(self):
        from afk.checkouts import is_exact_clean_commit

        nested = self.candidate / "one" / "two" / "three"
        nested.mkdir(parents=True)
        (nested / "input.txt").write_text("nested\n", encoding="utf-8")
        self.git("add", "one/two/three/input.txt")
        self.git("commit", "-m", "add nested tracked file")
        self.candidate_sha = self.git("rev-parse", "HEAD")

        self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_bounds_nested_gitlink_repositories(self):
        from afk import checkouts

        leaf = self.temp / "gitlink-leaf"
        leaf.mkdir()
        self.git("init", "-b", "main", cwd=leaf)
        self.git("config", "user.email", "afk@example.invalid", cwd=leaf)
        self.git("config", "user.name", "AFK Test", cwd=leaf)
        (leaf / "input.txt").write_text("leaf\n", encoding="utf-8")
        self.git("add", ".", cwd=leaf)
        self.git("commit", "-m", "leaf", cwd=leaf)
        middle = self.temp / "gitlink-middle"
        middle.mkdir()
        self.git("init", "-b", "main", cwd=middle)
        self.git("config", "user.email", "afk@example.invalid", cwd=middle)
        self.git("config", "user.name", "AFK Test", cwd=middle)
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(leaf),
            "nested",
            cwd=middle,
        )
        self.git("commit", "-m", "middle", cwd=middle)
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(middle),
            "nested",
        )
        self.git("commit", "-m", "nested gitlinks")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
        )

        with mock.patch.object(
            checkouts,
            "EXACT_CANDIDATE_REPOSITORY_LIMIT",
            3,
        ):
            self.assertTrue(
                checkouts.is_exact_clean_commit(self.candidate, self.candidate_sha)
            )

        with mock.patch.object(
            checkouts,
            "EXACT_CANDIDATE_REPOSITORY_LIMIT",
            2,
        ):
            self.assertFalse(
                checkouts.is_exact_clean_commit(self.candidate, self.candidate_sha)
            )

    def test_exact_candidate_fails_closed_when_regular_file_becomes_fifo_at_open(self):
        from afk.checkouts import is_exact_clean_commit

        target = self.candidate / "input.txt"
        real_open = os.open
        real_fstat = os.fstat
        opened_descriptors = []
        inspected_descriptors = []
        required_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK

        def replace_with_fifo(path, flags, *args, **kwargs):
            if path == os.fsencode(target.name) and kwargs.get("dir_fd") is not None:
                self.assertEqual(flags & required_flags, required_flags)
                target.unlink()
                os.mkfifo(target)
                descriptor = real_open(path, flags, *args, **kwargs)
                opened_descriptors.append(descriptor)
                return descriptor
            return real_open(path, flags, *args, **kwargs)

        def record_fstat(descriptor):
            if descriptor in opened_descriptors:
                inspected_descriptors.append(descriptor)
            return real_fstat(descriptor)

        with (
            mock.patch("afk.checkouts.os.open", side_effect=replace_with_fifo),
            mock.patch("afk.checkouts.os.fstat", side_effect=record_fstat),
        ):
            self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

        self.assertEqual(inspected_descriptors, opened_descriptors)

    def test_exact_candidate_rejects_oversized_sparse_file_before_reading(self):
        from afk.checkouts import is_exact_clean_commit

        os.truncate(self.candidate / "input.txt", 1024**4)

        with mock.patch(
            "afk.checkouts.os.fdopen",
            side_effect=AssertionError("oversized file payload must not be read"),
        ):
            self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_rejects_same_size_in_place_mutation_during_hashing(self):
        from afk.checkouts import is_exact_clean_commit

        target = self.candidate / "input.txt"
        real_open = os.open
        real_fstat = os.fstat
        opened_descriptors = []
        target_fstats = 0

        def record_target_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == os.fsencode(target.name) and kwargs.get("dir_fd") is not None:
                opened_descriptors.append(descriptor)
            return descriptor

        def mutate_before_final_fstat(descriptor):
            nonlocal target_fstats
            if descriptor in opened_descriptors:
                target_fstats += 1
                if target_fstats == 2:
                    target.write_bytes(b"mutated content\n")
            return real_fstat(descriptor)

        with (
            mock.patch("afk.checkouts.os.open", side_effect=record_target_open),
            mock.patch("afk.checkouts.os.fstat", side_effect=mutate_before_final_fstat),
        ):
            self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

        self.assertEqual(target_fstats, 2)
        self.assertEqual(target.read_bytes(), b"mutated content\n")

    def test_exact_candidate_rejects_executable_without_owner_execute(self):
        from afk.checkouts import is_exact_clean_commit

        target = self.candidate / "input.txt"
        target.chmod(0o755)
        self.git("add", "input.txt")
        self.git("commit", "-m", "make input executable")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

        target.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_accepts_non_executable_with_other_execute(self):
        from afk.checkouts import is_exact_clean_commit

        target = self.candidate / "input.txt"
        self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

        target.chmod(target.stat().st_mode | stat.S_IXOTH)

        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_allows_files_ignored_by_committed_gitignore(self):
        from afk.checkouts import is_exact_clean_commit

        (self.candidate / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore generated log")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        (self.candidate / "ignored.log").write_text("generated\n", encoding="utf-8")

        self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_bounds_committed_ignore_materialization(self):
        from afk import checkouts

        (self.candidate / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "add ignore policy")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        real_run_trusted_read_git = checkouts.run_trusted_read_git
        blob_reads = []

        def record_blob_read(args, **kwargs):
            if args[:2] == ["cat-file", "blob"]:
                blob_reads.append(args)
            return real_run_trusted_read_git(args, **kwargs)

        with mock.patch(
            "afk.checkouts.run_trusted_read_git",
            side_effect=record_blob_read,
        ):
            for file_limit, byte_limit in ((0, 1024), (1, 4)):
                with mock.patch.multiple(
                    checkouts,
                    EXACT_CANDIDATE_IGNORE_FILE_LIMIT=file_limit,
                    EXACT_CANDIDATE_IGNORE_BYTES_LIMIT=byte_limit,
                ):
                    self.assertFalse(
                        checkouts.is_exact_clean_commit(
                            self.candidate, self.candidate_sha
                        )
                    )

        self.assertEqual(blob_reads, [])

    def test_exact_candidate_preserves_nested_committed_ignore_negation(self):
        from afk.checkouts import is_exact_clean_commit

        generated = self.candidate / "generated"
        generated.mkdir()
        (self.candidate / ".gitignore").write_text(
            "generated/*.log\n", encoding="utf-8"
        )
        (generated / ".gitignore").write_text("!keep.log\n", encoding="utf-8")
        self.git("add", ".gitignore", "generated/.gitignore")
        self.git("commit", "-m", "add nested ignore negation")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        (generated / "drop.log").write_bytes(b"")
        (generated / "keep.log").write_bytes(b"")

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_rejects_ignored_symlink_replacing_tracked_directory(self):
        from afk.checkouts import is_exact_clean_commit

        tracked = self.candidate / "tracked"
        tracked.mkdir()
        (tracked / "input.txt").write_text("tracked content\n", encoding="utf-8")
        self.git("add", "tracked/input.txt")
        (self.candidate / ".gitignore").write_text("tracked\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "track ignored directory content")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        external = self.temp / "external"
        external.mkdir()
        (external / "input.txt").write_text("tracked content\n", encoding="utf-8")
        shutil.rmtree(tracked)
        tracked.symlink_to(external, target_is_directory=True)

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_rejects_tracked_parent_replaced_after_scan(self):
        from afk import checkouts

        tracked = self.candidate / "tracked"
        tracked.mkdir()
        (tracked / "input.txt").write_text("tracked content\n", encoding="utf-8")
        self.git("add", "tracked/input.txt")
        self.git("commit", "-m", "add tracked directory")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        external = self.temp / "external"
        external.mkdir()
        (external / "input.txt").write_text("tracked content\n", encoding="utf-8")
        real_collect_untracked_worktree_paths = (
            checkouts._collect_untracked_worktree_paths
        )

        def replace_parent_after_scan(*args, **kwargs):
            paths = real_collect_untracked_worktree_paths(*args, **kwargs)
            shutil.rmtree(tracked)
            tracked.symlink_to(external, target_is_directory=True)
            return paths

        with mock.patch(
            "afk.checkouts._collect_untracked_worktree_paths",
            side_effect=replace_parent_after_scan,
        ):
            self.assertFalse(
                checkouts.is_exact_clean_commit(self.candidate, self.candidate_sha)
            )

    def test_exact_candidate_rejects_ignored_file_replaced_after_scan(self):
        from afk import checkouts

        (self.candidate / ".gitignore").write_text("ignored.data\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore generated data")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        ignored = self.candidate / "ignored.data"
        ignored.write_bytes(b"generated\n")
        real_collect_untracked_worktree_paths = (
            checkouts._collect_untracked_worktree_paths
        )

        def replace_ignored_after_scan(*args, **kwargs):
            paths = real_collect_untracked_worktree_paths(*args, **kwargs)
            ignored.unlink()
            os.mkfifo(ignored)
            return paths

        with mock.patch(
            "afk.checkouts._collect_untracked_worktree_paths",
            side_effect=replace_ignored_after_scan,
        ):
            self.assertFalse(
                checkouts.is_exact_clean_commit(self.candidate, self.candidate_sha)
            )

    def test_exact_candidate_rejects_fifo_ignored_by_committed_gitignore(self):
        from afk.checkouts import is_exact_clean_commit

        (self.candidate / ".gitignore").write_text("ignored.pipe\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore generated fifo")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        os.mkfifo(self.candidate / "ignored.pipe")

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_allows_gitignore_below_committed_ignored_directory(self):
        from afk.checkouts import is_exact_clean_commit

        (self.candidate / ".gitignore").write_text("generated/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore generated directory")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        generated = self.candidate / "generated"
        generated.mkdir()
        (generated / ".gitignore").write_text("*\n", encoding="utf-8")
        (generated / "output.log").write_text("generated\n", encoding="utf-8")

        self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_prunes_committed_ignored_directory(self):
        from afk.checkouts import is_exact_clean_commit

        (self.candidate / ".gitignore").write_text("generated/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore generated directory")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        generated = self.candidate / "generated"
        generated.mkdir()
        for index in range(64):
            (generated / f"output-{index}.log").write_text(
                "generated\n", encoding="utf-8"
            )
        real_scandir = os.scandir

        def reject_ignored_scan(path):
            if not isinstance(path, int) and Path(path) == generated:
                raise AssertionError("ignored directory leaves must not be enumerated")
            return real_scandir(path)

        with mock.patch("afk.checkouts.os.scandir", side_effect=reject_ignored_scan):
            self.assertTrue(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_bounds_flat_ignored_file_evaluation(self):
        from afk import checkouts

        (self.candidate / ".gitignore").write_text("*.log\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore generated logs")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        path_limit = 4
        byte_limit = 1024
        for index in range(path_limit + 1):
            (self.candidate / f"generated-{index}.log").write_bytes(b"")
        real_run_trusted_read_git = checkouts.run_trusted_read_git
        oversized_requests = []

        def record_ignore_request(args, **kwargs):
            input_data = kwargs.get("input_data") or b""
            if args[:1] == ["check-ignore"] and input_data.count(b"\0") > path_limit:
                oversized_requests.append(input_data.count(b"\0"))
            return real_run_trusted_read_git(args, **kwargs)

        with (
            mock.patch.object(
                checkouts, "EXACT_CANDIDATE_UNTRACKED_PATH_LIMIT", path_limit
            ),
            mock.patch.object(
                checkouts, "EXACT_CANDIDATE_UNTRACKED_BYTES_LIMIT", byte_limit
            ),
            mock.patch(
                "afk.checkouts.run_trusted_read_git",
                side_effect=record_ignore_request,
            ),
        ):
            self.assertFalse(
                checkouts.is_exact_clean_commit(self.candidate, self.candidate_sha)
            )

        self.assertEqual(oversized_requests, [])

    def test_exact_candidate_bounds_directories_across_breadth_frontiers(self):
        from afk import checkouts

        directory = self.candidate
        for depth in range(5):
            directory /= f"d{depth}"
            directory.mkdir()
        real_run_trusted_read_git = checkouts.run_trusted_read_git
        ignore_requests = []

        def record_ignore_request(args, **kwargs):
            if args[:1] == ["check-ignore"]:
                ignore_requests.append(kwargs.get("input_data"))
            return real_run_trusted_read_git(args, **kwargs)

        with (
            mock.patch.object(checkouts, "EXACT_CANDIDATE_UNTRACKED_PATH_LIMIT", 4),
            mock.patch.object(checkouts, "EXACT_CANDIDATE_UNTRACKED_BYTES_LIMIT", 1024),
            mock.patch(
                "afk.checkouts.run_trusted_read_git",
                side_effect=record_ignore_request,
            ),
        ):
            self.assertFalse(
                checkouts.is_exact_clean_commit(self.candidate, self.candidate_sha)
            )

        self.assertEqual(len(ignore_requests), 4)

    def test_exact_candidate_rejects_unignored_extra_file(self):
        from afk.checkouts import is_exact_clean_commit

        (self.candidate / "unexpected.log").write_text("unexpected\n", encoding="utf-8")

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_rejects_unreadable_untracked_directory(self):
        from afk.checkouts import is_exact_clean_commit

        hidden = self.candidate / "hidden"
        hidden.mkdir()
        real_scandir = os.scandir
        attempted = []

        def deny_hidden_scan(path):
            if not isinstance(path, int) and Path(path).resolve() == hidden:
                attempted.append(hidden)
                raise PermissionError("hidden directory is unreadable")
            return real_scandir(path)

        with mock.patch("afk.checkouts.os.scandir", side_effect=deny_hidden_scan):
            self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

        self.assertEqual(attempted, [hidden])

    def test_exact_candidate_does_not_trust_repo_info_exclude(self):
        from afk.checkouts import is_exact_clean_commit

        exclude = Path(self.git("rev-parse", "--git-path", "info/exclude"))
        exclude.write_text("unexpected.log\n", encoding="utf-8")
        (self.candidate / "unexpected.log").write_text("unexpected\n", encoding="utf-8")

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_does_not_trust_configured_global_excludes(self):
        from afk.checkouts import is_exact_clean_commit

        excludes = self.temp / "global-excludes"
        excludes.write_text("unexpected.log\n", encoding="utf-8")
        self.git("config", "core.excludesFile", str(excludes))
        (self.candidate / "unexpected.log").write_text("unexpected\n", encoding="utf-8")

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_rejects_untracked_root_gitignore(self):
        from afk.checkouts import is_exact_clean_commit

        (self.candidate / ".gitignore").write_text("*\n", encoding="utf-8")
        (self.candidate / "unexpected.log").write_text("unexpected\n", encoding="utf-8")

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    def test_exact_candidate_rejects_untracked_nested_gitignore(self):
        from afk.checkouts import is_exact_clean_commit

        generated = self.candidate / "generated"
        generated.mkdir()
        (generated / ".gitignore").write_text("*\n", encoding="utf-8")
        (generated / "unexpected.log").write_text("unexpected\n", encoding="utf-8")

        self.assertFalse(is_exact_clean_commit(self.candidate, self.candidate_sha))

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_ignores_replace_refs_for_the_approved_candidate(self):
        (self.repository / "input.txt").write_text(
            "evil replacement\n", encoding="utf-8"
        )
        self.git("-C", str(self.repository), "add", "input.txt")
        self.git("-C", str(self.repository), "commit", "-m", "evil replacement")
        replacement_sha = self.git("-C", str(self.repository), "rev-parse", "HEAD")
        self.git("replace", self.candidate_sha, replacement_sha)
        self.assertEqual(self.git("show", "HEAD:input.txt"), "evil replacement")
        request = self.temp / "replace-request.json"
        result = self.temp / "replace-result.json"
        self.write_request(request, command=["/bin/cat", "/candidate/input.txt"])

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["exit_code"], 0, broker_result["stderr"])
        self.assertEqual(broker_result["stdout"], "exact candidate\n")

    def test_does_not_fetch_a_missing_promised_blob_on_the_host(self):
        remote = self.temp / "promisor.git"
        self.git(
            "clone",
            "--bare",
            str(self.repository),
            str(remote),
            cwd=self.temp,
        )
        marker = self.temp / "promisor-fetch-ran"
        upload_pack = self.temp / "upload-pack"
        upload_pack.write_text(
            f"#!/bin/sh\ntouch {marker}\nexit 1\n",
            encoding="utf-8",
        )
        upload_pack.chmod(0o755)
        self.git("remote", "add", "origin", str(remote))
        self.git("config", "core.repositoryFormatVersion", "1")
        self.git("config", "extensions.partialClone", "origin")
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.partialCloneFilter", "blob:none")
        self.git("config", "remote.origin.uploadpack", str(upload_pack))
        blob_sha = self.git("rev-parse", "HEAD:input.txt")
        common_dir = Path(self.git("rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = self.candidate / common_dir
        (common_dir / "objects" / blob_sha[:2] / blob_sha[2:]).unlink()
        request = self.temp / "promisor-request.json"
        result = self.temp / "promisor-result.json"
        fake_bin = self.temp / "promisor-bin"
        fake_bin.mkdir()
        (fake_bin / "bwrap").symlink_to(shutil.which("true"))
        self.write_request(request, command=["/usr/bin/true"])

        completed = self.run_broker(
            request, result, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"}
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            completed.stderr, "Candidate does not match its exact clean commit\n"
        )
        self.assertFalse(result.exists())
        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_runs_against_read_only_exact_candidate_with_writable_scratch(self):
        probe = textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path

            candidate = Path("/candidate/input.txt")
            scratch = Path("/work/output.txt")
            scratch.write_text("scratch works\\n", encoding="utf-8")
            try:
                candidate.write_text("mutated\\n", encoding="utf-8")
            except OSError:
                read_only = True
            else:
                read_only = False
            print(candidate.read_text(encoding="utf-8").strip())
            print(scratch.read_text(encoding="utf-8").strip())
            print("READ_ONLY" if read_only else "WRITABLE")
            print(
                "GIT_METADATA_HIDDEN"
                if not Path("/candidate/.git").exists()
                else "GIT_METADATA_VISIBLE"
            )
            print(
                "STATE_HIDDEN"
                if "XDG_STATE_HOME" not in os.environ
                else "STATE_LEAKED"
            )
            print(
                "EVIDENCE_HIDDEN"
                if "AFK_EVIDENCE_DIR" not in os.environ
                else "EVIDENCE_LEAKED"
            )
            print("candidate stderr", file=sys.stderr)
            raise SystemExit(7)
            """
        ).lstrip()
        request = self.temp / "request.json"
        result = self.temp / "result.json"
        fake_bin = self.temp / "bin"
        fake_bin.mkdir()
        real_bwrap = shutil.which("bwrap")
        launcher = fake_bin / "bwrap"
        launcher.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import os
                import sys
                from pathlib import Path

                Path({str(self.candidate / "input.txt")!r}).write_text(
                    "drifted after verification\\n", encoding="utf-8"
                )
                os.execv({real_bwrap!r}, [{real_bwrap!r}, *sys.argv[1:]])
                """
            ).lstrip(),
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        self.write_request(request, command=["/usr/bin/python3", "-c", probe])

        completed = self.run_broker(
            request,
            result,
            env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "XDG_STATE_HOME": str(self.temp / "state"),
                "AFK_EVIDENCE_DIR": str(self.temp / "evidence"),
            },
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "candidate_sha": self.candidate_sha,
                "status": "failed",
                "failure_classification": "abnormal_exit",
                "summary": "Candidate command exited with status 7",
                "exit_code": 7,
                "stdout": (
                    "exact candidate\n"
                    "scratch works\n"
                    "READ_ONLY\n"
                    "GIT_METADATA_HIDDEN\n"
                    "STATE_HIDDEN\n"
                    "EVIDENCE_HIDDEN\n"
                ),
                "stderr": "candidate stderr\n",
            },
        )
        self.assertEqual(
            (self.candidate / "input.txt").read_text(encoding="utf-8"),
            "drifted after verification\n",
        )

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_denies_validation_and_host_capabilities_but_keeps_declared_access(self):
        request = self.temp / "capability-request.json"
        result = self.temp / "capability-result.json"
        evidence = self.temp / "evidence" / "validation.log"
        run_store = self.temp / "run-store" / "run.json"
        harness = self.temp / "trusted-harness.py"
        credential = self.temp / "credentials" / "token"
        unrelated = self.temp / "unrelated-host-file"
        for path in (evidence, run_store, harness, credential, unrelated):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"host-only:{path.name}\n", encoding="utf-8")
        docker_socket_path = self.temp / "docker.sock"
        broker_socket_path = self.temp / "broker.sock"
        host_paths = {
            "validation_request": str(request),
            "broker_result": str(result),
            "validation_evidence": str(evidence),
            "run_store": str(run_store),
            "trusted_harness": str(harness),
            "credential_file": str(credential),
            "unrelated_host_file": str(unrelated),
        }
        host_dev_stat = os.stat("/dev")
        host_dev_identity = [host_dev_stat.st_dev, host_dev_stat.st_ino]
        host_pid_namespace_stat = os.stat("/proc/self/ns/pid")
        host_pid_namespace_identity = [
            host_pid_namespace_stat.st_dev,
            host_pid_namespace_stat.st_ino,
        ]
        probe = textwrap.dedent(
            f"""
            import json
            import os
            import socket
            import stat
            import sys
            from pathlib import Path

            checks = {{}}

            def attempt(name, operation):
                try:
                    operation()
                except OSError as exc:
                    checks[name] = {{
                        "classification": "denied",
                        "error": type(exc).__name__,
                        "errno": exc.errno,
                    }}
                else:
                    checks[name] = {{"classification": "exposed"}}

            host_paths = {host_paths!r}
            for name, value in host_paths.items():
                attempt(
                    name + "_read",
                    lambda value=value: Path(value).read_bytes(),
                )
                attempt(
                    name + "_write_create",
                    lambda value=value: Path(value).write_text(
                        "candidate write", encoding="utf-8"
                    ),
                )
            attempt(
                "candidate_write",
                lambda: Path("/candidate/candidate-write").write_text(
                    "candidate write", encoding="utf-8"
                ),
            )

            scratch = Path("/work/scratch")
            scratch.write_text("scratch works", encoding="utf-8")
            checks["candidate_read"] = {{
                "classification": "permitted"
                if Path("/candidate/input.txt").read_text(encoding="utf-8")
                == "exact candidate\\n"
                else "wrong_value"
            }}
            checks["scratch_write"] = {{
                "classification": "permitted"
                if scratch.read_text(encoding="utf-8") == "scratch works"
                else "wrong_value",
                "value": scratch.read_text(encoding="utf-8"),
            }}
            private_tmp = Path("/tmp/private-scratch")
            private_tmp.write_text("private tmp works", encoding="utf-8")
            checks["private_tmp_write"] = {{
                "classification": "permitted"
                if private_tmp.read_text(encoding="utf-8") == "private tmp works"
                else "wrong_value",
                "value": private_tmp.read_text(encoding="utf-8"),
            }}

            sandbox_pid_namespace_stat = os.stat("/proc/self/ns/pid")
            sandbox_pid_namespace_identity = [
                sandbox_pid_namespace_stat.st_dev,
                sandbox_pid_namespace_stat.st_ino,
            ]
            self_proc_visible = Path("/proc/self").exists()
            checks["proc_namespace"] = {{
                "classification": "permitted"
                if self_proc_visible
                and sandbox_pid_namespace_identity
                != {host_pid_namespace_identity!r}
                else "exposed",
                "host_identity": {host_pid_namespace_identity!r},
                "sandbox_identity": sandbox_pid_namespace_identity,
                "self_visible": self_proc_visible,
            }}

            dev_stat = os.stat("/dev")
            sandbox_dev_identity = [dev_stat.st_dev, dev_stat.st_ino]
            safe_dev_entries = {{
                "core": "symlink",
                "fd": "symlink",
                "full": "character",
                "mqueue": "directory",
                "null": "character",
                "ptmx": "symlink",
                "pts": "directory",
                "random": "character",
                "shm": "directory",
                "stderr": "symlink",
                "stdin": "symlink",
                "stdout": "symlink",
                "tty": "character",
                "urandom": "character",
                "zero": "character",
            }}

            def dev_entry_type(path):
                mode = os.lstat(path).st_mode
                if path.is_symlink():
                    return "symlink"
                if path.is_dir():
                    return "directory"
                if stat.S_ISCHR(mode):
                    return "character"
                return "unexpected"

            with Path("/dev/null").open("wb", buffering=0) as null_output:
                null_written = null_output.write(b"discard")
            with Path("/dev/null").open("rb", buffering=0) as null_input:
                null_read = null_input.read(1)
            dev_entries = {{
                path.name: dev_entry_type(path)
                for path in Path("/dev").iterdir()
            }}
            unexpected_dev_entries = {{
                name: entry_type
                for name, entry_type in dev_entries.items()
                if safe_dev_entries.get(name) != entry_type
            }}
            checks["dev_namespace"] = {{
                "classification": "permitted"
                if sandbox_dev_identity != {host_dev_identity!r}
                and null_written == 7
                and null_read == b""
                and not unexpected_dev_entries
                else "exposed",
                "host_identity": {host_dev_identity!r},
                "sandbox_identity": sandbox_dev_identity,
                "entries": dev_entries,
                "unexpected_entries": unexpected_dev_entries,
                "null_read_empty": null_read == b"",
                "null_written": null_written,
            }}

            secret_names = (
                "AFK_RUN_STORE",
                "AWS_SECRET_ACCESS_KEY",
                "DOCKER_HOST",
                "GH_TOKEN",
                "XDG_STATE_HOME",
            )
            checks["credential_environment"] = {{
                "classification": "denied"
                if all(name not in os.environ for name in secret_names)
                else "exposed",
                "present": [name for name in secret_names if name in os.environ],
            }}

            def connect_unix(path):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(0.2)
                    client.connect(path)

            attempt("docker_socket", lambda: connect_unix({str(docker_socket_path)!r}))
            attempt("broker_socket", lambda: connect_unix({str(broker_socket_path)!r}))
            attempt(
                "default_docker_socket",
                lambda: connect_unix("/var/run/docker.sock"),
            )
            attempt(
                "host_network",
                lambda: socket.create_connection(
                    ("127.0.0.1", __HOST_NETWORK_PORT__), timeout=0.2
                ).close(),
            )

            unexpected = {{
                name: check
                for name, check in checks.items()
                if check["classification"]
                not in {{"denied", "permitted"}}
            }}
            print(json.dumps(checks, sort_keys=True))
            if unexpected:
                print(
                    "capability boundary exposed: "
                    + json.dumps(unexpected, sort_keys=True),
                    file=sys.stderr,
                )
                raise SystemExit(97)
            """
        )

        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as host_network,
            socket.socket(socket.AF_UNIX) as docker_socket,
            socket.socket(socket.AF_UNIX) as broker_socket,
        ):
            host_network.bind(("127.0.0.1", 0))
            host_network.listen()
            docker_socket.bind(str(docker_socket_path))
            docker_socket.listen()
            broker_socket.bind(str(broker_socket_path))
            broker_socket.listen()
            host_network_port = host_network.getsockname()[1]
            probe = probe.replace("__HOST_NETWORK_PORT__", str(host_network_port))
            self.write_request(
                request,
                command=["/usr/bin/python3", "-c", probe],
            )
            self.assertFalse(result.exists())
            host_sentinels = {
                path: path.read_bytes()
                for path in (
                    request,
                    evidence,
                    run_store,
                    harness,
                    credential,
                    unrelated,
                )
            }
            completed = self.run_broker(
                request,
                result,
                env={
                    "AFK_RUN_STORE": str(self.temp / "run-store"),
                    "AWS_SECRET_ACCESS_KEY": "host-secret",
                    "DOCKER_HOST": f"unix://{docker_socket_path}",
                    "GH_TOKEN": "host-secret",
                    "XDG_STATE_HOME": str(self.temp / "state"),
                },
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result.is_file())
        self.assertFalse(result.is_symlink())
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(
            set(broker_result),
            {
                "schema_version",
                "candidate_sha",
                "status",
                "exit_code",
                "stdout",
                "stderr",
            },
        )
        self.assertEqual(broker_result["schema_version"], 1)
        self.assertEqual(broker_result["exit_code"], 0, broker_result["stderr"])
        self.assertEqual(broker_result["status"], "completed")
        self.assertEqual(broker_result["candidate_sha"], self.candidate_sha)
        checks = json.loads(broker_result["stdout"])
        for path, sentinel in host_sentinels.items():
            with self.subTest(host_sentinel=path):
                self.assertEqual(path.read_bytes(), sentinel)

        for name in host_paths:
            for operation in ("read", "write_create"):
                check_name = f"{name}_{operation}"
                with self.subTest(protected_operation=check_name):
                    self.assertEqual(
                        checks[check_name],
                        {
                            "classification": "denied",
                            "error": "FileNotFoundError",
                            "errno": errno.ENOENT,
                        },
                    )

        denial_checks = (
            "candidate_write",
            "docker_socket",
            "broker_socket",
            "default_docker_socket",
            "host_network",
        )
        for check_name in denial_checks:
            with self.subTest(denied_operation=check_name):
                self.assertEqual(checks[check_name]["classification"], "denied")
                self.assertTrue(checks[check_name]["error"])
                self.assertIsInstance(checks[check_name]["errno"], int)
        self.assertEqual(checks["candidate_write"]["errno"], errno.EROFS)

        self.assertEqual(
            checks["candidate_read"],
            {"classification": "permitted"},
        )
        self.assertEqual(
            checks["scratch_write"],
            {"classification": "permitted", "value": "scratch works"},
        )
        self.assertEqual(
            checks["private_tmp_write"],
            {"classification": "permitted", "value": "private tmp works"},
        )
        self.assertEqual(
            checks["credential_environment"],
            {"classification": "denied", "present": []},
        )
        self.assertEqual(checks["proc_namespace"]["classification"], "permitted")
        self.assertTrue(checks["proc_namespace"]["self_visible"])
        self.assertNotEqual(
            checks["proc_namespace"]["sandbox_identity"],
            checks["proc_namespace"]["host_identity"],
        )
        self.assertEqual(checks["dev_namespace"]["classification"], "permitted")
        self.assertNotEqual(
            checks["dev_namespace"]["sandbox_identity"],
            checks["dev_namespace"]["host_identity"],
        )
        self.assertEqual(checks["dev_namespace"]["entries"]["null"], "character")
        self.assertEqual(checks["dev_namespace"]["unexpected_entries"], {})
        self.assertTrue(checks["dev_namespace"]["null_read_empty"])
        self.assertEqual(checks["dev_namespace"]["null_written"], 7)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_treats_option_looking_commands_as_executable_argv(self):
        commands = [
            ["--ro-bind", "/etc", "/work/etc", "/usr/bin/true"],
            ["--share-net", "/usr/bin/true"],
        ]
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                request = self.temp / f"option-command-{index}-request.json"
                result = self.temp / f"option-command-{index}-result.json"
                self.write_request(request, command=command)

                completed = self.run_broker(request, result)

                self.assertEqual(completed.returncode, 0, completed.stderr)
                broker_result = json.loads(result.read_text(encoding="utf-8"))
                self.assertNotEqual(broker_result["exit_code"], 0)
                self.assertIn(f"execvp {command[0]}", broker_result["stderr"])

    def test_fails_closed_when_exact_candidate_snapshot_is_unavailable(self):
        request = self.temp / "snapshot-failure-request.json"
        result = self.temp / "snapshot-failure-result.json"
        result.write_text("prior success\n", encoding="utf-8")
        self.write_request(request, command=["/usr/bin/true"])
        fake_bin = self.temp / "snapshot-failure-bin"
        fake_bin.mkdir()
        (fake_bin / "bwrap").symlink_to(shutil.which("true"))
        real_git = shutil.which("git")
        listing_seen = self.temp / "snapshot-listing-seen"
        git = fake_bin / "git"
        git.write_text(
            textwrap.dedent(
                f"""
                #!{sys.executable}
                import os
                import sys
                from pathlib import Path

                if "ls-tree" in sys.argv[1:]:
                    marker = Path({str(listing_seen)!r})
                    if marker.exists():
                        raise SystemExit(1)
                    marker.touch()
                os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
                """
            ).lstrip(),
            encoding="utf-8",
        )
        git.chmod(0o755)

        completed = self.run_broker(
            request,
            result,
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "exact Candidate snapshot is unavailable\n")
        self.assertFalse(result.exists())

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is unavailable")
    def test_materializes_exact_blobs_without_archive_attribute_rewrites(self):
        (self.candidate / ".gitattributes").write_text(
            "ignored.txt export-ignore\nsubstituted.txt export-subst\n",
            encoding="utf-8",
        )
        (self.candidate / "ignored.txt").write_text(
            "committed but export-ignored\n", encoding="utf-8"
        )
        (self.candidate / "substituted.txt").write_text(
            "$Format:%H$\n", encoding="utf-8"
        )
        executable = self.candidate / "executable.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        (self.candidate / "executable-link").symlink_to("executable.sh")
        self.git("add", ".")
        self.git("commit", "-m", "add archive attributes")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        request = self.temp / "attributes-request.json"
        result = self.temp / "attributes-result.json"
        self.write_request(
            request,
            command=[
                "/usr/bin/python3",
                "-c",
                (
                    "from pathlib import Path; "
                    'print(Path("/candidate/ignored.txt").read_text(), end=""); '
                    'print(Path("/candidate/substituted.txt").read_text(), '
                    'end=""); '
                    "import os, stat; "
                    "print(oct(stat.S_IMODE(os.lstat("
                    '"/candidate/executable.sh").st_mode))); '
                    'print(os.readlink("/candidate/executable-link"))'
                ),
            ],
        )

        completed = self.run_broker(request, result)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["exit_code"], 0, broker_result["stderr"])
        self.assertEqual(
            broker_result["stdout"],
            "committed but export-ignored\n$Format:%H$\n0o755\nexecutable.sh\n",
        )

    def test_rejects_gitlinks_instead_of_materializing_an_incomplete_tree(self):
        submodule = self.temp / "submodule"
        submodule.mkdir()
        self.git("init", "-b", "main", cwd=submodule)
        self.git("config", "user.email", "afk@example.invalid", cwd=submodule)
        self.git("config", "user.name", "AFK Test", cwd=submodule)
        (submodule / "input.txt").write_text("submodule\n", encoding="utf-8")
        self.git("add", ".", cwd=submodule)
        self.git("commit", "-m", "submodule", cwd=submodule)
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(submodule),
            "nested",
        )
        self.git("commit", "-m", "add gitlink")
        self.candidate_sha = self.git("rev-parse", "HEAD")
        request = self.temp / "gitlink-request.json"
        result = self.temp / "gitlink-result.json"
        fake_bin = self.temp / "gitlink-bin"
        fake_bin.mkdir()
        (fake_bin / "bwrap").symlink_to(shutil.which("true"))
        self.write_request(request, command=["/usr/bin/true"])

        completed = self.run_broker(
            request, result, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"}
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            completed.stderr,
            "exact Candidate snapshot contains an unsupported entry\n",
        )
        self.assertFalse(result.exists())

    def write_request(
        self,
        path,
        *,
        command,
        candidate_path=None,
        candidate_sha=None,
        timeout_seconds=None,
        output_byte_limit=None,
        execution=None,
    ):
        request = {
            "schema_version": 1,
            "candidate_sha": (
                self.candidate_sha if candidate_sha is None else candidate_sha
            ),
            "candidate_path": (
                str(self.candidate) if candidate_path is None else candidate_path
            ),
            "command": command,
        }
        if timeout_seconds is not None:
            request["timeout_seconds"] = timeout_seconds
        if output_byte_limit is not None:
            request["output_byte_limit"] = output_byte_limit
        if execution is not None:
            request["execution"] = execution
        path.write_text(
            json.dumps(request),
            encoding="utf-8",
        )

    def run_broker(self, request, result, *, env=None, input_text=None):
        broker_env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        broker_env.update(env or {})
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "afk.candidate_broker",
                "--request",
                str(request),
                "--result",
                str(result),
            ],
            cwd=ROOT,
            env=broker_env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def git(self, *args, cwd=None):
        completed = subprocess.run(
            ["git", *args],
            cwd=self.candidate if cwd is None else cwd,
            env=self.git_environment,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
