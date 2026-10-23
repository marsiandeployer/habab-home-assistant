#!/usr/bin/env python3
"""Bounded USB/IP + ADB provisioning for exactly one Huawei P30 Pro."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=kwargs.pop('timeout', 30), **kwargs).strip()


def select_device(serial=None, adb=None):
    adb = adb or ['adb']
    rows = [line.split() for line in run(adb + ['devices']).splitlines()[1:] if line.strip()]
    if serial:
        rows = [row for row in rows if row[0] == serial]
    if len(rows) != 1:
        raise RuntimeError('Expected exactly one selected ADB device; no device was changed')
    serial, state = rows[0][:2]
    if state != 'device':
        raise RuntimeError('Huawei is ' + state + '; approve USB debugging on its screen')
    model = run(adb + ['-s', serial, 'shell', 'getprop', 'ro.product.model'])
    if model != 'VOG-L29':
        raise RuntimeError('Wrong device model: ' + model + '; expected VOG-L29')
    return serial


def verify_runtime(prefix):
    """Read only our live Core's runtime flag; never print its other environment."""
    rows = run(prefix + ['shell', 'ps', '-A', '-o', 'PID,ARGS']).splitlines()
    command = 'python3 -P /usr/local/share/habab-android/launch.py --config /config'
    pids = [parts[0] for row in rows if len(parts := row.strip().split(None, 1)) == 2
            and parts[0].isdigit() and parts[1] == command]
    if len(pids) != 1:
        raise RuntimeError('Expected exactly one running Habab Core process')
    environment = subprocess.check_output(prefix + [
        'shell', 'run-as', 'ru.habab.home', 'cat', '/proc/' + pids[0] + '/environ'], timeout=30)
    values = [item for item in environment.split(b'\0') if item.startswith(b'HABAB_RUNTIME_KIND=')]
    if values != [b'HABAB_RUNTIME_KIND=android_proot']:
        raise RuntimeError('Live Core does not have the required Android runtime context')
    return {'result': 'PASS', 'pid': int(pids[0]), 'runtimeKind': 'android_proot'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'attach', 'install', 'start', 'verify-runtime'])
    parser.add_argument('--serial')
    parser.add_argument('--apk', type=Path)
    parser.add_argument('--manifest-url')
    parser.add_argument('--adb', default=os.environ.get('HABAB_ADB', 'adb'))
    parser.add_argument('--host', choices=['127.0.0.1', 'localhost'])
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    if args.action == 'attach':
        exported = run(['usbip', '--tcp-port', '17575', 'list', '-r', '127.0.0.1'])
        rows = re.findall(r'^\s+(\d+-\d+(?:\.\d+)*):.*\(12d1:107e\)', exported, re.MULTILINE)
        if len(rows) != 1:
            raise RuntimeError('Expected exactly one exported Huawei in normal Android mode')
        imported = run(['usbip', 'port'])
        if f'/{rows[0]}' not in imported:
            subprocess.run(['usbip', '--tcp-port', '17575', 'attach', '-r', '127.0.0.1', '-b', rows[0]], check=True, timeout=15)
        print('Huawei USB/IP attached; approve USB debugging if prompted')
        return
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('Invalid ADB server port')
    adb = [args.adb]
    if args.host: adb += ['-H', args.host]
    if args.port: adb += ['-P', str(args.port)]
    serial = select_device(args.serial, adb)
    prefix = adb + ['-s', serial]
    if args.action == 'verify-runtime':
        print(json.dumps(verify_runtime(prefix)))
        return
    if args.action == 'inspect':
        properties = ['ro.product.model', 'ro.build.version.sdk', 'ro.build.version.release', 'ro.product.cpu.abi', 'ro.build.version.emui']
        print(json.dumps({name: run(prefix + ['shell', 'getprop', name]) for name in properties}, indent=2))
        print(run(prefix + ['shell', 'df', '-h', '/data']))
    elif args.action == 'install':
        if not args.apk or not args.apk.is_file():
            parser.error('--apk must name an existing APK')
        subprocess.run(prefix + ['install', '--no-streaming', '-r', str(args.apk.resolve())], check=True, timeout=180)
        command = ['am', 'start', '-n', 'ru.habab.home/.MainActivity']
        if args.manifest_url:
            if not re.fullmatch(r'https://[a-zA-Z0-9./_-]+\.json', args.manifest_url):
                parser.error('Invalid public manifest URL')
            command += ['--es', 'install_url', args.manifest_url]
        print(run(prefix + ['shell', shlex.join(command)]))
    else:
        print(run(prefix + ['shell', 'am start -f 0x10008000 -n ru.habab.home/.MainActivity --ez start_server true']))


if __name__ == '__main__':
    main()
