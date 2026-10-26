# Linux coldboot discriminator

One disposable cold start of `habab-home:linux-smoke-3bdc03-20261026`, with the image's default ENTRYPOINT/CMD, an empty private `/config`, 1 CPU, 1 GiB RAM, and a Docker `--internal` network. No published ports, devices, external credentials, or host runtime were used. See `receipt.json` for observed evidence and limits.

`run.py` creates and removes only its uniquely named container, internal network, and temporary config directory. `container_probe.py` performs HTTP and WebSocket requests through container loopback; the synthetic onboarding password and access token stay in process memory. To repeat after reviewing this partial receipt:

```sh
python3 habab/qa/linux-coldboot-final-20261026/run.py
```

The recorded run reached `RUNNING`, loaded `habab_home`, and returned the expected feedback status (`enabled=false`, `credential_configured=false`, `ready=false`). Seed SHA-256 and top-level keys matched before onboarding and after readiness. The run remains **FAIL_OR_UNVERIFIED** because its first version queried `auth/current_user` at invalid REST paths and got HTTP 404. The saved helper now uses the native WebSocket `auth/current_user`, but that correction was not rerun. The successful admin-only feedback command is evidence of admin access, not a substitute for the missing current-user readback. No startup error summary was required because the integration and command both worked. The earlier missing-component observation was not reproduced; startup timing remains a plausible but unconfirmed explanation.
