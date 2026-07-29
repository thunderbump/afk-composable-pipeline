import json
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.retrospective_attempt import (  # noqa: E402
    RETROSPECTIVE_PROMPT,
    RETROSPECTIVE_TIMEOUT_SECONDS,
    retrospective_evidence_identity,
    run_retrospective_attempt,
)
from afk.retrospective_result import (  # noqa: E402
    CATEGORIES,
    CONFIDENCE,
    PRIORITIES,
    SCOPES,
    normalize_retrospective_result,
)
from afk.run_store import RunStore, RunStoreBusy, RunStoreError  # noqa: E402
from afk.run_summary import build_run_summary  # noqa: E402


BASE_SHA = "a" * 40


class RetrospectiveAttemptTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = RunStore(self.root / "afk")
        self.store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={"secret": "must-not-cross-the-boundary"},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        self.store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": "validated"},
            recorded_at="2026-07-28T10:01:00Z",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_runs_one_fresh_contained_analysis_and_reuses_its_sealed_outcome(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        analyzer = self.analyzer(
            f"""
            import json, sys
            request = json.load(sys.stdin)
            print(json.dumps({analysis!r}))
            print("REQUEST=" + json.dumps(request, sort_keys=True), file=sys.stderr)
            """
        )

        first = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_executable=str(analyzer),
        )
        second = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_executable=str(self.root / "must-not-run"),
        )

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "empty")
        self.assertFalse(first["warning"])
        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        self.assertTrue(self.store.verify_evidence("run-001", evidence))
        files = self.evidence_files(evidence)
        self.assertEqual(
            files,
            {
                "analysis.json",
                "command.json",
                "input.json",
                "manifest.json",
                "outcome.json",
                "result.json",
                "stderr.log",
                "stdout.log",
            },
        )
        command = self.evidence_json(evidence, "command.json")
        self.assertEqual(command["timeout_seconds"], RETROSPECTIVE_TIMEOUT_SECONDS)
        self.assertEqual(
            command["policy"],
            {
                "control_plane_network": "model-api-only",
                "filesystem": "minimal-read",
                "interactive": False,
                "network": "disabled",
                "permission_profile": "retrospective-analysis",
                "runtime_home": "isolated",
                "session": "fresh",
            },
        )
        self.assertNotIn("--sandbox", command["argv"])
        self.assertIn("--ephemeral", command["argv"])
        self.assertEqual(command["argv"][1], "exec")
        request = self.evidence_json(evidence, "input.json")
        serialized_request = json.dumps(request)
        self.assertEqual(request["run"]["run_id"], "run-001")
        self.assertNotIn("must-not-cross-the-boundary", serialized_request)
        self.assertNotIn("repository source", serialized_request)

    def test_secret_shaped_executable_is_redacted_from_effect_and_evidence(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        observed_argv = self.root / "observed-argv.json"
        analyzer = self.analyzer(
            f"""
            import json, pathlib, sys
            pathlib.Path({str(observed_argv)!r}).write_text(
                json.dumps(sys.argv), encoding="utf-8"
            )
            print({json.dumps(json.dumps(analysis))})
            """
        )
        secret_shaped_executable = self.root / "--token"
        analyzer.rename(secret_shaped_executable)

        with patch.dict(
            "os.environ",
            {"PATH": f"{self.root}:/usr/bin:/bin"},
            clear=False,
        ):
            outcome = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=secret_shaped_executable.name,
            )

        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        command = self.evidence_json(evidence, "command.json")
        effect = self.store.effect("run-001", "retrospective-analysis-2")
        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(
            json.loads(observed_argv.read_text(encoding="utf-8"))[1],
            "exec",
        )
        self.assertEqual(command["argv"][:2], ["--token", "[REDACTED]"])
        self.assertEqual(effect["intended"]["command"], command)
        self.assertNotIn('"exec"', json.dumps(command))
        self.assertNotIn('"exec"', json.dumps(effect))

    def test_uses_private_auth_only_runtime_and_least_privilege_codex_profile(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        configured_codex_home = self.root / "configured-codex"
        configured_codex_home.mkdir(mode=0o700)
        auth = configured_codex_home / "auth.json"
        auth.write_text('{"access_token":"runtime-secret"}\n', encoding="utf-8")
        auth.chmod(0o600)
        sentinel = configured_codex_home / "must-not-change"
        sentinel.write_text("original\n", encoding="utf-8")
        observation = self.root / "runtime-observation.json"
        analyzer = self.analyzer(
            f"""
            import json, os, pathlib, sys, tomllib
            codex_home = pathlib.Path(os.environ["CODEX_HOME"])
            home = pathlib.Path(os.environ["HOME"])
            observed = {{
                "argv": sys.argv,
                "auth_present": (codex_home / "auth.json").is_file(),
                "codex_entries": sorted(path.name for path in codex_home.iterdir()),
                "config": tomllib.loads(
                    (codex_home / "config.toml").read_text(encoding="utf-8")
                ),
                "codex_home": str(codex_home),
                "home": str(home),
            }}
            pathlib.Path({str(observation)!r}).write_text(
                json.dumps(observed), encoding="utf-8"
            )
            print(json.dumps({analysis!r}))
            """
        )

        with patch.dict(
            "os.environ",
            {"CODEX_HOME": str(configured_codex_home)},
            clear=False,
        ):
            outcome = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=str(analyzer),
            )

        observed = json.loads(observation.read_text(encoding="utf-8"))
        config = observed["config"]
        profile = config["permissions"]["retrospective-analysis"]
        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(observed["argv"][1], "exec")
        self.assertTrue(observed["auth_present"])
        self.assertEqual(observed["codex_entries"], ["auth.json", "config.toml"])
        self.assertEqual(config["default_permissions"], "retrospective-analysis")
        self.assertEqual(config["web_search"], "disabled")
        self.assertEqual(profile["filesystem"], {":minimal": "read"})
        self.assertFalse(profile["network"]["enabled"])
        self.assertEqual(config["approval_policy"], "never")
        self.assertNotIn("mcp_servers", config)
        for feature in (
            "apps",
            "browser_use",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "computer_use",
            "enable_mcp_apps",
            "in_app_browser",
            "multi_agent",
            "multi_agent_v2",
            "plugins",
            "plugin_sharing",
            "remote_plugin",
            "shell_tool",
            "standalone_web_search",
            "unified_exec",
        ):
            self.assertFalse(config["features"][feature], feature)
        self.assertIn("--strict-config", observed["argv"])
        self.assertIn("--ephemeral", observed["argv"])
        self.assertIn("--ignore-rules", observed["argv"])
        self.assertNotIn("--sandbox", observed["argv"])
        self.assertIn('approval_policy="never"', observed["argv"])
        self.assertFalse(Path(observed["codex_home"]).exists())
        self.assertFalse(Path(observed["home"]).exists())
        self.assertEqual(
            auth.read_text(encoding="utf-8"),
            '{"access_token":"runtime-secret"}\n',
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(
            {path.name for path in configured_codex_home.iterdir()},
            {"auth.json", "must-not-change"},
        )

    def test_rejects_injected_codex_arguments_before_launch(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Codex executable is invalid",
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=["codex", "--dangerously-bypass-approvals"],
            )

    def test_concurrent_callers_start_exactly_one_analysis_process(self):
        analysis = self.empty_analysis(summary="No actionable findings.")
        starts = self.root / "analysis-starts"
        analyzer = self.analyzer(
            f"""
            import json, time
            with open({str(starts)!r}, "a", encoding="utf-8") as stream:
                stream.write("started\\n")
                stream.flush()
            time.sleep(0.25)
            print(json.dumps({analysis!r}))
            """
        )
        result = []

        worker = threading.Thread(
            target=lambda: result.append(
                run_retrospective_attempt(
                    self.store,
                    "run-001",
                    episode_sequence=2,
                    codex_executable=str(analyzer),
                )
            )
        )
        worker.start()
        deadline = time.monotonic() + 2
        while not starts.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        with self.assertRaises(RunStoreBusy):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=str(self.root / "must-not-run"),
            )
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["status"], "empty")
        self.assertEqual(starts.read_text(encoding="utf-8"), "started\n")

    def test_stale_prepared_claim_recovers_partial_evidence_without_relaunch(self):
        class SimulatedCrash(RuntimeError):
            pass

        analysis = self.empty_analysis(summary="No actionable findings.")
        starts = self.root / "analysis-starts"
        analyzer = self.analyzer(
            f"""
            import json
            with open({str(starts)!r}, "a", encoding="utf-8") as stream:
                stream.write("started\\n")
            print(json.dumps({analysis!r}))
            """
        )
        original_write = self.store.write_evidence_text
        writes = 0

        def crash_after_partial_evidence(*args, **kwargs):
            nonlocal writes
            result = original_write(*args, **kwargs)
            writes += 1
            if writes == 1:
                raise SimulatedCrash("crash during evidence persistence")
            return result

        with (
            patch.object(
                self.store,
                "write_evidence_text",
                side_effect=crash_after_partial_evidence,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=str(analyzer),
            )

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_executable=str(self.root / "must-not-run"),
        )

        self.assertEqual(outcome["status"], "interrupted")
        self.assertTrue(outcome["warning"])
        self.assertEqual(starts.read_text(encoding="utf-8"), "started\n")
        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        self.assertTrue(self.store.verify_evidence("run-001", evidence))

    def test_stale_claim_finishes_durable_result_without_relaunch(self):
        class SimulatedCrash(RuntimeError):
            pass

        analysis = self.empty_analysis(summary="No actionable findings.")
        starts = self.root / "analysis-starts"
        analyzer = self.analyzer(
            f"""
            import json
            with open({str(starts)!r}, "a", encoding="utf-8") as stream:
                stream.write("started\\n")
            print(json.dumps({analysis!r}))
            """
        )
        evidence = retrospective_evidence_identity(
            self.store, "run-001", episode_sequence=2
        )
        original_seal = self.store.seal_evidence
        crashed = False

        def crash_before_attempt_seal(run_id, relative_directory):
            nonlocal crashed
            if relative_directory == evidence and not crashed:
                crashed = True
                raise SimulatedCrash("crash after durable result")
            return original_seal(run_id, relative_directory)

        with (
            patch.object(
                self.store,
                "seal_evidence",
                side_effect=crash_before_attempt_seal,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=str(analyzer),
            )

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_executable=str(self.root / "must-not-run"),
        )

        self.assertEqual(outcome["status"], "empty")
        self.assertFalse(outcome["warning"])
        self.assertEqual(starts.read_text(encoding="utf-8"), "started\n")
        self.assertTrue(self.store.verify_evidence("run-001", evidence))

    def test_malformed_sealed_outcomes_are_rejected(self):
        def base():
            return {
                "schema_version": 1,
                "run_id": "run-001",
                "episode_sequence": 2,
                "status": "empty",
                "warning": False,
                "process_findings_count": 0,
                "improvement_proposals_count": 0,
            }

        cases = []
        for label, mutate in (
            ("boolean schema", lambda value: value.update(schema_version=True)),
            (
                "boolean count",
                lambda value: value.update(process_findings_count=False),
            ),
            ("non-string status", lambda value: value.update(status=[])),
            ("missing field", lambda value: value.pop("warning")),
            ("extra field", lambda value: value.update(extra="unexpected")),
            ("success warning", lambda value: value.update(warning=True)),
            (
                "empty with findings",
                lambda value: value.update(process_findings_count=1),
            ),
            ("passed without findings", lambda value: value.update(status="passed")),
            (
                "warning without summary",
                lambda value: value.update(status="invalid", warning=True),
            ),
        ):
            value = base()
            mutate(value)
            cases.append((label, value))

        for index, (label, value) in enumerate(cases, start=1):
            with self.subTest(label=label):
                store, sequence = self.distinct_episode(index)
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                store.write_evidence_value(
                    "run-001",
                    f"{evidence}/result.json",
                    value,
                )
                store.seal_evidence("run-001", evidence)

                with self.assertRaisesRegex(
                    RunStoreError,
                    "sealed retrospective outcome is invalid",
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                        codex_executable=str(self.root / "must-not-run"),
                    )

    def test_malformed_partial_outcomes_are_rejected_without_relaunch(self):
        cases = (
            {
                "schema_version": 1,
                "run_id": "run-001",
                "episode_sequence": 2,
                "status": "interrupted",
                "warning": True,
                "process_findings_count": 1,
                "improvement_proposals_count": 0,
                "warning_summary": "contradictory count",
            },
            {
                "schema_version": 1,
                "run_id": "run-001",
                "episode_sequence": 2,
                "status": "unavailable",
                "warning": True,
                "process_findings_count": 0,
                "improvement_proposals_count": 0,
                "warning_summary": "x" * 1025,
            },
        )
        for index, malformed in enumerate(cases, start=1):
            with self.subTest(index=index):
                store, sequence = self.distinct_episode(index + 20)
                analysis = self.empty_analysis(summary="No actionable findings.")
                analyzer = self.analyzer(f"print({json.dumps(json.dumps(analysis))})")
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                original_seal = store.seal_evidence

                def crash_before_attempt_seal(run_id, relative_directory):
                    if relative_directory == evidence:
                        raise RuntimeError("crash after durable result")
                    return original_seal(run_id, relative_directory)

                with (
                    patch.object(
                        store,
                        "seal_evidence",
                        side_effect=crash_before_attempt_seal,
                    ),
                    self.assertRaisesRegex(RuntimeError, "crash after durable result"),
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                        codex_executable=str(analyzer),
                    )
                result_path = store.root / "runs" / "run-001" / evidence / "result.json"
                result_path.write_text(
                    json.dumps(malformed, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    RunStoreError,
                    "sealed retrospective outcome is invalid",
                ):
                    run_retrospective_attempt(
                        store,
                        "run-001",
                        episode_sequence=sequence,
                        codex_executable=str(self.root / "must-not-run"),
                    )

    def test_nonempty_valid_analysis_is_passed(self):
        analysis = self.empty_analysis(summary="One process issue was found.")
        analysis["process_findings"] = [
            {
                "id": "finding-1",
                "category": "orchestration",
                "title": "Attention interrupted the run",
                "evidence": [
                    {"artifact": "events.jsonl", "event_sequence": 2},
                ],
                "impact": "An operator had to resume it.",
                "confidence": "high",
            }
        ]
        analyzer = self.analyzer(f"print({json.dumps(json.dumps(analysis))})")

        outcome = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_executable=str(analyzer),
        )

        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(outcome["process_findings_count"], 1)
        self.assertEqual(outcome["improvement_proposals_count"], 0)

    def test_prompt_advertises_every_validator_vocabulary_value(self):
        summary = build_run_summary(self.store, "run-001", episode_sequence=2)
        analysis = self.empty_analysis(summary="One process issue was found.")
        analysis["process_findings"] = [
            {
                "id": "finding-1",
                "category": CATEGORIES[0],
                "title": "Attention interrupted the run",
                "evidence": [
                    {"artifact": "events.jsonl", "event_sequence": 2},
                ],
                "impact": "An operator had to resume it.",
                "confidence": CONFIDENCE[0],
            }
        ]
        analysis["improvement_proposals"] = [
            {
                "id": "proposal-1",
                "addresses": ["finding-1"],
                "scope": SCOPES[0],
                "priority": PRIORITIES[0],
                "title": "Reduce interruptions",
                "rationale": "The Run needed operator attention.",
                "suggested_change": "Improve interruption recovery.",
                "requires_human_decision": True,
            }
        ]
        cases = (
            ("category", CATEGORIES),
            ("confidence", CONFIDENCE),
            ("scope", SCOPES),
            ("priority", PRIORITIES),
        )

        for field, values in cases:
            target = (
                analysis["process_findings"][0]
                if field in {"category", "confidence"}
                else analysis["improvement_proposals"][0]
            )
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertIn(value, RETROSPECTIVE_PROMPT)
                    target[field] = value
                    self.assertEqual(
                        normalize_retrospective_result(summary, analysis),
                        analysis,
                    )

    def test_completed_episode_gets_its_own_stable_identity(self):
        store = RunStore(self.root / "completed" / "afk")
        store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        store.append_event(
            "run-001",
            "run.completed",
            state="completed",
            data={},
            recorded_at="2026-07-28T10:01:00Z",
        )
        analysis = self.empty_analysis(summary="No actionable findings.")
        analysis["terminal_outcome"] = "completed"
        analyzer = self.analyzer(f"print({json.dumps(json.dumps(analysis))})")

        outcome = run_retrospective_attempt(
            store,
            "run-001",
            episode_sequence=2,
            codex_executable=str(analyzer),
        )

        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(
            retrospective_evidence_identity(
                store,
                "run-001",
                episode_sequence=2,
            ),
            "retrospective/completed-2",
        )

    def test_invalid_and_unavailable_results_are_sealed_warnings(self):
        cases = (
            ("invalid", str(self.analyzer("print('not-json')"))),
            ("unavailable", str(self.root / "missing-codex")),
            (
                "unavailable",
                str(self.analyzer("raise SystemExit(7)")),
            ),
        )
        for index, (expected, command) in enumerate(cases, start=1):
            with self.subTest(expected=expected, index=index):
                store, sequence = self.distinct_episode(index)
                outcome = run_retrospective_attempt(
                    store,
                    "run-001",
                    episode_sequence=sequence,
                    codex_executable=command,
                )
                self.assertEqual(outcome["status"], expected)
                self.assertTrue(outcome["warning"])
                evidence = retrospective_evidence_identity(
                    store, "run-001", episode_sequence=sequence
                )
                self.assertTrue(store.verify_evidence("run-001", evidence))
                self.assertNotIn(
                    "analysis.json",
                    self.evidence_files(evidence, store=store),
                )

    def test_timeout_is_interrupted_and_never_retried(self):
        analyzer = self.analyzer(
            """
            import subprocess, time
            subprocess.Popen(["sleep", "60"])
            time.sleep(60)
            """
        )
        with patch("afk.retrospective_attempt.RETROSPECTIVE_TIMEOUT_SECONDS", 0.05):
            first = run_retrospective_attempt(
                self.store,
                "run-001",
                episode_sequence=2,
                codex_executable=str(analyzer),
            )

        second = run_retrospective_attempt(
            self.store,
            "run-001",
            episode_sequence=2,
            codex_executable=str(self.root / "must-not-run"),
        )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "interrupted")
        self.assertTrue(first["warning"])

    def empty_analysis(self, *, summary):
        return {
            "schema_version": 1,
            "run_id": "run-001",
            "terminal_outcome": "attention_required",
            "summary": summary,
            "process_findings": [],
            "improvement_proposals": [],
        }

    def analyzer(self, program):
        path = self.root / f"codex-{len(list(self.root.glob('codex-*')))}"
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(program).strip() + "\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def distinct_episode(self, index):
        root = self.root / f"case-{index}"
        store = RunStore(root / "afk")
        store.create_run(
            bead_id="central-bhap.8.3",
            repository="https://example.invalid/acme/pipeline.git",
            base_branch="main",
            base_sha=BASE_SHA,
            start_request={},
            run_id="run-001",
            created_at="2026-07-28T10:00:00Z",
        )
        store.append_event(
            "run-001",
            "run.attention_required",
            state="attention_required",
            data={"checkpoint": f"case-{index}"},
            recorded_at="2026-07-28T10:01:00Z",
        )
        return store, 2

    def evidence_files(self, evidence, *, store=None):
        selected = store or self.store
        directory = selected.root / "runs" / "run-001" / evidence
        return {path.name for path in directory.iterdir()}

    def evidence_json(self, evidence, name):
        path = self.store.root / "runs" / "run-001" / evidence / name
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
