# Synthetic issue #8 edge-hop candidate

This is an alternative staging pair for the same synthetic home ID,
`issue8pilot`, hostname `home-issue8pilot.remote.habab.ru`, and loopback port
`32808`. It uses the existing public edge as both SSH reverse-forward relay
and HTTPS endpoint, avoiding a new VM104-facing SSH NAT route:

- `sshd-home-issue8pilot.conf` is the per-home `Match` fragment from
  `manage_home.py`. The corresponding authorized key must be enrolled only
  after review; no key or account is included here.
- `nginx-home-issue8pilot.conf` is `manage_home.py --tls-termination local`
  output. On the edge it proxies the exact hostname directly to
  `127.0.0.1:32808` and keeps the listener loopback-only.

The edge already has public SSH listener port 22, public HTTPS on 443, and an
active `$connection_upgrade` map. The HTTP-01 catch-all uses `/var/www/html`;
the staged exact-host Nginx block references the future certificate path. No
client config, private key, user account, certificate, or live config is
included. The client would use the exact hostname and edge SSH port 22 after
the edge host-key fingerprint is reviewed and pinned out of band.

This candidate still needs root review before installing a customer account or
per-home Match/key on the edge's shared SSH daemon. The daemon's current global
defaults are broader; the generated Match fragment restricts this one user to
public-key remote forwarding on exactly `127.0.0.1:32808`, no shell/session,
and the matching authorized-key line also limits its listener. The edge
certificate must first be issued through the existing HTTP-01 catch-all
webroot `/var/www/html`. The issue-8 exact-route HTTP-to-VM104 candidate remains
in the parent staging directory for comparison.

Local test: `python3 -m unittest -v test_local_edge_hop.py`. It starts only
temporary loopback SSHD/Nginx processes with ephemeral keys/certificate, routes
one generic synthetic HTTP fixture through the generated reverse tunnel, and
checks that a second remote port is rejected. It never contacts the live edge
or HA service. It requires root and allocates a free relay port from the
product range; a separate test checks the staged `32808` fragments exactly.
