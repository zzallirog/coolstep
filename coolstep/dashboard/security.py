"""Dashboard hardening middleware.

The coolstep dashboard is designed for **single-user loopback** access.
There is no authentication and there is no plan to add one — the project
expects upstream gates (SSH tunnel, reverse proxy with auth) for remote
deployments. See `docs/security.md` for the full threat model.

This module closes two practical browser-side attack vectors that remain
even with a 127.0.0.1 bind:

1. **DNS rebinding.** A malicious DNS server resolves an attacker-controlled
   domain to 127.0.0.1 from the victim's browser. Without Host header
   validation, requests reach the dashboard with attacker JS execution
   context. Defense: reject any request whose `Host:` header is not in
   an allowlist derived from the bound interface.

2. **Form-CSRF.** Even though the dashboard does not set CORS headers
   (browsers block cross-origin XHR/fetch by default), a hostile page
   can still issue a "simple" form POST to `http://localhost:18889/api/mode/quiet`
   from the victim's browser. The browser includes no credentials (there
   are none), but the action still executes. Defense: on mutating methods,
   require Origin to match the bound URL **or** Sec-Fetch-Site to indicate
   same-origin / direct navigation. Non-browser clients (curl, systemd
   timer) send neither header and are allowed through — the attacker
   scenario requires a browser context anyway.

What this middleware deliberately does NOT do:

- It does not authenticate users. Anyone with direct loopback access
  (any process running as the same user, anything tunnelled to :18889)
  can switch thermal modes. That is the documented trust boundary.
- It does not encrypt anything. TLS belongs at the reverse-proxy layer.
- It does not rate-limit. The endpoints are cheap and there is no
  abuse vector that loopback hardening doesn't already address.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})

DEFAULT_HOST_ALLOWLIST = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _host_from_header(header_value: str | None) -> str | None:
    """Strip port from the Host header (RFC 7230 §5.4). Returns lowercase host."""
    if not header_value:
        return None
    h = header_value.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    if ":" in h:
        return h.rsplit(":", 1)[0]
    return h


def build_host_allowlist(host: str, extras: list[str] | None = None) -> frozenset[str]:
    """Host header allowlist = loopback names ∪ bound host ∪ user-supplied extras.

    Extras are needed when a reverse proxy fronts the dashboard under a
    public FQDN — the proxy preserves the original Host header by default.
    """
    out: set[str] = set(DEFAULT_HOST_ALLOWLIST)
    out.add(host.lower())
    for extra in extras or []:
        out.add(extra.lower().strip())
    return frozenset(h for h in out if h)


def build_origin_allowlist(host: str, port: int, extras: list[str] | None = None) -> frozenset[str]:
    """Allowed Origin header values that match the bound URL.

    Includes both http:// and https:// schemes because the dashboard itself
    speaks HTTP but a fronting proxy may terminate TLS and forward unchanged.
    Loopback bindings expand to all loopback names so that browsers visiting
    via `localhost` or `127.0.0.1` both succeed.
    """
    out: set[str] = set()
    if host.lower() in LOOPBACK_HOSTS:
        bases = {"127.0.0.1", "localhost", "[::1]"}
    else:
        bases = {host.lower()}
    for b in bases:
        out.add(f"http://{b}:{port}")
        out.add(f"https://{b}:{port}")
    for extra in extras or []:
        e = extra.strip().lower()
        if e:
            out.add(e)
    return frozenset(out)


class HardeningMiddleware(BaseHTTPMiddleware):
    """Validate Host header on every request; validate Origin / Sec-Fetch-Site on mutations.

    Returns 421 (Misdirected Request) for Host mismatch and 403 for blocked
    cross-origin mutations. Response bodies point operators at `docs/security.md`
    so the failure mode is debuggable rather than mysterious.
    """

    def __init__(
        self,
        app,
        host_allowlist: frozenset[str],
        origin_allowlist: frozenset[str],
    ) -> None:
        super().__init__(app)
        self.host_allowlist = host_allowlist
        self.origin_allowlist = origin_allowlist

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        host = _host_from_header(request.headers.get("host"))
        if host is None or host not in self.host_allowlist:
            return JSONResponse(
                {
                    "error": "host_not_allowed",
                    "detail": (
                        f"Host header {host!r} not in dashboard allowlist "
                        "(DNS-rebinding defense). See docs/security.md."
                    ),
                },
                status_code=421,
            )

        if request.method in MUTATING_METHODS:
            sec_fetch_site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            ok = False
            if sec_fetch_site in {"same-origin", "none"} or origin and origin.lower() in self.origin_allowlist:
                ok = True
            elif origin is None and sec_fetch_site is None:
                # Non-browser caller (curl, systemd timer, internal CLI).
                # The attacker scenario for form-CSRF requires a browser
                # context which always sends at least one of these.
                ok = True
            if not ok:
                return JSONResponse(
                    {
                        "error": "csrf_blocked",
                        "detail": (
                            "Cross-origin mutation blocked (form-CSRF defense). "
                            f"Origin={origin!r}, Sec-Fetch-Site={sec_fetch_site!r}. "
                            "See docs/security.md."
                        ),
                    },
                    status_code=403,
                )

        return await call_next(request)
