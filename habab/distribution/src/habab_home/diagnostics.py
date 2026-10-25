"""Owner-scoped, bounded diagnostics for currently mapped assistant sessions."""

from __future__ import annotations

import errno
import io
from itertools import islice
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
import zipfile

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as HA_VERSION

from .agent_diagnostics import safe_error_summary
from .const import DOMAIN, VERSION
from .setup_agent import AgentError, VOICE_TURN_METADATA_KEY, VOICE_TURN_METADATA_VALUE
from .voice_history import VOICE_EVENT_PROTOCOL_KEY, decode_voice_event_part

MAX_LIST_LIMIT = 20
MAX_MAPPED_SESSIONS = 200
MAX_EXPORT_SESSIONS = 5
MAX_MESSAGES = 60
MAX_PARTS_PER_MESSAGE = 50
MAX_EXPORT_BYTES = 1024 * 1024
MAX_PROJECTED_BYTES = 32 * 1024
MAX_PROJECTED_EVENTS = 200
MAX_PROJECTED_ITEMS = 5000
MAX_PROVIDER_MODELS = 10
MAX_RUNTIME_RESPONSE_BYTES = 2_000_000
MAX_TEXT_SCAN_CHARS = 32 * 1024
MAX_OPENCODE_LOG_FILES = 10
MAX_HA_LOG_FILES = 3
MAX_INSTANCE_LOG_FILE_BYTES = 8 * 1024
MAX_INSTANCE_LOG_BYTES = 192 * 1024
MAX_WINDOW = timedelta(days=31)
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
CHANNELS = {"all", "web", "telegram", "voice"}
STATUSES = {"all", "active", "error", "cancelled", "completed", "unknown"}
_SENSITIVE_FIELD = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|private[_-]?key|signing[_-]?key|encryption[_-]?key|"
    r"secret[_-]?key|token|secret|password|passwd|authorization|cookie|"
    r"credential|oauth|access[_-]?key|refresh[_-]?token|id[_-]?token|"
    r"verification[_-]?code|auth[_-]?code)"
    r"(?:$|[_-])", re.IGNORECASE,
)
_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.-]{8,}", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9\"'])(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|private[_-]?key|"
    r"signing[_-]?key|encryption[_-]?key|secret[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|oauth[_-]?token|id[_-]?token|token|password|passwd|"
    r"client[_-]?secret|secret|authorization|cookie|oauth[_-]?code|verification[_-]?code)"
    r"(\s*[:=]\s*)([^\s,;]+)", re.IGNORECASE,
)
_JSON_SECRET_FIELD = re.compile(
    r'(?P<key_open>")(?P<key>(?:\\.|[^"\\]){0,256})(?P<key_close>")'
    r'(?P<separator>\s*:\s*)(?:'
    r'(?P<value_open>")(?P<value>(?:\\.|[^"\\])*)(?P<value_close>")'
    r'|(?P<scalar>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null)'
    r'(?=\s*[,}\]]))'
)
_ESCAPED_JSON_SECRET_FIELD = re.compile(
    r'(?P<key_open>\\")(?P<key>(?:\\.|[^"\\]){0,256}?)(?P<key_close>\\")'
    r'(?P<separator>\s*:\s*)(?:'
    r'(?P<value_open>\\")(?P<value>(?:\\\\|\\(?!")|[^\\])*)(?P<value_close>\\")'
    r'|(?P<scalar>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null)'
    r'(?=\s*[,}\]]))'
)
_COOKIE_HEADER = re.compile(r"\b(?:set-cookie|cookie)\s*:\s*[^\r\n]+", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"([?&](?:[A-Za-z0-9_-]*_)?(?:access_token|refresh_token|oauth_token|id_token|"
    r"private_key|signing_key|encryption_key|token|code|key|secret|password|auth|state)=)[^&#\s]+",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_LONG_OPAQUE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+=-]{48,}(?![A-Za-z0-9])")
_OPENCODE_LOG_NAME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}\.log\Z")
_HA_LOG_NAME = re.compile(r"home-assistant\.log(?:\.\d{1,3})?\Z")


def _sources(history_available: bool, reason: str | None = None,
             session_limit_reached: bool = False) -> dict:
    return {
        "history": {"available": history_available, "reason": reason,
                    "limit_messages": MAX_MESSAGES,
                    "session_limit": MAX_MAPPED_SESSIONS,
                    "runtime_response_bytes_per_session": MAX_RUNTIME_RESPONSE_BYTES,
                    "parts_per_message": MAX_PARTS_PER_MESSAGE,
                    "projection_bytes_limit": MAX_PROJECTED_BYTES,
                    "projection_events_limit": MAX_PROJECTED_EVENTS,
                    "projection_items_limit": MAX_PROJECTED_ITEMS,
                    "session_limit_reached": session_limit_reached,
                    "scope": "currently_mapped_sessions_only",
                    "retention": "available_while_owner_mapping_exists"},
        "backend_ack": {"available": False, "reason": "not_recorded"},
        "model_request": {"available": False, "reason": "not_recorded"},
        "opencode_files": {"available": False, "reason": "not_exposed_by_runtime"},
        "home_assistant_logs": {"available": False, "reason": "not_collected"},
        "voice_transport": {"available": False, "reason": "not_recorded"},
        "cancellation": {"available": False, "reason": "not_durably_recorded"},
        "native_assist_history": {"available": False, "reason": "not_recorded_by_habab"},
    }


def _admin(request):
    user = request.get("hass_user")
    return user if user and user.is_active and user.is_admin else None


def _mapped_sessions(assistant, owner: str) -> list[dict]:
    """Return only session IDs already mapped to this owner and channel."""
    records = []
    web_session = getattr(assistant, "web_sessions", {}).get(owner)
    if isinstance(web_session, str) and SESSION_ID.fullmatch(web_session):
        records.append({"session_id": web_session, "channel": "web", "modes": ["web"]})
    prefix = owner + ":"
    for key, session_id in getattr(assistant, "telegram_sessions", {}).items():
        if (isinstance(key, str) and key.startswith(prefix)
                and isinstance(session_id, str) and SESSION_ID.fullmatch(session_id)):
            records.append({"session_id": session_id, "channel": "telegram", "modes": ["telegram"]})
    unique = {(row["channel"], row["session_id"]): row for row in records}
    return sorted(unique.values(), key=lambda row: (row["channel"], row["session_id"]))[:MAX_MAPPED_SESSIONS]


def _mapping_is_current(assistant, owner: str, mapping: dict) -> bool:
    session_id = mapping["session_id"]
    if mapping["channel"] == "web":
        return getattr(assistant, "web_sessions", {}).get(owner) == session_id
    prefix = owner + ":"
    return any(isinstance(key, str) and key.startswith(prefix) and value == session_id
               for key, value in getattr(assistant, "telegram_sessions", {}).items())


def _parse_time(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid_time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _time_window(query) -> tuple[datetime | None, datetime | None]:
    start, end = _parse_time(query.get("from")), _parse_time(query.get("to"))
    if ((start is None) != (end is None)
            or start and end and (end <= start or end - start > MAX_WINDOW)):
        raise ValueError("invalid_time_window")
    return start, end


def _timestamp(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = value / 1000 if value > 10_000_000_000 else value
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        if len(value) > 64:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


class ProjectionBudget:
    """Bound sanitized output while walking OpenCode messages and tool parts."""

    def __init__(self, max_bytes: int | None = None,
                 max_events: int | None = None):
        self.remaining_bytes = MAX_PROJECTED_BYTES if max_bytes is None else max_bytes
        self.max_events = MAX_PROJECTED_EVENTS if max_events is None else max_events
        self.event_count = 0
        self.remaining_items = MAX_PROJECTED_ITEMS
        self.truncation_count = 0

    def mark_truncated(self) -> None:
        self.truncation_count += 1

    def claim_event(self) -> bool:
        if self.event_count >= self.max_events:
            self.mark_truncated()
            return False
        self.event_count += 1
        return True

    def claim_item(self) -> bool:
        if self.remaining_items <= 0:
            self.mark_truncated()
            return False
        self.remaining_items -= 1
        return True

    def clean_string(self, text: str) -> str:
        if self.remaining_bytes <= 0:
            self.mark_truncated()
            return "[truncated]"
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            encoded = text.encode("utf-8", "replace")
            text = encoded.decode("utf-8")
        if len(encoded) <= self.remaining_bytes:
            self.remaining_bytes -= len(encoded)
            return text
        clipped = encoded[:self.remaining_bytes].decode("utf-8", "ignore")
        self.remaining_bytes -= len(clipped.encode("utf-8"))
        self.mark_truncated()
        return clipped


def _redact_text(runtime, value: str, limit: int = 12000,
                 budget: ProjectionBudget | None = None) -> str:
    if len(value) > MAX_TEXT_SCAN_CHARS:
        if budget is not None:
            budget.mark_truncated()
        return "[truncated oversized text]"
    text = runtime.clean(value) if runtime is not None and callable(getattr(runtime, "clean", None)) else value
    text = _BEARER.sub("[redacted credential]", text)
    text = _COOKIE_HEADER.sub("[redacted cookie header]", text)
    def redact_json_secret(match):
        key = match.group("key")
        if len(key) > 256:
            return match.group(0)
        sensitive = False
        for _ in range(2):
            try:
                key = json.loads('"' + key + '"')
            except (json.JSONDecodeError, TypeError):
                break
            if isinstance(key, str) and _SENSITIVE_FIELD.fullmatch(key):
                sensitive = True
                break
        if not sensitive:
            return match.group(0)
        quote = match.group("value_open") or match.group("key_open")
        return (match.group("key_open") + match.group("key") + match.group("key_close")
                + match.group("separator") + quote + "[redacted]"
                + (match.group("value_close") or quote))
    text = _JSON_SECRET_FIELD.sub(redact_json_secret, text)
    text = _ESCAPED_JSON_SECRET_FIELD.sub(redact_json_secret, text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + "[redacted]", text)
    text = _QUERY_SECRET.sub(r"\1[redacted]", text)
    text = _TELEGRAM_TOKEN.sub("[redacted credential]", text)
    text = _LONG_OPAQUE.sub("[redacted opaque value]", text)
    text = text[:limit]
    return budget.clean_string(text) if budget is not None else text


def _safe_directory(path: Path, boundary: Path) -> bool:
    """Reject symlinked or non-directory components under a fixed config root."""
    try:
        path = Path(os.path.abspath(path))
        boundary = Path(os.path.abspath(boundary))
        relative = path.relative_to(boundary)
        current = boundary
        candidates = [current]
        for part in relative.parts:
            current = current / part
            candidates.append(current)
        for candidate in candidates:
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return False
        return True
    except (OSError, ValueError):
        return False


def _read_log_tail(
    path: Path, limit: int
) -> tuple[bytes | None, int | None, bool, str | None]:
    """Read at most `limit` bytes from a regular non-symlink file's tail."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        reason = "unsafe_file" if error.errno in {errno.ELOOP, errno.EMLINK} else "read_error"
        return None, None, False, reason
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, None, False, "unsafe_file"
        size = info.st_size
        offset = max(0, size - limit)
        length = min(size, limit)
        raw = os.pread(descriptor, length, offset)
        truncated = offset > 0 or len(raw) < size
        if offset and raw:
            line_end = raw.find(b"\n")
            raw = raw[line_end + 1:] if line_end >= 0 else b""
        return raw, size, truncated, None
    finally:
        os.close(descriptor)


def _collect_log_source(
    directory: Path,
    *,
    filename_pattern: re.Pattern,
    archive_prefix: str,
    max_files: int,
    boundary: Path,
) -> tuple[dict, list[tuple[str, bytes, int, bool]]]:
    """Collect metadata and bounded raw tails from one fixed log directory."""
    source = {
        "available": False, "reason": None, "scope": "home_assistant_instance",
        "candidate_files": 0, "included_files": [], "omitted_files": 0,
        "unsafe_files": 0, "read_errors": 0, "truncated_files": 0, "bytes": 0,
    }
    if not _safe_directory(directory, boundary):
        source["reason"] = "directory_missing_or_unsafe"
        return source, []
    try:
        with os.scandir(directory) as iterator:
            candidates = []
            matching_count = 0
            for entry in iterator:
                if not filename_pattern.fullmatch(entry.name):
                    continue
                matching_count += 1
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    source["unsafe_files"] += 1
                else:
                    candidates.append(entry.name)
    except OSError:
        source["reason"] = "directory_unreadable"
        return source, []
    candidates.sort(reverse=True)
    source["candidate_files"] = matching_count
    selected = candidates[:max_files]
    source["omitted_files"] = max(0, len(candidates) - len(selected))
    result = []
    for name in selected:
        raw, original_bytes, truncated, error = _read_log_tail(
            directory / name, MAX_INSTANCE_LOG_FILE_BYTES)
        if error == "unsafe_file":
            source["unsafe_files"] += 1
            continue
        if error:
            source["read_errors"] += 1
            continue
        if raw is None or original_bytes is None:
            source["read_errors"] += 1
            continue
        archive_name = f"{archive_prefix}/{name}"
        result.append((archive_name, raw, original_bytes, truncated))
    if result:
        source["available"] = True
    elif source["unsafe_files"]:
        source["reason"] = "unsafe_files_skipped"
    elif source["candidate_files"] == 0:
        source["reason"] = "no_matching_files"
    else:
        source["reason"] = "files_unreadable"
    return source, result


def _ha_log_sort_key(name: str) -> tuple[int, int]:
    if name == "home-assistant.log":
        return (0, 0)
    suffix = name.removeprefix("home-assistant.log.")
    return (1, int(suffix))


def _collect_instance_log_tails(
    config_root: Path, opencode_root: Path
) -> tuple[dict, list[tuple[str, bytes, int, bool]]]:
    """Use only OpenCode's known log folder and Home Assistant's fixed log names."""
    sources = {}
    files = []
    runtime_log_dir = opencode_root / "data" / "opencode" / "log"
    opencode_source, opencode_files = _collect_log_source(
        runtime_log_dir, filename_pattern=_OPENCODE_LOG_NAME,
        archive_prefix="opencode/logs", max_files=MAX_OPENCODE_LOG_FILES,
        boundary=config_root)
    files.extend(opencode_files)
    sources["opencode_logs"] = opencode_source

    # Home Assistant Core's configured log and numeric rotations are a separate
    # instance-wide source; no arbitrary filesystem paths enter from the request.
    ha_source = {
        "available": False, "reason": None, "scope": "home_assistant_instance",
        "candidate_files": 0, "included_files": [], "omitted_files": 0,
        "unsafe_files": 0, "read_errors": 0, "truncated_files": 0, "bytes": 0,
    }
    if not _safe_directory(config_root, config_root):
        ha_source["reason"] = "directory_missing_or_unsafe"
    else:
        try:
            with os.scandir(config_root) as iterator:
                names = [entry.name for entry in iterator if _HA_LOG_NAME.fullmatch(entry.name)]
            ha_source["candidate_files"] = len(names)
            safe_names = []
            for name in names:
                try:
                    info = (config_root / name).lstat()
                except OSError:
                    ha_source["read_errors"] += 1
                    continue
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    ha_source["unsafe_files"] += 1
                else:
                    safe_names.append(name)
            names = safe_names
            names.sort(key=_ha_log_sort_key)
            selected = names[:MAX_HA_LOG_FILES]
            ha_source["omitted_files"] = max(0, len(names) - len(selected))
            for name in selected:
                raw, original_bytes, truncated, error = _read_log_tail(
                    config_root / name, MAX_INSTANCE_LOG_FILE_BYTES)
                if error == "unsafe_file":
                    ha_source["unsafe_files"] += 1
                    continue
                if error or raw is None or original_bytes is None:
                    ha_source["read_errors"] += 1
                    continue
                files.append((f"homeassistant/{name}", raw, original_bytes, truncated))
            if any(item[0].startswith("homeassistant/") for item in files):
                ha_source["available"] = True
            elif ha_source["unsafe_files"]:
                ha_source["reason"] = "unsafe_files_skipped"
            elif not names:
                ha_source["reason"] = "no_matching_files"
            else:
                ha_source["reason"] = "files_unreadable"
        except OSError:
            ha_source["reason"] = "directory_unreadable"
    sources["home_assistant_logs"] = ha_source
    return sources, files


def _redact_instance_log_tails(
    runtime, sources: dict, raw_files: list[tuple[str, bytes, int, bool]]
) -> dict[str, bytes]:
    """Redact bounded log text and cap the combined included log payload."""
    archive_files = {}
    remaining = MAX_INSTANCE_LOG_BYTES
    source_for_prefix = {
        "opencode/logs/": sources["opencode_logs"],
        "homeassistant/": sources["home_assistant_logs"],
    }
    for archive_name, raw, original_bytes, truncated in raw_files:
        text = raw.decode("utf-8", errors="replace")
        text_truncated = len(text) > MAX_TEXT_SCAN_CHARS
        cleaned = _redact_text(runtime, text, limit=MAX_TEXT_SCAN_CHARS)
        encoded = cleaned.encode("utf-8")
        if len(encoded) > remaining:
            encoded = encoded[:remaining].decode("utf-8", errors="ignore").encode("utf-8")
            truncated = True
        if len(encoded) > remaining:
            encoded = b""
            truncated = True
        truncated = truncated or text_truncated
        remaining -= len(encoded)
        archive_files[archive_name] = encoded
        source = next(value for prefix, value in source_for_prefix.items()
                      if archive_name.startswith(prefix))
        source["included_files"].append({
            "name": archive_name.rsplit("/", 1)[-1],
            "original_bytes": original_bytes,
            "included_bytes": len(encoded),
            "truncated": truncated,
        })
        source["bytes"] += len(encoded)
        if truncated:
            source["truncated_files"] += 1
    return archive_files


def _clean_value(runtime, value, *, depth=0, budget: ProjectionBudget | None = None):
    if depth > 5:
        if budget is not None:
            budget.mark_truncated()
        return "[truncated]"
    if budget is not None and budget.remaining_bytes <= 0:
        budget.mark_truncated()
        return "[truncated]"
    if budget is not None and not budget.claim_item():
        return "[truncated]"
    if isinstance(value, str):
        return _redact_text(runtime, value, budget=budget)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        if len(value) > 50 and budget is not None:
            budget.mark_truncated()
        cleaned = []
        items = value[:50]
        for index, item in enumerate(items):
            if budget is not None and budget.remaining_bytes <= 0:
                budget.mark_truncated()
                break
            if budget is not None and budget.remaining_items <= 0:
                budget.mark_truncated()
                cleaned.append("[truncated]")
                break
            cleaned.append(_clean_value(runtime, item, depth=depth + 1, budget=budget))
            if budget is not None and budget.remaining_items <= 0 and index + 1 < len(items):
                budget.mark_truncated()
                cleaned.append("[truncated]")
                break
        return cleaned
    if isinstance(value, dict):
        result = {}
        if len(value) > 50 and budget is not None:
            budget.mark_truncated()
        for index, (key, item) in enumerate(islice(value.items(), 50)):
            if budget is not None and budget.remaining_bytes <= 0:
                budget.mark_truncated()
                break
            if budget is not None and budget.remaining_items <= 0:
                budget.mark_truncated()
                result["[truncated]"] = "[truncated]"
                break
            raw_key = str(key)
            key_too_long = len(raw_key) > 100
            safe_key = raw_key[:100] if not key_too_long else "[truncated-key]"
            normalized_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", safe_key)
            sensitive = key_too_long or _SENSITIVE_FIELD.search(normalized_key) is not None
            if budget is not None:
                safe_key = budget.clean_string(safe_key)
            result[safe_key] = ("[redacted]" if sensitive
                                else _clean_value(runtime, item, depth=depth + 1, budget=budget))
            if budget is not None and budget.remaining_items <= 0 and len(value) > index + 1:
                budget.mark_truncated()
                result["[truncated]"] = "[truncated]"
                break
        return result
    return "[omitted]"


def _message_times(message: dict) -> tuple[datetime | None, datetime | None]:
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    times = info.get("time") if isinstance(info.get("time"), dict) else {}
    created = _timestamp(times.get("created"))
    ended = _timestamp(times.get("completed") or times.get("updated"))
    return created, ended


def _provider_model(info: dict, budget: ProjectionBudget | None = None) -> dict:
    provider = info.get("providerID")
    model = info.get("modelID")
    if (not isinstance(provider, str) or len(provider) > 100
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", provider)):
        provider = None
    if (not isinstance(model, str) or len(model) > 160
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", model)):
        model = None
    if budget is not None:
        provider = budget.clean_string(provider) if provider is not None else None
        model = budget.clean_string(model) if model is not None else None
    return {"provider": provider, "model": model}


def _duration_ms(message: dict) -> int | None:
    created, ended = _message_times(message)
    if created is None or ended is None or ended < created:
        return None
    return min(int((ended - created).total_seconds() * 1000), 86_400_000)


def _tool_timing(state: dict) -> tuple[datetime | None, datetime | None, int | None]:
    """Read per-tool timestamps when the OpenCode record contains them."""
    timing = state.get("time") if isinstance(state.get("time"), dict) else {}
    started = _timestamp(timing.get("start"))
    ended = _timestamp(timing.get("end"))
    if started is None or ended is None or ended < started:
        return started, ended, None
    duration = min(int((ended - started).total_seconds() * 1000), 86_400_000)
    return started, ended, duration


def _event_order_key(event: dict) -> tuple[int, int, int]:
    """Order projected events by their stable source message and part IDs."""
    parts = str(event.get("id", "")).split(".")
    try:
        message_index = int(parts[0])
    except (ValueError, IndexError):
        return MAX_MESSAGES, MAX_PARTS_PER_MESSAGE, 2
    if len(parts) == 2 and parts[1] == "error":
        return message_index, -1, 0
    if len(parts) == 3 and parts[1] == "voice":
        try:
            return message_index, -1, int(parts[2])
        except ValueError:
            return message_index, -1, 0
    if len(parts) >= 2:
        try:
            part_index = int(parts[1])
        except ValueError:
            return message_index, MAX_PARTS_PER_MESSAGE, 2
        result_index = 1 if len(parts) > 2 and parts[2] == "result" else 0
        return message_index, part_index, result_index
    return message_index, MAX_PARTS_PER_MESSAGE, 2


def _voice_event(part: dict, budget: ProjectionBudget) -> dict | None:
    metadata = part.get("metadata")
    if isinstance(metadata, dict) and metadata.get(VOICE_EVENT_PROTOCOL_KEY):
        raw = part.get("text")
        if isinstance(raw, str) and len(raw) > MAX_TEXT_SCAN_CHARS:
            budget.mark_truncated()
            return None
        return decode_voice_event_part(part)
    return None


def _voice_turn_texts(runtime, message: dict, budget: ProjectionBudget) -> list[dict] | None:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None
    for part in parts[:MAX_PARTS_PER_MESSAGE]:
        metadata = part.get("metadata") if isinstance(part, dict) else None
        if (isinstance(metadata, dict)
                and metadata.get(VOICE_TURN_METADATA_KEY) == VOICE_TURN_METADATA_VALUE):
            raw = part.get("text")
            if not isinstance(raw, str) or len(raw) > MAX_TEXT_SCAN_CHARS:
                budget.mark_truncated()
                return []
            bounded_message = {"info": message.get("info", {}), "parts": [part]}
            visible = getattr(runtime, "visible", None)
            projected = visible([bounded_message]) if callable(visible) else []
            if len(projected) == 2 and [item.get("role") for item in projected] == ["user", "assistant"]:
                return projected
            return []
    return None


def _tool_events(runtime, message: dict, channel: str, created: datetime | None,
                 message_index: int, budget: ProjectionBudget) -> list[dict]:
    events = []
    parts = message.get("parts")
    if not isinstance(parts, list):
        return events
    for part_index, part in enumerate(parts[:MAX_PARTS_PER_MESSAGE]):
        if budget.remaining_bytes <= 0:
            budget.mark_truncated()
            break
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        tool_name = part.get("tool")
        if not isinstance(tool_name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", tool_name):
            tool_name = "unknown"
        tool_name = budget.clean_string(tool_name)
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        if status not in {"running", "completed", "error"}:
            status = "unknown"
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        tool_started, tool_ended, tool_duration = _tool_timing(state)
        base = {"at": None, "_sort_at": _iso(created), "channel": channel,
                **_provider_model(info, budget), "tool": tool_name,
                "status": status, "duration_ms": None, "exit_code": None}
        call_id = part.get("callID")
        if isinstance(call_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", call_id):
            base["correlation_id"] = budget.clean_string(call_id)
        if "input" in state:
            if not budget.claim_event():
                break
            events.append({**base, "at": _iso(tool_started), "id": f"{message_index}.{part_index}.call",
                           "stage": "tool_call", "kind": "tool_invocation",
                           "input": _clean_value(runtime, state.get("input"), budget=budget),
                           "output": None, "text": None,
                           "confirmation": "runtime_call_recorded", "available": True,
                           "reason": None})
        if "output" in state or status in {"completed", "error"}:
            if not budget.claim_event():
                break
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            exit_code = metadata.get("exit")
            if type(exit_code) is not int or not -255 <= exit_code <= 255:
                exit_code = None
            events.append({**base, "at": _iso(tool_ended), "id": f"{message_index}.{part_index}.result",
                           "stage": "tool_result", "kind": "tool_output",
                           "duration_ms": tool_duration,
                           "input": None,
                           "output": _clean_value(runtime, state.get("output"), budget=budget) if "output" in state else None,
                           "text": None, "exit_code": exit_code,
                           "confirmation": "runtime_result_recorded" if "output" in state else "unknown",
                           "available": "output" in state,
                           "reason": None if "output" in state else "result_not_recorded"})
    return events


def project_messages(runtime, raw_messages, channel: str,
                     budget: ProjectionBudget | None = None) -> list[dict]:
    """Project bounded runtime records; never return raw OpenCode payloads."""
    budget = budget or ProjectionBudget()
    events = []
    if not isinstance(raw_messages, list):
        return events
    bounded_messages = raw_messages[-MAX_MESSAGES:]
    indexed_messages = list(enumerate(bounded_messages))
    # Reserve output budget for the newest conversation evidence first. Final
    # sorting below restores chronological message and part order for display.
    for message_index, message in reversed(indexed_messages):
        if budget.remaining_bytes <= 0:
            budget.mark_truncated()
            break
        if not isinstance(message, dict):
            continue
        truncations_before_message = budget.truncation_count
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = info.get("role")
        model_info = _provider_model(info, budget)
        created, _ended = _message_times(message)
        message_parts = message.get("parts")
        if isinstance(message_parts, list) and len(message_parts) > MAX_PARTS_PER_MESSAGE:
            budget.mark_truncated()
        message_error = info.get("error")
        if role == "assistant" and isinstance(message_error, (dict, str)):
            summary = safe_error_summary(message_error)
            if summary.get("name") != "other" or summary.get("http_status") is not None:
                if not budget.claim_event():
                    break
                cancelled = summary.get("name") == "MessageAbortedError"
                events.append({"id": f"{message_index}.error", "at": _iso(created),
                               "channel": channel,
                               "stage": "cancellation" if cancelled else "model_request",
                               **model_info,
                               "kind": "stage_error", "role": "assistant",
                               "text": None, "tool": None, "input": None,
                               "duration_ms": _duration_ms(message),
                               "output": summary, "exit_code": None,
                               "status": "cancelled" if cancelled else "error",
                               "confirmation": "runtime_error_recorded", "available": True,
                               "reason": None})
        voice_turn = _voice_turn_texts(runtime, message, budget)
        if voice_turn is not None:
            for index, item in enumerate(voice_turn):
                text = item.get("text")
                if isinstance(text, str):
                    if not budget.claim_event():
                        break
                    events.append({"id": f"{message_index}.voice.{index}", "at": _iso(created),
                                   "channel": "voice", "stage": "model_text",
                                   **model_info,
                                   "duration_ms": _duration_ms(message),
                                   "kind": "voice_transcript" if item["role"] == "user" else "assistant_text",
                                   "role": item["role"], "text": _redact_text(runtime, text, budget=budget),
                                   "tool": None, "input": None, "output": None, "exit_code": None,
                                   "confirmation": "not_applicable", "available": True, "reason": None})
        else:
            parts = message.get("parts") if isinstance(message.get("parts"), list) else []
            for part_index, part in enumerate(parts[:MAX_PARTS_PER_MESSAGE]):
                if budget.remaining_bytes <= 0:
                    budget.mark_truncated()
                    break
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                voice = _voice_event(part, budget)
                if voice is not None:
                    text = voice.get("text")
                    event_role = voice.get("role")
                    event_channel = "voice"
                    kind = "voice_transcript" if event_role == "user" else "assistant_text"
                elif part.get("synthetic") is True or role not in {"user", "assistant"}:
                    continue
                else:
                    text = part.get("text")
                    event_role = role
                    event_channel = channel
                    kind = "user_message" if role == "user" else "assistant_text"
                if isinstance(text, str) and text.strip():
                    if not budget.claim_event():
                        break
                    events.append({"id": f"{message_index}.{part_index}", "at": _iso(created),
                                   "channel": event_channel,
                                   **model_info,
                                   "duration_ms": _duration_ms(message),
                                   "stage": "model_text" if event_role == "assistant" else "conversation_message",
                                   "kind": kind, "role": event_role,
                                   "text": _redact_text(runtime, text, budget=budget), "tool": None,
                                   "input": None, "output": None, "exit_code": None,
                                   "confirmation": "not_applicable" if event_role == "assistant" else "unconfirmed",
                                   "available": True, "reason": None})
        events.extend(_tool_events(runtime, message, channel, created, message_index, budget))
        if budget.truncation_count > truncations_before_message:
            break
    events.sort(key=lambda event: (event.get("at") or event.get("_sort_at") or "",
                                   _event_order_key(event)))
    for event in events:
        event.pop("_sort_at", None)
    return events


def _session_status(runtime, owner: str, session_id: str, messages: list) -> tuple[str, dict | None]:
    if getattr(runtime, "active", None) == (owner, session_id):
        return "active", runtime.diagnostics.get(owner, session_id)
    error = runtime.diagnostics.get(owner, session_id)
    bounded_messages = messages[-MAX_MESSAGES:] if isinstance(messages, list) else []
    for message in reversed(bounded_messages):
        if isinstance(message, dict):
            info = message.get("info") if isinstance(message.get("info"), dict) else {}
            role = info.get("role")
            message_error = info.get("error")
            if role == "assistant" and isinstance(message_error, (dict, str)):
                summary = safe_error_summary(message_error)
                if summary.get("name") != "other" or summary.get("http_status") is not None:
                    return ("cancelled" if summary.get("name") == "MessageAbortedError" else "error",
                            summary)
            if role == "assistant":
                _created, ended = _message_times(message)
                parts = message.get("parts") if isinstance(message.get("parts"), list) else []
                if len(parts) > MAX_PARTS_PER_MESSAGE:
                    return "unknown", None
                has_text = any(isinstance(part, dict) and part.get("type") == "text"
                               and isinstance(part.get("text"), str) and part["text"].strip()
                               for part in parts[:MAX_PARTS_PER_MESSAGE])
                has_pending_tool = any(isinstance(part, dict) and part.get("type") == "tool"
                                       and isinstance(part.get("state"), dict)
                                       and part["state"].get("status") == "running"
                                       for part in parts[:MAX_PARTS_PER_MESSAGE])
                if ended and has_text and not has_pending_tool:
                    return "completed", None
                return ("error", error) if error else ("unknown", None)
            if role == "user":
                return ("error", error) if error else ("unknown", None)
    if error:
        return "error", error
    return "unknown", None


def _matches_window(events: list[dict], start: datetime | None, end: datetime | None,
                    raw_messages: list | None = None) -> bool:
    if start is None and end is None:
        return True
    for event in events:
        at = _parse_time(event.get("at")) if event.get("at") else None
        if at is None and isinstance(raw_messages, list):
            try:
                message_index = int(str(event.get("id", "")).split(".", 1)[0])
            except ValueError:
                message_index = -1
            if 0 <= message_index < len(raw_messages):
                message = raw_messages[message_index]
                info = message.get("info") if isinstance(message, dict) else None
                timing = info.get("time") if isinstance(info, dict) else None
                at = _timestamp(timing.get("created")) if isinstance(timing, dict) else None
        if at is not None and (start is None or at >= start) and (end is None or at < end):
            return True
    return False


async def _session_data(hass, owner: str, mapping: dict,
                        budget: ProjectionBudget | None = None) -> tuple[dict, list[dict], list]:
    assistant = hass.data[DOMAIN]["assistant"]
    runtime, messages, message_limit_reached = await _load_session_messages(
        hass, owner, mapping)
    return _project_session_messages(
        assistant, runtime, owner, mapping, messages,
        message_limit_reached=message_limit_reached,
        budget=budget,
    )


async def _load_session_messages(hass, owner: str, mapping: dict) -> tuple[object, list, bool]:
    """Fetch one already-mapped session once, retaining only its bounded history."""
    assistant = hass.data[DOMAIN]["assistant"]
    runtime = assistant.runtime
    if not _mapping_is_current(assistant, owner, mapping):
        raise AgentError("not_found")
    raw_messages = await runtime.request(
        "GET", f"session/{quote(mapping['session_id'], safe='')}/message?limit={MAX_MESSAGES}")
    if not _mapping_is_current(assistant, owner, mapping):
        raise AgentError("not_found")
    if not isinstance(raw_messages, list):
        raise AgentError("invalid_runtime_response")
    return runtime, raw_messages[-MAX_MESSAGES:], len(raw_messages) > MAX_MESSAGES


def _session_time_bounds(messages: list) -> tuple[datetime | None, datetime | None]:
    """Read creation/activity timestamps from the bounded runtime message metadata."""
    created_times = []
    activity_times = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        created, ended = _message_times(message)
        if created is not None:
            created_times.append(created)
            activity_times.append(created)
        if ended is not None and (created is None or ended >= created):
            activity_times.append(ended)
    return (min(created_times) if created_times else None,
            max(activity_times) if activity_times else None)


def _project_session_messages(assistant, runtime, owner: str, mapping: dict,
                              messages: list, *, message_limit_reached: bool = False,
                              budget: ProjectionBudget | None = None) -> tuple[dict, list[dict], list]:
    if not _mapping_is_current(assistant, owner, mapping):
        raise AgentError("not_found")
    budget = budget or ProjectionBudget()
    truncations_before = budget.truncation_count
    if message_limit_reached:
        budget.mark_truncated()
    events = project_messages(runtime, messages, mapping["channel"], budget)
    status, error = _session_status(runtime, owner, mapping["session_id"], messages)
    voice_present = any(event.get("channel") == "voice" for event in events)
    modes = [mapping["channel"]] + (["voice"] if mapping["channel"] == "web" and voice_present else [])
    created_at, updated_at = _session_time_bounds(messages)
    provider_models = []
    for message in messages:
        if budget.remaining_bytes <= 0:
            budget.mark_truncated()
            break
        info = message.get("info") if isinstance(message, dict) and isinstance(message.get("info"), dict) else {}
        pair = _provider_model(info, budget)
        if pair["provider"] is not None or pair["model"] is not None:
            if pair not in provider_models:
                if len(provider_models) >= MAX_PROVIDER_MODELS:
                    budget.mark_truncated()
                    break
                provider_models.append(pair)
    session = {
        "session_id": mapping["session_id"], "channel": mapping["channel"],
        "modes": modes, "status": status,
        "provider_models": provider_models,
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
        "processing_duration_ms": sum(
            _duration_ms(message) or 0 for message in messages
            if isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("role") == "assistant") or None,
        "message_count": sum(event["kind"] in {"user_message", "assistant_text", "voice_transcript"} for event in events),
        "at_message_limit": len(messages) >= MAX_MESSAGES,
        "events_truncated": budget.truncation_count > truncations_before,
        "error": _clean_value(runtime, error) if error else None,
    }
    return session, events, messages


def _unavailable_stage(stage: str, channel: str, reason: str = "not_recorded") -> dict:
    return {"id": "unavailable." + stage, "at": None, "channel": channel,
            "stage": stage, "kind": "stage_unavailable", "role": None, "text": None,
            "tool": None, "input": None, "output": None, "exit_code": None,
            "duration_ms": None,
            "confirmation": "unknown", "available": False, "reason": reason}


def _stage_rows(events: list[dict], channel: str) -> list[dict]:
    present = {event["stage"] for event in events}
    result = list(events)
    for stage in ("client_send", "backend_accept", "model_request", "tool_call", "tool_result", "cancellation"):
        if stage not in present:
            result.append(_unavailable_stage(stage, channel))
    if any(event.get("channel") == "voice" for event in events):
        if "voice_transport" not in present:
            result.append(_unavailable_stage("voice_transport", "voice"))
    return sorted(result, key=lambda event: (event.get("at") or "", _event_order_key(event)))


def _valid_channel(value: str | None) -> str:
    value = value or "all"
    if value not in CHANNELS:
        raise ValueError("invalid_channel")
    return value


def _valid_status(value: str | None) -> str:
    value = value or "all"
    if value not in STATUSES:
        raise ValueError("invalid_status")
    return value


class DiagnosticsSessionsView(HomeAssistantView):
    url = "/api/habab_home/diagnostics/sessions"
    name = "api:habab_home:diagnostics:sessions"
    requires_auth = True

    async def get(self, request):
        owner = _admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        hass = request.app["hass"]
        assistant = hass.data.get(DOMAIN, {}).get("assistant")
        if assistant is None:
            return self.json({"sessions": [], "next_cursor": None,
                              "sources": _sources(False, "assistant_unavailable")})
        try:
            start, end = _time_window(request.query)
            channel = _valid_channel(request.query.get("channel"))
            requested_status = _valid_status(request.query.get("status"))
            limit = int(request.query.get("limit", str(MAX_LIST_LIMIT)))
            cursor = int(request.query.get("cursor", "0"))
            if not 1 <= limit <= MAX_LIST_LIMIT or not 0 <= cursor <= MAX_MAPPED_SESSIONS:
                raise ValueError("invalid_page")
            filter_session = request.query.get("session_id")
            if filter_session is not None and not SESSION_ID.fullmatch(filter_session):
                raise ValueError("invalid_session")
        except (TypeError, ValueError):
            return self.json_message("invalid_filter", status_code=400)
        all_records = _mapped_sessions(assistant, owner.id)
        session_limit_reached = len(all_records) >= MAX_MAPPED_SESSIONS
        records = all_records
        if channel in {"web", "telegram"}:
            records = [record for record in records if record["channel"] == channel]
        elif channel == "voice":
            records = [record for record in records if record["channel"] == "web"]
        if filter_session:
            records = [record for record in records if record["session_id"] == filter_session]
        total_records = len(records)
        records = records[cursor:cursor + limit]
        sessions = []
        history_available = True
        for record in records:
            try:
                session, events, raw_messages = await _session_data(hass, owner.id, record)
            except AgentError:
                history_available = False
                raw_messages = []
                session = {"session_id": record["session_id"], "channel": record["channel"],
                           "modes": record["modes"], "status": "unknown", "created_at": None,
                           "updated_at": None, "provider_models": [], "processing_duration_ms": None,
                           "message_count": 0, "at_message_limit": False,
                           "events_truncated": False, "error": None}
                events = []
            if channel == "voice" and "voice" not in session["modes"]:
                continue
            if requested_status != "all" and session["status"] != requested_status:
                continue
            if not _matches_window(events, start, end, raw_messages):
                continue
            sessions.append(session)
        next_offset = cursor + len(records)
        next_cursor = str(next_offset) if next_offset < total_records else None
        response = self.json({"sessions": sessions, "next_cursor": next_cursor,
                              "sources": _sources(history_available,
                                                  None if history_available else "runtime_unavailable",
                                                  session_limit_reached)})
        response.headers["Cache-Control"] = "no-store"
        return response


class DiagnosticsSessionView(HomeAssistantView):
    url = "/api/habab_home/diagnostics/sessions/{session_id}"
    name = "api:habab_home:diagnostics:session"
    requires_auth = True

    async def get(self, request, session_id):
        owner = _admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            return self.json_message("not_found", status_code=404)
        try:
            channel_query = _valid_channel(request.query.get("channel"))
            start, end = _time_window(request.query)
        except ValueError:
            return self.json_message("invalid_filter", status_code=400)
        hass = request.app["hass"]
        assistant = hass.data.get(DOMAIN, {}).get("assistant")
        if assistant is None:
            return self.json_message("not_found", status_code=404)
        mapped = _mapped_sessions(assistant, owner.id)
        session_limit_reached = len(mapped) >= MAX_MAPPED_SESSIONS
        matches = [record for record in mapped
                   if record["session_id"] == session_id and
                   (channel_query in {"all", record["channel"]}
                    or channel_query == "voice" and record["channel"] == "web")]
        if len(matches) != 1:
            return self.json_message("not_found", status_code=404)
        try:
            session, events, raw_messages = await _session_data(hass, owner.id, matches[0])
        except AgentError as error:
            if error.code == "not_found":
                return self.json_message("not_found", status_code=404)
            response = self.json({
                "session": {"session_id": session_id, "channel": matches[0]["channel"],
                            "modes": matches[0]["modes"], "status": "unknown",
                            "created_at": None, "updated_at": None, "message_count": 0,
                            "provider_models": [], "processing_duration_ms": None,
                            "at_message_limit": False, "events_truncated": False, "error": None},
                "events": [_unavailable_stage("conversation_history", matches[0]["channel"], "runtime_unavailable")],
                "sources": _sources(False, "runtime_unavailable", session_limit_reached),
            })
            response.headers["Cache-Control"] = "no-store"
            return response
        if channel_query == "voice" and "voice" not in session["modes"]:
            return self.json_message("not_found", status_code=404)
        if start is not None and end is not None:
            events = [event for event in events
                      if _matches_window([event], start, end, raw_messages)]
        response = self.json({"session": session, "events": _stage_rows(events, session["channel"]),
                              "sources": _sources(True, session_limit_reached=session_limit_reached)})
        response.headers["Cache-Control"] = "no-store"
        return response


class DiagnosticsExportView(HomeAssistantView):
    url = "/api/habab_home/diagnostics/export"
    name = "api:habab_home:diagnostics:export"
    requires_auth = True

    async def post(self, request):
        owner = _admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        content_length = getattr(request, "content_length", None)
        if content_length is not None and content_length > 8192:
            return self.json_message("invalid_export", status_code=413)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("invalid_request")
            start, end = _parse_time(data.get("from")), _parse_time(data.get("to"))
            if ((start is None) != (end is None)
                    or start and end and (end <= start or end - start > MAX_WINDOW)):
                raise ValueError("invalid_time_window")
            channel = _valid_channel(data.get("channel"))
            session_id = data.get("session_id")
            if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)):
                raise ValueError("invalid_session")
            if session_id is None and start is None:
                raise ValueError("selection_required")
        except (ValueError, TypeError):
            return self.json_message("invalid_export", status_code=400)
        hass = request.app["hass"]
        assistant = hass.data.get(DOMAIN, {}).get("assistant")
        if assistant is None:
            return self.json_message("assistant_unavailable", status_code=503)
        mapped = _mapped_sessions(assistant, owner.id)
        session_limit_reached = len(mapped) >= MAX_MAPPED_SESSIONS
        records = mapped
        if channel in {"web", "telegram"}:
            records = [record for record in records if record["channel"] == channel]
        elif channel == "voice":
            records = [record for record in records if record["channel"] == "web"]
        if session_id is not None:
            records = [record for record in records if record["session_id"] == session_id]
            if channel == "voice":
                records = [record for record in records if record["channel"] == "web"]
            if not records:
                return self.json_message("not_found", status_code=404)
        if len(records) > MAX_EXPORT_SESSIONS:
            return self.json_message("export_session_limit", status_code=413)
        exported = []
        history_available = True
        projection_budget = ProjectionBudget()
        loaded_sessions = []
        for record_index, record in enumerate(records):
            try:
                runtime, messages, message_limit_reached = await _load_session_messages(
                    hass, owner.id, record)
                _created_at, activity_at = _session_time_bounds(messages)
                loaded_sessions.append({
                    "record": record, "record_index": record_index,
                    "runtime": runtime, "messages": messages,
                    "message_limit_reached": message_limit_reached,
                    "activity_at": activity_at,
                })
            except AgentError:
                history_available = False
                loaded_sessions.append({
                    "record": record, "record_index": record_index,
                    "runtime": None, "messages": [],
                    "message_limit_reached": False, "activity_at": None,
                })

        # Fetch each bounded history once, then give the shared projection
        # budget to the most recently active mapped sessions first.
        unknown_time = datetime.min.replace(tzinfo=timezone.utc)
        loaded_sessions.sort(
            key=lambda item: (item["activity_at"] is not None,
                              item["activity_at"] or unknown_time),
            reverse=True,
        )
        exported_with_recency = []
        for item in loaded_sessions:
            record = item["record"]
            try:
                if item["runtime"] is None:
                    raise AgentError("runtime_unavailable")
                session, events, _raw = _project_session_messages(
                    assistant, item["runtime"], owner.id, record, item["messages"],
                    message_limit_reached=item["message_limit_reached"],
                    budget=projection_budget,
                )
            except AgentError:
                history_available = False
                session = {"session_id": record["session_id"], "channel": record["channel"],
                           "modes": record["modes"], "status": "unknown", "created_at": None,
                           "updated_at": None, "provider_models": [], "processing_duration_ms": None,
                           "message_count": 0, "at_message_limit": False,
                           "events_truncated": False, "error": None}
                events = []
            if channel == "voice" and "voice" not in session["modes"]:
                continue
            if not _matches_window(events, start, end, item["messages"]):
                continue
            if not events and session["status"] == "unknown":
                events.append(_unavailable_stage("conversation_history", session["channel"],
                                                 "runtime_unavailable"))
            exported_with_recency.append((
                item["activity_at"], item["record_index"],
                {"session": session, "events": _stage_rows(events, session["channel"])},
            ))
        oldest_time = datetime.max.replace(tzinfo=timezone.utc)
        exported_with_recency.sort(
            key=lambda item: (item[0] is None, item[0] or oldest_time, item[1]),
        )
        exported = [item[2] for item in exported_with_recency]
        if session_id is not None and not exported:
            return self.json_message("not_found", status_code=404)
        manifest = {
            "product": "Habab Home", "versions": {
                "habab": VERSION, "home_assistant_core": HA_VERSION,
                "frontend": None, "opencode": None,
            },
            "build_identity": None,
            "unavailable_build_fields": ["frontend_version", "opencode_version", "build_identity"],
            "period": {"from": _iso(start), "to": _iso(end), "timezone": "UTC"},
            "sources": _sources(history_available,
                                 None if history_available else "runtime_unavailable",
                                 session_limit_reached),
            "limits": {"sessions": MAX_EXPORT_SESSIONS, "messages_per_session": MAX_MESSAGES,
                       "parts_per_message": MAX_PARTS_PER_MESSAGE,
                       "mapped_sessions": MAX_MAPPED_SESSIONS,
                       "runtime_response_bytes_per_session": MAX_RUNTIME_RESPONSE_BYTES,
                       "projection_bytes": MAX_PROJECTED_BYTES,
                       "projection_events": MAX_PROJECTED_EVENTS,
                       "projection_items": MAX_PROJECTED_ITEMS,
                       "provider_models_per_session": MAX_PROVIDER_MODELS,
                       "archive_bytes": MAX_EXPORT_BYTES, "window_days": MAX_WINDOW.days},
            "redactions_applied": ["configured provider/runtime secrets", "secret-named fields",
                                   "credential-like string patterns"],
            "excluded": ["audio", "provider configuration", "Home Assistant config and secret files",
                         "raw OpenCode server logs", "Home Assistant logs", "native Assist history"],
            "audio_included": False,
            "message_limit_reached": any(item["session"].get("at_message_limit") for item in exported),
            "projection_truncated": projection_budget.truncation_count > 0,
            "truncated": (projection_budget.truncation_count > 0 or
                          any(item["session"].get("at_message_limit") for item in exported)),
        }
        history = {"sessions": exported}
        text_lines = ["Habab Home assistant diagnostics", "UTC timestamps; model text is not proof of actions."]
        for item in exported:
            text_lines.append(f"\n[{item['session']['channel']}] {item['session']['session_id']} ({item['session']['status']})")
            for event in item["events"]:
                text = event.get("text") or event.get("tool") or ""
                if event.get("input") is not None:
                    text += " input=" + json.dumps(event["input"], ensure_ascii=False)
                if event.get("output") is not None:
                    text += " output=" + json.dumps(event["output"], ensure_ascii=False)
                text_lines.append(f"{event.get('at') or 'time unavailable'} {event['stage']}: {text}")
        files = {
            "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            "history.json": json.dumps(history, ensure_ascii=False, indent=2).encode("utf-8"),
            "history.txt": ("\n".join(text_lines) + "\n").encode("utf-8"),
            "opencode/availability.json": json.dumps(_sources(True)["opencode_files"], indent=2).encode("utf-8"),
        }
        if sum(map(len, files.values())) > MAX_EXPORT_BYTES:
            return self.json_message("export_too_large", status_code=413)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        payload = buffer.getvalue()
        if len(payload) > MAX_EXPORT_BYTES:
            return self.json_message("export_too_large", status_code=413)
        response = web.Response(body=payload, content_type="application/zip")
        response.headers["Content-Disposition"] = 'attachment; filename="habab-diagnostics.zip"'
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


class DiagnosticsRuntimeLogsExportView(HomeAssistantView):
    """Explicit admin-only export of bounded logs shared by this HA instance."""

    url = "/api/habab_home/diagnostics/runtime-logs/export"
    name = "api:habab_home:diagnostics:runtime_logs_export"
    requires_auth = True

    async def post(self, request):
        if _admin(request) is None:
            return self.json_message("not_allowed", status_code=403)
        content_length = getattr(request, "content_length", None)
        if content_length is not None and content_length > 8192:
            return self.json_message("invalid_export", status_code=413)
        try:
            data = await request.json()
            if not isinstance(data, dict) or data:
                raise ValueError("empty_request_required")
        except (ValueError, TypeError):
            return self.json_message("invalid_export", status_code=400)

        hass = request.app["hass"]
        assistant = hass.data.get(DOMAIN, {}).get("assistant")
        runtime = getattr(assistant, "runtime", None)
        if runtime is None or not isinstance(getattr(runtime, "root", None), (str, Path)):
            return self.json_message("runtime_logs_unavailable", status_code=503)
        config_root = Path(hass.config.path()).absolute()
        opencode_root = Path(hass.config.path(".habab-opencode")).absolute()
        if Path(runtime.root).absolute() != opencode_root:
            return self.json_message("runtime_logs_unavailable", status_code=503)

        sources, raw_files = await hass.async_add_executor_job(
            _collect_instance_log_tails, config_root, opencode_root)
        log_files = _redact_instance_log_tails(runtime, sources, raw_files)
        truncated = any(source["truncated_files"] or source["omitted_files"]
                        for source in sources.values())
        captured_at = datetime.now(timezone.utc)
        manifest = {
            "product": "Habab Home",
            "scope": {
                "kind": "home_assistant_instance",
                "shared_across_conversations": True,
                "shared_across_users": True,
                "session_id": None,
                "warning": "These bounded runtime logs may contain events from other conversations on this instance.",
            },
            "captured_at": _iso(captured_at),
            "timezone": "UTC",
            "sources": sources,
            "limits": {
                "opencode_files": MAX_OPENCODE_LOG_FILES,
                "home_assistant_files": MAX_HA_LOG_FILES,
                "bytes_per_file_tail": MAX_INSTANCE_LOG_FILE_BYTES,
                "total_redacted_log_bytes": MAX_INSTANCE_LOG_BYTES,
                "text_scan_chars_per_file": MAX_TEXT_SCAN_CHARS,
                "archive_bytes": MAX_EXPORT_BYTES,
            },
            "redactions_applied": [
                "configured provider and runtime secrets",
                "secret-named assignments and credential-like strings",
                "oversized text tails truncated before export",
            ],
            "excluded": [
                "conversation history and tool payloads",
                "provider configuration and Home Assistant config files",
                "audio and native Assist history",
                "files outside the fixed OpenCode and Home Assistant log allowlists",
            ],
            "truncated": truncated,
        }
        archive_files = {
            "logs/manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            **log_files,
        }
        if sum(map(len, archive_files.values())) > MAX_EXPORT_BYTES:
            return self.json_message("export_too_large", status_code=413)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in archive_files.items():
                archive.writestr(name, content)
        payload = buffer.getvalue()
        if len(payload) > MAX_EXPORT_BYTES:
            return self.json_message("export_too_large", status_code=413)
        response = web.Response(body=payload, content_type="application/zip")
        response.headers["Content-Disposition"] = 'attachment; filename="habab-runtime-logs.zip"'
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def async_register_views(hass):
    hass.http.register_view(DiagnosticsSessionsView())
    hass.http.register_view(DiagnosticsSessionView())
    hass.http.register_view(DiagnosticsExportView())
    hass.http.register_view(DiagnosticsRuntimeLogsExportView())
