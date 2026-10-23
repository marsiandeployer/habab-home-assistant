"""Owner-scoped native Home Assistant config and options flows."""
import asyncio
from contextlib import asynccontextmanager
import math
import re
import secrets
from urllib.parse import urlsplit

import probatio

from homeassistant.components import websocket_api
from homeassistant.config_entries import DISCOVERY_SOURCES, SOURCE_USER
from homeassistant.data_entry_flow import InvalidData, UnknownFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN


DOMAIN_PATTERN = re.compile(r"[a-z0-9_]{1,80}\Z")
ERROR_PATTERN = re.compile(r"[a-z0-9_]{1,80}\Z")
LANGUAGE_PATTERN = re.compile(r"[a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,8}){0,2}\Z")
MODEL_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
MODEL_FIELD_TYPE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
TERMINAL_PHASES = {"done", "abort", "error", "expired"}
SAFE_ERRORS = {
    "invalid_domain", "needs_xiaomi_card", "unknown_flow", "unknown_entry",
    "invalid_input", "unknown_interaction", "flow_not_submittable",
    "flow_not_cancellable", "interaction_active", "protected_ui_required",
    "invalid_language", "setup_failed", "unsupported_schema",
}


def _strip_defaults(value):
    if isinstance(value, dict):
        return {
            key: _strip_defaults(item)
            for key, item in value.items()
            if key not in {"default", "suggested_value"}
        }
    if isinstance(value, list):
        return [_strip_defaults(item) for item in value]
    return value


def _translate_fields(fields, translations, domain, step_id, section_path=""):
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if isinstance(name, str) and field.get("type") == "expandable":
            field["label"] = translations.get(
                f"component.{domain}.config.step.{step_id}.sections.{name}.name", name
            )
            child_path = f"sections.{name}."
        else:
            child_path = section_path
            if isinstance(name, str):
                prefix = f"{section_path}data."
                field["label"] = translations.get(
                    f"component.{domain}.config.step.{step_id}.{prefix}{name}", name
                )
                description = translations.get(
                    f"component.{domain}.config.step.{step_id}.{section_path}data_description.{name}"
                )
                if description:
                    field["description"] = description
        schema = field.get("schema")
        if isinstance(schema, list):
            _translate_fields(schema, translations, domain, step_id, child_path)


def _safe_errors(errors, field_names):
    if not isinstance(errors, dict):
        return {}
    safe = {}
    for key, value in errors.items():
        if key != "base" and key not in field_names:
            continue
        safe[key] = value if isinstance(value, str) and ERROR_PATTERN.fullmatch(value) else "invalid"
    return safe


def _field_names(fields):
    names = set()
    if not isinstance(fields, list):
        return names
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if isinstance(name, str):
            names.add(name)
        names.update(_field_names(field.get("schema")))
    return names


def _model_input_fields(view):
    """Expose only bounded technical field structure, never form contents."""
    if view.get("phase") != "form" or not isinstance(view.get("data_schema"), list):
        return None
    fields = []
    for field in view["data_schema"][:50]:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not isinstance(name, str) or not MODEL_FIELD_NAME.fullmatch(name):
            continue
        native_type = field.get("type")
        if not isinstance(native_type, str) or not MODEL_FIELD_TYPE.fullmatch(native_type):
            selector = field.get("selector")
            selector_type = next(iter(selector)) if isinstance(selector, dict) and len(selector) == 1 else None
            native_type = selector_type if isinstance(selector_type, str) and MODEL_FIELD_TYPE.fullmatch(selector_type) else None
        if native_type is None or not isinstance(field.get("required"), bool):
            continue
        fields.append({"name": name, "type": native_type, "required": field["required"]})
    return fields


def _safe_description_placeholders(value):
    """Keep bounded JSON primitives for the protected native form only."""
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key, item in list(value.items())[:50]:
        if not isinstance(key, str) or not key or len(key) > 128:
            continue
        if isinstance(item, str) and len(item) <= 2048:
            safe[key] = item
        elif item is None or isinstance(item, bool):
            safe[key] = item
        elif isinstance(item, int) and abs(item) <= 10**12:
            safe[key] = item
        elif isinstance(item, float) and math.isfinite(item) and abs(item) <= 10**12:
            safe[key] = item
    return safe


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


class IntegrationSetup:
    """Keep generic flow ownership and UI state in memory for this HA runtime."""

    def __init__(self, hass, runtime):
        self.hass = hass
        self.runtime = runtime
        self.interactions = {}
        self._lock = asyncio.Lock()
        self._flow_locks = {}
        self._flow_lock_refs = {}
        self._creation_locks = {}

    @property
    def flow_owners(self):
        return self.runtime.flow_owners

    def _flow_manager(self, kind):
        return self.hass.config_entries.options if kind == "options" else self.hass.config_entries.flow

    def _record_for(self, owner, interaction_id):
        record = self.interactions.get(interaction_id)
        if not record or record["owner"] != owner:
            raise ValueError("unknown_interaction")
        return record

    def _language(self, language):
        if not isinstance(language, str) or len(language) > 20 or not LANGUAGE_PATTERN.fullmatch(language):
            raise ValueError("invalid_language")
        return language

    def _flow_for(self, kind, flow_id):
        return next((item for item in self._flow_manager(kind).async_progress()
                     if item.get("flow_id") == flow_id), None)

    @asynccontextmanager
    async def _locked_flow(self, flow_id):
        async with self._lock:
            lock = self._flow_locks.setdefault(flow_id, asyncio.Lock())
            self._flow_lock_refs[flow_id] = self._flow_lock_refs.get(flow_id, 0) + 1
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            async with self._lock:
                references = self._flow_lock_refs.get(flow_id, 1) - 1
                if references <= 0:
                    active = any(record.get("flow_id") == flow_id and record["phase"] not in TERMINAL_PHASES
                                 for record in self.interactions.values())
                    if not active:
                        self._flow_locks.pop(flow_id, None)
                        self._flow_lock_refs.pop(flow_id, None)
                    else:
                        self._flow_lock_refs[flow_id] = 0
                else:
                    self._flow_lock_refs[flow_id] = references

    def _abort_native_flow(self, kind, flow_id):
        try:
            self._flow_manager(kind).async_abort(flow_id)
        except Exception:
            pass

    async def _translations(self, domain, language):
        if not isinstance(language, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{2,20}", language):
            language = self.hass.config.language
        try:
            return await async_get_translations(self.hass, language, "config", {domain})
        except Exception:
            return {}

    async def _view(self, result, domain, language, kind, entry_id=None):
        result_type = result.get("type")
        step_id = result.get("step_id")
        translation_domain = result.get("translation_domain")
        if not isinstance(translation_domain, str) or not DOMAIN_PATTERN.fullmatch(translation_domain):
            translation_domain = domain
        translations = await self._translations(translation_domain, language)
        if result_type not in {"form", "menu", "progress", "progress_done", "external", "create_entry", "abort"}:
            result_type = "error"
        view = {
            "domain": domain, "handler": domain, "kind": kind, "phase": "error",
            "type": result_type, "translation_domain": translation_domain,
            "last_step": result.get("last_step") is True,
            "description_placeholders": _safe_description_placeholders(result.get("description_placeholders")),
        }
        if isinstance(step_id, str):
            view["step_id"] = step_id

        if result_type == "form":
            raw_schema = result.get("data_schema")
            fields = []
            if raw_schema is not None:
                try:
                    serialized = probatio.to_field_list(raw_schema, custom_serializer=cv.custom_serializer)
                except Exception:
                    view.update(phase="error", errors={"base": "unsupported_schema"})
                    return view
                for raw_field in serialized:
                    field = _strip_defaults(raw_field)
                    fields.append(field)
                _translate_fields(fields, translations, translation_domain, step_id)
            view.update(phase="form", data_schema=fields,
                        errors=_safe_errors(result.get("errors"), {
                            field["name"] for field in fields
                            if isinstance(field, dict) and isinstance(field.get("name"), str)
                        }))
            if isinstance(result.get("title"), str):
                view["title"] = result["title"]
            elif isinstance(step_id, str):
                title = translations.get(f"component.{translation_domain}.config.step.{step_id}.title")
                if title:
                    view["title"] = title
        elif result_type == "menu":
            raw_options = result.get("menu_options", [])
            if isinstance(raw_options, dict):
                pairs = [(key, label) for key, label in raw_options.items()]
            elif isinstance(raw_options, (list, tuple)):
                pairs = [(item, None) for item in raw_options]
            elif isinstance(raw_options, (set, frozenset)):
                pairs = [(item, None) for item in sorted(raw_options)]
            else:
                pairs = []
            options = []
            for item, native_label in pairs[:100]:
                if not isinstance(item, str) or not item or len(item) > 128:
                    continue
                translated = translations.get(
                    f"component.{translation_domain}.config.step.{step_id}.menu_options.{item}"
                )
                label = translated if isinstance(translated, str) else native_label
                options.append({"value": item, "label": label if isinstance(label, str) else item})
            if result.get("sort") is True:
                options.sort(key=lambda option: option["label"].casefold())
            view.update(phase="menu", menu_field="next_step_id", menu_options=options)
        elif result_type in {"progress", "progress_done"}:
            view["phase"] = "progress"
            progress = result.get("progress")
            if isinstance(progress, (int, float)) and 0 <= progress <= 1:
                view["progress"] = progress
        elif result_type == "external":
            view["phase"] = "external"
            url = _safe_url(result.get("url"))
            if url:
                view["url"] = url
        elif result_type == "create_entry":
            if kind == "config":
                entry = result.get("result")
                entry_id = getattr(entry, "entry_id", None)
            view.update(phase="progress", entry_id=entry_id)
            self._add_entry_state(view, entry_id)
        elif result_type == "abort":
            view["phase"] = "abort"
            reason = result.get("reason")
            if isinstance(reason, str) and ERROR_PATTERN.fullmatch(reason):
                view["errors"] = {"base": reason}
        else:
            view["errors"] = {"base": "setup_failed"}
        return view

    def _add_entry_state(self, view, entry_id):
        if not isinstance(entry_id, str):
            view.update(phase="error", errors={"base": "entry_missing"})
            return
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            view.update(phase="error", errors={"base": "entry_missing"})
            return
        view["entry_state"] = entry.state.value
        registry = entity_registry.async_get(self.hass)
        view["entity_count"] = len(entity_registry.async_entries_for_config_entry(registry, entry_id))
        if entry.state.value == "loaded":
            view["phase"] = "done"
        elif entry.state.value == "setup_error":
            view.update(phase="error", errors={"base": "setup_failed"})
        else:
            view["phase"] = "progress"

    def _public_view(self, record):
        allowed = ("id", "flow_id", "domain", "handler", "kind", "phase", "type", "step_id",
                   "title", "translation_domain", "last_step", "description_placeholders", "data_schema",
                   "errors", "menu_field", "menu_options", "url", "entry_state", "entity_count", "progress")
        view = {key: record[key] for key in allowed if key in record}
        entry_id = record.get("entry_id")
        if record["kind"] == "options" and isinstance(entry_id, str):
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == record["domain"]:
                view["entry_id"] = entry_id
        return view

    def _make_record(self, owner, flow_id, domain, kind, entry_id=None):
        interaction_id = secrets.token_urlsafe(18)
        record = {"id": interaction_id, "owner": owner, "flow_id": flow_id,
                  "domain": domain, "kind": kind, "phase": "error", "entry_id": entry_id}
        self.interactions[interaction_id] = record
        if flow_id:
            self.flow_owners[flow_id] = (owner, kind)
            self._flow_locks[flow_id] = asyncio.Lock()
            self._flow_lock_refs[flow_id] = 0
        return record

    async def _reclaim_idle_flow_lock(self, flow_id):
        """Discard a native flow lock when no operation can still be using it."""
        if not flow_id:
            return
        async with self._lock:
            lock = self._flow_locks.get(flow_id)
            if (lock is not None and not lock.locked() and self._flow_lock_refs.get(flow_id, 0) == 0
                    and not any(record.get("flow_id") == flow_id and record["phase"] not in TERMINAL_PHASES
                                for record in self.interactions.values())):
                self._flow_locks.pop(flow_id, None)
                self._flow_lock_refs.pop(flow_id, None)

    async def _apply_result(self, record, result, language):
        view = await self._view(result, record["domain"], language, record["kind"], record.get("entry_id"))
        if view.get("entry_id"):
            record["entry_id"] = view["entry_id"]
        old_flow_id = record.get("flow_id")
        new_flow_id = result.get("flow_id")
        if isinstance(new_flow_id, str):
            record["flow_id"] = new_flow_id
            self.flow_owners[new_flow_id] = (record["owner"], record["kind"])
        if result.get("type") in {"create_entry", "abort"}:
            if old_flow_id:
                self.flow_owners.pop(old_flow_id, None)
            record["flow_id"] = None
            await self._reclaim_idle_flow_lock(old_flow_id)
        for key in ("domain", "handler", "kind", "phase", "type", "step_id", "title",
                    "translation_domain", "last_step", "description_placeholders", "data_schema",
                    "errors", "menu_field", "menu_options", "url", "entry_state", "entity_count", "progress"):
            record.pop(key, None)
        record.update(view)

    async def status(self, owner, language="en"):
        language = self._language(language)
        result = []
        async with self._lock:
            owned = [record for record in self.interactions.values() if record["owner"] == owner]
        for record in owned:
            flow_id = record.get("flow_id")
            if flow_id and record["phase"] not in TERMINAL_PHASES:
                should_return = True
                async with self._locked_flow(flow_id):
                    if (self.interactions.get(record["id"]) is not record
                            or record.get("flow_id") != flow_id or record["phase"] in TERMINAL_PHASES):
                        should_return = False
                    else:
                        try:
                            current = await self._flow_manager(record["kind"]).async_configure(flow_id)
                        except UnknownFlow:
                            record.update(phase="expired", flow_id=None, url=None)
                            self.flow_owners.pop(flow_id, None)
                        except Exception:
                            self._abort_native_flow(record["kind"], flow_id)
                            record.update(phase="error", flow_id=None, errors={"base": "setup_failed"}, url=None)
                            self.flow_owners.pop(flow_id, None)
                        else:
                            await self._apply_result(record, current, language)
                if not should_return:
                    if self.interactions.get(record["id"]) is record:
                        result.append(self._public_view(record))
                    continue
            elif record["phase"] == "progress" and record.get("entry_id"):
                self._add_entry_state(record, record["entry_id"])
            result.append(self._public_view(record))
        return {"interactions": result}

    async def start(self, owner, domain, language="en"):
        language = self._language(language)
        if not isinstance(domain, str) or not DOMAIN_PATTERN.fullmatch(domain):
            raise ValueError("invalid_domain")
        if domain == "xiaomi_home":
            raise ValueError("needs_xiaomi_card")
        lock = self._creation_locks.setdefault(("config", owner, domain), asyncio.Lock())
        async with lock:
            existing = next((item for item in self.interactions.values()
                             if item["owner"] == owner and item["domain"] == domain
                             and item["phase"] not in TERMINAL_PHASES), None)
            if existing:
                return self._public_view(existing)
            try:
                result = await self.hass.config_entries.flow.async_init(domain, context={"source": SOURCE_USER})
            except Exception:
                record = self._make_record(owner, None, domain, "config")
                record.update(phase="error", errors={"base": "setup_failed"})
                return self._public_view(record)
            record = self._make_record(owner, result.get("flow_id"), domain, "config")
            await self._apply_result(record, result, language)
            return self._public_view(record)

    async def adopt(self, owner, flow_id, language="en"):
        language = self._language(language)
        if not isinstance(flow_id, str) or not flow_id or len(flow_id) > 256:
            raise ValueError("unknown_flow")
        async with self._lock:
            if flow_id in self.flow_owners:
                raise ValueError("unknown_flow")
            flow = self._flow_for("config", flow_id)
            context = flow.get("context") if flow else None
            domain = flow.get("handler") if flow else None
            if (not isinstance(context, dict) or context.get("source") not in DISCOVERY_SOURCES
                    or not isinstance(domain, str) or not DOMAIN_PATTERN.fullmatch(domain)
                    or domain == "xiaomi_home"):
                raise ValueError("unknown_flow")
            record = self._make_record(owner, flow_id, domain, "config")
        async with self._locked_flow(flow_id):
            if self.interactions.get(record["id"]) is not record or record.get("flow_id") != flow_id:
                raise ValueError("unknown_interaction")
            try:
                result = await self._flow_manager("config").async_configure(flow_id)
            except UnknownFlow:
                record.update(phase="expired", flow_id=None)
                self.flow_owners.pop(flow_id, None)
            except Exception:
                self._abort_native_flow("config", flow_id)
                record.update(phase="error", flow_id=None, errors={"base": "setup_failed"})
                self.flow_owners.pop(flow_id, None)
            else:
                await self._apply_result(record, result, language)
        return self._public_view(record)

    async def options_start(self, owner, entry_id, language="en"):
        language = self._language(language)
        if not isinstance(entry_id, str):
            raise ValueError("unknown_entry")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == "xiaomi_home":
            raise ValueError("needs_xiaomi_card")
        if entry is None:
            raise ValueError("unknown_entry")
        lock = self._creation_locks.setdefault(("options", owner, entry_id), asyncio.Lock())
        async with lock:
            existing = next((item for item in self.interactions.values()
                             if item["owner"] == owner and item.get("entry_id") == entry_id
                             and item["kind"] == "options" and item["phase"] not in TERMINAL_PHASES), None)
            if existing:
                return self._public_view(existing)
            try:
                result = await self.hass.config_entries.options.async_init(entry_id)
            except Exception:
                record = self._make_record(owner, None, entry.domain, "options", entry_id)
                record.update(phase="error", errors={"base": "setup_failed"})
                return self._public_view(record)
            record = self._make_record(owner, result.get("flow_id"), entry.domain, "options", entry_id)
            await self._apply_result(record, result, language)
            return self._public_view(record)

    async def submit(self, owner, interaction_id, user_input, language="en"):
        language = self._language(language)
        if not isinstance(interaction_id, str) or len(interaction_id) > 128:
            raise ValueError("unknown_interaction")
        if not isinstance(user_input, dict):
            raise ValueError("invalid_input")
        record = self._record_for(owner, interaction_id)
        flow_id = record.get("flow_id")
        if not flow_id or record["phase"] not in {"form", "menu"}:
            raise ValueError("flow_not_submittable")
        async with self._locked_flow(flow_id):
            if (self.interactions.get(interaction_id) is not record or record["owner"] != owner
                    or record.get("flow_id") != flow_id or record["phase"] not in {"form", "menu"}):
                raise ValueError("unknown_interaction")
            try:
                result = await self._flow_manager(record["kind"]).async_configure(flow_id, user_input)
            except InvalidData as error:
                try:
                    current = await self._flow_manager(record["kind"]).async_configure(flow_id)
                except UnknownFlow:
                    record.update(phase="expired", flow_id=None)
                    self.flow_owners.pop(flow_id, None)
                except Exception:
                    self._abort_native_flow(record["kind"], flow_id)
                    record.update(phase="error", flow_id=None, errors={"base": "setup_failed"}, url=None)
                    self.flow_owners.pop(flow_id, None)
                else:
                    await self._apply_result(record, current, language)
                    record["errors"] = _safe_errors(
                        getattr(error, "schema_errors", None), _field_names(record.get("data_schema"))
                    )
            except UnknownFlow:
                record.update(phase="expired", flow_id=None)
                self.flow_owners.pop(flow_id, None)
            except Exception:
                self._abort_native_flow(record["kind"], flow_id)
                record.update(phase="error", flow_id=None, errors={"base": "setup_failed"}, url=None)
                self.flow_owners.pop(flow_id, None)
            else:
                await self._apply_result(record, result, language)
        return self._public_view(record)

    async def cancel(self, owner, interaction_id):
        if not isinstance(interaction_id, str) or len(interaction_id) > 128:
            raise ValueError("unknown_interaction")
        record = self._record_for(owner, interaction_id)
        flow_id = record.get("flow_id")
        if not flow_id or record["phase"] in TERMINAL_PHASES:
            raise ValueError("flow_not_cancellable")
        async with self._locked_flow(flow_id):
            if record["owner"] != owner or record.get("flow_id") != flow_id or record["phase"] in TERMINAL_PHASES:
                raise ValueError("flow_not_cancellable")
            try:
                self._flow_manager(record["kind"]).async_abort(flow_id)
            except UnknownFlow:
                record.update(phase="expired")
            else:
                record.update(phase="abort")
            self.flow_owners.pop(flow_id, None)
            record["flow_id"] = None
            record.pop("url", None)
        return self._public_view(record)

    async def dismiss(self, owner, interaction_id):
        if not isinstance(interaction_id, str) or len(interaction_id) > 128:
            raise ValueError("unknown_interaction")
        async with self._lock:
            record = self._record_for(owner, interaction_id)
            if record["phase"] not in TERMINAL_PHASES:
                raise ValueError("interaction_active")
            self.interactions.pop(interaction_id, None)
            flow_id = record.get("flow_id")
            if flow_id:
                self.flow_owners.pop(flow_id, None)
                self._flow_locks.pop(flow_id, None)
        return {"dismissed": True}

    async def agent_action(self, owner, action, body):
        language = getattr(self.hass.config, "language", "en")
        if action == "integration_setup_status":
            state = await self.status(owner, language)
            summaries = []
            for item in state["interactions"]:
                summary = {
                    "interaction_id": item["id"], "flow_id": self.interactions[item["id"]].get("flow_id"),
                    "domain": item["domain"], "kind": item["kind"], "phase": item["phase"],
                    **({"step_id": item["step_id"]} if item.get("step_id") else {}),
                    **({"menu_options": [option["value"] for option in item["menu_options"]]}
                       if item.get("menu_options") else {}),
                    **({"menu_field": item["menu_field"]} if item.get("menu_field") else {}),
                }
                input_fields = _model_input_fields(item)
                if input_fields is not None:
                    summary["input_fields"] = input_fields
                summaries.append(summary)
            return {"interactions": summaries}
        if action == "start_integration":
            view = await self.start(owner, body.get("domain", ""), language)
        elif action == "options_start":
            view = await self.options_start(owner, body.get("entry_id"), language)
        elif action == "adopt_discovered_flow":
            view = await self.adopt(owner, body.get("flow_id"), language)
        elif action in {"continue_integration", "options_continue"}:
            flow_id = body.get("flow_id")
            kind = "options" if action == "options_continue" else "config"
            if not isinstance(flow_id, str) or self.flow_owners.get(flow_id) != (owner, kind):
                raise ValueError("unknown_flow")
            record = next((item for item in self.interactions.values() if item.get("flow_id") == flow_id), None)
            if record is None:
                raise ValueError("unknown_flow")
            user_input = body.get("input", {})
            if not isinstance(user_input, dict):
                raise ValueError("invalid_input")
            if record["phase"] == "menu":
                allowed = {item["value"] for item in record.get("menu_options", [])}
                if set(user_input) != {"next_step_id"} or user_input.get("next_step_id") not in allowed:
                    raise ValueError("protected_ui_required")
            elif record["phase"] == "form" and record.get("data_schema") == [] and user_input == {}:
                pass
            else:
                raise ValueError("protected_ui_required")
            view = await self.submit(owner, record["id"], user_input, language)
        else:
            raise ValueError("unknown_action")
        summary = {
            "interaction_id": view["id"],
            "flow_id": self.interactions[view["id"]].get("flow_id"),
            "domain": view["domain"], "kind": view["kind"], "phase": view["phase"],
            **({"step_id": view["step_id"]} if view.get("step_id") else {}),
            **({"menu_options": [option["value"] for option in view["menu_options"]]}
               if view.get("menu_options") else {}),
            **({"menu_field": view["menu_field"]} if view.get("menu_field") else {}),
        }
        input_fields = _model_input_fields(view)
        if input_fields is not None:
            summary["input_fields"] = input_fields
        return summary


@callback
def async_register_commands(hass):
    websocket_api.async_register_command(hass, setup_status)
    websocket_api.async_register_command(hass, setup_start)
    websocket_api.async_register_command(hass, setup_adopt)
    websocket_api.async_register_command(hass, setup_submit)
    websocket_api.async_register_command(hass, setup_cancel)
    websocket_api.async_register_command(hass, setup_dismiss)


async def _dispatch(hass, connection, msg, operation):
    user = connection.user
    if user is None or not user.is_active or not user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "unauthorized")
        return
    try:
        result = await operation(hass.data[DOMAIN]["integration_setup"], user.id)
        connection.send_result(msg["id"], result)
    except ValueError as error:
        code = str(error) if str(error) in SAFE_ERRORS else "setup_failed"
        connection.send_error(msg["id"], code, code)
    except Exception:
        connection.send_error(msg["id"], "setup_failed", "setup_failed")


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/setup/status",
    probatio.Optional("language", default="en"): probatio.All(str, probatio.Length(min=2, max=20)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def setup_status(hass, connection, msg):
    await _dispatch(hass, connection, msg, lambda controller, owner:
                    controller.status(owner, msg["language"]))


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/setup/start",
    probatio.Required("domain"): probatio.All(str, probatio.Length(min=1, max=80)),
    probatio.Optional("language", default="en"): probatio.All(str, probatio.Length(min=2, max=20)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def setup_start(hass, connection, msg):
    await _dispatch(hass, connection, msg, lambda controller, owner:
                    controller.start(owner, msg["domain"], msg["language"]))


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/setup/adopt",
    probatio.Required("flow_id"): probatio.All(str, probatio.Length(min=1, max=256)),
    probatio.Optional("language", default="en"): probatio.All(str, probatio.Length(min=2, max=20)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def setup_adopt(hass, connection, msg):
    await _dispatch(hass, connection, msg, lambda controller, owner:
                    controller.adopt(owner, msg["flow_id"], msg["language"]))


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/setup/submit",
    probatio.Required("interaction_id"): probatio.All(str, probatio.Length(min=1, max=128)),
    probatio.Required("input"): dict,
    probatio.Optional("language", default="en"): probatio.All(str, probatio.Length(min=2, max=20)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def setup_submit(hass, connection, msg):
    await _dispatch(hass, connection, msg, lambda controller, owner:
                    controller.submit(owner, msg["interaction_id"], msg["input"], msg["language"]))


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/setup/cancel",
    probatio.Required("interaction_id"): probatio.All(str, probatio.Length(min=1, max=128)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def setup_cancel(hass, connection, msg):
    await _dispatch(hass, connection, msg, lambda controller, owner:
                    controller.cancel(owner, msg["interaction_id"]))


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/setup/dismiss",
    probatio.Required("interaction_id"): probatio.All(str, probatio.Length(min=1, max=128)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def setup_dismiss(hass, connection, msg):
    await _dispatch(hass, connection, msg, lambda controller, owner:
                    controller.dismiss(owner, msg["interaction_id"]))
