"""Tests for server/telemetry.py (the server-side opt-in PostHog module).

Distinct from the SDK-side mem0.memory.telemetry suites: this covers the
server's ENABLED gate, its default-off posture, and the local-only dashboard
nudge that intentionally ignores the flag.
"""

import importlib
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import telemetry as server_telemetry  # noqa: E402


def _reload_with(env_overrides: dict):
    with patch.dict(os.environ, env_overrides, clear=False):
        return importlib.reload(server_telemetry)


@pytest.fixture(autouse=True)
def _restore_module():
    """Reload back to the conftest baseline after each test."""
    yield
    importlib.reload(server_telemetry)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    mod = _reload_with({"MEM0_TELEMETRY": ""})
    assert mod.ENABLED is False


def test_opt_in_via_env(monkeypatch):
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    mod = _reload_with({"MEM0_TELEMETRY": "true"})
    assert mod.ENABLED is True


def test_capture_once_noop_when_disabled(monkeypatch):
    """No PostHog client is constructed and nothing is sent while disabled."""
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    mod = _reload_with({"MEM0_TELEMETRY": ""})
    with patch.object(mod, "_get_client") as mock_client:
        mod.capture_admin_registered(email="admin@example.com")
        mock_client.assert_not_called()


def test_capture_once_sends_when_enabled(monkeypatch):
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    mod = _reload_with(
        {
            "MEM0_TELEMETRY": "true",
            "MEM0_TELEMETRY_STATE_PATH": os.path.join(_SERVER_DIR, ".test-telemetry-state.json"),
        }
    )
    client = MagicMock()
    with patch.object(mod, "_get_client", return_value=client), patch.object(
        mod, "_load_state", return_value={}
    ), patch.object(mod, "_save_state") as mock_save:
        mod.capture_admin_registered(email="admin@example.com")
        client.capture.assert_called_once()
        _, kwargs = client.capture.call_args
        assert kwargs["event"] == "admin_registered"
        # Only the email DOMAIN leaves the box, never the full address.
        assert kwargs["properties"]["email_domain"] == "example.com"
        assert "@" not in str(kwargs["properties"])
        # _save_state fires for install_id creation AND the sent_at marker.
        assert mock_save.call_count == 2


def test_dashboard_nudge_not_gated_by_flag(monkeypatch, caplog):
    """The nudge is a LOCAL console hint and must fire even when disabled."""
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    mod = _reload_with({"MEM0_TELEMETRY": ""})
    assert mod.ENABLED is False
    with caplog.at_level(logging.INFO):
        mod.log_dashboard_nudge_once("http://localhost:3002")
    assert any("dashboard" in r.message for r in caplog.records)


def test_capture_once_dedupes_via_state(monkeypatch):
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    mod = _reload_with({"MEM0_TELEMETRY": "true"})
    state = {"admin_registered_sent_at": "2026-01-01T00:00:00+00:00"}
    client = MagicMock()
    with patch.object(mod, "_get_client", return_value=client), patch.object(
        mod, "_load_state", return_value=state
    ):
        mod.capture_admin_registered(email="admin@example.com")
        client.capture.assert_not_called()
