"""Map the selected AI connection to its task executor without storing secrets."""

from collections.abc import Mapping
import os
import re
from pathlib import Path


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


def task_executor_runtime_metadata(settings, *, binary_path=None, digest=None):
    """Return safe executor configuration and cheap Codex installation facts.

    Codex verifies the binary digest before execution. Session setup only checks
    the executable and its small digest sidecar; it never hashes the binary.
    """
    configured = executor_status(settings)
    task_executor = {
        "executor": configured["executor"],
        "model": configured["model"],
        "service_tier": configured["service_tier"],
    }
    binary = str(binary_path or os.environ.get("HABAB_CODEX_BINARY", "/opt/habab-codex/codex"))
    expected_digest = digest if digest is not None else os.environ.get("HABAB_CODEX_SHA256")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        try:
            with open(binary + ".sha256", "rb") as sidecar:
                first = sidecar.read(256).decode("ascii").strip().split()
            expected_digest = first[0] if first and re.fullmatch(r"[a-f0-9]{64}", first[0]) else None
        except (OSError, UnicodeError, ValueError):
            expected_digest = None
    try:
        present = Path(binary).is_file()
        executable = present and os.access(binary, os.X_OK)
    except (OSError, ValueError):
        present, executable = None, None
    if present is False:
        state = "not_installed"
    elif present is None:
        state = "unknown"
    elif not executable:
        state = "not_ready"
    elif expected_digest:
        state = "available"
    else:
        state = "not_verified"
    return {
        "task_executor": task_executor,
        "codex_runtime": {
            "installation_state": state,
            "binary_present": present,
            "executable": executable,
            "integrity_digest_configured": bool(expected_digest),
            "content_hash_verified": False,
        },
    }
