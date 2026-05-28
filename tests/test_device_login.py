#!/usr/bin/env python3
"""Tests for non-blocking device login helpers."""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import device_login  # noqa: E402


def _fake_load_config():
    return {}


def test_start_device_authorization_returns_expected_fields():
    with (
        patch.object(device_login, "get_api_base", lambda _fn: "https://api.outreachmagic.test"),
        patch.object(device_login, "detect_client_platform", lambda **kwargs: "cursor"),
        patch.object(device_login, "default_client_label", lambda **kwargs: "test-client"),
        patch.object(device_login.socket, "gethostname", lambda: "host1"),
        patch.object(
            device_login,
            "_post_json",
            lambda url, body: {
                "device_code": "dev_abc",
                "user_code": "GL3Q-2HQZ",
                "verification_uri": "https://app.outreachmagic.test/connect",
                "expires_in": 600,
                "interval": 3,
            },
        ),
    ):
        flow = device_login.start_device_authorization(_fake_load_config, platform="cursor")

    assert flow["device_code"] == "dev_abc"
    assert flow["user_code"] == "GL3Q-2HQZ"
    assert flow["connect_url"] == "https://app.outreachmagic.test/connect?user_code=GL3Q2HQZ"
    assert flow["expires_in"] == 600
    assert flow["interval"] == 3


def test_claim_device_token_pending_with_zero_wait():
    with patch.object(
        device_login,
        "_post_json",
        lambda *args, **kwargs: {"error": "authorization_pending"},
    ):
        out = device_login.claim_device_token(
            "https://api.outreachmagic.test",
            device_code="dev_pending",
            wait_seconds=0,
            interval=1,
        )

    assert out["status"] == "pending"
    assert out["access_token"] is None


def test_claim_device_token_success():
    with patch.object(
        device_login,
        "_post_json",
        lambda *args, **kwargs: {"access_token": "om_agent_123"},
    ):
        out = device_login.claim_device_token(
            "https://api.outreachmagic.test",
            device_code="dev_ok",
            wait_seconds=0,
        )

    assert out["status"] == "success"
    assert out["access_token"] == "om_agent_123"


def test_claim_device_token_terminal_error():
    with patch.object(
        device_login,
        "_post_json",
        lambda *args, **kwargs: {"error": "expired_token"},
    ):
        out = device_login.claim_device_token(
            "https://api.outreachmagic.test",
            device_code="dev_expired",
            wait_seconds=0,
        )

    assert out["status"] == "expired_token"
    assert out["access_token"] is None


if __name__ == "__main__":
    test_start_device_authorization_returns_expected_fields()
    test_claim_device_token_pending_with_zero_wait()
    test_claim_device_token_success()
    test_claim_device_token_terminal_error()
    print("ok")
