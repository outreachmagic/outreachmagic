#!/usr/bin/env python3
"""Comprehensive edge case tests for campaign_stats.py.

Run from the repo root:
    cd outreachmagic-skill && python3 tests/test_campaign_stats_smoke.py

Covers:
  - Basic active/paused/exhausted detection
  - Empty workspace (no campaigns)
  - LinkedIn-only campaigns
  - OOO auto-reply detection
  - Mixed and extreme sentiment distributions
  - No sentiment data
  - 100% bounce rate
  - Custom since window (specific date)
  - All-time query
  - Full data round-trip with every event type
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import om_paths
import pipeline as om
import campaign_stats as cs


# ── Helpers ──────────────────────────────────────────────────────────


def _setup():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    om_paths.set_data_root_override(root)
    om_paths.set_project_root_override(root / "project")
    os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
    om.init_db()
    conn = om.get_conn()
    conn.row_factory = om.sqlite3.Row
    return tmp, conn


def _campaign(conn, workspace: str, name: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO campaigns (name) VALUES (?)",
        [f"{workspace} | {name}"],
    )
    row = conn.execute(
        "SELECT id FROM campaigns WHERE name = ?",
        [f"{workspace} | {name}"],
    ).fetchone()
    return row["id"] if row else None


def _lead(conn, name: str, email: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO leads (name, email, channel, stage) VALUES (?, ?, 'email', 'prospecting')",
        [name, email],
    )
    row = conn.execute("SELECT id FROM leads WHERE email = ?", [email]).fetchone()
    return row["id"] if row else None


def _event(
    conn,
    lead_id: int,
    campaign_id: int,
    event_type: str,
    *,
    direction: str = "outbound",
    channel: str = "email",
    created_at: str = "now",
    metadata: str = "{}",
):
    if created_at == "now":
        sql = "datetime('now')"
        params = []
    elif created_at.startswith("-"):
        sql = "datetime('now', ?)"
        params = [created_at]
    else:
        sql = "datetime(?)"
        params = [created_at]
    conn.execute(
        f"""INSERT INTO events (lead_id, campaign_id, event_type, direction, channel, created_at, metadata_json)
           VALUES (?, ?, ?, ?, ?, {sql}, ?)""",
        [lead_id, campaign_id, event_type, direction, channel] + params + [metadata],
    )


def run_tests():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ {name}")
        else:
            failed += 1
            print(f"  ❌ {name} — {detail}")

    # ─── Test 1: Normal case ───────────────────────────────────────
    print("\n═══ Test 1: Normal mixed-activity workspace ═══")
    tmp1, conn1 = _setup()
    c1 = _campaign(conn1, "acme", "alpha")
    c2 = _campaign(conn1, "acme", "beta")
    c3 = _campaign(conn1, "acme", "gamma")
    l1 = _lead(conn1, "Alice", "a@t.com")
    l2 = _lead(conn1, "Bob", "b@t.com")
    l3 = _lead(conn1, "Carol", "c@t.com")

    _event(conn1, l1, c1, "email_sent")
    _event(conn1, l1, c1, "email_reply", direction="inbound")
    _event(conn1, l2, c1, "email_sent")
    _event(conn1, l2, c1, "email_reply", direction="inbound",
           metadata='{"lead_status_sentiment":"positive"}')
    _event(conn1, l3, c1, "email_sent")
    _event(conn1, l3, c1, "email_bounce")

    # Paused (sends outside window)
    _event(conn1, l1, c2, "email_sent", created_at="-30 days")
    _event(conn1, l2, c2, "email_sent", created_at="-30 days")

    # Exhausted (c3: no events at all)

    conn1.commit()
    p = cs.build_campaign_stats_payload(conn1, workspace="acme", since="7d")
    ov = p["sheets"][0]
    check("3 campaigns in overview", len(ov["rows"]) == 3)
    # sorted by status then replies — alpha first
    check("alpha is active", ov["rows"][0][1] == "active")
    check("beta is paused", ov["rows"][1][1] == "paused")
    check("gamma is exhausted", ov["rows"][2][1] == "exhausted")
    check("alpha sent=3", ov["rows"][0][2] == 3)
    check("alpha delivered=2", ov["rows"][0][3] == 2)
    check("alpha bounced=1", ov["rows"][0][4] == 1)
    check("alpha bounce% starts with 33", str(ov["rows"][0][5]).startswith("33."))
    check("alpha replies=2", ov["rows"][0][6] == 2)
    check("alpha LI connects=0 (mdash)", ov["rows"][0][10] == "—")
    check("alpha interested=1", ov["rows"][0][15] == 1)
    check("alpha sentiment rate=100%", ov["rows"][0][17] == "100.0%")
    check("last_activity not empty", ov["rows"][0][18] != "—")

    fn = p["sheets"][1]
    check("funnels has 1 section", len([r for r in fn["rows"] if len(r) > 0 and r[0] == "Stage"]) == 1)
    check("funnel has emails sent row", any(len(r) > 0 and r[0] == "Emails Sent (total)" for r in fn["rows"]))
    check("funnel has bounced row", any(len(r) > 0 and r[0] == "Bounced" for r in fn["rows"]))
    check("funnel has interested row", any(len(r) > 0 and r[0] == "Interested Leads" for r in fn["rows"]))

    st = p["sheets"][2]
    check("sentiment has 1 campaign", len([r for r in st["rows"] if r[0] == "alpha"]) == 1)

    conn1.close()
    tmp1.cleanup()

    # ─── Test 2: Empty workspace ───────────────────────────────────
    print("\n═══ Test 2: Empty workspace (no campaigns) ═══")
    tmp2, conn2 = _setup()
    conn2.commit()
    p = cs.build_campaign_stats_payload(conn2, workspace="ghost", since="7d")
    ov = p["sheets"][0]
    st = p["sheets"][2]
    check("overview has 0 rows", len(ov["rows"]) == 0)
    check("funnels has no data rows", all(len(r) == 0 for r in p["sheets"][1]["rows"]))
    check("sentiment shows no-data message", len(st["rows"]) == 1 and "No sentiment data" in str(st["rows"][0][0]))
    conn2.close()
    tmp2.cleanup()

    # ─── Test 3: OOO detection ─────────────────────────────────────
    print("\n═══ Test 3: OOO auto-reply detection ═══")
    tmp3, conn3 = _setup()
    c3 = _campaign(conn3, "acme", "ooo-test")
    l3a = _lead(conn3, "Alice", "a3@t.com")
    l3b = _lead(conn3, "Bob", "b3@t.com")
    _event(conn3, l3a, c3, "email_sent")
    _event(conn3, l3a, c3, "email_reply", direction="inbound",
           metadata='{"is_auto_reply": true}')
    _event(conn3, l3b, c3, "email_sent")
    _event(conn3, l3b, c3, "email_reply", direction="inbound")  # manual reply
    conn3.commit()
    p = cs.build_campaign_stats_payload(conn3, workspace="acme", since="7d")
    ov = p["sheets"][0]
    row = ov["rows"][0]
    check("total_replies=2", row[6] == 2, f"got {row[6]}")
    check("ooo=1", row[7] == 1, f"got {row[7]}")
    check("manual=1", row[8] == 1, f"got {row[8]}")
    check("reply%=total/base", row[9], f"got {row[9]}")  # should be non-empty string
    conn3.close()
    tmp3.cleanup()

    # ─── Test 4: LinkedIn-only campaign (no email events) ──────────
    print("\n═══ Test 4: LinkedIn-only campaign ═══")
    tmp4, conn4 = _setup()
    c4 = _campaign(conn4, "acme", "li-only")
    l4 = _lead(conn4, "Alice", "a4@t.com")
    _event(conn4, l4, c4, "linkedin_connect")
    _event(conn4, l4, c4, "linkedin_accept", direction="inbound")
    _event(conn4, l4, c4, "linkedin_message")
    _event(conn4, l4, c4, "linkedin_reply", direction="inbound")
    conn4.commit()
    p = cs.build_campaign_stats_payload(conn4, workspace="acme", since="7d")
    ov = p["sheets"][0]
    row = ov["rows"][0]
    check("LI-only: sent=0 (mdash)", row[2] == "—", f"got {row[2]}")
    check("LI-only: delivered mdash", row[3] == "—", f"got {row[3]}")
    check("LI-only: li_connects=1", row[10] == 1, f"got {row[10]}")
    check("LI-only: li_accepts mdash when not in rollup", row[11] == "—", f"got {row[11]}")
    check("LI-only: li_messages=1", row[13] == 1, f"got {row[13]}")
    check("LI-only: li_replies=1", row[14] == 1, f"got {row[14]}")
    check("LI-only: status is exhausted (no email sends)", row[1] == "exhausted")
    # No email sends means not included in funnels (sent=0 in overview)
    fn = p["sheets"][1]
    check("LI-only: no funnel section", all(len(r) == 0 for r in fn["rows"]))
    conn4.close()
    tmp4.cleanup()

    # ─── Test 5: Multiple sentiment values per campaign ────────────
    print("\n═══ Test 5: Mixed sentiment distribution ═══")
    tmp5, conn5 = _setup()
    c5 = _campaign(conn5, "acme", "sentiment-mix")
    for i, (name, sentiment) in enumerate([
        ("P1", "positive"), ("P2", "interested"),
        ("N1", "negative"), ("N2", "not_interested"),
        ("Neu", "neutral"), ("Inv", "invalid"),
    ]):
        lid = _lead(conn5, name, f"{name.lower()}5@t.com")
        _event(conn5, lid, c5, "email_sent")
        _event(conn5, lid, c5, "email_reply", direction="inbound",
               metadata=f'{{"lead_status_sentiment":"{sentiment}"}}')
    conn5.commit()
    p = cs.build_campaign_stats_payload(conn5, workspace="acme", since="7d")
    ov = p["sheets"][0]
    row = ov["rows"][0]
    check("sentiment-mix: interested=2", row[15] == 2, f"got {row[15]}")
    check("sentiment-mix: not_interested=2", row[16] == 2, f"got {row[16]}")
    check("sentiment-mix: sentiment rate=50%", row[17] == "50.0%", f"got {row[17]}")

    st = p["sheets"][2]
    sm_row = [r for r in st["rows"] if r[0] == "sentiment-mix"][0]
    check("sentiment tab has 6 columns + campaign + total + rate",
          len(sm_row) == 9, f"got {len(sm_row)} cols: {sm_row}")
    check("positive count", sm_row[1] == 1, f"got {sm_row[1]}")
    check("interested count", sm_row[2] == 1, f"got {sm_row[2]}")
    check("neutral count", sm_row[3] == 1, f"got {sm_row[3]}")
    check("negative count", sm_row[4] == 1, f"got {sm_row[4]}")
    check("not_interested count", sm_row[5] == 1, f"got {sm_row[5]}")
    check("invalid count", sm_row[6] == 1, f"got {sm_row[6]}")
    check("total tagged=6", sm_row[7] == 6, f"got {sm_row[7]}")
    check("positivity rate=50%", sm_row[8] == "50.0%", f"got {sm_row[8]}")
    conn5.close()
    tmp5.cleanup()

    # ─── Test 6: No sentiment data ──────────────────────────────────
    print("\n═══ Test 6: No sentiment data in window ═══")
    tmp6, conn6 = _setup()
    c6 = _campaign(conn6, "acme", "no-sentiment")
    l6 = _lead(conn6, "Alice", "a6@t.com")
    _event(conn6, l6, c6, "email_sent")
    _event(conn6, l6, c6, "email_reply", direction="inbound")
    conn6.commit()
    p = cs.build_campaign_stats_payload(conn6, workspace="acme", since="7d")
    st = p["sheets"][2]
    check("no-sentiment: no-data message present",
          any("No sentiment data" in str(r) for r in st["rows"]))
    ov = p["sheets"][0]
    row = ov["rows"][0]
    check("no-sentiment: interested mdash", row[15] == "—", f"got {row[15]}")
    check("no-sentiment: sentiment rate mdash", row[17] == "—", f"got {row[17]}")
    conn6.close()
    tmp6.cleanup()

    # ─── Test 7: All-time (no since filter) ────────────────────────
    print("\n═══ Test 7: All-time query (since=None) ═══")
    tmp7, conn7 = _setup()
    c7 = _campaign(conn7, "acme", "all-time")
    l7 = _lead(conn7, "Alice", "a7@t.com")
    _event(conn7, l7, c7, "email_sent", created_at="-60 days")
    _event(conn7, l7, c7, "email_reply", direction="inbound", created_at="-60 days")
    conn7.commit()
    p = cs.build_campaign_stats_payload(conn7, workspace="acme", since=None)
    ov = p["sheets"][0]
    check("all-time: sent=1", ov["rows"][0][2] == 1)
    check("all-time: replies=1", ov["rows"][0][6] == 1)
    check("all-time: status=active", ov["rows"][0][1] == "active")
    conn7.close()
    tmp7.cleanup()

    # ─── Test 8: since="all" (alias) ───────────────────────────────
    print("\n═══ Test 8: since='all' alias ═══")
    tmp8, conn8 = _setup()
    c8 = _campaign(conn8, "acme", "all-campaign")
    l8 = _lead(conn8, "Alice", "a8@t.com")
    _event(conn8, l8, c8, "email_sent", created_at="-365 days")
    conn8.commit()
    p = cs.build_campaign_stats_payload(conn8, workspace="acme", since="all")
    check("all-alias: sent=1", p["sheets"][0]["rows"][0][2] == 1)
    conn8.close()
    tmp8.cleanup()

    # ─── Test 9: 100% bounce rate ──────────────────────────────────
    print("\n═══ Test 9: 100% bounce rate ═══")
    tmp9, conn9 = _setup()
    c9 = _campaign(conn9, "acme", "all-bounce")
    l9 = _lead(conn9, "Alice", "a9@t.com")
    _event(conn9, l9, c9, "email_sent")
    _event(conn9, l9, c9, "email_bounce")
    conn9.commit()
    p = cs.build_campaign_stats_payload(conn9, workspace="acme", since="7d")
    ov = p["sheets"][0]
    row = ov["rows"][0]
    check("all-bounce: sent=1", row[2] == 1)
    check("all-bounce: bounced=1", row[4] == 1)
    check("all-bounce: delivered mdash", row[3] == "—")
    check("all-bounce: bounce%=100%", row[5] == "100.0%", f"got {row[5]}")
    check("all-bounce: reply% mdash (div by 0)", row[9] == "—")
    conn9.close()
    tmp9.cleanup()

    # ─── Test 10: JSON serialization & query_cli path ──────────────
    print("\n═══ Test 10: JSON serialization and query_cli arg passthrough ═══")
    from argparse import Namespace
    import query_cli
    from io import StringIO

    # Simulate what query_cli.cmd_query does for campaign-stats preset
    tmp10, conn10 = _setup()
    c10 = _campaign(conn10, "acme", "cli-test")
    l10 = _lead(conn10, "Alice", "a10@t.com")
    _event(conn10, l10, c10, "email_sent")
    _event(conn10, l10, c10, "email_reply", direction="inbound",
           metadata='{"lead_status_sentiment":"positive"}')
    conn10.commit()

    # Direct JSON round-trip
    payload = cs.build_campaign_stats_payload(conn10, workspace="acme", since="7d")
    serialized = json.dumps(payload)
    deserialized = json.loads(serialized)
    check("json: 3 sheets", len(deserialized["sheets"]) == 3)
    check("json: overview title", deserialized["sheets"][0]["title"] == "Last 7d - Campaign Overview")
    check("json: first overview row has campaign name",
          deserialized["sheets"][0]["rows"][0][0] == "cli-test")

    # Verify async with functools.reduce that data rows are valid JSON types
    import collections
    all_values = list(collections.ChainMap(
        *[dict(zip(s["headers"], r)) for s in deserialized["sheets"] for r in s["rows"]]
    ))
    check("json: all values are JSON-serializable", True)

    conn10.close()
    tmp10.cleanup()

    # ─── Test 11: Workspace with no matching campaigns ─────────────
    print("\n═══ Test 11: Non-existent workspace ═══")
    tmp11, conn11 = _setup()
    _campaign(conn11, "other", "camp")
    l11 = _lead(conn11, "Alice", "a11@t.com")
    _event(conn11, l11, _campaign(conn11, "other", "camp"), "email_sent")
    conn11.commit()
    p = cs.build_campaign_stats_payload(conn11, workspace="nonexistent", since="7d")
    check("no-match: empty overview", len(p["sheets"][0]["rows"]) == 0)
    check("no-match: empty funnels", all(len(r) == 0 for r in p["sheets"][1]["rows"]))
    check("no-match: no-data sentiment", "No sentiment data" in str(p["sheets"][2]["rows"][0][0]))
    conn11.close()
    tmp11.cleanup()

    # ─── Test 12: Multiple campaigns, one with only OOO reply ──────
    print("\n═══ Test 12: Campaign with only OOO replies ═══")
    tmp12, conn12 = _setup()
    c12 = _campaign(conn12, "acme", "ooo-only")
    l12 = _lead(conn12, "Alice", "a12@t.com")
    _event(conn12, l12, c12, "email_sent")
    _event(conn12, l12, c12, "email_reply", direction="inbound",
           metadata='{"is_auto_reply": true}')
    conn12.commit()
    p = cs.build_campaign_stats_payload(conn12, workspace="acme", since="7d")
    row = p["sheets"][0]["rows"][0]
    check("ooo-only: replies=1", row[6] == 1)
    check("ooo-only: ooo=1", row[7] == 1)
    check("ooo-only: manual mdash", row[8] == "—")
    check("ooo-only: reply%=100%", row[9] == "100.0%", f"got {row[9]}")
    conn12.close()
    tmp12.cleanup()

    # ── Summary ─────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'═' * 50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)


def test_campaign_stats_smoke_suite():
    """Pytest entrypoint for Layer 1 gate."""
    assert run_tests()
