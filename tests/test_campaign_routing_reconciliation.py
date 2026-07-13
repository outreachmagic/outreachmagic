#!/usr/bin/env python3
"""Tests for campaign-routing reconciliation (moving already-ingested rows).

Placeholder names only (acme, widgetco, clientx) -- public repo.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    WORKSPACE_ROUTING_MULTI,
    assign_campaign_map,
    deactivate_campaign_map,
    reconcile_workspace_routing,
)


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _add_lead(email, name="Lead", platform=None):
    lead_id = int(om.add_lead(name=name, email=email)["id"])
    if platform:
        conn = om.get_conn()
        conn.execute(
            "UPDATE leads SET latest_source_platform = ? WHERE id = ?", (platform, lead_id)
        )
        conn.commit()
        conn.close()
    return lead_id


def _add_event(conn, lead_id, campaign_id, event_type="email_sent", event_at="2026-05-01T00:00:00Z"):
    cur = conn.execute(
        "INSERT INTO events (lead_id, event_type, campaign_id, created_at) VALUES (?, ?, ?, ?)",
        (lead_id, event_type, campaign_id, event_at),
    )
    return cur.lastrowid


def _seed_ws_event(conn, workspace_id, lead_id, event_id, event_type="email_sent",
                   event_at="2026-05-01T00:00:00Z", idem=None):
    idem = idem or f"idem_{workspace_id}_{event_id}"
    conn.execute(
        """INSERT INTO workspace_lead_events
               (org_id, workspace_id, lead_id, event_id, event_type, event_at, idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (DEFAULT_ORG_ID, workspace_id, lead_id, event_id, event_type, event_at, idem),
    )
    conn.execute(
        """INSERT OR IGNORE INTO workspace_leads (id, org_id, workspace_id, lead_id, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'prospecting', datetime('now'), datetime('now'))""",
        (f"wl_{workspace_id}_{lead_id}", DEFAULT_ORG_ID, workspace_id, lead_id),
    )


def _setup_two_workspaces():
    _reset_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Acme", slug="acme")
    om.create_workspace("Widget Co", slug="widgetco")
    return "ws_acme", "ws_widgetco"


def test_dry_run_previews_without_mutating():
    ws_old, ws_new = _setup_two_workspaces()
    lead_id = _add_lead("dry@test.com")
    conn = om.get_conn()
    cid = om.ensure_campaign(conn, "acme summer", lead_id)
    ev = _add_event(conn, lead_id, cid)
    _seed_ws_event(conn, ws_old, lead_id, ev)
    # Current rule routes 'acme summer' to widgetco, but the event sits in acme.
    assign_campaign_map(conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_new,
                        campaign_name="acme", match_strategy="rule_contains")
    conn.commit()

    result = reconcile_workspace_routing(conn, DEFAULT_ORG_ID, dry_run=True)
    # Nothing moved.
    still_old = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_events WHERE workspace_id = ?", (ws_old,)
    ).fetchone()[0]
    conn.close()
    assert result["dry_run"] is True
    assert result["events_would_move"] == 1
    assert result["leads_would_move"] == 1
    assert result["events_moved"] == 0
    assert still_old == 1


def test_full_move_after_deactivating_shadow():
    ws_alpha, ws_beta = _setup_two_workspaces()
    lead_id = _add_lead("shadow@test.com")
    conn = om.get_conn()
    cid = om.ensure_campaign(conn, "acme summer", lead_id)
    ev = _add_event(conn, lead_id, cid)
    _seed_ws_event(conn, ws_alpha, lead_id, ev)
    # Stale name_exact -> alpha shadows a broader rule_contains -> beta.
    name_exact_id = assign_campaign_map(
        conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_alpha,
        campaign_name="acme summer", match_strategy="name_exact",
    )
    assign_campaign_map(conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_beta,
                        campaign_name="acme", match_strategy="rule_contains")
    conn.commit()

    # Step 1: reconcile while shadowed -> resolution still returns alpha, no move.
    pre = reconcile_workspace_routing(conn, DEFAULT_ORG_ID, dry_run=True)
    assert pre["events_would_move"] == 0

    # Step 2: deactivate the stale name_exact row.
    deactivate_campaign_map(conn, DEFAULT_ORG_ID, name_exact_id)
    conn.commit()

    # Step 3: reconcile for real -> event + lead move to beta.
    result = reconcile_workspace_routing(conn, DEFAULT_ORG_ID, dry_run=False)
    assert result["events_moved"] == 1
    assert result["leads_moved"] == 1
    in_beta = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_events WHERE workspace_id = ?", (ws_beta,)
    ).fetchone()[0]
    in_alpha = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_events WHERE workspace_id = ?", (ws_alpha,)
    ).fetchone()[0]
    lead_in_beta = conn.execute(
        "SELECT COUNT(*) FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?", (ws_beta, lead_id)
    ).fetchone()[0]
    lead_in_alpha = conn.execute(
        "SELECT COUNT(*) FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?", (ws_alpha, lead_id)
    ).fetchone()[0]
    conn.close()
    assert in_beta == 1 and in_alpha == 0
    assert lead_in_beta == 1 and lead_in_alpha == 0


def test_partial_move_lead_spans_two_workspaces():
    ws_old, ws_new = _setup_two_workspaces()
    lead_id = _add_lead("partial@test.com")
    conn = om.get_conn()
    # Two campaigns for the same lead, both currently ingested into ws_old.
    cid_move = om.ensure_campaign(conn, "acme summer", lead_id)
    cid_stay = om.ensure_campaign(conn, "clientx winter", lead_id)
    ev_move = _add_event(conn, lead_id, cid_move)
    ev_stay = _add_event(conn, lead_id, cid_stay)
    _seed_ws_event(conn, ws_old, lead_id, ev_move, idem="idem_move")
    _seed_ws_event(conn, ws_old, lead_id, ev_stay, idem="idem_stay")
    # Only 'acme summer' should move (rule_contains 'acme' -> widgetco). 'clientx winter' has no rule.
    assign_campaign_map(conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_new,
                        campaign_name="acme", match_strategy="rule_contains")
    conn.commit()

    result = reconcile_workspace_routing(conn, DEFAULT_ORG_ID, dry_run=False)
    assert result["events_moved"] == 1
    # Lead now present in both workspaces (spans them legitimately).
    in_old = conn.execute(
        "SELECT COUNT(*) FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?", (ws_old, lead_id)
    ).fetchone()[0]
    in_new = conn.execute(
        "SELECT COUNT(*) FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?", (ws_new, lead_id)
    ).fetchone()[0]
    ev_old = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_events WHERE workspace_id = ? AND lead_id = ?", (ws_old, lead_id)
    ).fetchone()[0]
    ev_new = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_events WHERE workspace_id = ? AND lead_id = ?", (ws_new, lead_id)
    ).fetchone()[0]
    conn.close()
    assert in_old == 1 and in_new == 1
    assert ev_old == 1 and ev_new == 1


def test_tags_and_linkedin_move_on_full_evacuation():
    ws_old, ws_new = _setup_two_workspaces()
    lead_id = _add_lead("tags@test.com")
    conn = om.get_conn()
    cid = om.ensure_campaign(conn, "acme summer", lead_id)
    ev = _add_event(conn, lead_id, cid)
    _seed_ws_event(conn, ws_old, lead_id, ev)
    conn.execute(
        "INSERT INTO workspace_lead_tags (id, workspace_id, lead_id, tag) VALUES (?, ?, ?, 'vip')",
        (f"wlt_{ws_old}_{lead_id}_x", ws_old, lead_id),
    )
    conn.execute(
        """INSERT INTO workspace_lead_linkedin_status (id, workspace_id, lead_id, sender_profile, is_connected)
           VALUES (?, ?, ?, 'sender-a', 1)""",
        (f"lis_{ws_old}_{lead_id}_a", ws_old, lead_id),
    )
    assign_campaign_map(conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_new,
                        campaign_name="acme", match_strategy="rule_contains")
    conn.commit()

    reconcile_workspace_routing(conn, DEFAULT_ORG_ID, dry_run=False)
    tag_new = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? AND tag = 'vip'",
        (ws_new, lead_id),
    ).fetchone()[0]
    tag_old = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?", (ws_old, lead_id)
    ).fetchone()[0]
    li_new = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_linkedin_status WHERE workspace_id = ? AND lead_id = ?",
        (ws_new, lead_id),
    ).fetchone()[0]
    li_old = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_linkedin_status WHERE workspace_id = ? AND lead_id = ?",
        (ws_old, lead_id),
    ).fetchone()[0]
    conn.close()
    assert tag_new == 1 and tag_old == 0
    assert li_new == 1 and li_old == 0


def test_events_with_no_campaign_counted_skipped():
    ws_old, ws_new = _setup_two_workspaces()
    lead_id = _add_lead("nocampaign@test.com")
    conn = om.get_conn()
    # Event with a NULL campaign_id -> no derivable campaign name.
    ev = _add_event(conn, lead_id, None)
    _seed_ws_event(conn, ws_old, lead_id, ev)
    assign_campaign_map(conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_new,
                        campaign_name="acme", match_strategy="rule_contains")
    conn.commit()

    result = reconcile_workspace_routing(conn, DEFAULT_ORG_ID, dry_run=False)
    still_old = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_events WHERE workspace_id = ?", (ws_old,)
    ).fetchone()[0]
    conn.close()
    assert result["skipped_no_campaign"] == 1
    assert result["events_moved"] == 0
    assert still_old == 1


def test_platform_and_workspace_filters():
    ws_old, ws_new = _setup_two_workspaces()
    lead_sl = _add_lead("sl@test.com", platform="smartlead")
    lead_pr = _add_lead("pr@test.com", platform="prosp")
    conn = om.get_conn()
    cid = om.ensure_campaign(conn, "acme summer", lead_sl)
    ev_sl = _add_event(conn, lead_sl, cid)
    ev_pr = _add_event(conn, lead_pr, cid)
    _seed_ws_event(conn, ws_old, lead_sl, ev_sl, idem="idem_sl")
    _seed_ws_event(conn, ws_old, lead_pr, ev_pr, idem="idem_pr")
    assign_campaign_map(conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_new,
                        campaign_name="acme", match_strategy="rule_contains")
    conn.commit()

    # platform filter (best-effort via leads.latest_source_platform): only smartlead lead moves.
    result = reconcile_workspace_routing(conn, DEFAULT_ORG_ID, platform_filter="smartlead", dry_run=False)
    assert result["events_moved"] == 1
    moved_pr = conn.execute(
        "SELECT COUNT(*) FROM workspace_lead_events WHERE workspace_id = ? AND lead_id = ?", (ws_new, lead_pr)
    ).fetchone()[0]
    assert moved_pr == 0

    # workspace filter: from a non-source workspace yields nothing to move.
    res2 = reconcile_workspace_routing(
        conn, DEFAULT_ORG_ID, from_workspace_id=ws_new, dry_run=True
    )
    conn.close()
    assert res2["events_would_move"] == 0


def test_sender_accounts_never_touched():
    ws_old, ws_new = _setup_two_workspaces()
    lead_id = _add_lead("sender@test.com")
    conn = om.get_conn()
    conn.execute(
        "INSERT INTO sender_accounts (id, email, email_domain) VALUES (1, 'from@acme.com', 'acme.com')"
    )
    conn.execute(
        "INSERT INTO workspace_sender_accounts (workspace_id, sender_account_id) VALUES (?, 1)", (ws_old,)
    )
    cid = om.ensure_campaign(conn, "acme summer", lead_id)
    ev = _add_event(conn, lead_id, cid)
    _seed_ws_event(conn, ws_old, lead_id, ev)
    assign_campaign_map(conn, DEFAULT_ORG_ID, source_platform="*", workspace_id=ws_new,
                        campaign_name="acme", match_strategy="rule_contains")
    conn.commit()

    reconcile_workspace_routing(conn, DEFAULT_ORG_ID, dry_run=False)
    link = conn.execute(
        "SELECT workspace_id FROM workspace_sender_accounts WHERE sender_account_id = 1"
    ).fetchone()
    conn.close()
    # The sender-account link is unchanged -- reconcile never derives it from routing.
    assert link["workspace_id"] == ws_old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
