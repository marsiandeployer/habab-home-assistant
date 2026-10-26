# Product tunnel: live prerequisites

Read-only snapshot: **2026-10-25 18:15 UTC**. No live DNS, Nginx, SSH, account,
certificate, or listener changes were made. Remote edge configuration was not
read; only public DNS/TLS metadata was queried. No HA private configuration was
read.

## What exists

- **Support relay is not an isolated product SSH endpoint.**
  [`habab/support/README.md`](../support/README.md) describes an operator-approved
  support session with a 60-minute default and a 120-minute maximum. Its installer
  creates temporary support accounts, writes
  `/etc/ssh/sshd_config.d/90-habab-support.conf`, and reloads the existing system
  `ssh` service. That file exists on this VM and is included through the system
  SSH config. The relay client metadata names `95.217.227.164:2222`; the server
  map documents that as NAT to this VM's general SSH daemon on port 22. The
  current daemon listens on wildcard IPv4 and IPv6 port 22. This is not a
  separate host/daemon/listener for product homes. **No dedicated public product
  SSH host/port is ready.** Treat `95.217.227.164:2222` only as the current
  support/admin SSH ingress until an isolated product ingress is approved.
- **Current SSH policy is not the product policy.** Effective `sshd -T -C` for
  a support-pattern user reports public-key-only auth, remote forwarding, and
  `PermitListen 127.0.0.1:*`, with `MaxSessions 1`. A not-yet-provisioned
  `habab-home-*` user inherits general SSH defaults instead: password auth and
  forwarding enabled, `PermitListen any`, and `MaxSessions 10`. Product accounts
  need an explicit per-home key policy and a documented operator provision/revoke
  path. A small manual pilot does not need automatic billing or lease checks.
- **The existing public name behavior reaches the edge.** Two different names,
  `home-example.remote.habab.ru` and `home-preflight-check.remote.habab.ru`, both
  resolved to `62.109.14.209`; no IPv6 result was returned. Recursive DNS proves
  those names currently reach that edge, though it does not reveal whether this
  is a wildcard record or two explicit records. The edge is not this VM's
  private interface (`10.10.10.10`).
- **Current edge TLS does not cover product names.** A public TLS handshake to
  `62.109.14.209:443` with SNI `home-example.remote.habab.ru` returned a
  certificate whose SANs are `*.wpmix.net` and `wpmix.net` (valid until
  2026-12-20). Neither SAN covers `home-example.remote.habab.ru`.
- **Nginx has general includes and a shared WebSocket map, but no product route.**
  VM104's main config includes `/etc/nginx/conf.d/*.conf` and
  `/etc/nginx/sites-enabled/*`. There is an active
  `map $http_upgrade $connection_upgrade` in `sites-enabled/star-websocket-proxy`,
  but no `home-*.remote.habab.ru` vhost. Public TLS terminates at the edge; the
  existing edge-to-VM path is HTTP on port 80. The operator generator now
  defaults to `--tls-termination edge` and emits a VM backend `listen 80` block
  without cert paths; `--tls-termination local` retains the standalone TLS
  vhost. The edge-side certificate and exact-host TLS vhost remain separate
  deployment artifacts and are not provisioned by the generator.
- **No product operator roots are installed.** `/etc/habab-product-tunnel`,
  `/var/lib/habab-product-tunnel`, and their key/state subdirectories are absent.
  Generic SSH/Nginx include roots exist, but there is no product-specific root or
  live manifest. The current `manage_home.py` deliberately refuses `/` as its
  sandbox root; it is a source generator, not a live installer.
- **Loopback port snapshot:** the generator's 32000–39999 range has existing
  listeners. Port 32000 was unused in this snapshot, but any chosen port must be
  checked again immediately before apply and bound only to `127.0.0.1`.

## Shared live assets to preserve

- Do not reuse or change the current system SSH daemon/port 22, the public
  support/admin NAT endpoint `95.217.227.164:2222`,
  `/etc/ssh/sshd_config.d/90-habab-support.conf`, or `/etc/habab-support` keys
  and expiring-session state for product homes. The support client and product
  client must keep separate accounts and public keys.
- Do not replace or edit the edge certificate currently presented for
  `*.wpmix.net`, or its existing exact-host routes. Product hostnames under
  `*.remote.habab.ru` need separate certificate coverage and exact-host routing.
- Do not modify the shared VM104 Nginx configuration or the active
  `star-websocket-proxy` vhost/map to add one home. Use a separate product vhost
  and reuse the existing WebSocket map after staging and validating the new
  route. Reload only the reviewed config owner after its own syntax test.

## Apply gates before a manual beta

1. Choose and document a **dedicated product SSH ingress**. Do not enroll product
   homes on the support/admin endpoint `95.217.227.164:2222` without a separate
   reviewed design. If this VM is selected, decide on an isolated SSH listener
   and ingress port, distinct from its general daemon, and verify its NAT/firewall
   path before creating users. Pick a free loopback forward port only after a
   fresh listener check.
2. Provision one per-home key, account, and restricted SSH policy from reviewed
   output. An operator may install and revoke these manually for a small beta;
   automated enrollment, entitlement, and lease services are not prerequisites.
   Do not run the sandbox generator against `/`.
3. Provision a TLS certificate that covers `home-<id>.remote.habab.ru`, then add
   the exact-host route at the TLS edge. Add the generated edge-mode HTTP/WebSocket
   vhost on VM104 for the loopback port, reusing the existing shared upgrade
   map. Restrict the VM backend to the immediate edge peer after observing it;
   ensure the edge normalizes XFF and sends `X-Forwarded-Proto: https`, and
   verify the scheme/client chain that reaches HA. The generated backend asserts
   HTTPS itself, so it must not be reachable directly by HTTP clients. Do not
   enable HA proxy trust until its actual immediate peer is confirmed.
4. In a staged copy, check the SSH policy with `sshd -t` and check the edge and
   VM Nginx route with `nginx -t`. Apply/reload only those reviewed config
   owners. Confirm the home-specific key and route can be manually revoked and
   the active SSH session stopped. Do not reuse support-session keys/accounts.
5. Before beta access, verify from an external network that the exact host reaches
   the HA sign-in page, native HA authentication remains required, the tunnel
   listener is loopback-only, and local HA/automation still works with the tunnel
   disconnected.

## Prototype evidence versus live status

`manage_home.py` renders per-home SSH/key/Nginx fragments and sandbox state. The
updated default edge HTTP template passed isolated `nginx -t`; the receipt
[`syntax-acceptance-edge.json`](syntax-acceptance-edge.json) records source SHA-256
`b6bc821fff21e0904c9e541d621b4097d6ae68cd49df6189c88ab479f38905b5`. The prior
[`syntax-acceptance.json`](syntax-acceptance.json) records an earlier source's
`sshd -t` and local-TLS Nginx syntax checks; the SSH fragment has not been
rechecked against the updated source. All fixtures used synthetic key material
and temporary config/cert files. They prove parser acceptance only, not a live
account, product listener, deployed route, certificate coverage, external HA
login, or server revoke. **External product access: NOT RUN.**
