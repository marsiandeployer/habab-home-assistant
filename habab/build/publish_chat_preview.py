"""Publish the shared demo assistant; retained command name for existing scripts."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from assistant_bundle import assistant_release, publish_assistant, render_entrypoint
from demo_service_worker import publish_retirement_worker

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'distribution/src/habab_home/frontend/setup-chat.js'
PUBLIC = ROOT / 'public'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    release = publish_assistant(PUBLIC, SOURCE) if args.publish else assistant_release(SOURCE)
    index = PUBLIC / 'index.html'
    before = index.read_text(encoding='utf-8')
    after = render_entrypoint(before, release['module_url'])
    if args.publish:
        publish_retirement_worker(PUBLIC, ROOT / 'build/retire-demo-service-worker.js')
        temporary = PUBLIC / '.index.assistant.next'
        temporary.write_text(after, encoding='utf-8')
        os.replace(temporary, index)
    print(json.dumps({**release, 'published': args.publish,
                      'entrypoint_before_sha256': hashlib.sha256(before.encode()).hexdigest(),
                      'entrypoint_after_sha256': hashlib.sha256(after.encode()).hexdigest()}, indent=2))


if __name__ == '__main__':
    main()
