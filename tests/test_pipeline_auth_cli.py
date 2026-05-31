#!/usr/bin/env python3
"""Tests for login/logout CLI auth behavior."""

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch
import io
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(tempfile.mkdtemp()))

import pipeline as om  # noqa: E402


def _capture_output(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def test_login_generate_url_prints_parseable_fields():
    fake_device_login = types.SimpleNamespace(
        start_device_authorization=lambda *args, **kwargs: {
            "connect_url": "https://app.outreachmagic.io/connect?user_code=ABC123",
            "user_code": "ABC-123",
            "device_code": "device_123",
            "expires_in": 900,
        },
        claim_device_token=lambda *args, **kwargs: {},
        run_device_login=lambda *args, **kwargs: "om_agent_unused",
    )
    with patch.dict(sys.modules, {"device_login": fake_device_login}):
        out = _capture_output(lambda: om.login(generate_url=True))

    assert "OUTREACHMAGIC_URL=https://app.outreachmagic.io/connect?user_code=ABC123" in out
    assert "OUTREACHMAGIC_CODE=ABC-123" in out
    assert "OUTREACHMAGIC_DEVICE_CODE=device_123" in out
    assert "OUTREACHMAGIC_EXPIRES_IN=900" in out


def test_login_claim_token_pending():
    fake_device_login = types.SimpleNamespace(
        start_device_authorization=lambda *args, **kwargs: {},
        claim_device_token=lambda *args, **kwargs: {"status": "pending", "access_token": None},
        run_device_login=lambda *args, **kwargs: "om_agent_unused",
    )
    with (
        patch.dict(sys.modules, {"device_login": fake_device_login}),
        patch.object(om.routing_cloud, "get_api_base", lambda *_args, **_kwargs: "https://api.test"),
    ):
        out = _capture_output(lambda: om.login(claim_token=True, device_code="dev_code", wait_seconds=0))
    assert "STATUS=pending" in out


def test_login_claim_token_success_saves_key():
    fake_device_login = types.SimpleNamespace(
        start_device_authorization=lambda *args, **kwargs: {},
        claim_device_token=lambda *args, **kwargs: {"status": "success", "access_token": "om_agent_test"},
        run_device_login=lambda *args, **kwargs: "om_agent_unused",
    )
    saved = {}
    with (
        patch.dict(sys.modules, {"device_login": fake_device_login}),
        patch.object(om.routing_cloud, "get_api_base", lambda *_args, **_kwargs: "https://api.test"),
        patch.object(om, "_save_agent_key_and_validate", lambda key: saved.setdefault("key", key)),
    ):
        out = _capture_output(lambda: om.login(claim_token=True, device_code="dev_code", wait_seconds=0))
    assert saved["key"] == "om_agent_test"
    assert "STATUS=success" in out


def test_logout_clears_agent_credentials():
    cfg = {"agent_key": "om_agent_123", "token": "legacy", "other": "keep"}
    saved = {}
    with (
        patch.object(om, "load_config", lambda: dict(cfg)),
        patch.object(om, "save_config", lambda new_cfg: saved.setdefault("cfg", new_cfg)),
    ):
        out = _capture_output(om.logout)

    assert "Logged out. Cleared local agent credentials." in out
    assert "agent_key" not in saved["cfg"]
    assert "token" not in saved["cfg"]
    assert saved["cfg"]["other"] == "keep"


def test_main_allows_login_when_db_missing():
    called = {}
    with (
        patch.object(om, "db_exists", lambda: False),
        patch.object(om, "login", lambda **kwargs: called.setdefault("login", kwargs)),
        patch.object(sys, "argv", ["pipeline.py", "login", "--generate-url"]),
    ):
        om.main()
    assert called["login"]["generate_url"] is True


def test_main_allows_logout_when_db_missing():
    called = {"logout": 0}
    with (
        patch.object(om, "db_exists", lambda: False),
        patch.object(om, "logout", lambda: called.__setitem__("logout", called["logout"] + 1)),
        patch.object(sys, "argv", ["pipeline.py", "logout"]),
    ):
        om.main()
    assert called["logout"] == 1


if __name__ == "__main__":
    test_login_generate_url_prints_parseable_fields()
    test_login_claim_token_pending()
    test_login_claim_token_success_saves_key()
    test_logout_clears_agent_credentials()
    test_main_allows_login_when_db_missing()
    test_main_allows_logout_when_db_missing()
    print("ok")
