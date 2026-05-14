"""Dashboard test fixtures.

Endpoint caches in `coolstep.dashboard.server` are process-global (correct
production behaviour — cache once across requests). Tests mutate the
underlying files between calls and would see stale cached responses without
a between-test reset.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_endpoint_caches_each_test() -> None:
    from coolstep.dashboard.server import _reset_endpoint_caches

    _reset_endpoint_caches()
