"""Small private indexes of prior channel conversation IDs for diagnostics."""

from __future__ import annotations

import re


MAX_ARCHIVED_WEB_SESSIONS_PER_OWNER = 50
MAX_ARCHIVED_TELEGRAM_SESSIONS_PER_OWNER = 50
MAX_SESSION_INDEX_OWNERS = 128
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SCOPE_ID = re.compile(r"[0-9a-f]{64}\Z")


def normalize_session_index(
    value: object, limit: int
) -> dict[str, list[dict[str, str]]]:
    """Keep only bounded owner keys and session metadata from private storage."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[dict[str, str]]] = {}
    for owner, records in reversed(list(value.items())):
        if len(result) >= MAX_SESSION_INDEX_OWNERS:
            break
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            continue
        if not isinstance(records, list):
            continue
        clean = []
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            session_id = record.get("session_id")
            archived_at = record.get("archived_at")
            if (not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)
                    or session_id in seen or not isinstance(archived_at, str)
                    or not archived_at or len(archived_at) > 64):
                continue
            seen.add(session_id)
            normalized = {"session_id": session_id, "archived_at": archived_at}
            scope_id = record.get("scope_id")
            if isinstance(scope_id, str) and _SCOPE_ID.fullmatch(scope_id):
                normalized["scope_id"] = scope_id
            clean.append(normalized)
            if len(clean) >= limit:
                break
        if clean:
            result[owner] = clean
    return result


def remember_session(
    index: dict[str, list[dict[str, str]]], owner: str, session_id: str,
    archived_at: str, limit: int, scope_id: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Return a new index with this owner's session first and the oldest pruned."""
    if (not isinstance(owner, str) or not owner or len(owner) > 128
            or not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)
            or not isinstance(archived_at, str) or not archived_at or len(archived_at) > 64
            or (scope_id is not None and (not isinstance(scope_id, str)
                                          or not _SCOPE_ID.fullmatch(scope_id)))):
        return index
    updated = dict(index)
    updated.pop(owner, None)
    previous = [record for record in index.get(owner, [])
                if record.get("session_id") != session_id]
    new_record = {"session_id": session_id, "archived_at": archived_at}
    if scope_id is not None:
        new_record["scope_id"] = scope_id
    updated[owner] = ([new_record, *previous]
                      [:limit])
    while len(updated) > MAX_SESSION_INDEX_OWNERS:
        updated.pop(next(iter(updated)))
    return updated


def normalize_web_session_index(value: object) -> dict[str, list[dict[str, str]]]:
    return normalize_session_index(value, MAX_ARCHIVED_WEB_SESSIONS_PER_OWNER)


def normalize_telegram_session_index(value: object) -> dict[str, list[dict[str, str]]]:
    return normalize_session_index(value, MAX_ARCHIVED_TELEGRAM_SESSIONS_PER_OWNER)


def remember_web_session(
    index: dict[str, list[dict[str, str]]], owner: str, session_id: str, archived_at: str
) -> dict[str, list[dict[str, str]]]:
    return remember_session(
        index, owner, session_id, archived_at, MAX_ARCHIVED_WEB_SESSIONS_PER_OWNER)


def remember_telegram_session(
    index: dict[str, list[dict[str, str]]], owner: str, session_id: str, archived_at: str,
    scope_id: str,
) -> dict[str, list[dict[str, str]]]:
    return remember_session(
        index, owner, session_id, archived_at, MAX_ARCHIVED_TELEGRAM_SESSIONS_PER_OWNER,
        scope_id)
