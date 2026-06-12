"""Tests for share_email default and whoami helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


def test_resolve_share_email_from_config():
    with patch.object(om, "load_config", return_value={"account_email": "user@example.com"}):
        with patch.object(om, "get_agent_key", return_value=None):
            assert om.resolve_share_email(None) == "user@example.com"
            assert om.resolve_share_email("override@example.com") == "override@example.com"


def test_resolve_share_email_from_status_api():
    with patch.object(om, "load_config", return_value={}):
        with patch.object(om, "get_agent_key", return_value="om_agent_test"):
            with patch.object(om, "routing_cloud") as rc:
                rc.get_api_base.return_value = "https://app.example"
                with patch.object(om.connections_cloud, "fetch_status") as fetch:
                    fetch.return_value = {
                        "accountEmail": "owner@example.com",
                        "organizationId": "org_1",
                    }
                    with patch.object(om, "save_config"):
                        assert om.resolve_share_email(None) == "owner@example.com"


def test_cmd_whoami_json(monkeypatch, capsys):
    monkeypatch.setattr(om, "_account_access_revoked", lambda: False)
    monkeypatch.setattr(om, "get_agent_key", lambda: "om_agent_test")
    monkeypatch.setattr(om, "load_config", lambda: {"organization_id": "org_1"})
    monkeypatch.setattr(
        om.connections_cloud,
        "fetch_status",
        lambda *_a, **_k: {
            "plan": "pro",
            "accountEmail": "whoami@example.com",
            "organizationId": "org_1",
        },
    )
    monkeypatch.setattr(om, "save_config", lambda _cfg: None)
    monkeypatch.setattr(om.routing_cloud, "get_api_base", lambda _c: "https://app.example")

    om.cmd_whoami(json_output=True)
    out = json.loads(capsys.readouterr().out)
    assert out["email"] == "whoami@example.com"
    assert out["plan"] == "pro"
    assert out["access_revoked"] is False
