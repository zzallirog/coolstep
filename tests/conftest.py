"""Top-level pytest fixtures shared across the whole test tree.

We monkeypatch `DEFAULT_HOST_ALLOWLIST` to include `testserver` (the default
Host header that `fastapi.testclient.TestClient` sends when no `base_url`
is set) so that the dashboard hardening middleware does not 421-reject
every existing test. Production defaults stay loopback-only; the tests
opt-in to the relaxed allowlist here rather than each test having to
construct `TestClient(app, base_url="http://127.0.0.1:18889")`.

`test_security.py` exercises the production allowlist explicitly and
proves that off-allowlist hosts are still rejected.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_testserver_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from coolstep.dashboard import security

    augmented = security.DEFAULT_HOST_ALLOWLIST | {"testserver"}
    monkeypatch.setattr(security, "DEFAULT_HOST_ALLOWLIST", augmented)
