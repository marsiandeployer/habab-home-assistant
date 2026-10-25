# Public HTTPS reverse-tunnel read-only receipt

**Captured:** 2026-10-25. **Result:** proposal is technically compatible with existing code and ingress, but public access is not configured or accepted.

| Evidence class | Read-only finding | Boundary |
|---|---|---|
| Parent-provided platform receipt | Huawei VOG-L29, Android 10 / API 29; USB-host feature present. | Declaration from parent; this task did not use ADB or inspect the phone. |
| Parent-provided current runtime | Native16 / embedded Core 0.3.6 running; `tunnel_enabled=false`; SSH endpoint not configured. | Parent read-only snapshot; not independently rechecked. |
| Existing Android implementation | `Tunnel.java` pins SSH host key, uses public-key auth, binds remote listener to `127.0.0.1`, forwards to phone `127.0.0.1:8123`, and reconnects. `ServerService` launches it after HA status responds. | Source inspection only; no device execution. |
| Settings lifecycle | `MainActivity.tunnelDialog()` requires `stopped()`; that guard rejects while `ServerService.active`, stopping, busy, or maintenance is active. Saving writes preferences. | Source inspection; no preferences changed. |
| Existing VM104 ingress | Local listener snapshot showed Nginx on 80/443 and no listener on 18123; local Nginx vhosts include `habab-voice.wpmix.net` and `habab-demo.wpmix.net`, no `habab-p30.wpmix.net`. | Listener/vhost snapshot; free-port condition can change. Voice relay is separate. |
| Existing edge ingress | Read-only edge inspection showed TLS Nginx and existing proxy to VM104 HTTP ingress at 95.217.227.164:80; no `habab-p30.wpmix.net` vhost. | Config inspection only; no request to proposed host. |
| Certificate | Edge's existing local wildcard certificate SAN is `*.wpmix.net`, expiry 2026-12-20. | Certificate file inspection; no public TLS/SNI handshake performed. |
| SSH host pin | Local ED25519 host public key fingerprint is `SHA256:9aarYaFSU/z4kKUCyKrjzTeUxtNb0vBfbyeJpdvlyjQ`; public key value is in the plan. | Local host key inspection; out-of-band confirmation still required before pinning. |
| SSH policy | Current effective `sshd -T -C user=habab-s-p30,...` inherits broad `PermitListen 127.0.0.1:*` from wildcard support-account rule. | Read-only config evaluation; no proposed dedicated account/config has been added. |
| HA reverse-proxy guidance | Official docs require enabling XFF trust and listing trusted proxy addresses; HTTP settings restart HA and revert if not confirmed within five minutes. | Documentation only; no HA HTTP settings changed. |
| DNS/public reachability | NOT RUN by instruction. | No DNS lookup, domain publication, or external HTTPS request. |
| Phone/service lifecycle | NOT RUN. | No ADB, USB, phone setting, server service, or Telegram action. |
| SSH/Nginx/DNS/HA mutations | NOT RUN. | No account, key, listener, vhost, DNS record, or HA config change. |
| External acceptance | NOT RUN. | No login, WebSocket, browser, or external-network test. |

## Reproduce read-only checks

On VM104, from `/root/homeassistent`:

```bash
ss -ltnp
sudo nginx -T 2>/dev/null | rg 'server_name|proxy_pass'
sudo sshd -T -C user=habab-s-p30,host=localhost,addr=127.0.0.1 \
  | rg '^(allowtcpforwarding|gatewayports|permitlisten|maxsessions|permittty|allowagentforwarding|x11forwarding|passwordauthentication|authenticationmethods|permitopen) '
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

The edge read-only check used the established SSH management path with strict host checking and only displayed `server_name` and certificate date/SAN fields. The VM104 self-NAT SSH endpoint `95.217.227.164:2222` was not contacted from VM104.
