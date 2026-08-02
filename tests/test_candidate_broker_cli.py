import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
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
        self.assertEqual(completed.stderr, "exact Candidate snapshot is unavailable\n")
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
                "status": "completed",
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
        probe = textwrap.dedent(
            f"""
            import json
            import os
            import socket
            import sys
            from pathlib import Path

            checks = {{}}

            def denied(name, operation):
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
                denied(name, lambda value=value: Path(value).read_bytes())
            denied(
                "host_write",
                lambda: Path(host_paths["unrelated_host_file"]).write_text(
                    "candidate write", encoding="utf-8"
                ),
            )
            denied(
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
                else "wrong_value"
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
                else "exposed"
            }}

            def connect_unix(path):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(0.2)
                    client.connect(path)

            denied("docker_socket", lambda: connect_unix({str(docker_socket_path)!r}))
            denied("broker_socket", lambda: connect_unix({str(broker_socket_path)!r}))
            denied(
                "default_docker_socket",
                lambda: connect_unix("/var/run/docker.sock"),
            )
            denied(
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
        broker_result = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(broker_result["exit_code"], 0, broker_result["stderr"])
        checks = json.loads(broker_result["stdout"])
        self.assertEqual(checks["candidate_read"]["classification"], "permitted")
        self.assertEqual(checks["scratch_write"]["classification"], "permitted")
        denied_checks = set(checks) - {"candidate_read", "scratch_write"}
        self.assertTrue(denied_checks)
        self.assertEqual(
            {checks[name]["classification"] for name in denied_checks},
            {"denied"},
        )

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

                if sys.argv[1:2] == ["ls-tree"]:
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

    def write_request(self, path, *, command, candidate_path=None, candidate_sha=None):
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate_sha": (
                        self.candidate_sha if candidate_sha is None else candidate_sha
                    ),
                    "candidate_path": (
                        str(self.candidate)
                        if candidate_path is None
                        else candidate_path
                    ),
                    "command": command,
                }
            ),
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
