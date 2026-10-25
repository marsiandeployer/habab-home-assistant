"""Resolve the provider actually used by the shared assistant."""

from .providers import PROVIDERS


LEGACY_PROVIDER_ORDER = ("deepseek", "openai", "habab", "gemini")


def effective_provider_entry(entries):
    """Return the same configured entry selected for assistant requests.

    Explicit persisted selection wins. Older installations without that option
    keep the existing provider fallback order until an administrator selects one.
    """
    entries = tuple(entries)
    for entry in entries:
        if (entry.data.get("provider") in PROVIDERS
                and getattr(entry, "options", {}).get("assistant_selected")):
            return entry

    for provider in LEGACY_PROVIDER_ORDER:
        for entry in entries:
            if entry.data.get("provider") == provider:
                return entry
    return None
