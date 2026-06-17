#!/usr/bin/env python3
"""Quick smoke test: seeds a temp DB, runs the campaign-stats query, validates output.

Run from the repo root:
    cd outreachmagic-skill && python3 tests/test_campaign_stats_smoke.py
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


def main():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    om_paths.set_data_root_override(root)
    om_paths.set_project_root_override(root / "project")
    os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

    print("1. Initializing database...")
    om.init_db()
    conn = om.get_conn()

    print("2. Seeding test data...")
    # Campaigns
    conn.execute(
        "INSERT INTO campaigns (name) VALUES ('acme | alpha'), ('acme | beta'), ('acme | gamma')"
    )
    c_alpha = conn.execute(
        "SELECT id FROM campaigns WHERE name = 'acme | alpha'"
    ).fetchone()[0]
    c_beta = conn.execute(
        "SELECT id FROM campaigns WHERE name = 'acme | beta'"
    ).fetchone()[0]
    c_gamma = conn.execute(
        "SELECT id FROM campaigns WHERE name = 'acme | gamma'"
    ).fetchone()[0]

    # Leads
    conn.execute(
        """INSERT INTO leads (name, email, channel, stage)
           VALUES ('Alice', 'alice@acme.com', 'email', 'prospecting')"""
    )
    alice = conn.execute(
        "SELECT id FROM leads WHERE email = 'alice@acme.com'"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO leads (name, email, channel, stage)
           VALUES ('Bob', 'bob@acme.com', 'email', 'prospecting')"""
    )
    bob = conn.execute(
        "SELECT id FROM leads WHERE email = 'bob@acme.com'"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO leads (name, email, channel, stage)
           VALUES ('Carol', 'carol@acme.com', 'email', 'prospecting')"""
    )
    carol = conn.execute(
        "SELECT id FROM leads WHERE email = 'carol@acme.com'"
    ).fetchone()[0]

    # Events for alpha (active — lots of activity)
    events = [
        (alice, c_alpha, "email_sent", "outbound", "email", "now", '{}'),
        (alice, c_alpha, "email_reply", "inbound", "email", "now", '{}'),
        (bob, c_alpha, "email_sent", "outbound", "email", "now", '{}'),
        (bob, c_alpha, "email_reply", "inbound", "email", "now",
         '{"lead_status_sentiment":"positive","lead_status_display":"Interested"}'),
        (bob, c_alpha, "linkedin_connect", "outbound", "linkedin", "now", '{}'),
        (bob, c_alpha, "linkedin_accept", "inbound", "linkedin", "now", '{}'),
        (carol, c_alpha, "email_sent", "outbound", "email", "now", '{}'),
        (carol, c_alpha, "email_bounce", "outbound", "email", "now", '{}'),
    ]
    for lid, cid, etype, direction, channel, created, meta in events:
        conn.execute(
            """INSERT INTO events (lead_id, campaign_id, event_type, direction, channel, created_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, datetime(?), ?)""",
            (lid, cid, etype, direction, channel, created, meta),
        )

    # Events for beta (paused — had sends but none in window)
    conn.execute(
        "INSERT INTO events (lead_id, campaign_id, event_type, direction, channel, created_at) "
        "VALUES (?, ?, 'email_sent', 'outbound', 'email', datetime('now', '-30 days'))",
        (alice, c_beta),
    )
    conn.execute(
        "INSERT INTO events (lead_id, campaign_id, event_type, direction, channel, created_at) "
        "VALUES (?, ?, 'email_sent', 'outbound', 'email', datetime('now', '-30 days'))",
        (bob, c_beta),
    )

    # Gamma has zero events (exhausted)

    conn.commit()

    print("3. Running campaign-stats query (since=7d)...")
    payload = cs.build_campaign_stats_payload(conn, workspace="acme", since="7d")

    assert payload["template"] == "campaign-stats", f"Wrong template: {payload['template']}"
    assert payload["workspace"] == "acme"
    assert len(payload["sheets"]) == 3, f"Expected 3 sheets, got {len(payload['sheets'])}"

    # Validate Sheet 1: Overview
    ov = payload["sheets"][0]
    assert ov["title"] == "Campaign Overview", f"Wrong title: {ov['title']}"
    print(f"   Overview headers ({len(ov['headers'])}): {ov['headers']}")
    print(f"   Overview rows: {len(ov['rows'])}")
    for row in ov["rows"]:
        print(f"     {row[0]:20s} status={row[1]:10s} sent={row[2]} replies={row[6]}")

    # Find alpha row
    alpha_row = [r for r in ov["rows"] if r[0] == "alpha"]
    if not alpha_row:
        print(f"   DEBUG: first row r[0]={ov['rows'][0][0]!r}")
        print(f"   DEBUG: all row[0] values: {[r[0] for r in ov['rows']]}")
    assert len(alpha_row) == 1, f"Expected 1 alpha row, got {len(alpha_row)}, rows={[[r[0], r[1], r[2]] for r in ov['rows']]}"
    ar = alpha_row[0]
    assert ar[1] == "active", f"alpha should be active, got {ar[1]}"
    assert ar[2] == 3, f"alpha sent should be 3, got {ar[2]}"  # 3 sent
    assert ar[5].startswith("3"), f"alpha bounce % should be ~33%, got {ar[5]}"  # 1/3 bounced

    # Check alpha has linkedin counts
    assert ar[10] == 1, f"alpha LI connects should be 1, got {ar[10]}"

    # Check sentiment
    assert ar[15] == 1, f"alpha interested should be 1, got {ar[15]}"  # Bob is positive
    assert ar[16] == "—", f"alpha not_interested should be '—' (0 → mdash), got {ar[16]!r}"
    assert ar[17] == "100.0%", f"alpha sentiment_rate should be 100.0%, got {ar[17]!r}"

    # Find beta row
    beta_row = [r for r in ov["rows"] if r[0] == "beta"]
    assert len(beta_row) == 1
    assert beta_row[0][1] == "paused", f"beta should be paused, got {beta_row[0][1]}"
    assert beta_row[0][2] == "—", f"beta sent in window should be '—', got {beta_row[0][2]}"
    assert beta_row[0][6] == 0, f"beta total replies should be 0, got {beta_row[0][6]}"

    # Find gamma row
    gamma_row = [r for r in ov["rows"] if r[0] == "gamma"]
    assert len(gamma_row) == 1
    assert gamma_row[0][1] == "exhausted", f"gamma should be exhausted, got {gamma_row[0][1]}"

    # Validate Sheet 2: Funnels
    fn = payload["sheets"][1]
    assert fn["title"] == "Campaign Funnels", f"Wrong title: {fn['title']}"
    print(f"   Funnels rows: {len(fn['rows'])}")
    stage_rows = [r for r in fn["rows"] if len(r) > 0 and r[0] == "Stage"]
    assert len(stage_rows) == 1, f"Expected 1 funnel section, got {len(stage_rows)}"

    # Validate Sheet 3: Sentiment
    st = payload["sheets"][2]
    assert st["title"] == "Lead Sentiment", f"Wrong title: {st['title']}"
    print(f"   Sentiment rows: {len(st['rows'])}")
    for row in st["rows"]:
        print(f"     {row[0]:20s} positive={row[1]} interested={row[2]} negative={row[4]}")
    # Should have 1 campaign with sentiment data
    sentiment_campaigns = [r for r in st["rows"] if len(r) > 1 and r[1] != ""]
    assert len(sentiment_campaigns) == 1, f"Expected 1 campaign with sentiment, got {len(sentiment_campaigns)}"

    # Validate JSON serialization
    json_str = json.dumps(payload)
    assert len(json_str) > 100, "JSON output too short"
    print("   JSON serializes OK")

    conn.close()
    tmp.cleanup()

    print("\n✅ All campaign-stats smoke tests passed!")


if __name__ == "__main__":
    main()
