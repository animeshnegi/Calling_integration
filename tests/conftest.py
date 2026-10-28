"""Shared isolation for process-wide application state in the test suite."""
from collections import defaultdict, deque

import pytest

from app import admin as admin_module


@pytest.fixture(autouse=True)
def isolated_login_rate_limit(monkeypatch):
    # Real clients share a per-process sign-in budget. Independent tests must
    # not inherit each other's IP/username attempts, regardless of xdist order.
    monkeypatch.setattr(admin_module, "_LOGIN_BUCKETS", defaultdict(deque))
