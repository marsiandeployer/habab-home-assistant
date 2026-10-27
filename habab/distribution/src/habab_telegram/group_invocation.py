"""Literal family-group invocation syntax; this does not infer message intent."""
import re

_PREFIX = re.compile(r"^(?:Ко|Эй\s+дом)(?=$|[^\w])", re.IGNORECASE)
_SEPARATORS = " \t\r\n,:;.!?—–-"
_CONTROLS = frozenset({'help', 'new', 'cancel'})


def group_request(text, username):
    """Return a nonempty addressed request, or None for ordinary family text."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    # Telegram command addressing is a literal protocol, including the bot name.
    if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}', username):
        command = re.fullmatch(r'/([A-Za-z]+)@' + re.escape(username), text, re.IGNORECASE)
        if command and command[1].lower() in _CONTROLS:
            return '/' + command[1].lower()
        mention = re.compile(r'(?<![\w@])@' + re.escape(username) + r'(?!\w)', re.IGNORECASE)
    else:
        mention = None
    prefix = _PREFIX.match(text)
    mentioned = mention.search(text) if mention else None
    if not prefix and not mentioned:
        return None
    if prefix:
        text = text[prefix.end():].lstrip(_SEPARATORS)
    if mentioned:
        text = mention.sub('', text).strip(_SEPARATORS)
    return text if text else None
