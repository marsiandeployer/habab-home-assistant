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

Run `connect.ps1` after local Home Assistant is healthy. It checks the local
HTTP endpoint, opens a remote forward bound to `127.0.0.1` on the relay, and
reconnects with a 5–60 second backoff. Keep the process open; Ctrl+C ends the
connection. It does not open a listener or firewall port on the home router.
This starter is manually started/stopped and does not provide the product UI's
enable/disable control or a confirmed connected-status indicator.

## Required cloud-side contract

For each subscribed home, the cloud side still needs a stable identity and
unique loopback port, an exact-host HTTPS route to that port, and an SSH key
policy restricted to remote forwarding on that exact `127.0.0.1:<port>` only.
The public hostname needs DNS and a valid certificate. The route must preserve
Home Assistant WebSocket upgrades and forward the original HTTPS scheme.
Configure Home Assistant's trusted proxy only for the immediate proxy peer
actually observed by HA; retain native login and do not enable anonymous or
trusted-network authentication. Disconnecting this client stops the tunnel;
subscription cancellation must also revoke the relay key and hostname route.

The relay-side source examples are [sshd-home.example.conf](sshd-home.example.conf)
and [nginx-home.example.conf](nginx-home.example.conf). They show the sample
home `habab-home-example`, hostname `home-example.remote.habab.ru`, and
loopback port `32001`; replace these with values provisioned for one home.
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

Review the unified diff. To write the snippets and local ownership state into
that sandbox, repeat the command with `--apply`. Repeating the same provision
is idempotent. A different home cannot claim a hostname or port already
present in the managed state. Existing files without a matching ownership
manifest are left untouched. If a managed file has changed since generation,
the tool refuses to overwrite or remove it.

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
from [nginx-home.example.conf](nginx-home.example.conf); install it in the
existing TLS relay's `http` configuration, then run `nginx -t` before reload.
The tool does not reload either daemon. Confirm DNS and certificate readiness
separately. Revocation removes snippets and the key file, but the operator
must reload sshd/nginx to apply the revocation; terminate an already connected
SSH session separately if immediate disconnection is required.

This is not a subscription service: there is no billing check, account or
device enrollment API, hostname/DNS/certificate provisioning, relay account
creation, or cloud Home Assistant deployment. The existing
`support_tunnel.ps1` / `support/relay.py` is intentionally temporary,
operator-approved support access and is not the per-home product relay.

Local automation and local voice remain on the home HA instance. If Internet
or relay connectivity is lost, remote access is unavailable; this tunnel does
not change local HA operation. The separate cloud-HA option needs a distinct
design for its local Zigbee gateway; USB is not forwarded by this feature.
