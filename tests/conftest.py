"""Global pytest fixtures ensuring test isolation."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_pku_session_cache():
    """Ensure in-memory Blackboard session cache never leaks across tests."""
    from pku_sync import auth

    auth.clear_session_cache()
    yield
    auth.clear_session_cache()
