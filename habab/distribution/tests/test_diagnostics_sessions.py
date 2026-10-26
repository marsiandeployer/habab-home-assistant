"""Bounded, private indexes for archived conversation diagnostics."""

from homeassistant.components.habab_home.diagnostics_sessions import (
    MAX_ARCHIVED_TELEGRAM_SESSIONS_PER_OWNER,
    MAX_ARCHIVED_WEB_SESSIONS_PER_OWNER,
    MAX_SESSION_INDEX_OWNERS,
    normalize_telegram_session_index,
    normalize_web_session_index,
    remember_telegram_session,
    remember_web_session,
)


def _records(count, prefix="ses"):
    return [
        {"session_id": f"{prefix}_{index}", "archived_at": f"2026-10-{index % 28 + 1:02d}T00:00:00Z"}
        for index in range(count)
    ]


def test_web_archive_keeps_newest_sessions_within_owner_limit():
    index = {"alice": list(reversed(_records(MAX_ARCHIVED_WEB_SESSIONS_PER_OWNER)))}

    updated = remember_web_session(index, "alice", "ses_new", "2026-10-26T12:00:00Z")

    assert len(updated["alice"]) == MAX_ARCHIVED_WEB_SESSIONS_PER_OWNER
    assert updated["alice"][0]["session_id"] == "ses_new"
    assert "ses_0" not in {record["session_id"] for record in updated["alice"]}
    assert index["alice"][0]["session_id"] == f"ses_{MAX_ARCHIVED_WEB_SESSIONS_PER_OWNER - 1}"


def test_owner_index_evicts_least_recently_updated_owner():
    index = {f"owner_{number}": _records(1, f"ses{number}")
             for number in range(MAX_SESSION_INDEX_OWNERS)}

    updated = remember_web_session(index, "owner_new", "ses_recent", "2026-10-26T12:00:00Z")

    assert len(updated) == MAX_SESSION_INDEX_OWNERS
    assert "owner_new" in updated
    assert "owner_0" not in updated
    assert list(updated)[-1] == "owner_new"


def test_web_normalization_drops_invalid_and_duplicate_sessions():
    index = normalize_web_session_index({
        "alice": [
            {"session_id": "ses_valid", "archived_at": "2026-10-26T12:00:00Z"},
            {"session_id": "ses_valid", "archived_at": "2026-10-25T12:00:00Z"},
            {"session_id": "invalid/session", "archived_at": "2026-10-24T12:00:00Z"},
            {"session_id": "ses_missing_time"},
        ],
        "bad_owner": "not-a-list",
    })

    assert index == {
        "alice": [{"session_id": "ses_valid", "archived_at": "2026-10-26T12:00:00Z"}]
    }


def test_telegram_archive_stores_only_opaque_scope_hash():
    scope_id = "a" * 64

    updated = remember_telegram_session(
        {}, "alice", "ses_telegram", "2026-10-26T12:00:00Z", scope_id)

    assert updated["alice"] == [{
        "session_id": "ses_telegram",
        "archived_at": "2026-10-26T12:00:00Z",
        "scope_id": scope_id,
    }]
    assert "chat_id" not in str(updated)
    assert remember_telegram_session(
        updated, "alice", "ses_invalid_scope", "2026-10-26T12:00:00Z", "raw-chat-id") is updated


def test_telegram_normalization_bounds_each_owner_and_validates_scope():
    index = normalize_telegram_session_index({
        "alice": [
            {"session_id": "ses_bad_scope", "archived_at": "2026-10-26T12:00:00Z",
             "scope_id": "raw-chat-id"},
            *[
                {"session_id": f"ses_{number}", "archived_at": "2026-10-26T12:00:00Z",
                 "scope_id": "b" * 64}
                for number in range(MAX_ARCHIVED_TELEGRAM_SESSIONS_PER_OWNER + 1)
            ],
        ]
    })

    assert len(index["alice"]) == MAX_ARCHIVED_TELEGRAM_SESSIONS_PER_OWNER
    assert "scope_id" not in index["alice"][0]
    assert all(record.get("scope_id") == "b" * 64
               for record in index["alice"][1:])
    assert "raw-chat-id" not in str(index)
