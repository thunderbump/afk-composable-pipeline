import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from afk.pi_workers import (
    PONYTAIL_EXTENSION_SOURCE,
    PONYTAIL_PACKAGE_NAME,
    build_pi_real_worker_agent,
)


class PiWorkersTest(unittest.TestCase):
    def test_build_pi_real_worker_agent_returns_safe_real_agent_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout_path = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"
            api_key_file = temp_path / "pi-api-key.txt"

            checkout_path.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()
            api_key_file.write_text("secret\n", encoding="utf-8")

            agent = build_pi_real_worker_agent(
                pi_bin="/opt/pi/bin/pi",
                provider="openai-codex",
                model="gpt-5.4-mini",
                thinking="high",
                ponytail_extension=PONYTAIL_PACKAGE_NAME,
                codex_home=str(codex_home),
                config_home=str(config_home),
                pi_config_home=str(pi_config_home),
                pi_coding_agent_dir=str(pi_coding_agent_dir),
                checkout_path=checkout_path,
                wrapper_secret_file=str(api_key_file),
            )

            self.assertEqual(agent["type"], "real-agent-command")
            self.assertEqual(agent["provider"], "openai-codex")
            self.assertEqual(
                agent["command"],
                [
                    "/opt/pi/bin/pi",
                    "-p",
                    "{prompt}",
                    "--provider",
                    "openai-codex",
                    "--model",
                    "gpt-5.4-mini",
                    "--thinking",
                    "high",
                    "--extension",
                    "ponytail",
                ],
            )
            self.assertEqual(agent["result_path"], "agent-result.json")
            self.assertEqual(agent["codex_home"], str(codex_home))
            self.assertEqual(agent["config_home"], str(config_home))
            self.assertEqual(
                agent["env"],
                {
                    "PI_CONFIG_HOME": str(pi_config_home),
                    "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
                },
            )
            self.assertEqual(agent["wrapper_secret_files"], {"primary": str(api_key_file)})

    def test_build_pi_real_worker_agent_rejects_models_above_gpt_5_4(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout_path = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"

            checkout_path.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

            with self.assertRaisesRegex(ValueError, "gpt-5.4 or lower"):
                build_pi_real_worker_agent(
                    pi_bin="/opt/pi/bin/pi",
                    provider="openai-codex",
                    model="gpt-5.5",
                    codex_home=str(codex_home),
                    config_home=str(config_home),
                    pi_config_home=str(pi_config_home),
                    checkout_path=checkout_path,
                )

    def test_build_pi_real_worker_agent_requires_core_mount_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout_path = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"

            checkout_path.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()

            cases = [
                ("codex_home", None, str(config_home), str(pi_config_home), "agent.codex_home is required"),
                ("config_home", str(codex_home), None, str(pi_config_home), "agent.config_home is required"),
                (
                    "pi_config_home",
                    str(codex_home),
                    str(config_home),
                    None,
                    "agent.env.PI_CONFIG_HOME is required",
                ),
            ]
            for _, raw_codex_home, raw_config_home, raw_pi_config_home, expected in cases:
                with self.subTest(expected=expected):
                    with self.assertRaisesRegex(ValueError, expected):
                        build_pi_real_worker_agent(
                            pi_bin="/opt/pi/bin/pi",
                            provider="openai-codex",
                            model="gpt-5.4-mini",
                            codex_home=raw_codex_home,
                            config_home=raw_config_home,
                            pi_config_home=raw_pi_config_home,
                            pi_coding_agent_dir=str(pi_coding_agent_dir),
                            checkout_path=checkout_path,
                        )

    def test_build_pi_real_worker_agent_requires_pi_coding_agent_dir_for_openai_codex(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout_path = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"

            checkout_path.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()

            with self.assertRaisesRegex(ValueError, "agent.env.PI_CODING_AGENT_DIR is required"):
                build_pi_real_worker_agent(
                    pi_bin="/opt/pi/bin/pi",
                    provider="openai-codex",
                    model="gpt-5.4-mini",
                    codex_home=str(codex_home),
                    config_home=str(config_home),
                    pi_config_home=str(pi_config_home),
                    checkout_path=checkout_path,
                )

    def test_build_pi_real_worker_agent_supports_one_shot_ponytail_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            checkout_path = temp_path / "checkout"
            codex_home = temp_path / "codex-home"
            config_home = temp_path / "xdg-config"
            pi_config_home = temp_path / "pi-config"
            pi_coding_agent_dir = temp_path / "pi-coding-agent"

            checkout_path.mkdir()
            codex_home.mkdir()
            config_home.mkdir()
            pi_config_home.mkdir()
            pi_coding_agent_dir.mkdir()

            agent = build_pi_real_worker_agent(
                pi_bin="/opt/pi/bin/pi",
                provider="openai-codex",
                model="gpt-5.4",
                codex_home=str(codex_home),
                config_home=str(config_home),
                pi_config_home=str(pi_config_home),
                pi_coding_agent_dir=str(pi_coding_agent_dir),
                checkout_path=checkout_path,
                ponytail_extension_source=PONYTAIL_EXTENSION_SOURCE,
            )

            self.assertEqual(agent["command"][-2:], ["--extension", PONYTAIL_EXTENSION_SOURCE])


if __name__ == "__main__":
    unittest.main()
