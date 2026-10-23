"""Bounded, redacted OpenCode error summaries for the owner web chat."""

from collections import OrderedDict

MAX_DIAGNOSTICS = 100

_KNOWN_ERROR_NAMES = {
    "APIError", "AuthenticationError", "ContextOverflowError", "InvalidModelError",
    "MessageAbortedError", "ModelNotFoundError", "ProviderAuthError", "ProviderError",
    "RateLimitError", "UnknownError",
}
_FLAG_MARKERS = {
    "missing_reasoning_content": ("missing_reasoning_content",),
    "mentions_reasoning_content": ("reasoning_content",),
    "invalid_model": ("invalid_model", "model_not_found", "model not found"),
    "quota": ("insufficient_quota", "quota_exceeded", "billing_hard_limit", "rate_limit_exceeded"),
    "mentions_tool_calls": ("tool_calls", "tool calls"),
    "mentions_context_limit": ("context_length_exceeded", "maximum context length", "context_limit"),
    "mentions_timeout": ("timeout", "timed out", "etimedout"),
}


def _walk_error_values(value, depth=0):
    if depth > 4:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            if isinstance(child, (dict, list)):
                yield from _walk_error_values(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:20]:
            if isinstance(child, (dict, list)):
                yield from _walk_error_values(child, depth + 1)


def _http_status(values):
    for key, value in values:
        if key not in ("status", "statusCode", "status_code", "httpStatus"):
            continue
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdecimal() and 100 <= int(value) <= 599:
            return int(value)
    return None


def safe_error_summary(error):
    """Extract enum-like metadata only; never retain provider text or headers."""
    if not isinstance(error, (dict, str)):
        error = {}
    values = list(_walk_error_values(error))
    if isinstance(error, str):
        values.append(("message", error[:128 * 1024]))
    raw_name = next((value for key, value in values
                     if key in ("name", "code", "type") and isinstance(value, str)), "")
    name = raw_name if raw_name in _KNOWN_ERROR_NAMES else "other"

    # Inspect free-form strings only in memory to derive fixed booleans. They are
    # never copied to the record or returned to the client.
    searchable = []
    for key, value in values:
        if key.lower() in ("message", "responsebody", "response_body", "body", "code"):
            if isinstance(value, str):
                searchable.append(value[:128 * 1024].lower())
            elif isinstance(value, (dict, list)):
                searchable.append(str(value)[:128 * 1024].lower())
    technical_text = "\n".join(searchable)
    result = {"name": name, "http_status": _http_status(values)}
    result.update({
        flag: any(marker in technical_text for marker in markers)
        for flag, markers in _FLAG_MARKERS.items()
    })
    return result


class AgentDiagnostics:
    """Keep at most one safe error summary per owner/session, in process memory."""

    def __init__(self, limit=MAX_DIAGNOSTICS):
        self.limit = limit
        self._records = OrderedDict()

    def record(self, owner, session_id, error):
        key = (owner, session_id)
        self._records.pop(key, None)
        self._records[key] = safe_error_summary(error)
        while len(self._records) > self.limit:
            self._records.popitem(last=False)

    def clear(self, owner, session_id):
        self._records.pop((owner, session_id), None)

    def get(self, owner, session_id):
        value = self._records.get((owner, session_id))
        return dict(value) if value else None
