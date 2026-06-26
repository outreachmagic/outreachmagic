#!/usr/bin/env python3
"""Billing contract tests — align skill CLI with billing policy."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
CONTRACT_PATH = Path(__file__).resolve().parent / "billing_contract.json"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from constants import (  # noqa: E402
    BILLING_UPGRADE_URL,
    USAGE_CRITICAL_PERCENT,
    USAGE_WARNING_PERCENT,
)

CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _patch_status_fetch(monkeypatch, payload: dict):
    monkeypatch.setattr(om, "_check_account_access_revoked", lambda: False)
    monkeypatch.setattr(om, "_require_agent_key", lambda: "om_agent_test")
    monkeypatch.setattr(om.routing_cloud, "get_api_base", lambda _load: "https://app.example")
    monkeypatch.setattr(om.connections_cloud, "fetch_status", lambda *_a, **_k: payload)


@pytest.mark.parametrize("tier", ["free", "pro", "agency"])
def test_skill_md_documents_plan_limits(tier):
    limits = CONTRACT["plans"][tier]
    skill = (ROOT / "skills" / "outreachmagic" / "SKILL.md").read_text(encoding="utf-8")
    monthly = limits["monthly_events"]
    assert str(monthly) in skill.replace(",", "")
    if monthly >= 1000:
        formatted = f"{monthly:,}"
        assert formatted in skill or str(monthly) in skill


def test_pipeline_usage_thresholds_match_contract():
    thresholds = CONTRACT["usage_thresholds"]
    assert USAGE_WARNING_PERCENT == thresholds["warning_percent"]
    assert USAGE_CRITICAL_PERCENT == thresholds["critical_percent"]


def test_pipeline_upgrade_url_matches_contract():
    assert BILLING_UPGRADE_URL == CONTRACT["upgrade_url"]


def test_cmd_status_shows_plan_and_usage(capsys, monkeypatch):
    limits = CONTRACT["plans"]["pro"]
    _patch_status_fetch(
        monkeypatch,
        {
            "plan": "pro",
            "eventsUsed": 12000,
            "eventsLimit": limits["monthly_events"],
            "eventsBuffered": 0,
            "bufferCap": limits["buffer_cap"],
            "usageExhausted": False,
            "usageCritical": False,
            "resetsAt": "2026-07-01T00:00:00Z",
            "connections": [],
            "workspaceMode": "single",
            "workspacesCount": 1,
            "routingConfigVersion": 3,
        },
    )
    om.cmd_status()
    out = capsys.readouterr().out
    assert "Plan: Pro" in out
    assert "12,000 / 50,000" in out or "12000 / 50000" in out


def test_cmd_status_critical_usage_warning(capsys, monkeypatch):
    limits = CONTRACT["plans"]["pro"]
    used = int(limits["monthly_events"] * 0.96)
    _patch_status_fetch(
        monkeypatch,
        {
            "plan": "pro",
            "eventsUsed": used,
            "eventsLimit": limits["monthly_events"],
            "eventsBuffered": 0,
            "bufferCap": limits["buffer_cap"],
            "billingNotice": None,
            "usageExhausted": False,
            "usageCritical": False,
            "resetsAt": "2026-07-01T00:00:00Z",
            "connections": [],
            "workspaceMode": "single",
            "workspacesCount": 1,
            "routingConfigVersion": 1,
        },
    )
    om.cmd_status()
    out = capsys.readouterr().out
    assert "96% used" in out
    assert BILLING_UPGRADE_URL in out


def test_cmd_status_exhausted_with_buffered_events(capsys, monkeypatch):
    limits = CONTRACT["plans"]["pro"]
    _patch_status_fetch(
        monkeypatch,
        {
            "plan": "pro",
            "eventsUsed": limits["monthly_events"],
            "eventsLimit": limits["monthly_events"],
            "eventsBuffered": 47,
            "bufferCap": limits["buffer_cap"],
            "billingNotice": None,
            "usageExhausted": True,
            "usageCritical": True,
            "resetsAt": "2026-07-01T00:00:00Z",
            "connections": [],
            "workspaceMode": "single",
            "workspacesCount": 1,
            "routingConfigVersion": 1,
        },
    )
    om.cmd_status()
    out = capsys.readouterr().out
    assert "Quota reached" in out
    assert "47 buffered" in out
    assert str(limits["buffer_cap"]) in out
    assert BILLING_UPGRADE_URL in out


def test_cmd_status_portal_billing_notice_takes_precedence(capsys, monkeypatch):
    notice = "Relay quota reached. 12 events buffered — run pull or upgrade to deliver."
    _patch_status_fetch(
        monkeypatch,
        {
            "plan": "free",
            "eventsUsed": CONTRACT["plans"]["free"]["monthly_events"],
            "eventsLimit": CONTRACT["plans"]["free"]["monthly_events"],
            "eventsBuffered": 12,
            "bufferCap": CONTRACT["plans"]["free"]["buffer_cap"],
            "billingNotice": notice,
            "usageExhausted": True,
            "usageCritical": True,
            "resetsAt": "2026-07-15T00:00:00Z",
            "connections": [],
            "workspaceMode": "single",
            "workspacesCount": 1,
            "routingConfigVersion": 1,
            "upgradeUrl": BILLING_UPGRADE_URL,
        },
    )
    om.cmd_status()
    out = capsys.readouterr().out
    assert notice in out
    assert BILLING_UPGRADE_URL in out


def test_relay_push_429_includes_buffer_cap_message(monkeypatch):
    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__(
                url="https://relay.example/push",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=io.BytesIO(b'{"error":"buffer_cap_exceeded"}'),
            )

        def read(self):
            return b'{"error":"buffer_cap_exceeded"}'

    def fake_urlopen(_req, timeout=None):
        raise FakeHTTPError()

    monkeypatch.setattr(om, "get_relay_push_settings", lambda **_k: {
        "batch_size": 10,
        "timeout_seconds": 5,
        "max_attempts": 1,
        "retry_base_seconds": 1,
    })
    monkeypatch.setattr(om.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(om, "_relay_log", lambda *_a, **_k: None)

    result = om._relay_push_batches(
        "om_agent_test",
        [{"platform": "agent", "action": "lead_snapshot"}],
        "client-1",
        stream_label="Lead",
    )
    assert result["throttled"] is True
    assert result["pushed"] == 0
    assert "buffer cap reached" in (result["error"] or "").lower()
    assert BILLING_UPGRADE_URL in (result["error"] or "")


def test_free_tier_buffer_headroom_matches_brand_math():
    """Free users need monthly_limit + buffer_cap stored before 429 (pricing.md)."""
    free = CONTRACT["plans"]["free"]
    assert free["monthly_events"] + free["buffer_cap"] == 3000


@pytest.mark.parametrize(
    "tier,at_limit,buffered,expect",
    [
        ("free", 1000, 0, "buffered"),
        ("free", 1000, 1999, "buffered"),
        ("free", 1000, 2000, "rejected"),
        ("pro", 50000, 0, "buffered"),
        ("agency", 250000, 499999, "buffered"),
    ],
)
def test_buffer_policy_examples_documented_in_brand(tier, at_limit, buffered, expect):
    """Mirror server-side classifyIngestDelivery — keep in sync with billing-buffer-test.mjs."""
    limits = CONTRACT["plans"][tier]
    monthly = limits["monthly_events"]
    buffer_cap = limits["buffer_cap"]
    increment = 1

    if at_limit + increment <= monthly:
        state = "delivered"
    elif buffered + increment <= buffer_cap:
        state = "buffered"
    else:
        state = "rejected"
    assert state == expect
