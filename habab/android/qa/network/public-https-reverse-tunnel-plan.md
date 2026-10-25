# P30 public HTTPS reverse tunnel plan

**Status:** proposal only; no settings, DNS, proxy, SSH, or phone changes made.
**Prepared:** 2026-10-25.
**Scope:** existing Android `Tunnel.java` + existing edge and VM104 Nginx ingress.

## Concrete target

Proposed public name: `habab-p30.wpmix.net` (not created or checked in DNS). The existing edge certificate loaded from `/etc/letsencrypt/live/wpmix.net/fullchain.pem` has SAN `*.wpmix.net` and currently expires 2026-12-20; that wildcard covers this one-label name. The existing edge is `62.109.14.209`, which already terminates HTTPS and proxies to `95.217.227.164:80` (VM104 ingress). No new public port is needed. Do not add an AAAA record unless a working IPv6 route to the edge is separately verified.

Use `18123/tcp` as the VM104-only reverse-forward listener, bound explicitly to `127.0.0.1`. It was free in the read-only listener snapshot on 2026-10-25. Recheck immediately before any future change.

## Request path

```text
Internet client
  -> habab-p30.wpmix.net:443 (edge 62.109.14.209; existing TLS termination)
  -> existing edge-to-VM104 HTTP ingress at 95.217.227.164:80
  -> new VM104 host vhost, HTTP/1.1 + WebSocket, proxy_pass http://127.0.0.1:18123
  -> sshd remote-forward listener 127.0.0.1:18123
  -> JSch connection initiated by Android app
  -> phone 127.0.0.1:8123 (embedded Home Assistant Core)
```

The hostname needs one DNS A record to `62.109.14.209`, after parent review. Add one exact host vhost on the edge that forwards to the current VM104 HTTP ingress, retaining `Host`, `X-Forwarded-For`, `X-Forwarded-Proto: https`, and WebSocket upgrade headers. Add one exact host vhost on VM104 that proxies to `127.0.0.1:18123`, uses HTTP/1.1 and WebSocket upgrade headers, appends XFF, preserves the HTTPS proto received from the edge, and disables response buffering. Do not publish 18123 or 8123 on a wildcard/public bind. Do not edit the existing `habab-voice.wpmix.net` route; it is a separate voice relay.

## SSH restriction and app values

Use a new, dedicated non-login account named `habab-s-p30`; do not reuse existing `habab-s-*` identities. The existing wildcard account match currently permits `PermitListen 127.0.0.1:*`, which is too broad for this use. After review, add a specific `Match User habab-s-p30` block **before** that broad match, with:

```text
AuthenticationMethods publickey
PasswordAuthentication no
AllowTcpForwarding remote
PermitListen 127.0.0.1:18123
GatewayPorts no
PermitOpen none
AllowAgentForwarding no
X11Forwarding no
PermitTTY no
MaxSessions 0
```

Use a non-login shell and a single phone-generated public key. `MaxSessions 0` blocks shell, login, and subsystem sessions while preserving forwarding according to the OpenSSH manual. The authorized key should use OpenSSH's `restrict` option with forwarding re-enabled narrowly and `permitlisten="127.0.0.1:18123"`; sshd's `AllowTcpForwarding remote` and exact `PermitListen` remain the server-side controls. Validate the effective config for this exact account with `sshd -T -C ...` before any reload. The currently read effective config for the proposed username inherits the broad wildcard rule, so this account/config does not yet exist in a restricted state.

Read-only host key pin evidence from this VM:

```text
Host: 95.217.227.164, port 2222 (the known NAT route to this same VM; never SSH from VM104 to itself)
Key type: ssh-ed25519
Fingerprint: SHA256:9aarYaFSU/z4kKUCyKrjzTeUxtNb0vBfbyeJpdvlyjQ
Tunnel.java pin value: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINu3oFc2O4yMoxoWcrjuKJhY7F5p6oQMjqsaAqHmBYtp
```

The fingerprint was computed from the local public host-key file. Confirm its fingerprint out of band before saving it to the phone; do not replace pinning with `ssh-keyscan` trust-on-first-use.

After creating the account, obtain the phone's public key from the app settings screen and install only that public key for `habab-s-p30`. Then the app fields are:

| Field | Value |
|---|---|
| SSH host | `95.217.227.164` |
| SSH port | `2222` |
| SSH user | `habab-s-p30` |
| Remote port | `18123` |
| SSH host-key pin | ED25519 value above, after out-of-band fingerprint verification |
| Public URL | `https://habab-p30.wpmix.net` |
| Tunnel enabled | checked only after DNS, both proxy vhosts, SSH account, and Core proxy settings are ready |

`Tunnel.java` authenticates with the phone key, requires strict host-key checking, and forwards `127.0.0.1:<remote port>` on the SSH server to phone `127.0.0.1:8123`. It retries with 5–60 second backoff and waits for a Home Assistant owner. The HTTPS public URL is app display/config state; DNS and both Nginx vhosts provide the actual browser route.

## Home Assistant HTTP settings and trust boundary

Set Home Assistant's Internet URL to `https://habab-p30.wpmix.net` under **Settings → System → Network** (or `homeassistant.external_url` if managed in YAML). Keep any local URL only if it is a stable address the phone itself can reach; otherwise leave it unset. Home Assistant's URL fields accept scheme/host/port, not URL paths.

The Nginx request reaches HA over the JSch-created socket to phone loopback, so the direct TCP peer HA is expected to see is `127.0.0.1`, not either public proxy IP. Verify this during a later controlled acceptance before changing HTTP trust. For reverse-proxy operation, enable `use_x_forwarded_for` / **Trust X-Forwarded-For** and trust only the immediate peer actually observed by HA (expected `127.0.0.1`); do not add broad phone or server networks. The edge/VM proxy chain must overwrite/append forwarding headers consistently. Since Android loopback is shared among local apps, trusting phone loopback means another local app could forge XFF; retain normal Home Assistant authentication and do not configure trusted-network authentication or bypass login. This arrangement supports proxy operation but does not isolate Android apps from each other.

HA HTTP proxy settings restart Core and, in the current HA UI flow, must be confirmed within five minutes or are rolled back. Set `external_url` separately; it does not require restart per HA docs. A private access URL alone does not grant authorization: test with an existing HA owner login and verify no unauthenticated access.

## Apply sequence, only after parent review

1. Capture current relevant state (DNS record state from the authorized DNS control plane, edge and VM104 Nginx vhost filenames/config test, SSH effective config, listener usage, existing app prefs). Do not inspect or record secrets.
2. Confirm there is no DNS/vhost collision for the proposed exact name. Verify the edge certificate SAN and validity at time of change; the read-only local certificate snapshot above is not a browser TLS test.
3. Create the dedicated SSH account and authorized public key; add the exact account-specific sshd block before the broad wildcard match. Confirm `sshd -t`, `sshd -T -C user=habab-s-p30,...`, and only then reload sshd. Do not change or restart other tunnel services.
4. Add the two exact-host Nginx vhosts and DNS A record. Run `nginx -t` on each host before reload. Verify the VM104 loopback listener is still free before enabling the Android forward.
5. In Home Assistant, configure XFF trust and external URL; confirm the HTTP settings within the five-minute rollback window only after local Core has returned healthy. Keep ordinary user login.
6. Stop **only** Android `phoneServerService` (`ServerService` in the APK) so the app's Tunnel settings dialog is available. Save the values above and enable the tunnel. Start `phoneServerService` once to apply them. Do not stop, restart, or edit Telegram QA, CDP/browser tunnels, owner cloud, voice relay, or other Android services.
7. Verify in order: phone service/Core healthy; app tunnel status `connected`; `ss -ltn` on VM104 shows `127.0.0.1:18123` only; edge TLS certificate matches proposed hostname; external browser reaches HA sign-in; owner login succeeds; authenticated WebSocket/frontend loads; no direct public 8123/18123 listener; normal HA auth remains required. Recheck from an external network. These are future acceptance steps, not completed tests.

## Rollback

If tunnel connection fails, disable tunnel in the app while the phone service is stopped, then start only `phoneServerService` and confirm local Core. For broader rollback, remove only the new exact-name edge and VM104 vhosts, remove only the new DNS A record, remove the dedicated account's authorized key/account and exact `Match User habab-s-p30` block, validate config and reload only the affected Nginx/sshd services. Revert the HA HTTP proxy settings using the UI rollback window or restore the captured prior HTTP settings and confirm local Core; revert `external_url` separately. Preserve all other vhosts, accounts, tunnels, and services.

## Primary references

- Home Assistant HTTP reverse proxy settings, proxy trust, and five-minute settings rollback (official docs, read 2026-10-25): https://www.home-assistant.io/integrations/http/
- Home Assistant `external_url` / `internal_url` (official docs, read 2026-10-25): https://www.home-assistant.io/integrations/homeassistant/
- OpenSSH `AllowTcpForwarding`, `GatewayPorts`, `PermitListen`, and `MaxSessions` (official manual, read 2026-10-25): https://man.openbsd.org/sshd_config
- Existing implementation references: `app/src/main/java/ru/habab/home/Tunnel.java` (SSH pin, retry, loopback forward); `MainActivity.java` (stopped-only settings); `ServerService.java` (starts tunnel after HA health); `habab/android/README.md` (existing TLS tunnel design).
