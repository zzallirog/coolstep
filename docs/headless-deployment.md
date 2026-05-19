# Headless deployment

> coolstep was built around the assumption that the dashboard runs on the
> same machine you are sitting at. When that's not the case, the patterns
> below cover the realistic shapes. See [`security.md`](security.md) for
> the trust model these patterns plug into.

## Default — loopback

```
coolstep-dashboard         # binds 127.0.0.1:18889
```

No flags needed; loopback is the gate. Anything else requires an
explicit auth choice (your choice — the daemon does not provide one).

## Pattern 1 — SSH tunnel (recommended)

Run the daemon under user-systemd on the remote host with the default
loopback bind. Reach the dashboard from your laptop by tunnelling:

```
ssh -L 18889:127.0.0.1:18889 user@remote-host
# then http://127.0.0.1:18889/ in the local browser
```

For systemd-user services to survive logouts on the headless host,
enable linger once:

```
loginctl enable-linger $USER
systemctl --user enable --now coolstep-collector coolstep-dashboard
```

Why this is recommended: you reuse the SSH auth you already have, no
new surface, no proxy config.

## Pattern 2 — reverse proxy with auth

When SSH tunnels are impractical (kiosk display, shared NOC dashboard),
front the daemon with an authenticating reverse proxy. Examples: Caddy
basic-auth, nginx + Authelia, Cloudflare Access, oauth2-proxy.

Same host as the daemon — keep loopback bind, proxy on the same box:

```
coolstep-dashboard --host 127.0.0.1 --port 18889
# Proxy terminates TLS + auth, then proxy_pass to 127.0.0.1:18889.
```

Proxy on a different host — expose on a private interface, and tell the
daemon which public FQDN the proxy will use:

```
coolstep-dashboard \
    --host 10.10.0.5 --port 18889 \
    --allow-public \
    --allow-host coolstep.example.net
```

- `--allow-public` is mandatory for any non-loopback bind.
- `--allow-host <fqdn>` adds the proxy's public hostname to the Host
  header allowlist. Without it, requests forwarded under that name are
  rejected by the hardening middleware.

Caddy minimal example:

```caddyfile
coolstep.example.net {
    basicauth {
        you  $2a$14$<bcrypt-hash>
    }
    reverse_proxy 10.10.0.5:18889
}
```

## Pattern 3 — WireGuard / VPN-only

For a closed admin network, bind on a WG interface and let the key be
the gate:

```
coolstep-dashboard \
    --host 10.66.0.5 --port 18889 \
    --allow-public \
    --allow-host coolstep.wg
```

Whoever holds the WG key is the operator. This matches the daemon's
single-user assumption better than basic auth does, because the key
gates network reachability, not just the HTTP layer.

## Health checks from other machines

Same `--allow-public` + `--allow-host` rules apply. Recommended:
scrape `/api/health` through the same reverse proxy that handles user
traffic, not a separately exposed port.

## Audit trail

Every dashboard-triggered mode change is appended to
`actuator-journal.jsonl` alongside actuator events, with the prior mode
and peer info. Read it via `/api/actuator-journal` or
`coolstep inspect`. When behind a reverse proxy, configure the proxy
to forward `X-Forwarded-For` so peer identification stays meaningful.
