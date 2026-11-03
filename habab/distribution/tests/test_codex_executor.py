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
import tomllib
from types import ModuleType
import unittest
import uuid
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
        session_id = "019c6e27-e55b-73d1-87d8-4e01f1f75043"
        session_dir = self.root / "codex" / "state" / "sessions" / "2026" / "11" / "03"
        self.fixture("import os,json,sys\n"
            + "from pathlib import Path\n"
            + "session_dir=Path(os.environ['CODEX_HOME'])/'sessions'/'2026'/'11'/'03'\n"
            + "session_dir.mkdir(parents=True)\n"
            + "(session_dir/'rollout-2026-11-03T12-00-00-" + session_id
            + ".jsonl').write_text('{}\\n')\n"
            + "json.dump({'env':dict(os.environ),'args':sys.argv,'prompt':sys.stdin.read()},open("
            + repr(str(capture)) + ",'w'))\n"
            + "print(" + repr(json.dumps({"type": "thread.started", "thread_id": session_id})) + ")\n"
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
        self.assertNotIn("OPENAI_API_KEY", child["env"])
        self.assertNotIn("FOREIGN_SECRET", child["env"])
        self.assertNotEqual(child["env"]["HOME"], os.environ.get("HOME"))
        self.assertEqual(child["env"]["CODEX_HOME"], str(self.root / "codex/state"))
        self.assertNotIn(self.key, child["args"])
        self.assertNotIn('model_provider="habab"', child["args"])
        for argument in ("--ephemeral", "--ignore-user-config", "--ignore-rules",
                         'service_tier="fast"', 'web_search="live"', "features.fast_mode=true",
                         "features.standalone_web_search=false",
                         'model_reasoning_effort="medium"', "--output-schema"):
            if argument in {"--ephemeral", "--ignore-user-config"}:
                self.assertNotIn(argument, child["args"])
            else:
                self.assertIn(argument, child["args"])
        scoped_config = tomllib.loads((self.root / "codex/state/config.toml").read_text())
        self.assertEqual(scoped_config["model_provider"], "openai")
        self.assertNotIn("openai_base_url", scoped_config)
        self.assertNotIn("mcp_servers", scoped_config)
        self.assertEqual(scoped_config["web_search"], "live")
        self.assertEqual(scoped_config["approval_policy"], "never")
        self.assertEqual(scoped_config["sandbox_mode"], "danger-full-access")
        self.assertTrue(scoped_config["features"]["fast_mode"])
        self.assertFalse(scoped_config["features"]["standalone_web_search"])
        self.assertEqual(scoped_config["model"], "gpt-6-luna")
        self.assertEqual((self.root / "codex/state/config.toml").stat().st_mode & 0o777, 0o600)
        cli_overrides = {child["args"][index + 1] for index, arg in enumerate(child["args"][:-1])
                         if arg == "-c"}
        self.assertEqual(cli_overrides, {
            f"{key}={module._toml_value(value)}"
            for key, value in module.codex_config_pairs(self.settings)})
        self.assertEqual(child["prompt"], "Текущий запрос")
        self.assertIn(self.key, self.secrets)
        self.assertIsNone(self.executor.process)
        self.assertEqual(self.executor.last_session_id, session_id)
        self.assertEqual(Path(self.executor.last_session_path).parent, session_dir)
        self.assertFalse(self.executor.last_session_truncated)
        self.assertEqual(self.executor.last_executor_metadata,
                         {"provider": "openai", "model": "gpt-6-luna", "service_tier": "fast"})

    async def test_real_web_search_jsonl_event_is_kept_as_compact_trace_tool(self):
        search = {"type": "item.completed", "item": {"id": "ws_1",
            "type": "web_search", "query": "LED BULB W5K color temperature",
            "action": {"type": "search"},
            "results": [{"title": "Bulb manual", "url": "https://example.test/manual"}]}}
        self.fixture(self.events([search, self.final_event()]))
        _, parts = await self.run_cli()
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["tool"], "web_search")
        self.assertEqual(parts[0]["state"]["input"]["queries"], ["LED BULB W5K color temperature"])
        self.assertEqual(json.loads(parts[0]["state"]["output"]), [{
            "title": "Bulb manual", "url": "https://example.test/manual"}])

    async def test_started_search_without_completion_is_finalized_as_unavailable(self):
        started = {"type": "item.started", "item": {"id": "ws_missing",
            "type": "web_search", "action": {"type": "other"}}}
        self.fixture(self.events([started, self.final_event()]))
        outcome, parts = await self.run_cli()
        self.assertEqual(outcome, OUTCOME)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["state"]["status"], "error")
        self.assertEqual(parts[0]["state"]["error"], "tool_result_unavailable")
        self.assertEqual(parts[0]["state"]["output"], "[]")

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
        self.assertNotIn("OPENAI_API_KEY", child["env"])
        self.assertIn('model_provider="habab"', child["args"])
        self.assertIn('model_providers.habab.name="Habab"', child["args"])
        self.assertIn('model_providers.habab.base_url="' + module.HABAB_CODEX_BASE_URL + '"', child["args"])
        self.assertIn('model_providers.habab.env_key="CODEX_API_KEY"', child["args"])
        self.assertIn("model_providers.habab.requires_openai_auth=false", child["args"])
        self.assertIn("model_providers.habab.supports_websockets=false", child["args"])
        self.assertIn("model_providers.habab.supports_standalone_web_search=false", child["args"])
        self.assertIn('web_search="disabled"', child["args"])
        self.assertIn('mcp_servers.habab_search.url="https://habab-voice.wpmix.net/mcp/exa"',
                      child["args"])
        self.assertIn('mcp_servers.habab_search.bearer_token_env_var="CODEX_API_KEY"',
                      child["args"])
        self.assertIn("features.standalone_web_search=false", child["args"])
        self.assertIn('model_providers.habab.capabilities.remote_compaction="unsupported"',
                      child["args"])
        scoped_config = tomllib.loads((self.root / "codex/state/config.toml").read_text())
        self.assertEqual(scoped_config["model_provider"], "habab")
        habab_provider = scoped_config["model_providers"]["habab"]
        self.assertEqual(habab_provider["name"], "Habab")
        self.assertEqual(habab_provider["wire_api"], "responses")
        self.assertEqual(habab_provider["base_url"], module.HABAB_CODEX_BASE_URL)
        self.assertEqual(habab_provider["env_key"], "CODEX_API_KEY")
        self.assertFalse(habab_provider["requires_openai_auth"])
        self.assertFalse(habab_provider["supports_websockets"])
        self.assertFalse(habab_provider["supports_standalone_web_search"])
        self.assertEqual(scoped_config["web_search"], "disabled")
        exa_server = scoped_config["mcp_servers"]["habab_search"]
        self.assertEqual(exa_server["url"], "https://habab-voice.wpmix.net/mcp/exa")
        self.assertEqual(exa_server["bearer_token_env_var"], "CODEX_API_KEY")
        self.assertFalse(scoped_config["features"]["standalone_web_search"])
        self.assertEqual(habab_provider["capabilities"]["remote_compaction"], "unsupported")
        self.assertNotIn("fixture-installation-key", (self.root / "codex/state/config.toml").read_text())
        self.assertNotIn("untrusted.example", " ".join(child["args"]))
        self.assertNotIn("fixture-installation-key", " ".join(child["args"]))

    async def test_habab_exa_mcp_jsonl_events_become_named_trace_tools(self):
        search = {"type": "item.completed", "item": {
            "id": "mcp-search-1", "type": "mcp_tool_call", "server": "habab_search",
            "tool": "web_search_exa", "arguments": {"query": "LED BULB W5K"},
            "status": "completed", "result": {"content": [{"type": "text",
                "text": "Bulb manual: https://example.test/manual " + self.key}],
                "isError": False}}}
        context = {"type": "item.completed", "item": {
            "id": "mcp-context-1", "type": "mcp_tool_call", "server": "habab_search",
            "tool": "get_code_context_exa", "arguments": {"query": "HA bulb entity"},
            "status": "completed", "result": {"content": [{"type": "text",
                "text": "Relevant Home Assistant docs"}], "isError": False}}}
        unrelated = {"type": "item.completed", "item": {
            "id": "mcp-other-1", "type": "mcp_tool_call", "server": "other",
            "tool": "web_search_exa", "arguments": {}, "status": "completed",
            "result": {"content": []}}}
        self.settings.update(provider="habab", api_key=self.key)
        self.fixture(self.events([search, context, unrelated, self.final_event()]))
        _, parts = await self.run_cli()
        self.assertEqual([part["tool"] for part in parts], ["web_search_exa", "get_code_context_exa"])
        self.assertEqual(parts[0]["state"]["input"], {"query": "LED BULB W5K"})
        result = json.loads(parts[0]["state"]["output"])
        self.assertNotIn(self.key, json.dumps(result))
        self.assertIn("[redacted]", result["content"][0]["text"])
        self.assertEqual(parts[1]["state"]["status"], "completed")

    async def test_timeout_terminates_process_and_clears_active_child(self):
        session_id = "019c6e27-e55b-73d1-87d8-4e01f1f75046"
        started = {"type": "item.started", "item": {"id": "item_0",
            "type": "command_execution", "command": "habab-ha devices", "aggregated_output": ""}}
        self.fixture("import os,time\nfrom pathlib import Path\n"
            + "d=Path(os.environ['CODEX_HOME'])/'sessions'/'2026'/'11'/'03'\n"
            + "d.mkdir(parents=True)\n"
            + "(d/'rollout-2026-11-03T12-00-00-" + session_id + ".jsonl').write_text('{}\\n')\n"
            + "print(" + repr(json.dumps({"type": "thread.started", "thread_id": session_id}))
            + ", flush=True)\n"
            + "print(" + repr(json.dumps(started)) + ", flush=True)\n"
            + "time.sleep(30)\n")
        with self.assertRaises(module.CodexError) as result:
            await self.run_cli(timeout=.08)
        self.assertEqual(result.exception.code, "runtime_timeout")
        self.assertIsNone(self.executor.process)
        self.assertEqual(self.executor.last_parts[0]["state"]["status"], "running")
        self.assertEqual(self.executor.last_session_id, session_id)
        self.assertTrue(Path(self.executor.last_session_path).is_file())

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
        session_id = "019c6e27-e55b-73d1-87d8-4e01f1f75043"
        search_trace = {"id": "ws_1", "callID": "ws_1", "type": "tool",
            "tool": "web_search", "state": {"status": "completed", "input": {
                "action": "search", "queries": ["lamp model"]}, "output": "[]"}}
        mcp_trace = {"id": "mcp_1", "callID": "mcp_1", "type": "tool",
            "tool": "web_search_exa", "state": {"status": "completed", "input": {}, "output": "[]"}}
        part = module.result_part("user-1", OUTCOME, [search_trace, mcp_trace], self.clean, session_id, True,
            {"provider": "habab", "model": "gpt-6-luna", "service_tier": "fast",
             "base_url": "https://must-not-persist.example"})
        record = {"info": {"role": "user", "id": "marker-1"}, "parts": [part]}
        result = module.normalize_history([record])
        self.assertEqual(result[0]["info"]["id"], "marker-1")
        self.assertEqual(result[0]["info"]["role"], "assistant")
        self.assertEqual(result[0]["info"]["parentID"], "user-1")
        self.assertEqual(result[0]["info"]["structured"], OUTCOME)
        self.assertEqual(result[0]["info"]["codex_session_id"], session_id)
        self.assertTrue(result[0]["info"]["codex_session_truncated"])
        self.assertEqual(result[0]["info"]["executor_metadata"], {
            "provider": "habab", "model": "gpt-6-luna", "service_tier": "fast"})
        self.assertNotIn("must-not-persist", part["text"])
        self.assertEqual(result[0]["parts"], [search_trace, mcp_trace])
        part["synthetic"] = False
        self.assertEqual(module.normalize_history([record]), [])

    def test_session_resolver_is_exact_and_rejects_symlink_rollouts(self):
        session_id = "019c6e27-e55b-73d1-87d8-4e01f1f75043"
        sessions = self.root / "codex-home" / "sessions" / "2026" / "11" / "03"
        sessions.mkdir(parents=True)
        rollout = sessions / f"rollout-2026-11-03T12-00-00-{session_id}.jsonl"
        rollout.write_text('{"type":"thread.started"}\n')
        resolved = module.find_codex_session_file(self.root / "codex-home", session_id)
        self.assertEqual(resolved, (rollout, False))
        self.assertEqual(rollout.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(module.find_codex_session_file(self.root / "codex-home", "../outside"))
        other = "019c6e27-e55b-73d1-87d8-4e01f1f75044"
        link = sessions / f"rollout-2026-11-03T12-01-00-{other}.jsonl"
        link.symlink_to(rollout)
        self.assertIsNone(module.find_codex_session_file(self.root / "codex-home", other))
        oversized_id = "019c6e27-e55b-73d1-87d8-4e01f1f75045"
        oversized = sessions / f"rollout-2026-11-03T12-02-00-{oversized_id}.jsonl"
        oversized.touch()
        with oversized.open("r+") as stream:
            stream.truncate(module.MAX_TRACE_BYTES + 1)
        self.assertEqual(module.find_codex_session_file(self.root / "codex-home", oversized_id),
                         (oversized, True))

    def test_session_retention_keeps_only_fifty_newest_regular_rollouts(self):
        home = self.root / "codex-home"
        sessions = home / "sessions" / "2026" / "11" / "03"
        sessions.mkdir(parents=True)
        ids = [str(uuid.uuid4()) for _ in range(51)]
        for i, session_id in enumerate(ids):
            path = sessions / f"rollout-2026-11-03T12-{i:02d}-00-{session_id}.jsonl"
            path.write_text("{}\n")
            os.utime(path, (i, i))
        module.retain_codex_sessions(home)
        retained = list(sessions.glob("rollout-*.jsonl"))
        self.assertEqual(len(retained), 50)
        self.assertFalse((sessions / f"rollout-2026-11-03T12-00-00-{ids[0]}.jsonl").exists())

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
