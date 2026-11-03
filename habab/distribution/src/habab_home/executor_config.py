"""Map the selected AI connection to its task executor without storing secrets."""

from collections.abc import Mapping


CODEX_PROVIDERS = frozenset({"openai", "habab"})
CODEX_MODEL = "gpt-6-luna"
CODEX_SERVICE_TIER = "fast"


def selected_executor(settings) -> str:
    """Return the executor implied by a selected AI provider configuration."""
    provider = settings.get("provider") if isinstance(settings, Mapping) else None
    return "codex" if provider in CODEX_PROVIDERS else "opencode"


def executor_status(settings) -> dict:
    """Return display metadata only; provider credentials are never included."""
    executor = selected_executor(settings)
    provider = settings.get("provider") if isinstance(settings, Mapping) else None
    return {
        "executor": executor,
        "provider": provider,
        "model": CODEX_MODEL if executor == "codex" else None,
        "service_tier": CODEX_SERVICE_TIER if executor == "codex" else None,
    }
