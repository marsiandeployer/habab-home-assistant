# Habab Home native Linux ARM64 beta

This installer targets Ubuntu Server 26.04 LTS on ARM64 with Python 3.14.2 or newer. It installs Home Assistant Core and the matching Habab frontend into a versioned Python environment, runs the service under systemd, and does not use Docker. Stock Raspberry Pi OS is outside the beta target.

The package builder takes a frontend wheel candidate and its workflow manifest, validates the candidate against the full source SHA, and creates a deterministic archive. It does not publish a release. Until the CI candidate has passed and been promoted, there is no public Linux download; the site articles describe the intended flow and identify this status.

The installer keeps user configuration in `/var/lib/habab-home/config` and versioned files under `/opt/habab-home/releases`. `current` and `previous` select active and prior versions. The service must answer on `127.0.0.1:8123` before an install or update is reported successful. Runtime pins, source revision, and OpenCode digest live with each release so rollback selects them together.

## Source checks

```sh
bash -n habab/distribution/linux/*.sh
python3 -m unittest discover -s habab/distribution/linux -p 'test_*.py' -v
```

These source tests use synthetic archives and fake command execution. They do not establish that the package workflow completed, or that installation has been tested on a physical Raspberry Pi.
