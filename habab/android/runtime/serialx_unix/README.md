# `serialx` Unix socket overlay

This is a narrow patch payload for the Android runtime's pinned `serialx 1.11.0`.
It adds `unix://` support for filesystem AF_UNIX stream sockets. The runtime
builder copies `serial_unix.py` to the installed
`serialx/platforms/serial_unix.py` and adds its import beside `serial_socket`
in `serialx/platforms/__init__.py`, guarded by exact version/context checks.
`platforms-init.patch` records the reviewed import diff. Do not patch the shared
build cache in place; apply the overlay only to the release rootfs, then attest
its files and target package version in release metadata.

The URL form is `unix:///absolute/path/to/socket`. Paths must be absolute and
shorter than Linux `sun_path`'s 108-byte bound. The sync adapter implements the
`BaseSerial` byte-stream API; the async adapter implements
`BaseSerialTransport` through asyncio's Unix stream transport. Baud rate and
line settings are accepted to match serial callers but have no meaning for a
byte stream. DTR/RTS are no-ops, like `socket://`.

This patch does not change ZHA's serial parser or ZNP protocol. The inspected
Home Assistant Core 2026.10.2 manual ZHA flow uses a free-string serial-path
selector and passes the submitted path to the radio manager, so no ZHA config
flow patch is needed for the URI. A host-side echo round-trip through both
public sync and async serialx APIs passed against cached serialx 1.11.0; see
`habab/android/qa/issue15-serialx-unix-20261026/host-roundtrip.json`. Runtime
construction and release attestation, Android PRoot AF_UNIX path translation,
USB, firmware and physical-device checks remain unverified.

To repeat the host-side check after building the runtime rootfs, run:

```sh
python3 habab/android/runtime/serialx_unix/smoke_test.py \
  --site-packages /path/to/rootfs/usr/local/lib/python3.14/site-packages
```

The expected output has `result`, `sync`, and `async` set to `PASS`. This only
checks host AF_UNIX transport behavior against serialx 1.11.0.

The runtime overlay builder's version guard and idempotence are covered by:

```sh
python3 -m unittest discover -s habab/android/tools \
  -p 'test_package_runtime_serialx.py' -v
```

Expected result: 2 tests pass. This fixture check does not build or verify a
complete release archive.

The release verifier's signed serialx attestation checks are covered by:

```sh
python3 -m unittest discover -s habab/android/tools \
  -p 'test_verify_runtime_serialx.py' -v
```

Expected result: 3 tests pass, including rejection of a signed archive whose
attested transport hash does not match its payload. The test uses a small
temporary archive and does not build the Android runtime.
