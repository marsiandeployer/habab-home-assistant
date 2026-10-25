"""Enroll an upstream credential from bounded private stdin; never save or echo it."""
import argparse
import json
import socket
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('--purpose', choices=('voice', 'text'))
    args = parser.parse_args()
    raw = sys.stdin.buffer.readline(4097)
    if len(raw) > 4096:
        raise ValueError('Enrollment input too large')
    payload = json.loads(raw)
    key = payload.get('upstream_key')
    if not isinstance(key, str) or not 8 <= len(key) <= 2048:
        raise ValueError('Invalid enrollment input')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(5)
        control.connect(args.socket)
        payload = {'upstream_key': key}
        if args.purpose is not None:
            payload['purpose'] = args.purpose
        control.sendall(json.dumps(payload).encode() + b'\n')
        with control.makefile('rb') as response:
            result = json.loads(response.readline(513))
    if result != {'enrolled': True}:
        raise ValueError('Upstream enrollment rejected')
    print(json.dumps({'enrolled': True, 'storage': 'process_memory'}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'enrolled': False}), file=sys.stderr)
        raise SystemExit(1)
