# Security model

> coolstep is a single-user local daemon. The dashboard binds
> `127.0.0.1:18889` by default. Anyone who can reach loopback on the
> host can drive the daemon — that is the trust boundary, and any
> remote exposure should add an authentication layer that you control.

## What ships in the box

- **Loopback bind by default.** The daemon refuses to bind any non-loopback
  address without the explicit `--allow-public` flag.
- **Transport-layer hardening middleware.** Validates the HTTP `Host` header
  on every request and the `Origin` / `Sec-Fetch-Site` headers on mutating
  endpoints. Non-browser clients (curl, systemd timers, internal CLI) are
  unaffected.
- **Audit journal.** Every mode switch lands in `actuator-journal.jsonl`
  alongside actuator events, with timestamp, target, previous mode, and
  peer information. Already surfaced by `/api/actuator-journal` and
  `coolstep inspect`.

## Authentication

The daemon does not ship an authentication layer of its own. coolstep is
intentionally small-scope, and most deployments already have an upstream
gate that fits better than anything we could bundle:

- **Single workstation** — loopback bind is the gate.
- **Remote access** — SSH tunnel (`ssh -L 18889:127.0.0.1:18889 host`).
  See [`headless-deployment.md`](headless-deployment.md).
- **Shared / public-fronted** — reverse proxy (Caddy basic-auth, nginx +
  Authelia, oauth2-proxy, Cloudflare Access). The dashboard accepts a
  fronting hostname via `--allow-host <fqdn>`.
- **Closed network** — WireGuard or VPN-only exposure; the key is the auth.

A built-in auth layer is **not on the default roadmap** but is not
philosophically refused — see *Contributing* below.

## Reporting an issue

Use GitHub's *Security → Report a vulnerability* tab on this repository
for any finding that bypasses the middleware, escalates beyond the
single-user trust boundary, or affects the published artefacts. See
[`SECURITY.md`](../SECURITY.md) at the repo root for the disclosure
channel and response timeline.

## Contributing

We accept PRs that:

- harden the middleware further (regression test that fails without the fix
  — template: `tests/dashboard/test_security.py`),
- extend the audit journal with additional context (peer identification
  behind common reverse proxies, structured fields, etc.),
- add deployment patterns to [`headless-deployment.md`](headless-deployment.md)
  from environments we haven't covered (k8s sidecar, nomad, etc.),
- prototype a built-in auth layer — open a draft issue first so we can
  agree on shape (auth backend pluggability, configuration storage, default
  behaviour for fresh installs).
