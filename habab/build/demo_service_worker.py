"""Publish the identical root-scope service-worker retirement endpoints."""

import os
from pathlib import Path

RETIREMENT_FILENAMES = ('sw-modern.js', 'sw-legacy.js', 'service_worker.js')
HEADERS_START = '# HABAB demo service-worker retirement headers:begin'
HEADERS_END = '# HABAB demo service-worker retirement headers:end'
HEADERS_BLOCK = '''/sw-modern.js
  Cache-Control: no-cache, no-store, must-revalidate
  Content-Type: text/javascript; charset=utf-8
  X-Content-Type-Options: nosniff

/sw-legacy.js
  Cache-Control: no-cache, no-store, must-revalidate
  Content-Type: text/javascript; charset=utf-8
  X-Content-Type-Options: nosniff

/service_worker.js
  Cache-Control: no-cache, no-store, must-revalidate
  Content-Type: text/javascript; charset=utf-8
  X-Content-Type-Options: nosniff'''


def _headers_with_worker_rules(headers):
    if HEADERS_START in headers or HEADERS_END in headers:
        if headers.count(HEADERS_START) != 1 or headers.count(HEADERS_END) != 1:
            raise ValueError('ambiguous service-worker retirement headers')
        before, rest = headers.split(HEADERS_START)
        old_block, after = rest.split(HEADERS_END)
        if old_block.strip('\n') == HEADERS_BLOCK:
            return headers
        return before + HEADERS_START + '\n' + HEADERS_BLOCK + '\n' + HEADERS_END + after
    return headers.rstrip() + '\n\n' + HEADERS_START + '\n' + HEADERS_BLOCK + '\n' + HEADERS_END + '\n'


def publish_retirement_worker(public_dir, source_file):
    public_dir = Path(public_dir)
    worker = Path(source_file).read_bytes()
    for filename in RETIREMENT_FILENAMES:
        target = public_dir / filename
        temporary = public_dir / f'.{filename}.next'
        temporary.write_bytes(worker)
        os.replace(temporary, target)

    headers_path = public_dir / '_headers'
    headers = headers_path.read_text(encoding='utf-8')
    rendered = _headers_with_worker_rules(headers)
    if rendered != headers:
        temporary = public_dir / '._headers.next'
        temporary.write_text(rendered, encoding='utf-8')
        os.replace(temporary, headers_path)
