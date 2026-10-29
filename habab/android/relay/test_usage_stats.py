import json
from pathlib import Path
import stat
import tempfile
import unittest
from usage_stats import UsageJournal, StreamUsage


class UsageTests(unittest.TestCase):
    def test_fragmented_stream_extracts_usage_without_persisting_content(self):
        observer = StreamUsage()
        data = b'data: {"choices":[{"delta":{"content":"PRIVATE"}}]}\n\ndata: {"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15,"secret":"PRIVATE"}}\n\ndata: [DONE]\n\n'
        for i in range(0, len(data), 3):
            observer.feed(data[i:i + 3])
        self.assertEqual(observer.usage, dict(prompt_tokens=12, completion_tokens=3, total_tokens=15))
        with tempfile.TemporaryDirectory() as directory:
            journal = UsageJournal(directory)
            journal.record(installation='synthetic-id', provider='deepseek', model='deepseek-flash', status='ok', elapsed_ms=12, usage=observer.usage)
            journal.record(installation='synthetic-id', provider='deepseek', model='deepseek-flash', status='failed', elapsed_ms=20)
            summary = journal.summary()[0]
            self.assertEqual(summary['requests'], 2)
            self.assertEqual(summary['missing_usage'], 1)
            self.assertEqual(summary['errors'], 1)
            self.assertEqual(summary['total_tokens'], 15)
            file = next(Path(directory).glob('*.jsonl'))
            self.assertNotIn('PRIVATE', file.read_text())
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_oversized_line_is_discarded_and_next_usage_still_read(self):
        observer = StreamUsage()
        observer.feed(b'x' * 300000 + b'\n')
        observer.feed(b'data: {"usage":{"total_tokens":9,"prompt_tokens":true,"completion_tokens":-1}}\n')
        self.assertEqual(observer.usage, {'total_tokens': 9})
        self.assertEqual(observer.pending, b'')
