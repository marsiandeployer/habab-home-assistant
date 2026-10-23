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
