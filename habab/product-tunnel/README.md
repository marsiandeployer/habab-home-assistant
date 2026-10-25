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

## What this does not provision

This is not a subscription service. There is no billing check, account or
device enrollment API, hostname/DNS/certificate provisioning, relay account
creation, key revocation workflow, or cloud Home Assistant deployment. The
existing `support_tunnel.ps1` / `support/relay.py` is intentionally temporary,
operator-approved support access and is not the per-home product relay. The
public HTTPS route and the relay-side exact-host/port configuration must be
implemented and accepted separately before this client can connect.

Local automation and local voice remain on the home HA instance. If Internet
or relay connectivity is lost, remote access is unavailable; this tunnel does
not change local HA operation. The separate cloud-HA option needs a distinct
design for its local Zigbee gateway; USB is not forwarded by this feature.
