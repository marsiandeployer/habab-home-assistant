"""Owner-scoped UI flows use Home Assistant's native schema serializer."""
import asyncio
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import homeassistant
import voluptuous as vol
from homeassistant.components.habab_home.integration_setup import IntegrationSetup
from homeassistant.components.habab_home import integration_setup as setup_module
from homeassistant.components.habab_home.const import DOMAIN
from homeassistant.data_entry_flow import FlowHandler, InvalidData, UnknownFlow
from homeassistant.helpers.selector import SelectSelector


class FlowManager:
    def __init__(self, initial=None, configured=None, progress=None):
        self.initial = initial or {}
        self.configured = configured or {}
        self.progress = progress or []
        self.async_configure = AsyncMock(side_effect=self.configure)
        self.async_abort = unittest.mock.Mock()
        self.async_init = AsyncMock(side_effect=self.initialize)

    async def initialize(self, *_args, **_kwargs):
        return self.initial

    async def configure(self, flow_id, user_input=None):
        result = self.configured.get(flow_id)
        if isinstance(result, Exception):
            raise result
        return result or {"type": "form", "flow_id": flow_id, "step_id": "user", "data_schema": None}

    def async_progress(self):
        return self.progress


def setup_controller(flow=None, options=None, entries=None):
    hass = SimpleNamespace(
        config=SimpleNamespace(language="en"),
        config_entries=SimpleNamespace(
            flow=flow or FlowManager(), options=options or FlowManager(),
            async_get_entry=(entries or {}).get,
        ),
    )
    runtime = SimpleNamespace(flow_owners={})
    controller = IntegrationSetup(hass, runtime)
    controller._translations = AsyncMock(return_value={
        "component.fixture.config.step.user.title": "Fixture setup",
        "component.fixture.config.step.user.data.username": "User name",
        "component.fixture.config.step.user.data.password": "Password",
    })
    return controller, hass, runtime


class IntegrationSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_native_core_schema_serializer_and_strips_all_defaults(self):
        controller, _, _ = setup_controller()
        schema = vol.Schema({
            vol.Required("username", default="fixture-default-secret"): str,
            vol.Optional("password", default="fixture-password-secret"): str,
            vol.Optional("region"): SelectSelector({"options": ["eu", "us"]}),
        })
        result = await controller._view({
            "type": "form", "flow_id": "flow-one", "step_id": "user", "data_schema": schema,
            "data": {"password": "fixture-entry-secret"},
            "description_placeholders": {"secret": "fixture-placeholder-secret"},
        }, "fixture", "en", "config")
        self.assertEqual(result["phase"], "form")
        fields = {field["name"]: field for field in result["data_schema"]}
        self.assertEqual(fields["username"]["type"], "string")
        self.assertEqual(fields["username"]["label"], "User name")
        self.assertEqual(fields["password"]["label"], "Password")
        self.assertIn("select", fields["region"]["selector"])
        encoded = json.dumps(result)
        for secret in ("fixture-default-secret", "fixture-password-secret", "fixture-entry-secret"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(result["description_placeholders"], {"secret": "fixture-placeholder-secret"})
        self.assertNotIn("data", result)
        self.assertEqual(result["handler"], "fixture")
        self.assertEqual(result["translation_domain"], "fixture")
        self.assertFalse(result["last_step"])

    async def test_form_values_go_directly_to_ha_and_are_never_echoed_or_stored(self):
        schema = vol.Schema({vol.Required("password"): str})
        manager = FlowManager(
            initial={"type": "form", "flow_id": "flow-one", "step_id": "user", "data_schema": schema},
            configured={"flow-one": {"type": "abort", "flow_id": "flow-one", "reason": "already_configured"}},
        )
        controller, _, runtime = setup_controller(flow=manager)
        interaction = await controller.start("alice", "fixture")
        private_value = "fixture-user-secret-value"
        result = await controller.submit("alice", interaction["id"], {"password": private_value})
        self.assertEqual(result["phase"], "abort")
        self.assertEqual(manager.async_configure.await_args.args, ("flow-one", {"password": private_value}))
        serialized = json.dumps(result) + repr(controller.interactions)
        self.assertNotIn(private_value, serialized)
        self.assertNotIn("flow-one", runtime.flow_owners)
        with self.assertRaises(ValueError):
            await controller.submit("alice", interaction["id"], {"password": "replay"})

    async def test_native_invalid_data_keeps_form_and_allows_corrected_resubmit(self):
        schema = vol.Schema({vol.Required("when"): str})
        manager = FlowManager(initial={"type": "form", "flow_id": "validation-flow", "step_id": "user",
                                       "data_schema": schema})
        manager.async_configure = AsyncMock(side_effect=[
            InvalidData("invalid", ["when"], "invalid", {"when": "invalid_value", "unknown": "nope"}),
            {"type": "form", "flow_id": "validation-flow", "step_id": "user", "data_schema": schema},
            {"type": "abort", "flow_id": "validation-flow", "reason": "already_configured"},
        ])
        controller, _, runtime = setup_controller(flow=manager)
        interaction = await controller.start("alice", "fixture")
        invalid = await controller.submit("alice", interaction["id"], {"when": "bad"})
        self.assertEqual(invalid["phase"], "form")
        self.assertEqual(invalid["errors"], {"when": "invalid_value"})
        self.assertEqual(invalid["flow_id"], "validation-flow")
        self.assertEqual(runtime.flow_owners["validation-flow"], ("alice", "config"))
        valid = await controller.submit("alice", interaction["id"], {"when": "2026-01-01"})
        self.assertEqual(valid["phase"], "abort")
        self.assertEqual(manager.async_configure.await_args_list[0].args, (
            "validation-flow", {"when": "bad"},
        ))
        self.assertEqual(manager.async_configure.await_args_list[2].args, (
            "validation-flow", {"when": "2026-01-01"},
        ))

    async def test_cross_owner_submit_and_dismiss_are_rejected(self):
        manager = FlowManager(initial={"type": "form", "flow_id": "flow-one", "step_id": "user",
                                       "data_schema": vol.Schema({vol.Required("host"): str})})
        controller, _, _ = setup_controller(flow=manager)
        interaction = await controller.start("alice", "fixture")
        with self.assertRaisesRegex(ValueError, "unknown_interaction"):
            await controller.submit("bob", interaction["id"], {"host": "secret"})
        with self.assertRaisesRegex(ValueError, "unknown_interaction"):
            await controller.dismiss("bob", interaction["id"])
        manager.async_configure.assert_not_awaited()

    async def test_adoption_is_limited_to_unowned_discovery_flows(self):
        manager = FlowManager(
            configured={"discovery-one": {"type": "menu", "flow_id": "discovery-one",
                                          "step_id": "choose", "menu_options": ["configure"]}},
            progress=[
                {"flow_id": "discovery-one", "handler": "fixture", "context": {"source": "zeroconf"}},
                {"flow_id": "owned-flow", "handler": "fixture", "context": {"source": "dhcp"}},
                {"flow_id": "user-flow", "handler": "fixture", "context": {"source": "user"}},
            ],
        )
        controller, _, runtime = setup_controller(flow=manager)
        runtime.flow_owners["owned-flow"] = ("bob", "config")
        adopted = await controller.adopt("alice", "discovery-one")
        self.assertEqual(adopted["phase"], "menu")
        self.assertEqual(runtime.flow_owners["discovery-one"], ("alice", "config"))
        for flow_id in ("owned-flow", "user-flow"):
            with self.subTest(flow_id=flow_id), self.assertRaisesRegex(ValueError, "unknown_flow"):
                await controller.adopt("alice", flow_id)

    async def test_options_flow_uses_same_owner_scoped_ui_path(self):
        entry = SimpleNamespace(entry_id="entry-one", domain="fixture",
                                state=SimpleNamespace(value="loaded"))
        options = FlowManager(initial={"type": "form", "flow_id": "options-one", "step_id": "init",
                                       "data_schema": vol.Schema({vol.Required("token"): str})})
        controller, _, runtime = setup_controller(options=options, entries={"entry-one": entry})
        interaction = await controller.options_start("alice", "entry-one")
        self.assertEqual(interaction["kind"], "options")
        self.assertEqual(interaction["entry_id"], "entry-one")
        self.assertEqual(runtime.flow_owners["options-one"], ("alice", "options"))
        with self.assertRaisesRegex(ValueError, "unknown_interaction"):
            await controller.submit("bob", interaction["id"], {"token": "fixture-secret"})
        options.async_configure.assert_not_awaited()

    async def test_options_entry_id_is_protected_ui_only_and_requires_existing_entry(self):
        entry = SimpleNamespace(entry_id="entry-one", domain="fixture",
                                state=SimpleNamespace(value="loaded"))
        options = FlowManager(initial={"type": "form", "flow_id": "options-one", "step_id": "init",
                                       "data_schema": vol.Schema({})})
        controller, hass, _ = setup_controller(options=options, entries={"entry-one": entry})
        protected = await controller.options_start("alice", "entry-one")
        self.assertEqual(protected["entry_id"], "entry-one")
        model_summary = await controller.agent_action("alice", "options_start", {"entry_id": "entry-one"})
        self.assertNotIn("entry_id", model_summary)
        hass.config_entries.async_get_entry = lambda _entry_id: None
        self.assertNotIn("entry_id", (await controller.status("alice"))["interactions"][0])

    async def test_model_summary_never_contains_form_schema_defaults_or_external_url(self):
        manager = FlowManager(
            initial={"type": "external", "flow_id": "oauth-flow", "step_id": "oauth",
                     "url": "https://example.invalid/authorize?state=fixture-secret"},
            configured={"oauth-flow": {"type": "external", "flow_id": "oauth-flow", "step_id": "oauth",
                                        "url": "https://example.invalid/authorize?state=fixture-secret"}},
        )
        controller, _, _ = setup_controller(flow=manager)
        result = await controller.agent_action("alice", "start_integration", {"domain": "fixture"})
        serialized = json.dumps(result)
        self.assertEqual(result["phase"], "external")
        self.assertNotIn("url", result)
        self.assertNotIn("fixture-secret", serialized)
        self.assertNotIn("data_schema", result)

        status = await controller.agent_action("alice", "integration_setup_status", {})
        serialized_status = json.dumps(status)
        self.assertNotIn("url", serialized_status)
        self.assertNotIn("data_schema", serialized_status)
        self.assertNotIn("fixture-secret", serialized_status)

    async def test_model_summary_has_only_native_time_date_field_structure(self):
        from homeassistant.components.time_date.config_flow import USER_SCHEMA

        manager = FlowManager(initial={"type": "form", "flow_id": "time-date-flow", "step_id": "user",
                                       "data_schema": USER_SCHEMA},
                              configured={"time-date-flow": {"type": "form", "flow_id": "time-date-flow",
                                                              "step_id": "user", "data_schema": USER_SCHEMA}})
        controller, _, _ = setup_controller(flow=manager)
        summary = await controller.agent_action("alice", "start_integration", {"domain": "time_date"})
        self.assertEqual(summary["input_fields"], [
            {"name": "display_options", "type": "select", "required": True},
        ])
        encoded = json.dumps(summary)
        self.assertNotIn("date_time_utc", encoded)
        self.assertNotIn("selector", encoded)
        status = await controller.agent_action("alice", "integration_setup_status", {})
        self.assertEqual(status["interactions"][0]["input_fields"], summary["input_fields"])
        with self.assertRaisesRegex(ValueError, "protected_ui_required"):
            await controller.agent_action("alice", "continue_integration", {
                "flow_id": "time-date-flow", "input": {"display_options": "date"},
            })

    async def test_model_field_metadata_excludes_defaults_and_selector_values(self):
        schema = vol.Schema({
            vol.Required("display_options", default="fixture-default-secret"): SelectSelector({
                "options": ["fixture-secret-option", "visible-choice"],
            }),
        })
        manager = FlowManager(initial={"type": "form", "flow_id": "metadata-flow", "step_id": "user",
                                       "data_schema": schema})
        controller, _, _ = setup_controller(flow=manager)
        summary = await controller.agent_action("alice", "start_integration", {"domain": "fixture"})
        self.assertEqual(summary["input_fields"], [
            {"name": "display_options", "type": "select", "required": True},
        ])
        encoded = json.dumps(summary)
        self.assertNotIn("fixture-default-secret", encoded)
        self.assertNotIn("fixture-secret-option", encoded)
        self.assertNotIn("visible-choice", encoded)
        self.assertNotIn("data_schema", summary)

    async def test_agent_cannot_submit_generic_secret_form_values(self):
        manager = FlowManager(initial={"type": "form", "flow_id": "flow-one", "step_id": "user",
                                       "data_schema": vol.Schema({vol.Required("password"): str})})
        controller, _, _ = setup_controller(flow=manager)
        await controller.agent_action("alice", "start_integration", {"domain": "fixture"})
        with self.assertRaisesRegex(ValueError, "protected_ui_required"):
            await controller.agent_action("alice", "continue_integration", {
                "flow_id": "flow-one", "input": {"password": "fixture-secret"},
            })
        manager.async_configure.assert_not_awaited()

    async def test_menu_submission_is_limited_to_native_options(self):
        native_handler = FlowHandler()
        native_handler.flow_id = "menu-flow"
        native_menu = native_handler.async_show_menu(
            step_id="choose",
            menu_options={"configure": "Native configure label", "ignore": "Native ignore label"},
        )
        manager = FlowManager(initial=native_menu,
                              configured={"menu-flow": {"type": "abort", "flow_id": "menu-flow",
                                                         "reason": "user"}})
        controller, _, _ = setup_controller(flow=manager)
        controller._translations = AsyncMock(return_value={
            "component.fixture.config.step.choose.menu_options.configure": "Translated configure",
            "component.fixture.config.step.choose.menu_options.ignore": "Translated ignore",
        })
        protected = await controller.start("alice", "fixture")
        result = await controller.agent_action("alice", "start_integration", {"domain": "fixture"})
        self.assertEqual(result["menu_options"], ["configure", "ignore"])
        self.assertEqual(result["menu_field"], "next_step_id")
        self.assertEqual(protected["menu_options"], [
            {"value": "configure", "label": "Translated configure"},
            {"value": "ignore", "label": "Translated ignore"},
        ])
        with self.assertRaisesRegex(ValueError, "protected_ui_required"):
            await controller.agent_action("alice", "continue_integration", {
                "flow_id": "menu-flow", "input": {"next_step_id": "arbitrary"},
            })
        with self.assertRaisesRegex(ValueError, "protected_ui_required"):
            await controller.agent_action("alice", "continue_integration", {
                "flow_id": "menu-flow", "input": {"next_step": "configure"},
            })
        result = await controller.agent_action("alice", "continue_integration", {
            "flow_id": "menu-flow", "input": {"next_step_id": "configure"},
        })
        self.assertEqual(result["phase"], "abort")
        self.assertEqual(manager.async_configure.await_args.args, (
            "menu-flow", {"next_step_id": "configure"},
        ))

    async def test_stale_flow_is_reported_expired(self):
        manager = FlowManager(initial={"type": "form", "flow_id": "stale-flow", "step_id": "user",
                                       "data_schema": vol.Schema({})},
                              configured={"stale-flow": UnknownFlow("stale-flow")})
        controller, _, runtime = setup_controller(flow=manager)
        interaction = await controller.start("alice", "fixture")
        status = await controller.status("alice")
        self.assertEqual(status["interactions"][0]["phase"], "expired")
        self.assertNotIn("stale-flow", runtime.flow_owners)

    async def test_terminal_submit_wins_over_queued_status_and_lock_is_reclaimed(self):
        manager = FlowManager(
            initial={"type": "form", "flow_id": "terminal-flow", "step_id": "user",
                     "data_schema": vol.Schema({})},
            configured={"terminal-flow": {"type": "abort", "flow_id": "terminal-flow",
                                           "reason": "already_configured"}},
        )
        controller, _, _ = setup_controller(flow=manager)
        interaction = await controller.start("alice", "fixture")
        native_lock = controller._flow_locks["terminal-flow"]
        await native_lock.acquire()
        submit = asyncio.create_task(controller.submit("alice", interaction["id"], {}))
        await asyncio.sleep(0)
        poll = asyncio.create_task(controller.status("alice"))
        await asyncio.sleep(0)
        native_lock.release()

        submitted, polled = await asyncio.gather(submit, poll)
        self.assertEqual(submitted["phase"], "abort")
        self.assertEqual(polled["interactions"][0]["phase"], "abort")
        self.assertEqual(manager.async_configure.await_count, 1)
        self.assertNotIn("terminal-flow", controller._flow_locks)
        self.assertEqual(await controller.dismiss("alice", interaction["id"]), {"dismissed": True})
        self.assertEqual(await controller.status("alice"), {"interactions": []})

    async def test_terminal_initial_result_does_not_leave_unused_flow_lock(self):
        manager = FlowManager(initial={"type": "abort", "flow_id": "already-done",
                                       "reason": "already_configured"})
        controller, _, _ = setup_controller(flow=manager)
        result = await controller.start("alice", "fixture")
        self.assertEqual(result["phase"], "abort")
        self.assertNotIn("already-done", controller._flow_locks)

    async def test_description_placeholders_are_protected_ui_only(self):
        manager = FlowManager(initial={
            "type": "form", "flow_id": "placeholder-flow", "step_id": "user",
            "data_schema": vol.Schema({vol.Required("username"): str}),
            "description_placeholders": {"instructions": "private onboarding hint"},
        })
        controller, _, _ = setup_controller(flow=manager)
        protected = await controller.start("alice", "fixture")
        self.assertEqual(protected["description_placeholders"], {
            "instructions": "private onboarding hint",
        })
        agent = await controller.agent_action("alice", "start_integration", {"domain": "fixture"})
        self.assertNotIn("private onboarding hint", json.dumps(agent))
        self.assertNotIn("description_placeholders", agent)
        filtered = setup_module._safe_description_placeholders({
            "huge_text": "x" * 2049,
            "nested": {"secret": "not-json-primitive"},
            "not_finite": math.nan,
            "huge_number": 10**30,
            "normal": "bounded",
        })
        self.assertEqual(filtered, {"normal": "bounded"})

    async def test_create_entry_is_not_done_until_actual_entry_is_loaded(self):
        entry = SimpleNamespace(entry_id="entry-one", state=SimpleNamespace(value="setup_in_progress"))
        entries = {"entry-one": entry}
        manager = FlowManager(initial={"type": "create_entry", "result": entry})
        controller, hass, _ = setup_controller(flow=manager, entries=entries)
        with patch("homeassistant.components.habab_home.integration_setup.entity_registry.async_get",
                   return_value=object()), \
             patch("homeassistant.components.habab_home.integration_setup.entity_registry.async_entries_for_config_entry",
                   return_value=[]):
            result = await controller.start("alice", "fixture")
            self.assertEqual(result["phase"], "progress")
            entry.state.value = "loaded"
            status = await controller.status("alice")
        self.assertEqual(status["interactions"][0]["phase"], "done")
        self.assertEqual(status["interactions"][0]["entry_state"], "loaded")
        self.assertEqual(status["interactions"][0]["entity_count"], 0)
        self.assertNotIn("entry_id", status["interactions"][0])
        self.assertTrue(hass.config_entries.async_get_entry("entry-one"))

    async def test_cancel_aborts_flow_without_deleting_any_existing_entry(self):
        manager = FlowManager(initial={"type": "form", "flow_id": "flow-one", "step_id": "user",
                                       "data_schema": vol.Schema({})})
        existing_entry = SimpleNamespace(entry_id="existing-entry")
        controller, hass, runtime = setup_controller(flow=manager, entries={"existing-entry": existing_entry})
        interaction = await controller.start("alice", "fixture")
        result = await controller.cancel("alice", interaction["id"])
        self.assertEqual(result["phase"], "abort")
        manager.async_abort.assert_called_once_with("flow-one")
        self.assertEqual(runtime.flow_owners, {})
        self.assertFalse(hasattr(hass.config_entries, "async_remove"))
        self.assertEqual(await controller.dismiss("alice", interaction["id"]), {"dismissed": True})
        self.assertIs(hass.config_entries.async_get_entry("existing-entry"), existing_entry)

    async def test_websocket_boundary_requires_active_admin_and_sanitizes_errors(self):
        class Connection:
            def __init__(self, user):
                self.user = user
                self.results = []
                self.errors = []

            def send_result(self, *args):
                self.results.append(args)

            def send_error(self, *args):
                self.errors.append(args)

        operation = AsyncMock(side_effect=ValueError("fixture-private-error"))
        hass = SimpleNamespace(data={DOMAIN: {"integration_setup": object()}})
        inactive = Connection(SimpleNamespace(id="alice", is_active=False, is_admin=True))
        await setup_module._dispatch(hass, inactive, {"id": 9}, operation)
        self.assertEqual(inactive.errors, [(9, "unauthorized", "unauthorized")])
        operation.assert_not_awaited()

        active = Connection(SimpleNamespace(id="alice", is_active=True, is_admin=True))
        await setup_module._dispatch(hass, active, {"id": 10}, operation)
        self.assertEqual(active.errors, [(10, "setup_failed", "setup_failed")])
        self.assertNotIn("fixture-private-error", json.dumps(active.errors))

    async def test_websocket_registration_exposes_the_six_setup_commands(self):
        with patch.object(setup_module.websocket_api, "async_register_command") as register:
            setup_module.async_register_commands(object())
        self.assertEqual({call.args[1].__name__ for call in register.call_args_list}, {
            "setup_status", "setup_start", "setup_adopt", "setup_submit", "setup_cancel", "setup_dismiss",
        })


if __name__ == "__main__":
    unittest.main()
