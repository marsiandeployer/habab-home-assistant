"""Private numeric usage journal. Never persist prompts, responses or credentials."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re


def token_counts(value):
    if not isinstance(value, dict):
        return None
    fields = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
    result = {k: value[k] for k in fields if type(value.get(k)) is int and 0 <= value[k] <= 10**12}
    for source, target in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')):
        if target not in result and type(value.get(source)) is int and 0 <= value[source] <= 10**12:
            result[target] = value[source]
    return result or None


class StreamUsage:
    """Read bounded SSE lines, retaining only numeric usage."""
    def __init__(self):
        self.pending = bytearray()
        self.usage = None
        self.discarding = False
        self.response_id = None
        self.model = None
        self.service_tier = None
        self.terminal_status = None

    def observe_response(self, payload):
        if not isinstance(payload, dict):
            return
        self.usage = token_counts(payload.get('usage')) or self.usage
        for field, pattern in (('response_id', r'resp_[A-Za-z0-9_-]{1,200}'),
                               ('model', r'[A-Za-z0-9_.:-]{1,128}'),
                               ('service_tier', r'(?:priority|fast|default|auto|flex|scale)')):
            value = payload.get('id' if field == 'response_id' else field)
            if isinstance(value, str) and re.fullmatch(pattern, value):
                setattr(self, field, value)
        if payload.get('status') in ('completed', 'failed', 'incomplete', 'cancelled'):
            self.terminal_status = payload['status']

    def feed(self, block):
        for part in block.splitlines(keepends=True):
            if not self.discarding:
                self.pending.extend(part)
                if len(self.pending) > 256 * 1024:
                    self.pending.clear()
                    self.discarding = True
            if part.endswith(b'\n'):
                if not self.discarding:
                    line = bytes(self.pending).strip()
                    if line.startswith(b'data:'):
                        try:
                            payload = json.loads(line[5:])
                            usage = token_counts(payload.get('usage')) if isinstance(payload, dict) else None
                            if usage is not None:
                                self.usage = usage
                            if isinstance(payload, dict):
                                self.observe_response(payload.get('response'))
                                if payload.get('type') == 'error':
                                    self.terminal_status = 'failed'
                        except (ValueError, UnicodeError, RecursionError):
                            pass
                self.pending.clear()
                self.discarding = False


class UsageJournal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ValueError('usage_directory_not_private')

    def record(self, *, installation, provider, model, status, elapsed_ms, usage=None, channel='text',
               request_id=None, response_id=None, service_tier=None):
        # Only these explicit fields can enter the journal.
        now = datetime.now(timezone.utc)
        event = dict(timestamp=now.isoformat(), installation=installation, provider=provider,
                     model=model, channel=channel, status=status, elapsed_ms=elapsed_ms,
                     usage=token_counts(usage))
        for name, value, pattern in (
                ('request_id', request_id, r'[A-Za-z0-9_-]{1,200}'),
                ('response_id', response_id, r'resp_[A-Za-z0-9_-]{1,200}'),
                ('service_tier', service_tier, r'(?:priority|fast|default|auto|flex|scale)')):
            if isinstance(value, str) and re.fullmatch(pattern, value):
                event[name] = value
        data = (json.dumps(event, separators=(',', ':')) + '\n').encode()
        path = self.directory / (now.strftime('%Y-%m-%d') + '.jsonl')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise ValueError('usage_file_not_private')
            os.write(fd, data)
        finally:
            os.close(fd)

    def summary(self):
        groups = {}
        for path in sorted(self.directory.glob('????-??-??.jsonl')):
            with path.open() as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    key = tuple(event.get(k) for k in ('installation', 'channel', 'provider', 'model'))
                    row = groups.setdefault(key, dict(installation=key[0], channel=key[1], provider=key[2], model=key[3],
                        requests=0, errors=0, missing_usage=0, prompt_tokens=0, completion_tokens=0, total_tokens=0))
                    row['requests'] += 1
                    row['errors'] += event.get('status') != 'ok'
                    usage = event.get('usage')
                    row['missing_usage'] += not isinstance(usage, dict)
                    for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                        if isinstance(usage, dict):
                            row[field] += usage.get(field, 0)
        return list(groups.values())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    args = parser.parse_args()
    print(json.dumps(UsageJournal(args.directory).summary(), ensure_ascii=False, indent=2))
