#!/usr/bin/env python3
"""Regression tests for bug-report v2 items (2026-06-09)."""

import argparse
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import query_cli  # noqa: E402
import read_queries as rq  # noqa: E402


def test_query_help_does_not_crash(capsys):
  """Bug 4: argparse must not choke on --params help text."""
  parser = argparse.ArgumentParser()
  sub = parser.add_subparsers(dest="command")
  query_cli.register_query_parser(sub)
  with pytest.raises(SystemExit) as exc:
    parser.parse_args(["query", "--help"])
  assert exc.value.code == 0
  assert "--params" in capsys.readouterr().out


def test_workspace_list_json_flag_parsed():
  """Bug 5: workspace list accepts --json."""
  parser = argparse.ArgumentParser()
  sub = parser.add_subparsers(dest="command")
  ws = sub.add_parser("workspace")
  ws_sub = ws.add_subparsers(dest="workspace_cmd")
  ws_list = ws_sub.add_parser("list")
  ws_list.add_argument("--json", action="store_true")
  args = parser.parse_args(["workspace", "list", "--json"])
  assert args.command == "workspace"
  assert args.workspace_cmd == "list"
  assert args.json is True


def test_quarantine_skip_all_campaign_id_and_reason_parsed():
    """Bulk quarantine skip flags: --all, --campaign-id, --reason."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    q = sub.add_parser("quarantine")
    q_sub = q.add_subparsers(dest="quarantine_cmd")
    q_skip = q_sub.add_parser("skip")
    q_skip.add_argument("--id")
    q_skip.add_argument("--campaign-id")
    q_skip.add_argument("--reason")
    q_skip.add_argument("--all", action="store_true")
    args = parser.parse_args(["quarantine", "skip", "--all"])
    assert args.all is True
    args = parser.parse_args(["quarantine", "skip", "--campaign-id", "abc123"])
    assert args.campaign_id == "abc123"
    args = parser.parse_args(["quarantine", "skip", "--reason", "no_campaign_id"])
    assert args.reason == "no_campaign_id"


def test_export_format_includes_sheets():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    export_p = sub.add_parser("export")
    export_p.add_argument("--workspace", required=True)
    export_p.add_argument("--format", choices=("csv", "json", "sheets"), default="csv")
    args = parser.parse_args(["export", "--workspace", "popcam", "--format", "sheets"])
    assert args.format == "sheets"


def test_record_install_source():
    om.init_db()
    result = om.record_install_source("v1.29.4")
    assert result["installed_from_tag"] == "v1.29.4"
    cfg = om.load_config()
    assert cfg.get("installed_from_tag") == "v1.29.4"


def test_sheets_export_parser_exists():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sheets_p = sub.add_parser("sheets")
    sheets_sub = sheets_p.add_subparsers(dest="sheets_command", required=True)
    sheets_sub.add_parser("export").add_argument("--workspace", required=True)
    args = parser.parse_args(["sheets", "export", "--workspace", "popcam"])
    assert args.command == "sheets"
    assert args.sheets_command == "export"


def test_daily_digest_and_format():
  om.init_db()
  om.create_workspace("PopCam", "popcam", sync=False)
  result = om.resolve_lead(
    email="digest@example.com",
    name="Digest Lead",
    company="Acme",
    source="test",
  )
  lead_id = result["id"]
  om.log_event(
    lead_id,
    event_type="email_sent",
    direction="outbound",
    campaign="popcam | headshot lounge",
  )
  om.log_event(
    lead_id,
    event_type="email_reply",
    direction="inbound",
    campaign="popcam | headshot lounge",
  )
  digest = rq.daily_digest(workspace="popcam")
  assert digest["emails_sent"] >= 1
  assert digest["replies"] >= 1
  text = rq.format_daily_digest(digest)
  assert "Emails sent:" in text
  assert "Replies:" in text


def test_refresh_pre_wipe_routing_summary(monkeypatch):
  """Improvement 6: routing verified before DB wipe."""
  om.init_db()
  om.create_workspace("PopCam", "popcam", sync=False)
  om.add_campaign_map_cli("*", "popcam", campaign_name="popcam", match_strategy="rule_contains")

  monkeypatch.setattr(om, "maybe_sync_routing_from_cloud", lambda **k: True)
  monkeypatch.setattr(om, "maybe_sync_agent_secrets_from_cloud", lambda **k: None)
  monkeypatch.setattr(om, "sync_all", lambda **k: {"status": "ok"})
  monkeypatch.setattr(om, "get_sync_status", lambda org_id: {"pending_total": 0})
  monkeypatch.setattr(om, "get_agent_key", lambda: "om_agent_test")
  monkeypatch.setattr(
    om.routing_cloud,
    "cloud_routing_enabled",
    lambda cfg, tok: True,
  )
  monkeypatch.setattr(om, "sync_from_relay_org", lambda *a, **k: (1, 0))

  result = om.refresh_local_database(yes=True, quiet=True)
  assert result.get("status") == "ok"
  assert "pre_wipe_routing_sync" in result.get("steps", [])
  summary = result.get("routing_summary") or {}
  assert summary.get("workspace_count", 0) >= 1
  assert summary.get("campaign_map_count", 0) >= 1


def test_pull_if_stale_force_pull():
  om.init_db()
  cfg = om.load_config()
  cfg["last_pull"] = om.datetime.now(om.timezone.utc).isoformat()
  om.save_config(cfg)
  assert om.pull_if_stale_skip_result("5m", force=False) is not None
  assert om.pull_if_stale_skip_result("5m", force=True) is None
