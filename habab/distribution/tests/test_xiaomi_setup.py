"""Native assistant setup preserves owner and OAuth boundaries across restart."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import probatio
from homeassistant.helpers.config_validation import multi_select
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.components.habab_telegram import TelegramManager
from homeassistant.components.habab_home import xiaomi_setup as module
from homeassistant.components.habab_home.xiaomi_setup import (
    SetupError, XiaomiSetup, authorization_link, callback_request,
)

REDIRECT = 'http://homeassistant.local:8123/api/webhook/fixture-hook'
AUTH = 'https://account.xiaomi.com/oauth2/authorize?' + urlencode({
    'redirect_uri': REDIRECT, 'state': 'fixture-only-state', 'client_id': 'fixture-client'})
OAUTH = {'type': 'progress', 'step_id': 'oauth', 'flow_id': 'flow-alice',
    'description_placeholders': {'link_left': f'<a href="{AUTH}">', 'link_right': '</a>'}}


class MemoryStore:
    def __init__(self, data=None): self.data = deepcopy(data)
    async def async_load(self): return deepcopy(self.data)
    async def async_save(self, data): self.data = deepcopy(data)


class XiaomiSetupTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        hass = SimpleNamespace(config=SimpleNamespace(path=lambda p: str(Path('/missing-fixture')/p)),
            config_entries=SimpleNamespace(async_entries=lambda d: [], async_get_entry=lambda eid: None,
                flow=SimpleNamespace(async_init=AsyncMock(), async_configure=AsyncMock())),
            async_add_executor_job=AsyncMock(return_value=False))
        with patch.object(module, 'Store', return_value=MemoryStore()): manager = XiaomiSetup(hass)
        return manager

    def test_callback_is_bound_to_one_owned_flow_and_not_arbitrary_http(self):
        result = callback_request(REDIRECT+'?'+urlencode({'code':'fixture-code','state':'fixture-only-state'}), AUTH)
        self.assertEqual(result[0], 'fixture-hook')
        for value in (
            'http://127.0.0.1/admin?code=fixture-code&state=fixture-only-state',
            REDIRECT+'?code=fixture-code&state=another-flow',
            REDIRECT+'-other?code=fixture-code&state=fixture-only-state',
            REDIRECT+'?code=fixture-code&code=second&state=fixture-only-state',
            REDIRECT+'?state=fixture-only-state',
            REDIRECT+'?code=fixture-code&state=fixture-only-state#fragment',
        ):
            with self.subTest(value=value), self.assertRaises(SetupError): callback_request(value, AUTH)

    def test_provider_link_has_only_official_host_and_registered_callback(self):
        self.assertEqual(authorization_link(OAUTH), AUTH)
        for url in ('https://evil.example/oauth2/authorize', AUTH.replace('account.xiaomi.com','account.xiaomi.com.evil.example'),
                    'https://account.xiaomi.com/oauth2/authorize?'+urlencode({'redirect_uri':'http://evil.example','state':'fixture'})):
            with self.assertRaises(SetupError): authorization_link({'description_placeholders':{'link_left':f'<a href="{url}">'}})

    async def test_owner_without_request_cannot_install_restart_or_submit_callback(self):
        manager=self.manager();manager.records['alice']={'phase':'oauth'};manager._flows['alice']=OAUTH
        for operation in (manager.install('bob',True),manager.restart('bob'),manager.complete_callback('bob','fixture')):
            with self.assertRaises(SetupError): await operation
        self.assertEqual(await manager.refresh('bob'),{'phase':'idle'})

    async def test_explicit_license_required_before_downloading(self):
        manager=self.manager();await manager.request('alice')
        with patch.object(module.asyncio,'create_subprocess_exec') as start:
            with self.assertRaises(SetupError): await manager.install('alice',False)
            start.assert_not_called()
        self.assertEqual(manager.records['alice']['phase'],'install_required')

    async def test_verified_install_requires_restart_and_retains_request(self):
        manager=self.manager();await manager.request('alice')
        process=SimpleNamespace(communicate=AsyncMock(return_value=(b'{"installed":true}',None)),returncode=0)
        with patch.object(module.asyncio,'create_subprocess_exec',AsyncMock(return_value=process)) as start:
            result=await manager.install('alice',True)
        self.assertEqual(start.call_args.args[1],'/usr/local/bin/habab-install-xiaomi-home')
        self.assertEqual(result['phase'],'restart_required')
        self.assertEqual(manager.store.data['alice']['phase'],'restart_required')

    async def test_restart_recovery_keeps_owner_and_expires_old_oauth(self):
        manager=self.manager();manager.store=MemoryStore({'alice':{'phase':'restarting','boot_id':'previous-boot','region':'ru'},
            'bob':{'phase':'oauth','flow_id':'old-flow','boot_id':'previous-boot','region':'ru'}})
        manager.hass.async_add_executor_job=AsyncMock(return_value=True)
        await manager.setup()
        self.assertEqual(manager.records['alice']['phase'],'ready')
        self.assertEqual(manager.records['bob']['phase'],'ready')
        self.assertNotIn('flow_id',manager.records['bob'])

    async def test_model_summary_excludes_authorization_url_and_callback_codes(self):
        manager=self.manager();manager._loaded_at_start=True
        await manager.request('alice')
        manager.hass.config_entries.flow.async_init.return_value={'type':'form','step_id':'eula','flow_id':'flow-alice'}
        manager.hass.config_entries.flow.async_configure.side_effect=[{'type':'form','step_id':'auth_config'},deepcopy(OAUTH)]
        ui=await manager.begin('alice','ru',True,'ru')
        self.assertEqual(ui['authorization_url'],AUTH)
        self.assertNotIn('authorization_url',(await manager.request('alice')))
        self.assertNotIn('fixture-only-state',str(manager.store.data))
        self.assertNotIn('fixture-client',str(manager.store.data))

    async def test_one_home_is_imported_and_only_registered_entry_id_is_persisted(self):
        manager=self.manager();manager.records['alice']={'phase':'oauth','flow_id':'flow-alice','region':'ru'}
        homes={'type':'form','step_id':'homes_select','flow_id':'flow-alice','errors':{'base':''},
            'data_schema':probatio.Schema({probatio.Required('home_infos'):multi_select({'home-one':'Fixture home [1 device]'}),
                probatio.Required('area_name_rule',default='room'):str})}
        created={'type':'create_entry','result':SimpleNamespace(entry_id='entry-one'),
            'data':{'access_token':'fixture-token-must-not-persist'}}
        manager.hass.config_entries.flow.async_configure.return_value=created
        await manager._advance('alice',homes)
        self.assertEqual(manager.records['alice']['phase'],'configuring')
        self.assertEqual(manager.records['alice']['entry_id'],'entry-one')
        self.assertNotIn('fixture-token',str(manager.store.data))
        self.assertNotIn('flow_id',manager.records['alice'])

    async def test_no_devices_does_not_repeat_the_same_form_indefinitely(self):
        manager=self.manager();manager.records['alice']={'phase':'oauth','flow_id':'flow-alice','region':'ru'}
        homes={'type':'form','step_id':'homes_select','errors':{'base':'no_devices'},
            'data_schema':probatio.Schema({probatio.Required('home_infos'):multi_select({'home-one':'Fixture [0]'}),
                probatio.Required('area_name_rule',default='room'):str})}
        await manager._advance('alice',homes)
        self.assertEqual(manager.records['alice']['error'],'no_devices_in_region')
        manager.hass.config_entries.flow.async_configure.assert_not_awaited()

    async def test_callback_not_stored_or_returned_in_agent_summary(self):
        manager=self.manager();manager.records['alice']={'phase':'oauth','flow_id':'flow-alice','region':'ru'}
        manager._flows['alice']=deepcopy(OAUTH)
        callback=REDIRECT+'?code=fixture-private-code&state=fixture-only-state'
        with patch.object(module.webhook,'async_handle_webhook',AsyncMock()) as handle,patch.object(manager,'refresh',AsyncMock(return_value={'phase':'oauth'})):
            result=await manager.complete_callback('alice',callback)
        self.assertEqual(result,{'phase':'oauth'})
        self.assertEqual(handle.call_args.args[1],'fixture-hook')
        self.assertNotIn('fixture-private-code',str(manager.records))
        self.assertNotIn('authorization_url',manager._summary('alice'))

    async def test_completed_summary_uses_native_single_owner_device_entry(self):
        manager=self.manager();manager.records['alice']={'phase':'completed','entry_id':'entry-one','region':'ru'}
        manager.hass.config_entries.async_get_entry=lambda _:SimpleNamespace(state=SimpleNamespace(value='loaded'))
        device=DeviceEntry(config_entry_id='entry-one',id='fixture-device',identifiers={('fixture','device')})
        with patch.object(module.device_registry,'async_get',return_value=SimpleNamespace(devices=[device])),patch.object(module.entity_registry,'async_get',return_value=SimpleNamespace(entities={'fixture':SimpleNamespace(config_entry_id='entry-one')})):
            result=await manager.refresh('alice')
        self.assertEqual(result['device_count'],1)
        self.assertEqual(result['entity_count'],1)
        self.assertEqual(result['phase'],'completed')

    async def test_existing_entry_is_not_complete_until_it_is_loaded(self):
        manager=self.manager();manager.records['alice']={'phase':'error','region':'ru'}
        manager.hass.config_entries.async_entries=lambda _: [SimpleNamespace(entry_id='entry-retry',state=SimpleNamespace(value='setup_retry'))]
        manager._ui_state=AsyncMock(return_value={'phase':'configuring'})
        result=await manager.begin('alice','ru',True,'ru')
        self.assertEqual(result['phase'],'configuring')
        manager.hass.config_entries.flow.async_init.assert_not_awaited()

    async def test_telegram_origin_survives_restart_and_acknowledges_only_loaded_entry(self):
        manager = self.manager()
        origin = {"channel": "telegram", "owner": "alice", "entry_id": "bot-one",
                  "chat_id": "42", "bot_id": 99}
        requested = await manager.request("alice", origin)
        self.assertNotIn("trusted_origin", requested)
        self.assertEqual(manager.store.data["alice"]["trusted_origin"], origin)

        entry = SimpleNamespace(entry_id="xiaomi-one", state=SimpleNamespace(value="loaded"))
        manager.hass.config_entries.async_get_entry = lambda entry_id: entry if entry_id == entry.entry_id else None
        telegram = SimpleNamespace(notify_setup_complete=AsyncMock(return_value=True))
        manager.hass.data = {"habab_telegram": telegram}
        manager.records["alice"].update(
            phase="configuring", entry_id=entry.entry_id, boot_id="previous-boot")
        stored = deepcopy(manager.records)
        with patch.object(module, "Store", return_value=MemoryStore(stored)):
            restarted = XiaomiSetup(manager.hass)
        await restarted.setup()
        self.assertEqual(restarted.records["alice"]["trusted_origin"], origin)
        not_loaded = SimpleNamespace(entry_id="xiaomi-one", state=SimpleNamespace(value="setup_in_progress"))
        self.assertFalse(await restarted.entry_loaded(not_loaded))
        self.assertFalse(telegram.notify_setup_complete.await_count)
        self.assertFalse(await restarted.entry_loaded(
            SimpleNamespace(entry_id="foreign-entry", state=SimpleNamespace(value="loaded"))))
        self.assertTrue(await restarted.entry_loaded(entry))
        self.assertFalse(await restarted.entry_loaded(entry))
        telegram.notify_setup_complete.assert_awaited_once_with(origin, "xiaomi_home")
        self.assertTrue(restarted.records["alice"]["completion_delivered"])

    async def test_web_started_oauth_attaches_first_telegram_origin_without_retargeting(self):
        manager = self.manager()
        manager._loaded_at_start = True
        await manager.request("alice")
        manager.hass.config_entries.flow.async_init.return_value = {
            "type": "form", "step_id": "eula", "flow_id": "web-flow"}
        manager.hass.config_entries.flow.async_configure.side_effect = [
            {"type": "form", "step_id": "auth_config"}, dict(OAUTH)]
        web_form = await manager.begin("alice", "ru", True, "ru")
        self.assertEqual(web_form["phase"], "oauth")
        record = manager.records["alice"]
        original_flow_id = record["flow_id"]
        original_flow = manager._flows["alice"]
        original_origin = {"channel": "telegram", "owner": "alice", "entry_id": "bot-one",
                           "chat_id": "42", "bot_id": 99}

        joined = await manager.request("alice", original_origin)
        self.assertEqual(joined["phase"], "oauth")
        self.assertEqual(record["flow_id"], original_flow_id)
        self.assertIs(manager._flows["alice"], original_flow)
        self.assertEqual(record["trusted_origin"], original_origin)
        self.assertEqual(manager.store.data["alice"]["trusted_origin"], original_origin)

        other_origin = {**original_origin, "entry_id": "other-bot", "chat_id": "43", "bot_id": 100}
        await manager.request("alice", other_origin)
        self.assertEqual(record["trusted_origin"], original_origin)
        self.assertEqual(manager.store.data["alice"]["trusted_origin"], original_origin)

        entry = SimpleNamespace(entry_id="xiaomi-one", state=SimpleNamespace(value="setup_in_progress"))
        manager.hass.config_entries.async_get_entry = lambda entry_id: (
            entry if entry_id == entry.entry_id else None)
        telegram = SimpleNamespace(notify_setup_complete=AsyncMock(return_value=True))
        manager.hass.data = {"habab_telegram": telegram}
        await manager._advance("alice", {"type": "create_entry",
                                          "result": SimpleNamespace(entry_id=entry.entry_id)})
        entry.state.value = "loaded"
        self.assertTrue(await manager.entry_loaded(entry))
        telegram.notify_setup_complete.assert_awaited_once_with(original_origin, "xiaomi_home")
        self.assertTrue(record["completion_delivered"])

    async def test_completed_or_unknown_setup_cannot_gain_telegram_origin(self):
        origin = {"channel": "telegram", "owner": "alice", "entry_id": "bot-one",
                  "chat_id": "42", "bot_id": 99}
        entry = SimpleNamespace(entry_id="xiaomi-one", state=SimpleNamespace(value="loaded"))
        for phase in ("completed", "unknown"):
            with self.subTest(phase=phase):
                manager = self.manager()
                manager.records["alice"] = {"phase": phase, "entry_id": entry.entry_id}
                manager.hass.config_entries.async_get_entry = lambda entry_id: (
                    entry if entry_id == entry.entry_id else None)
                if phase == "completed":
                    manager.hass.config_entries.async_entries = lambda _domain: [entry]
                else:
                    manager.hass.config_entries.async_entries = lambda _domain: []
                with patch.object(module.device_registry, "async_get",
                                  return_value=SimpleNamespace(devices=[])), \
                        patch.object(module.entity_registry, "async_get",
                                     return_value=SimpleNamespace(entities={})):
                    result = await manager.request("alice", origin)
                self.assertEqual(result["phase"], phase)
                self.assertNotIn("trusted_origin", manager.records["alice"])

    async def test_configuring_entry_can_gain_first_telegram_origin_without_replacement(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="xiaomi-one", state=SimpleNamespace(value="setup_in_progress"))
        manager.records["alice"] = {
            "phase": "configuring", "entry_id": entry.entry_id, "region": "ru",
            "flow_id": "existing-flow",
        }
        manager.hass.config_entries.async_get_entry = lambda entry_id: (
            entry if entry_id == entry.entry_id else None)
        origin = {"channel": "telegram", "owner": "alice", "entry_id": "bot-one",
                  "chat_id": "42", "bot_id": 99}
        with patch.object(module.device_registry, "async_get",
                          return_value=SimpleNamespace(devices=[])), \
                patch.object(module.entity_registry, "async_get",
                             return_value=SimpleNamespace(entities={})):
            result = await manager.request("alice", origin)
        self.assertEqual(result["phase"], "configuring")
        self.assertEqual(manager.records["alice"]["entry_id"], entry.entry_id)
        self.assertEqual(manager.records["alice"]["flow_id"], "existing-flow")
        self.assertIs(manager.hass.config_entries.async_get_entry(entry.entry_id), entry)
        self.assertEqual(manager.records["alice"]["trusted_origin"], origin)

    def test_safe_origin_preserves_only_complete_valid_member_identity(self):
        owner_origin = {"channel": "telegram", "owner": "alice", "entry_id": "bot-one",
                        "chat_id": "42", "bot_id": 99}
        member_origin = {**owner_origin, "chat_id": "50", "group_id": "-200", "user_id": 50}
        self.assertEqual(XiaomiSetup._safe_origin(owner_origin, "alice"), owner_origin)
        self.assertEqual(XiaomiSetup._safe_origin(member_origin, "alice"), member_origin)

        malformed_origins = (
            {key: value for key, value in member_origin.items() if key != "user_id"},
            {key: value for key, value in member_origin.items() if key != "group_id"},
            {**member_origin, "owner": "bob"},
            {**member_origin, "bot_id": True},
            {**member_origin, "bot_id": 0},
            {**member_origin, "user_id": True},
            {**member_origin, "user_id": 0},
            {**member_origin, "user_id": -50},
            {**member_origin, "user_id": "50"},
            {**member_origin, "group_id": "200"},
            {**member_origin, "group_id": "--200"},
            {**member_origin, "group_id": "-²"},
            {**member_origin, "group_id": "-１２"},
            {**member_origin, "group_id": "-" + "2" * 33},
            {**member_origin, "chat_id": "51"},
        )
        for malformed in malformed_origins:
            with self.subTest(origin=malformed):
                self.assertIsNone(XiaomiSetup._safe_origin(malformed, "alice"))

    async def test_member_origin_survives_restart_and_notifies_member_dm(self):
        xiaomi = self.manager()
        origin = {"channel": "telegram", "owner": "alice", "entry_id": "entry",
                  "chat_id": "50", "bot_id": 99, "group_id": "-200", "user_id": 50}
        await xiaomi.request("alice", origin)
        entry = SimpleNamespace(entry_id="xiaomi-one", state=SimpleNamespace(value="setup_in_progress"))
        xiaomi.hass.config_entries.async_get_entry = lambda entry_id: (
            entry if entry_id == entry.entry_id else None)

        bot_entry = SimpleNamespace(entry_id="entry", data={
            "owner_id": "alice", "bot_username": "testbot", "bot_id": 99,
        })
        telegram = object.__new__(TelegramManager)
        telegram.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(
                return_value=SimpleNamespace(is_active=True, is_admin=True))),
            config_entries=SimpleNamespace(async_entries=lambda _domain: [bot_entry]),
        )
        telegram.bindings = {"entry": {"chat_id": "10", "user_id": "20", "bot_id": 99}}
        telegram.entries = {"entry": {
            "chat_id": "10", "user_id": "20", "ready": True, "username": "testbot",
        }}
        telegram.group_bindings = {"entry": {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }}
        telegram.get_chat_member = AsyncMock(side_effect=lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "member", "is_member": True}))
        telegram.send = AsyncMock(return_value=True)
        xiaomi.hass.data = {"habab_telegram": telegram}
        xiaomi.records["alice"].update(
            phase="configuring", entry_id=entry.entry_id, boot_id="previous-boot")
        with patch.object(module, "Store", return_value=MemoryStore(deepcopy(xiaomi.records))):
            restarted = XiaomiSetup(xiaomi.hass)
        await restarted.setup()
        self.assertEqual(restarted.records["alice"]["trusted_origin"], origin)

        entry.state.value = "loaded"
        telegram.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "left", "is_member": False})
        self.assertFalse(await restarted.entry_loaded(entry))
        telegram.send.assert_not_awaited()
        self.assertNotIn("completion_delivered", restarted.records["alice"])

        telegram.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "member", "is_member": True})
        self.assertTrue(await restarted.entry_loaded(entry))
        telegram.send.assert_awaited_once_with(
            "entry", "50", "Настройка xiaomi_home завершена: интеграция загружена в Home Assistant.")
        self.assertNotEqual(telegram.send.await_args.args[1], "-200")
        self.assertTrue(restarted.records["alice"]["completion_delivered"])

    async def test_failed_restart_returns_to_retry_and_preserves_no_exception_text(self):
        manager=self.manager();manager.records['alice']={'phase':'restart_required','region':'ru'}
        tasks=[]
        def start(coro):
            task=asyncio.create_task(coro);tasks.append(task);return task
        manager.hass.async_create_task=start
        manager.hass.services=SimpleNamespace(async_call=AsyncMock(side_effect=RuntimeError('fixture-private-message')))
        with patch.object(module.asyncio,'sleep',AsyncMock()):
            await manager.restart('alice')
            await asyncio.gather(*tasks)
        self.assertEqual(manager.records['alice']['phase'],'restart_required')
        self.assertEqual(manager.records['alice']['error'],'restart_failed')
        self.assertNotIn('fixture-private-message',str(manager.store.data))
