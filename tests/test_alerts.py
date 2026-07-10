"""Tests del WebhookNotifier."""

import json
from unittest.mock import patch, MagicMock

import pytest

from core.alerts import WebhookNotifier


# ─── Habilitación ────────────────────────────────────────────────────────────
def test_disabled_when_no_url():
    n = WebhookNotifier(webhook_url="")
    assert not n.enabled
    # No-op silencioso: no debe excepcionar
    assert n.quarantine_breach(
        run_id="r1", dataset="x", ratio=0.5, threshold=0.05,
        total_rows=100, quarantined=50,
    ) is None


def test_enabled_when_url_provided():
    assert WebhookNotifier(webhook_url="https://example.com/hook").enabled


def test_picks_up_env_var(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://from-env.example.com")
    n = WebhookNotifier()
    assert n.enabled
    assert n.webhook_url == "https://from-env.example.com"


# ─── Payload ─────────────────────────────────────────────────────────────────
@patch("core.alerts.urlrequest")
def test_quarantine_breach_posts_correct_payload(mock_req):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_req.urlopen.return_value.__enter__.return_value = mock_resp

    n = WebhookNotifier(webhook_url="https://example.com/hook")
    n.quarantine_breach(
        run_id="r-001", dataset="ventas", ratio=0.10,
        threshold=0.05, total_rows=1000, quarantined=100,
    )

    mock_req.urlopen.assert_called_once()
    request = mock_req.Request.call_args
    payload = json.loads(request.kwargs["data"].decode("utf-8"))
    assert payload["username"] == "Data Clean Pipeline"
    assert payload["attachments"][0]["color"] == "warning"
    assert "ventas" in payload["attachments"][0]["text"]
    assert "r-001" in payload["attachments"][0]["text"]


@patch("core.alerts.urlrequest")
def test_schema_drift_uses_danger_color(mock_req):
    mock_req.urlopen.return_value.__enter__.return_value.status = 200
    n = WebhookNotifier(webhook_url="https://example.com/hook")
    n.schema_drift(run_id="r2", dataset="x", diff={"missing_columns": ["a"]})
    payload = json.loads(
        mock_req.Request.call_args.kwargs["data"].decode("utf-8")
    )
    assert payload["attachments"][0]["color"] == "danger"


@patch("core.alerts.urlrequest")
def test_pipeline_success_uses_good_color(mock_req):
    mock_req.urlopen.return_value.__enter__.return_value.status = 200
    n = WebhookNotifier(webhook_url="https://example.com/hook")
    n.pipeline_success(run_id="r3", dataset="x", duration_s=42.5, q_ratio=0.02)
    payload = json.loads(
        mock_req.Request.call_args.kwargs["data"].decode("utf-8")
    )
    assert payload["attachments"][0]["color"] == "good"


# ─── Failure handling ────────────────────────────────────────────────────────
@patch("core.alerts.urlrequest")
def test_webhook_timeout_does_not_raise(mock_req):
    """Pipeline NUNCA debe romperse por un fallo del notifier."""
    import socket
    mock_req.urlopen.side_effect = socket.timeout("simulated")
    n = WebhookNotifier(webhook_url="https://example.com/hook")
    # No debe excepcionar
    result = n._send("Test", "Body")
    assert result is False


@patch("core.alerts.urlrequest")
def test_webhook_non_2xx_returns_false(mock_req):
    mock_req.urlopen.return_value.__enter__.return_value.status = 500
    n = WebhookNotifier(webhook_url="https://example.com/hook")
    assert n._send("Test", "Body") is False


@patch("core.alerts.urlrequest")
def test_webhook_2xx_returns_true(mock_req):
    mock_req.urlopen.return_value.__enter__.return_value.status = 204
    n = WebhookNotifier(webhook_url="https://example.com/hook")
    assert n._send("Test", "Body") is True
