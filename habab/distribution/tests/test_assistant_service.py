"""The shared chat front door preserves channel histories and checks owner ACLs."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.agent_diagnostics import AgentDiagnostics
from homeassistant.components.habab_home.assistant import AssistantService
from homeassistant.components.habab_home.setup_agent import AgentError


class AssistantServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stores = []

        def store_factory(*args, **kwargs):
            store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
            self.stores.append((args, kwargs, store))
            return store

        self.store_patch = patch("homeassistant.components.habab_home.assistant.Store", side_effect=store_factory)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.user = SimpleNamespace(is_active=True, is_admin=True)
        # These legacy runtime/voice tests select the explicit OpenCode-only mode.
        provider = SimpleNamespace(data={"provider": "habab"}, options={"fast_dialog": False})
        self.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=self.user)),
            config_entries=SimpleNamespace(async_entries=lambda _domain: [provider]),
        )
        self.runtime = SimpleNamespace(
            lock=asyncio.Lock(), ensure=AsyncMock(), request=AsyncMock(), visible=Mock(return_value=[]),
            run=AsyncMock(return_value={"session_id": "s", "messages": []}),
            abort=AsyncMock(return_value=True), active=None, process=None,
            diagnostics=AgentDiagnostics(),
        )
        self.service = AssistantService(self.hass, self.runtime)
        self.voice_store = next(store for args, _kwargs, store in self.stores
                                if args[2] == "habab_home.voice_turn_receipts")

    async def test_voice_reads_current_web_text_and_pending_voice_without_new_session(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "web-alice", "bob": "web-bob"}
        self.service.web_contexts = {"alice": {"session_id": "web-alice", "epoch": 4}}
        self.service.web_history_cache[("alice", "web-alice")] = [
            {"role": "user", "text": "Назови комнату Кабинет"},
            {"role": "assistant", "text": "Буду называть её Кабинет"},
        ]
        self.service.web_history_cache[("bob", "web-bob")] = [
            {"role": "user", "text": "Другой владелец"}]
        await self.service.record_voice_event(
            "alice", "web-alice", "voice-context-1", "user", "Что мы обсуждали?",
            history_epoch=4)
        context = await self.service.voice_history_context("alice", include_messages=True)
        self.assertEqual(context["session_id"], "web-alice")
        self.assertEqual(context["messages"], [
            {"role": "user", "content": "Назови комнату Кабинет"},
            {"role": "assistant", "content": "Буду называть её Кабинет"},
            {"role": "user", "content": "Что мы обсуждали?"},
        ])
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()

    async def test_voice_after_restart_reads_existing_history_but_empty_home_starts_nothing(self):
        await self.service.setup()
        empty = await self.service.voice_history_context("alice", include_messages=True)
        self.assertEqual(empty, {"session_id": None, "epoch": 0, "messages": []})
        self.runtime.ensure.assert_not_awaited()
        self.service.web_sessions = {"alice": "existing"}
        self.runtime.request.return_value = [{"info": {"role": "user"}, "parts": []}]
        self.runtime.visible.return_value = [{"role": "user", "text": "Хочу обсудить освещение"}]
        context = await self.service.voice_history_context("alice", include_messages=True)
        self.assertEqual(context["messages"], [
            {"role": "user", "content": "Хочу обсудить освещение"}])
        self.runtime.request.assert_awaited_once_with("GET", "session/existing/message?limit=60")

    async def test_voice_context_rejects_revoked_owner_and_bounds_visible_history(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "current"}
        self.service.web_history_cache[("alice", "current")] = [
            {"role": "user", "text": "😀" * 5000} for _ in range(50)]
        context = await self.service.voice_history_context("alice", include_messages=True)
        self.assertLessEqual(sum(len(m["content"]) for m in context["messages"]), 16000)
        self.assertLessEqual(sum(len(m["content"].encode("utf-8")) for m in context["messages"]), 16000)
        self.assertLessEqual(len(context["messages"]), 20)
        self.user.is_active = False
        with self.assertRaises(AgentError):
            await self.service.voice_history_context("alice", include_messages=True)

    async def test_voice_warm_history_does_not_wait_for_running_command_or_previous_chat(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "current"}
        self.service.web_history_cache[("alice", "previous")] = [
            {"role": "user", "text": "Old chat"}]
        self.service.web_history_cache[("alice", "current")] = [
            {"role": "user", "text": "Current chat"}]
        async with self.service.web_lifecycle_lock:
            async with self.runtime.lock:
                context = await asyncio.wait_for(
                    self.service.voice_history_context("alice", include_messages=True), .5)
        self.assertEqual(context["messages"], [{"role": "user", "content": "Current chat"}])
        self.runtime.request.assert_not_awaited()

    async def test_voice_cold_read_rejects_new_chat_race_and_never_waits_for_busy_runtime(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "current"}
        async with self.runtime.lock:
            with self.assertRaisesRegex(AgentError, "busy"):
                await asyncio.wait_for(
                    self.service.voice_history_context("alice", include_messages=True), .5)
        async def reset_during_read(*args):
            self.service.web_sessions["alice"] = "new"
            self.service.web_contexts["alice"] = {"session_id": "new", "epoch": 1}
            return []
        self.runtime.request.side_effect = reset_during_read
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.voice_history_context("alice", include_messages=True)
        self.assertNotIn(("alice", "current"), self.service.web_history_cache)

    async def test_failed_web_turn_invalidates_voice_cache_and_reads_persisted_user_turn(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "current"}
        self.service.web_history_cache[("alice", "current")] = [
            {"role": "assistant", "text": "Before failure"}]
        self.runtime.run.side_effect = AgentError("runtime_error")
        with self.assertRaises(AgentError):
            await self.service.run("alice", "web", "current", "Persisted before failure")
        self.assertNotIn(("alice", "current"), self.service.web_history_cache)
        self.runtime.request.return_value = []
        self.runtime.visible.return_value = [
            {"role": "user", "text": "Persisted before failure"}]
        context = await self.service.voice_history_context("alice", include_messages=True)
        self.assertEqual(context["messages"], [
            {"role": "user", "content": "Persisted before failure"}])

    async def test_existing_web_and_telegram_history_maps_are_loaded_unchanged(self):
        self.stores[0][2].async_load.return_value = {"alice": "web-existing"}
        self.stores[1][2].async_load.return_value = {"alice:bot:chat": "tg-existing"}
        await self.service.setup()
        self.runtime.request.return_value = []
        web = await self.service.web_session("alice")
        tg = await self.service.telegram_session("alice", "bot", "chat")
        self.assertEqual(web["session_id"], "web-existing")
        self.assertEqual(tg["session_id"], "tg-existing")
        self.assertEqual(self.service.web_sessions, {"alice": "web-existing"})
        self.assertEqual(self.service.telegram_sessions, {"alice:bot:chat": "tg-existing"})
        for store in self.stores:
            store[2].async_save.assert_not_awaited()

    async def test_web_and_telegram_session_ids_cannot_cross_channels_or_owners(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "web-session", "bob": "bob-session"}
        self.service.telegram_sessions = {"alice:entry:10": "tg-session"}
        await self.service.run("alice", "web", "web-session", "web text")
        await self.service.run("alice", "telegram", "tg-session", "tg text", "entry", "10")
        self.assertEqual(self.runtime.run.await_count, 2)
        for args in (
            ("bob", "web", "web-session", "x"),
            ("alice", "telegram", "web-session", "x", "entry", "10"),
            ("alice", "web", "tg-session", "x"),
            ("alice", "telegram", "tg-session", "x", "other-entry", "10"),
        ):
            with self.assertRaises(AgentError) as error:
                await self.service.run(*args)
            self.assertEqual(error.exception.code, "not_found")
        self.assertEqual(self.runtime.run.await_count, 2)

    async def test_web_run_rejects_stale_voice_epoch_before_runtime_work(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {
            "session_id": "canonical-session", "epoch": 4,
        }}

        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.run(
                "alice", "web", "canonical-session", "old lease intent",
                expected_history_epoch=3)

        self.runtime.run.assert_not_awaited()
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()

    async def test_voice_event_records_fast_without_starting_or_calling_runtime(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {"session_id": "canonical-session", "epoch": 3}}
        result = await self.service.record_voice_event(
            "alice", "canonical-session", "gemini_evt_001", "user", "Включи свет",
            history_epoch=3)
        self.assertEqual(result, {
            "session_id": "canonical-session",
            "messages": [{"role": "user", "text": "Включи свет"}],
            "recorded": True, "duplicate": False, "pending": True,
        })
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()
        self.runtime.run.assert_not_awaited()
        self.voice_store.async_save.assert_awaited_once()

    async def test_voice_event_dedupe_conflict_and_receipt_namespace(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {"session_id": "canonical-session", "epoch": 0}}
        first = await self.service.record_voice_event(
            "alice", "canonical-session", "shared-id", "user", "Question", history_epoch=0)
        replay = await self.service.record_voice_event(
            "alice", "canonical-session", "shared-id", "user", "Question", history_epoch=0)
        self.assertFalse(first["duplicate"])
        self.assertTrue(replay["duplicate"])
        with self.assertRaisesRegex(AgentError, "voice_event_conflict"):
            await self.service.record_voice_event(
                "alice", "canonical-session", "shared-id", "assistant", "Changed", history_epoch=0)
        paired = await self.service.record_voice_turn(
            "alice", "canonical-session", "shared-id", "Question", "Answer", history_epoch=0)
        self.assertFalse(paired["duplicate"])
        self.assertEqual(len(self.service.voice_receipts), 2)

    async def test_unknown_receipt_protocol_is_not_projected_as_a_legacy_pair(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {"session_id": "canonical-session", "epoch": 0}}
        await self.service.record_voice_event(
            "alice", "canonical-session", "evt-protocol", "user", "Observed", history_epoch=0)
        receipt = next(iter(self.service.voice_receipts.values()))
        receipt["protocol"] = "unknown_voice_protocol"
        self.assertEqual(self.service._project_pending_voice(
            "alice", "canonical-session", [], 0), [])

    async def test_voice_event_schema_acl_session_and_epoch_are_enforced_before_persistence(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {"session_id": "canonical-session", "epoch": 4}}
        invalid = (
            {"role": "tool"}, {"role": {}}, {"provider": "openai"},
            {"correlation": "not-observed"}, {"event_id": "not an id"},
        )
        for changes in invalid:
            args = {"role": "user", "event_id": "valid-id", "provider": "gemini",
                    "correlation": None, "text": "Question"}
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(AgentError, "invalid_voice_event"):
                await self.service.record_voice_event(
                    "alice", "canonical-session", args["event_id"], args["role"], args["text"],
                    history_epoch=4, provider=args["provider"], correlation=args["correlation"])
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.record_voice_event(
                "alice", "stale-session", "valid-id", "user", "Question", history_epoch=4)
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.record_voice_event(
                "alice", "canonical-session", "valid-id", "user", "Question", history_epoch=3)
        self.user.is_active = False
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.service.record_voice_event(
                "alice", "canonical-session", "valid-id", "user", "Question", history_epoch=4)
        self.assertEqual(self.service.voice_receipts, {})
        self.voice_store.async_save.assert_not_awaited()

    async def test_pending_voice_projects_mixed_events_and_legacy_pairs_in_receipt_order(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {"session_id": "canonical-session", "epoch": 1}}
        await self.service.record_voice_event(
            "alice", "canonical-session", "evt-one", "user", "First", history_epoch=1)
        await self.service.record_voice_turn(
            "alice", "canonical-session", "pair-two", "Second user", "Second assistant",
            history_epoch=1)
        await self.service.record_voice_event(
            "alice", "canonical-session", "evt-three", "assistant", "Third", history_epoch=1)
        self.assertEqual(self.service._project_pending_voice(
            "alice", "canonical-session", [{"role": "assistant", "text": "Existing"}], 1), [
                {"role": "assistant", "text": "Existing"},
                {"role": "user", "text": "First"},
                {"role": "user", "text": "Second user"},
                {"role": "assistant", "text": "Second assistant"},
                {"role": "assistant", "text": "Third"},
            ])

    async def test_mixed_pending_events_materialize_no_reply_once_in_receipt_order(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {"session_id": "canonical-session", "epoch": 0}}
        await self.service.record_voice_event(
            "alice", "canonical-session", "evt-one", "user", "First", history_epoch=0)
        await self.service.record_voice_turn(
            "alice", "canonical-session", "pair-two", "Second user", "Second assistant",
            history_epoch=0)
        await self.service.record_voice_event(
            "alice", "canonical-session", "evt-three", "assistant", "Third", history_epoch=0)
        canonical = []
        posts = []

        async def request(method, _path, payload=None):
            if method == "GET":
                return [{"parts": list(canonical)}]
            self.assertEqual(method, "POST")
            self.assertTrue(payload["noReply"])
            part = payload["parts"][0]
            canonical.append(part)
            posts.append(part)
            return {}

        self.runtime.request.side_effect = request
        await self.service._materialize_pending_voice("alice", "canonical-session", 0)
        self.assertEqual(posts[0]["metadata"]["habab_voice_event_id"], "evt-one")
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0]["metadata"]["habab_voice_event_protocol"], "native_gemini_event.v1")
        self.assertEqual(posts[1]["metadata"], {
            "habab_voice_turn": "native_realtime.v1",
            "habab_voice_event_id": "pair-two",
            "habab_voice_payload_sha256": self.service.voice_receipts[
                '["alice",0,"pair-two"]']["payload_sha256"],
        })
        self.assertEqual(posts[2]["metadata"]["habab_voice_event_id"], "evt-three")
        self.assertTrue(all(item["synthetic"] for item in posts))
        self.assertTrue(all(self.service.voice_receipts[key]["state"] == "complete"
                            for key in self.service.voice_receipts))
        await self.service._materialize_pending_voice("alice", "canonical-session", 0)
        self.assertEqual(len(posts), 3)
        self.assertEqual(self.runtime.run.await_count, 0)

    async def test_cancelled_materialization_recovers_existing_event_without_reposting(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {"session_id": "canonical-session", "epoch": 2}}
        await self.service.record_voice_event(
            "alice", "canonical-session", "evt-recovery", "user", "Already heard", history_epoch=2)
        canonical = []
        fail_after_post = True
        posts = 0

        async def request(method, _path, payload=None):
            nonlocal fail_after_post, posts
            if method == "GET":
                return [{"parts": list(canonical)}]
            posts += 1
            canonical.extend(payload["parts"])
            if fail_after_post:
                fail_after_post = False
                raise asyncio.CancelledError
            return {}

        self.runtime.request.side_effect = request
        with self.assertRaises(asyncio.CancelledError):
            await self.service._materialize_pending_voice("alice", "canonical-session", 2)
        self.assertEqual(next(iter(self.service.voice_receipts.values()))["state"], "pending")
        await self.service._materialize_pending_voice("alice", "canonical-session", 2)
        self.assertEqual(posts, 1)
        self.assertEqual(next(iter(self.service.voice_receipts.values()))["state"], "complete")
        duplicate = await self.service.record_voice_event(
            "alice", "canonical-session", "evt-recovery", "user", "Already heard", history_epoch=2)
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(duplicate["pending"])
        self.runtime.run.assert_not_awaited()

    async def test_unbound_event_recovers_from_private_store_into_current_session(self):
        await self.service.setup()
        self.service.web_sessions = {}
        self.service.web_contexts = {"alice": {"session_id": None, "epoch": 5}}
        result = await self.service.record_voice_event(
            "alice", None, "evt-deferred", "assistant", "Ожидающий ответ", history_epoch=5)
        self.assertIsNone(result["session_id"])
        saved_receipts = json.loads(json.dumps(self.service.voice_receipts))
        self.stores[0][2].async_load.return_value = {}
        self.stores[2][2].async_load.return_value = {
            "alice": {"session_id": None, "epoch": 5},
        }
        self.voice_store.async_load.return_value = saved_receipts
        await self.service.setup()
        self.service.web_sessions = {"alice": "new-canonical-session"}
        self.service.web_contexts = {
            "alice": {"session_id": "new-canonical-session", "epoch": 5},
        }
        canonical = []

        async def request(method, _path, payload=None):
            if method == "GET":
                return [{"parts": list(canonical)}]
            canonical.extend(payload["parts"])
            return {}

        self.runtime.request.side_effect = request
        await self.service._materialize_pending_voice("alice", "new-canonical-session", 5)
        receipt = next(iter(self.service.voice_receipts.values()))
        self.assertEqual(receipt["session_id"], "new-canonical-session")
        self.assertEqual(receipt["state"], "complete")
        self.assertNotIn("text", receipt)
        self.runtime.run.assert_not_awaited()

    async def test_web_lifecycle_lock_keeps_new_chat_out_until_guarded_run_finishes(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {
            "session_id": "canonical-session", "epoch": 0,
        }}
        run_started = asyncio.Event()
        release_run = asyncio.Event()

        async def root_turn(_owner, _session_id, _text):
            run_started.set()
            await release_run.wait()
            return {"session_id": "canonical-session", "messages": []}

        self.runtime.run.side_effect = root_turn
        self.runtime.request.side_effect = [
            {"id": "replacement-session"}, [],
        ]
        guarded_run = asyncio.create_task(self.service.run(
            "alice", "web", "canonical-session", "voice tool intent",
            expected_history_epoch=0))
        await asyncio.wait_for(run_started.wait(), timeout=1)
        new_chat = asyncio.create_task(self.service.web_session("alice", new=True))
        await asyncio.sleep(0)
        self.assertFalse(new_chat.done())
        self.assertEqual(self.service.web_contexts["alice"], {
            "session_id": "canonical-session", "epoch": 0,
        })

        release_run.set()
        await asyncio.wait_for(guarded_run, timeout=1)
        replacement = await asyncio.wait_for(new_chat, timeout=1)
        self.assertEqual(replacement["session_id"], "replacement-session")
        self.assertEqual(self.service.web_contexts["alice"], {
            "session_id": "replacement-session", "epoch": 1,
        })
    async def test_trusted_origin_comes_only_from_active_mapped_session(self):
        await self.service.setup()
        self.hass.data = {"habab_telegram": SimpleNamespace(
            entry_for_owner=Mock(return_value=SimpleNamespace(
                entry_id="bot-entry", data={"bot_id": 123})),
            bindings={"bot-entry": {"bot_id": 123, "chat_id": "42", "user_id": "7"}},
            entries={"bot-entry": {"ready": True, "chat_id": "42", "user_id": "7"}},
        )}
        self.service.web_sessions = {"alice": "web-session"}
        self.service.telegram_sessions = {"alice:bot-entry:42": "tg-session"}
        self.runtime.active = ("alice", "tg-session")
        origin = await self.service.trusted_origin("alice", "tg-session")
        self.assertEqual(origin, {
            "channel": "telegram", "owner": "alice",
            "entry_id": "bot-entry", "chat_id": "42", "bot_id": 123,
        })
        self.assertIsNone(await self.service.trusted_origin("alice", "web-session"))
        self.assertIsNone(await self.service.trusted_origin("bob", "tg-session"))
        self.service.telegram_sessions["alice:other-bot:other-chat"] = "tg-session"
        self.assertIsNone(await self.service.trusted_origin("alice", "tg-session"))
        self.service.telegram_sessions.pop("alice:other-bot:other-chat")
        self.runtime.active = ("alice", "another-session")
        self.assertIsNone(await self.service.trusted_origin("alice", "tg-session"))
        self.user.is_active = False
        self.runtime.active = ("alice", "tg-session")
        with self.assertRaises(AgentError):
            await self.service.trusted_origin("alice", "tg-session")

    async def test_group_member_origin_delegates_live_acl_to_telegram_manager(self):
        await self.service.setup()
        entry = SimpleNamespace(entry_id="bot-entry", data={"bot_id": 123})
        telegram = SimpleNamespace(
            entry_for_owner=Mock(return_value=entry), bindings={}, entries={"bot-entry": {"ready": True}},
            member_origin=AsyncMock(return_value={
                "channel": "telegram", "owner": "alice", "entry_id": "bot-entry",
                "chat_id": "42", "bot_id": 123, "group_id": "-100", "user_id": 42,
            }),
        )
        self.hass.data = {"habab_telegram": telegram}
        self.service.telegram_sessions = {"alice:bot-entry:42": "member-session"}
        self.runtime.active = ("alice", "member-session")

        origin = await self.service.trusted_origin("alice", "member-session")
        self.assertEqual(origin["chat_id"], "42")
        self.assertEqual(origin["group_id"], "-100")
        telegram.member_origin.assert_awaited_once_with(
            "alice", "bot-entry", "42", "member-session")
        self.assertIsNone(await self.service.trusted_origin("alice", "web-session"))

        telegram.member_origin.reset_mock(return_value=True)
        telegram.member_origin.return_value = None
        self.assertIsNone(await self.service.trusted_origin("alice", "member-session"))
        telegram.member_origin.assert_awaited_once()

        self.service.telegram_sessions.pop("alice:bot-entry:42")
        telegram.member_origin.reset_mock()
        self.assertIsNone(await self.service.trusted_origin("alice", "member-session"))
        telegram.member_origin.assert_not_awaited()

    async def test_inactive_or_non_admin_owner_is_rejected_before_runtime(self):
        self.service.web_sessions = {"alice": "web-session"}
        self.user.is_active = False
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.service.run("alice", "web", "web-session", "x")
        self.user.is_active = True
        self.user.is_admin = False
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.service.web_session("alice")
        self.runtime.run.assert_not_awaited()
        self.runtime.ensure.assert_not_awaited()

    async def test_new_web_session_replaces_only_web_mapping(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "old-web"}
        self.service.telegram_sessions = {"alice:entry:10": "kept-tg"}
        self.runtime.request.side_effect = [
            {"id": "new-web"},
            [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "hello"}]}],
        ]
        result = await self.service.web_session("alice", new=True)
        self.assertEqual(result["session_id"], "new-web")
        self.assertEqual(self.service.telegram_sessions, {"alice:entry:10": "kept-tg"})
        self.stores[0][2].async_save.assert_awaited_once_with({"alice": "new-web"})

    async def test_telegram_forget_revokes_exact_active_session_even_after_owner_disable(self):
        await self.service.setup()
        self.service.telegram_sessions = {
            "alice:entry:10": "active-tg", "alice:other-entry:10": "other-tg",
        }
        self.runtime.active = ("alice", "active-tg")
        self.user.is_active = False
        session_id = await self.service.telegram_forget("alice", "entry", "10")
        self.assertEqual(session_id, "active-tg")
        self.runtime.abort.assert_awaited_once_with("alice", "active-tg")
        self.assertEqual(self.service.telegram_sessions, {"alice:other-entry:10": "other-tg"})
        self.stores[1][2].async_save.assert_awaited_once_with({"alice:other-entry:10": "other-tg"})

    async def test_telegram_reset_preserves_old_mapping_when_runtime_is_busy(self):
        await self.service.setup()
        old = {"alice:entry:10": "old-tg-session"}
        self.service.telegram_sessions = dict(old)
        self.runtime.active = ("alice", "web-session")
        await self.runtime.lock.acquire()
        try:
            with self.assertRaisesRegex(AgentError, "^busy$"):
                await self.service.telegram_reset("alice", "entry", "10")
        finally:
            self.runtime.lock.release()
        self.assertEqual(self.service.telegram_sessions, old)
        self.stores[1][2].async_save.assert_not_awaited()

    async def test_telegram_reset_preserves_old_mapping_when_session_creation_fails(self):
        await self.service.setup()
        old = {"alice:entry:10": "old-tg-session"}
        self.service.telegram_sessions = dict(old)
        self.runtime.active = ("alice", "web-session")
        self.runtime.request.side_effect = AgentError("runtime_error")
        with self.assertRaisesRegex(AgentError, "^runtime_error$"):
            await self.service.telegram_reset("alice", "entry", "10")
        self.assertEqual(self.service.telegram_sessions, old)
        self.stores[1][2].async_save.assert_not_awaited()

    async def test_diagnostics_are_safe_bounded_and_web_session_scoped(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "web-session"}
        self.service.telegram_sessions = {"alice:entry:10": "tg-session"}
        secret = "api-key-private"
        error = {
            "name": "APIError", "message": f"provider rejected {secret} reasoning_content",
            "data": {"statusCode": 400, "body": f"Bearer {secret}"},
        }
        self.service.diagnostics.record("alice", "web-session", error)
        self.service.diagnostics.record("alice", "tg-session", error)
        result = await self.service.diagnostics_for_web("alice", "web-session")
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("provider rejected", encoded)
        self.assertEqual(result["error"]["name"], "APIError")
        self.assertEqual(result["error"]["http_status"], 400)
        self.assertTrue(result["error"]["mentions_reasoning_content"])
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.diagnostics_for_web("alice", "tg-session")
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.diagnostics_for_web("bob", "web-session")

    async def test_run_assist_delegates_to_native_adapter(self):
        chat_log = object()
        config_entry = object()
        entry = SimpleNamespace(entry=config_entry, entity_id="conversation.test")
        with patch("homeassistant.components.habab_home.assistant.AssistAdapter") as adapter:
            adapter.return_value.complete = AsyncMock()
            await self.service.run_assist(entry, chat_log)
        adapter.assert_called_once_with(self.hass, config_entry)
        adapter.return_value.complete.assert_awaited_once_with(chat_log, "conversation.test")


if __name__ == "__main__":
    unittest.main()
