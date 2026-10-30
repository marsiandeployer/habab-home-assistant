# Удалённый веб-доступ Habab Home

Новый веб-туннель через исходящий HTTPS/WebSocket описан в [README-web.md](README-web.md).
Он передаёт веб-интерфейс и API Home Assistant, сохраняет штатную авторизацию и не использует SSH.
На Android клиент включается в настройках приложения; в Windows-контейнере его
настраивают через интеграцию **Habab Home Remote Access** в Home Assistant.
Поддомен и отдельный token дома пока выдаёт оператор; автоматическая регистрация
не реализована.

Ниже оставлены заметки по прежнему SSH-пилоту для операторов. Этот Windows-клиент
больше не входит в продуктовую сборку; используйте исходящий WSS-клиент из
`README-web.md` и [инструкцию Windows](../distribution/README-WINDOWS.md).

# Optional per-home remote access: client-side slice

This folder is a reviewable client-side starter for the paid remote-access
option: one stable HTTPS hostname per home, with the home initiating an
outbound SSH reverse tunnel to the cloud relay. Home Assistant stays on the
home computer; the tunnel forwards only its local web port. Home Assistant's
own sign-in remains the authorization boundary. No Home Assistant token or
password is stored or sent by this client.

## Local configuration

Copy `home.example.json` to `home.json`, then fill in the relay endpoint,
account, stable loopback port, and pinned relay host key that the cloud side
provisions for this home. Generate a separate SSH key pair per home and put
the private key at `identity_file`; enroll only its public key on the relay.
Verify the relay host-key fingerprint out of band before pinning it. Do not
reuse the support-session key or support relay account.

Run `control.ps1` on the Windows host after local Home Assistant is healthy:

```powershell
.\control.ps1 -Action Enable
.\control.ps1 -Action Status
.\control.ps1 -Action Disable
```

`Enable` starts the existing SSH client in the background. It checks the local
HTTP endpoint, opens a remote forward bound to `127.0.0.1` on the relay, and
reconnects with a 5–60 second backoff. `Status` prints JSON with `connecting`,
`running`, `retrying`, `disconnected`, or `error`. `running` means the SSH
process is active; it does not confirm that the relay accepted the reverse
forward. `Disable` stops the client and its SSH connection. The client does not
open a listener or firewall port on the home router. This is a local PowerShell
control path; it is not yet wired to a Habab Home UI.
There is no product-session TTL or server-side lease expiry: the client retries
indefinitely until stopped or its relay key is revoked.

## Relay setup for a manually provisioned home

For each manually provisioned home, the cloud side needs a stable identity and
unique loopback port, an exact-host HTTPS route to that port, and an SSH key
policy restricted to remote forwarding on that exact `127.0.0.1:<port>` only.
The public hostname needs DNS and a valid certificate. The route must preserve
Home Assistant WebSocket upgrades and forward the original HTTPS scheme.
Configure Home Assistant's trusted proxy only for the immediate proxy peer
actually observed by HA; retain native login and do not enable anonymous or
trusted-network authentication. Disconnecting this client stops the tunnel.
For a manual pilot, an operator can revoke the per-home key and route and stop
the active SSH session; billing and automated entitlement checks can follow
later.

The generated SSH policy constrains where the relay listens for a remote
forward. It does not constrain the destination chosen by the home-side SSH
client: the supplied `connect.ps1` targets the local Home Assistant port, but
the relay cannot enforce that target if a client using the enrolled key is
modified. Treat “only the local HA port” as a property of the supplied client,
not a server-enforced boundary. If the relay must enforce an HA-only destination
against a modified client, this SSH design needs a destination-aware relay
instead of relying on `PermitListen`.

The relay-side source examples are [sshd-home.example.conf](sshd-home.example.conf)
and [nginx-home.example.conf](nginx-home.example.conf). They show the sample
home `habab-home-example`, hostname `home-example.remote.habab.ru`, and
loopback port `32001`; replace these with values provisioned for one home.
The Nginx example is for a relay where TLS terminates locally. The operator
tool defaults to `--tls-termination edge`, matching this deployment's TLS edge
and HTTP backend; it does not generate or install the edge's own TLS vhost.
With this reverse tunnel, the SSH client on the home computer connects to
Home Assistant at `127.0.0.1`, so HA's immediate TCP peer is expected to be
`127.0.0.1`. Confirm that peer on the target installation before trusting only
that proxy address for `X-Forwarded-For`; ordinary HA login remains required.

## Operator provision and revoke

`manage_home.py` renders one isolated set of relay snippets per home: an
`sshd_config` Match block, a restricted `authorized_keys` entry, an exact-host
nginx server block, and an ownership manifest. It does not create a Unix
account, DNS record, certificate, or subscription. The relay user is
`habab-home-<id>` and the hostname is fixed to
`home-<id>.remote.habab.ru`; IDs are lowercase ASCII letters, digits, and
internal hyphens. The loopback port must be in `32000-39999`. It accepts only
an `ssh-ed25519` public key. Each home's files have fixed paths derived from
its ID; arbitrary output paths are not accepted.

Preview first. The sandbox root must be an absolute path, and the tool will
only create these fixed relative paths inside it:

```bash
python3 habab/product-tunnel/manage_home.py provision \
  --home-id example --host home-example.remote.habab.ru --port 32001 \
  --public-key 'ssh-ed25519 AAAA... operator@example' \
  --sandbox-root /tmp/product-relay-root
```

`edge` is the default: the generated VM backend vhost listens on HTTP port 80
and contains no certificate paths. Public TLS must already terminate at the
edge for this exact hostname. `--tls-termination local` instead generates the
standalone relay's previous `listen 443 ssl` vhost with local certificate
paths. The edge's own exact-host TLS vhost is a separate deployment artifact
and is not provisioned by this tool.

For `edge` mode, configure the edge to forward only its HTTPS route and
overwrite or sanitize incoming `X-Forwarded-For`. The generated backend sets
`X-Forwarded-Proto: https`; make sure only the observed edge peer can reach this
HTTP vhost so direct HTTP clients cannot use that assertion. Before configuring
Nginx RealIP, verify the immediate peer and forwarded-header chain; trust only
that observed peer, not a broad network. Verify the resulting scheme and client
address through to HA. In the reverse SSH path HA is expected to see its
immediate peer as `127.0.0.1`, but confirm that on the target installation before
setting `trusted_proxies`. Keep normal HA login enabled and do not enable
anonymous or trusted-network authentication.

Review the unified diff. To write the snippets and local ownership state into
that sandbox, repeat the command with `--apply`. Repeating the same provision
is idempotent. A different home cannot claim a hostname or port already
present in the managed state. Existing files without a matching ownership
manifest are left untouched. If a managed file has changed since generation,
the tool refuses to overwrite or remove it. The manifest records
`tls_termination`; changing modes changes that home's nginx fragment, so review
the preview before applying a mode change.

Revoke also previews by default and removes only the files for the named home
when explicitly applied:

```bash
python3 habab/product-tunnel/manage_home.py revoke --home-id example \
  --sandbox-root /tmp/product-relay-root
python3 habab/product-tunnel/manage_home.py revoke --home-id example \
  --sandbox-root /tmp/product-relay-root --apply
```

The generated snippets are source material for an operator to install after
creating the dedicated no-login relay account and preparing its directories.
The key file is written mode `0600` in the sandbox; production installation
must preserve root ownership and restrictive directory permissions. Include
the sshd fragment from the relay's `sshd_config` and run `sshd -t` before
reload. The nginx fragment relies on the one shared `$connection_upgrade` map
from [nginx-home.example.conf](nginx-home.example.conf); install edge-mode
output in the VM backend's `http` configuration and run `nginx -t` before
reload. For local mode, install the generated TLS vhost where that relay
terminates HTTPS and has the referenced certificate files.
The tool does not reload either daemon. Confirm DNS and certificate readiness
separately. Revocation removes snippets and the key file, but the operator
must reload sshd/nginx to apply the revocation; terminate an already connected
SSH session separately if immediate disconnection is required. The current
generator has no expiry timer or live revocation service, so these steps are
operator work rather than an automatic subscription or TTL lifecycle.

This is not a subscription service: there is no billing check, account or
device enrollment API, hostname/DNS/certificate provisioning, relay account
creation, or cloud Home Assistant deployment. A small manual pilot can use
operator-provisioned per-home keys, accounts, and routes. The existing
`support_tunnel.ps1` / `support/relay.py` is intentionally temporary,
operator-approved support access and is not the per-home product relay.

Local automation and local voice remain on the home HA instance. If Internet
or relay connectivity is lost, remote access is unavailable; this tunnel does
not change local HA operation. The separate cloud-HA option needs a distinct
design for its local Zigbee gateway; USB is not forwarded by this feature.
