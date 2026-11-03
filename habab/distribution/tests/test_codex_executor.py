"""Real subprocess fixtures for Codex isolation, JSONL, cancellation and history."""

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

# These executor tests also run outside Home Assistant, without provider calls.
PACKAGE = "habab_codex_fixture"
if PACKAGE not in sys.modules:
    package = ModuleType(PACKAGE)
    package.__path__ = [str(Path(__file__).parents[1] / "src" / "habab_home")]
    sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(
    PACKAGE + ".codex_executor", Path(__file__).parents[1] / "src/habab_home/codex_executor.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
outcomes = sys.modules[PACKAGE + ".action_outcome"]

OUTCOME = {"protocol": outcomes.OUTCOME_PROTOCOL, "kind": "answer",
           "text": "Проверено", "todo_additions": []}


class CodexExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = "fixture-codex-secret"
        self.secrets = set()
        self.executor = module.CodexExecutor(self.root, self.clean, self.secrets.add)
        self.binary = self.root / "fake-codex"
        self.settings = {"api_key": self.key, "model": "gpt-6-luna", "service_tier": "fast"}
        self.settings["provider"] = "openai"
        self.environment = patch.dict(os.environ, {"HABAB_CODEX_BINARY": str(self.binary),
            "FOREIGN_SECRET": "host-credential", "CODEX_HOME": "/foreign/codex"})
        self.environment.start()

    async def asyncTearDown(self):
        await self.executor.stop()
        self.environment.stop()
        self.temp.cleanup()

    def clean(self, value):
        return value.replace(self.key, "[redacted]")[:32000]

    async def executor_job(self, fn, *args):
        return fn(*args)

    def fixture(self, body):
        self.binary.write_text("#!" + sys.executable + "\n" + body)
        self.binary.chmod(0o700)
        Path(str(self.binary) + ".sha256").write_text(hashlib.sha256(self.binary.read_bytes()).hexdigest())

    async def run_cli(self, **kwargs):
        return await self.executor.run("Текущий запрос", self.settings, "fixture-control",
            lambda *args: None, self.executor_job, **kwargs)

    def events(self, items):
        return "import json,sys\nsys.stdin.read()\n" + "\n".join(
            "print(" + repr(json.dumps(item)) + ", flush=True)" for item in items)

    def final_event(self, value=OUTCOME):
        return {"type": "item.completed", "item": {"id": "item_final",
                "type": "agent_message", "text": json.dumps(value)}}

    async def test_actual_jsonl_trace_and_isolated_child_environment(self):
        capture = self.root / "capture.json"
        self.fixture("import os,json,sys\n"
            + "json.dump({'env':dict(os.environ),'args':sys.argv,'prompt':sys.stdin.read()},open("
            + repr(str(capture)) + ",'w'))\n"
            + "print(" + repr(json.dumps({"type": "thread.started", "thread_id": "thread-1"})) + ")\n"
            + "print(" + repr(json.dumps({"type": "item.completed", "item": {
                "id": "item_0", "type": "command_execution", "command": "/bin/bash -lc 'habab-ha devices'",
                "aggregated_output": self.key, "exit_code": 0, "status": "completed"}})) + ")\n"
            + "print(" + repr(json.dumps(self.final_event())) + ")\n"
            + "print('{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":3}}')\n")
        outcome, parts = await self.run_cli()
        self.assertEqual(outcome, OUTCOME)
        self.assertEqual(parts[0]["state"]["input"]["command"], "habab-ha devices")
        self.assertEqual(parts[0]["state"]["output"], "[redacted]")
        child = json.loads(capture.read_text())
        self.assertEqual(child["env"]["CODEX_API_KEY"], self.key)
        self.assertNotIn("FOREIGN_SECRET", child["env"])
        self.assertNotEqual(child["env"]["HOME"], os.environ.get("HOME"))
        self.assertEqual(child["env"]["CODEX_HOME"], str(self.root / "codex/state"))
        self.assertNotIn(self.key, child["args"])
        self.assertNotIn('model_provider="habab"', child["args"])
        for argument in ("--ephemeral", "--ignore-user-config", "--ignore-rules",
                         'service_tier="fast"', "features.fast_mode=true",
                         'model_reasoning_effort="medium"', "--output-schema"):
            self.assertIn(argument, child["args"])
        self.assertEqual(child["prompt"], "Текущий запрос")
        self.assertIn(self.key, self.secrets)
        self.assertIsNone(self.executor.process)

    async def test_long_line_above_streamreader_default_is_bounded(self):
        event = {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
                 "command": "printf fixture", "aggregated_output": "a" * 100000,
                 "exit_code": 0}}
        self.fixture(self.events([event, self.final_event()]))
        _, parts = await self.run_cli()
        self.assertEqual(len(parts[0]["state"]["output"]), 32000)

    async def test_habab_uses_installation_key_only_at_fixed_managed_responses_origin(self):
        capture = self.root / "managed.json"
        self.settings.update(provider="habab", api_key="fixture-installation-key",
                             base_url="https://untrusted.example/ignored")
        self.fixture("import os,json,sys\n"
            + "json.dump({'env':dict(os.environ),'args':sys.argv},open("
            + repr(str(capture)) + ",'w'))\n"
            + "sys.stdin.read()\nprint(" + repr(json.dumps(self.final_event())) + ")\n")
        await self.run_cli()
        child = json.loads(capture.read_text())
        self.assertEqual(child["env"]["CODEX_API_KEY"], "fixture-installation-key")
        self.assertIn('model_provider="habab"', child["args"])
        self.assertIn('model_providers.habab.wire_api="responses"', child["args"])
        self.assertIn('model_providers.habab.env_key="CODEX_API_KEY"', child["args"])
        self.assertIn('model_providers.habab.requires_openai_auth=false', child["args"])
        self.assertIn('model_providers.habab.base_url="' + module.HABAB_CODEX_BASE_URL + '"', child["args"])
        self.assertNotIn("untrusted.example", " ".join(child["args"]))
        self.assertNotIn("fixture-installation-key", " ".join(child["args"]))

    async def test_timeout_terminates_process_and_clears_active_child(self):
        started = {"type": "item.started", "item": {"id": "item_0",
            "type": "command_execution", "command": "habab-ha devices", "aggregated_output": ""}}
        self.fixture(self.events([started]) + "\nimport time\ntime.sleep(30)\n")
        with self.assertRaises(module.CodexError) as result:
            await self.run_cli(timeout=.08)
        self.assertEqual(result.exception.code, "runtime_timeout")
        self.assertIsNone(self.executor.process)
        self.assertEqual(self.executor.last_parts[0]["state"]["status"], "running")

    async def test_cancellation_terminates_only_owned_process_group(self):
        self.fixture("import time,sys\nsys.stdin.read()\ntime.sleep(30)\n")
        task = asyncio.create_task(self.run_cli())
        for _ in range(100):
            if self.executor.process is not None:
                break
            await asyncio.sleep(.005)
        process = self.executor.process
        self.assertIsNotNone(process)
        self.assertEqual(os.getpgid(process.pid), process.pid)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(self.executor.process)

    async def test_missing_binary_and_missing_key(self):
        with self.assertRaises(module.CodexError) as result:
            await self.run_cli()
        self.assertEqual(result.exception.code, "not_installed")
        self.settings["api_key"] = ""
        with self.assertRaises(module.CodexError) as result:
            await self.run_cli()
        self.assertEqual(result.exception.code, "provider_required")

    async def test_error_trace_is_redacted_without_fallback(self):
        self.fixture(self.events([{"type": "turn.failed", "error": {"message": self.key}}]))
        with self.assertRaises(module.CodexError) as result:
            await self.run_cli()
        self.assertEqual(result.exception.code, "model_error")
        self.assertNotIn(self.key, result.exception.detail)

    async def test_invalid_schema_and_invalid_jsonl(self):
        for event in ({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}):
            self.fixture(self.events([event]))
            with self.assertRaises(module.CodexError):
                await self.run_cli()
        self.fixture("print('not-json')\n")
        with self.assertRaises(module.CodexError):
            await self.run_cli()

    async def test_hash_verifier_is_used_when_pinned(self):
        self.fixture(self.events([self.final_event()]))
        calls = []
        with patch.dict(os.environ, {"HABAB_CODEX_SHA256": "a" * 64}):
            await self.executor.run("test", self.settings, "control",
                lambda *args: calls.append(args), self.executor_job)
        self.assertEqual(calls, [(str(self.binary), "a" * 64)])

    def test_synthetic_result_becomes_assistant_with_original_request_parent(self):
        part = module.result_part("user-1", OUTCOME, [], self.clean)
        record = {"info": {"role": "user", "id": "marker-1"}, "parts": [part]}
        result = module.normalize_history([record])
        self.assertEqual(result[0]["info"]["id"], "marker-1")
        self.assertEqual(result[0]["info"]["role"], "assistant")
        self.assertEqual(result[0]["info"]["parentID"], "user-1")
        self.assertEqual(result[0]["info"]["structured"], OUTCOME)
        part["synthetic"] = False
        self.assertEqual(module.normalize_history([record]), [])

    def test_unknown_history_shapes_pass_through_unchanged(self):
        records = ["large history fixture", None, 42, ["legacy"],
            {"info": None, "parts": []}, {"info": [], "parts": []},
            {"info": {}, "parts": None}, {"info": {}, "parts": "legacy"},
            {"info": {}, "parts": [None, "legacy", {"type": "text", "text": "ordinary"}]}]
        self.assertEqual(module.normalize_history(records), records)
        for original, projected in zip(records, module.normalize_history(records)):
            self.assertIs(projected, original)

    def test_receipts_reuse_native_validator_and_redact_large_json(self):
        request = {"action": "call_service", "domain": "todo", "service": "add_item",
                   "target": {"entity_id": "todo.shopping"}, "data": {"item": "хлеб"}}
        native = "habab-ha " + shlex.quote(json.dumps(request))
        command = "/bin/bash -lc " + shlex.quote(native)
        part = {"id": "item_0", "callID": "item_0", "type": "tool", "tool": "bash", "state": {
            "status": "completed", "input": {"command": module.native_command(command)},
            "output": json.dumps({"called": "todo.add_item", "verification": "persisted",
                       "items": [{"entity_id": "todo.shopping", "summary": "хлеб"}]})}}
        outcome = {**OUTCOME, "kind": "action_result", "todo_additions": [{
            "entity_id": "todo.shopping", "item": "хлеб"}]}
        marker = module.result_part("user-1", outcome, [part], self.clean)
        response = module.normalize_history([{"info": {"id": "marker-1", "role": "user"},
                                                "parts": [marker]}])[0]
        messages = [{"info": {"id": "user-1", "role": "user"}}, response]
        self.assertTrue(outcomes.todo_claims_verified(outcome, response, messages))
        part["state"]["output"] = "{}"
        self.assertFalse(outcomes.todo_claims_verified(outcome, {**response, "parts": [part]}, messages[:-1]))
        large = module.result_part("user-1", OUTCOME, [{"type": "tool", "tool": "bash",
            "output": "x" * 31900 + self.key}], self.clean)
        json.loads(large["text"])
        self.assertNotIn(self.key, large["text"])

    def test_existing_shared_history_and_new_request_are_supplied_once(self):
        history = [{"role": "user", "text": "Раньше"}, {"role": "assistant", "text": "Ответ"}]
        prompt = module.turn_prompt("system", {"channel": "telegram"}, history, "Сейчас")
        self.assertEqual(prompt.count("Сейчас"), 1)
        self.assertIn("Раньше", prompt)
        self.assertIn("telegram", prompt)


if __name__ == "__main__":
    unittest.main()
