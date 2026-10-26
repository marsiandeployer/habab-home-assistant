# Issue #8 exact-host edge pilot staging

These are generated review artifacts for the synthetic home ID `issue8pilot`
with relay loopback port `32808` and hostname
`home-issue8pilot.remote.habab.ru`:

- `edge-vhost.conf`: port 80 HTTP-01 challenge under `/var/www/habab.ru`,
  redirect for other HTTP paths, and port 443 TLS proxy to the existing
  `95.217.227.164:80` ingress.
- `vm104-backend-vhost.conf`: output of the existing `manage_home.py` edge
  mode; exact hostname routes to `127.0.0.1:32808` and preserves WebSocket
  upgrades.

Focused Python tests passed for host/port pairing, challenge and TLS paths,
WebSocket headers, invalid inputs, and rejection of a duplicate relay port.
An isolated local `nginx -t` also passed using a temporary self-signed test
certificate substituted only inside the temporary config; the staged files
still reference the future issued certificate paths.

The generated pair agrees on exact host and port assignment. The public edge
already has the shared `$connection_upgrade` map. A read-only `nginx -T`
metadata check on 2026-10-26 found no matching hostname on the edge or VM104,
no VM104 listener on `32808`, and absent VM104 product-tunnel state directories.
This only checks current route-name/port conflicts; no DNS record or ACME
challenge was tested.

No certificate was requested, no Nginx config was installed or reloaded, and
no SSH account/key, reverse tunnel, home device, or HA endpoint was created.
The certificate paths are future paths, so these snippets are not live-ready.
The dedicated product SSH ingress is still not provisioned; the sample home
cannot connect until an operator separately reviews and provisions that path.
After review and prerequisites, validate the exact hostname's DNS and HTTP-01
file reachability, obtain its certificate, run `nginx -t` on both hosts, then
perform a separately reviewed reload and external HA sign-in/WebSocket test.
