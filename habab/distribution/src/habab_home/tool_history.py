"""Bounded, redacted projection of actual OpenCode tool parts for chat history."""

import json
import re

MAX_TOOL_CALLS = 24
MAX_FIELD_BYTES = 8 * 1024
MAX_TURN_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_STATUSES = {"pending", "running", "completed", "error"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _clip_text(text, limit):
    """Bound escaped JSON bytes as well as the plaintext UTF-8 field."""
    if len(_encoded(text)) - 2 <= limit:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(_encoded(text[:middle])) - 2 <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low], True


def _safe_text(runtime, value):
    # diagnostics imports setup_agent: load its established redaction only once
    # SetupAgent is fully imported, rather than creating a module import cycle.
    from .diagnostics import ProjectionBudget, _clean_value, _redact_text

    if value is None:
        return "", False
    budget = ProjectionBudget(max_bytes=MAX_FIELD_BYTES)
    cleaned = _clean_value(runtime, value, budget=budget)
    text = cleaned if isinstance(cleaned, str) else json.dumps(
        cleaned, ensure_ascii=False, separators=(",", ":"))
    # Also redact dictionary keys and the final serialized representation.
    final_budget = ProjectionBudget(max_bytes=MAX_FIELD_BYTES)
    text = _redact_text(runtime, text, limit=MAX_FIELD_BYTES + 1, budget=final_budget)
    text, clipped = _clip_text(text, MAX_FIELD_BYTES)
    return text, clipped or budget.truncation_count > 0 or final_budget.truncation_count > 0


def _project_calls(runtime, calls, overflow):
    result = []
    for raw in calls:
        row = {"id": raw["id"], "name": raw["name"],
               "status": raw["state"].get("status", "unknown"),
               "input": "", "output": "", "error": "", "truncated": False}
        if not isinstance(row["status"], str) or row["status"] not in _STATUSES:
            row["status"] = "unknown"
        for field in ("input", "output", "error"):
            row[field], clipped = _safe_text(runtime, raw["state"].get(field))
            row["truncated"] = row["truncated"] or clipped
        if len(_encoded([*result, row])) > MAX_TURN_BYTES:
            row["truncated"] = True
            full = dict(row)
            row.update(input="", output="", error="")
            available = MAX_TURN_BYTES - len(_encoded([*result, row]))
            if available < 0:
                if result:
                    result[-1]["truncated"] = True
                break
            # Preserve an actual error first when the total turn budget is low.
            for field in ("error", "input", "output"):
                row[field], _clipped = _clip_text(full[field], available)
                available -= len(_encoded(row[field])) - 2
        result.append(row)
    if overflow and result:
        result[-1]["truncated"] = True
    return result


def tool_calls_by_message(runtime, messages):
    """Attach a turn's calls once to its final assistant message index.

    Native parentID is the only cross-message join. Without it, a message stays
    independent; voice and direct-dialogue JSON never becomes a tool event.
    """
    groups = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        parent = info.get("parentID")
        key = ("parent", parent) if isinstance(parent, str) and parent else ("message", index)
        group = groups.setdefault(key, {"target": index, "final": None,
                                         "calls": {}, "overflow": False})
        group["target"] = index
        if "structured" in info:
            group["final"] = index
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool" or part.get("synthetic"):
                continue
            name = part.get("tool")
            call_id = part.get("callID") or part.get("id")
            state = part.get("state")
            if (not isinstance(name, str) or name == "StructuredOutput"
                    or not _IDENTIFIER.fullmatch(name)
                    or not isinstance(call_id, str) or not _IDENTIFIER.fullmatch(call_id)
                    or not isinstance(state, dict)):
                continue
            if call_id in group["calls"]:
                prior = group["calls"][call_id]
                if prior["name"] == name:
                    prior["state"].update(state)
                continue
            if len(group["calls"]) >= MAX_TOOL_CALLS:
                group["overflow"] = True
                continue
            group["calls"][call_id] = {"id": call_id, "name": name, "state": dict(state)}
    projected = {}
    for group in groups.values():
        if group["calls"]:
            target = group["final"] if group["final"] is not None else group["target"]
            projected[target] = _project_calls(runtime, group["calls"].values(), group["overflow"])
    return projected
