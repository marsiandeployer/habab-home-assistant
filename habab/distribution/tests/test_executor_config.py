"""Task executor selection is derived from the selected AI provider only."""

import json
import unittest

from homeassistant.components.habab_home.executor_config import executor_status, selected_executor


class ExecutorSelectionTests(unittest.TestCase):
    def test_codex_uses_openai_and_habab_provider_configurations(self):
        for provider in ("openai", "habab"):
            with self.subTest(provider=provider):
                settings = {"provider": provider, "api_key": f"fixture-{provider}-key"}
                self.assertEqual(selected_executor(settings), "codex")
                self.assertEqual(executor_status(settings), {
                    "executor": "codex", "provider": provider,
                    "model": "gpt-6-luna", "service_tier": "fast",
                })
                self.assertNotIn(settings["api_key"], json.dumps(executor_status(settings)))

    def test_deepseek_gemini_unknown_and_missing_provider_use_opencode(self):
        for settings in (
            {"provider": "deepseek", "api_key": "fixture-deepseek-key"},
            {"provider": "gemini", "api_key": "fixture-gemini-key"},
            {"provider": "other", "api_key": "fixture-other-key"},
            {}, None,
        ):
            with self.subTest(settings=settings):
                self.assertEqual(selected_executor(settings), "opencode")
                result = executor_status(settings)
                self.assertEqual(result["executor"], "opencode")
                self.assertIsNone(result["model"])
                self.assertIsNone(result["service_tier"])
                if isinstance(settings, dict) and "api_key" in settings:
                    self.assertNotIn(settings["api_key"], json.dumps(result))

