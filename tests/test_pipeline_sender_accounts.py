"""Sender account import, sync round-trip, and reply/bounce stat computation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SAMPLE_CSV = (
    ROOT.parent / "outreachmagic-brand" / "engineering" / "export-emailacc-20260712T05_39_56.csv"
)

_CSV_HEADER = (
    "_id,first_name,last_name,provider,daily_limit,warmup_status,email,created_at,status,"
    "SPF,DKIM,DMARC,warmup_enabled_date,username,smtp_username,smtp_host,smtp_port,imap_host,"
    "imap_port,warmup_max_daily_limit,warmup_custom_words,overall_hscore,google_hscore,"
    "microsoft_hscore,other_hscore,ooo_rr,ooo_rr_14,ooo_rr_30,ooo_rr_90,bounce_r,miss_warmup_r,tags"
)
_CSV_ROW = (
    "69a7a45f466b7294895bb770,Gabriel,Price,MICROSOFT365,2,ACTIVE,gabriel@rentpopcam.com,"
    "Wed Mar 04 2026 03:17:51 GMT+0000 (Coordinated Universal Time),ACTIVE,PASS,PASS,PASS,"
    "Sat Mar 07 2026 07:17:33 GMT+0000 (Coordinated Universal Time),,,,,,,5,joyous-excited,"
    "100,100,100,100,10,10,9.76,8.51,-1,0,provider_inboxkit-azure;capacity_high;popcam_all;popcam_segment_a"
)


def _write_fixture_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sender_accounts.csv"
    path.write_text(_CSV_HEADER + "\n" + _CSV_ROW + "\n", encoding="utf-8")
    return path


def test_parse_sender_accounts_csv_ignores_smtp_imap_and_splits_tags(tmp_path):
    import pipeline_sender_accounts as psa

    rows = psa.parse_sender_accounts_csv(str(_write_fixture_csv(tmp_path)))
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "gabriel@rentpopcam.com"
    assert row["external_id"] == "69a7a45f466b7294895bb770"
    assert row["overall_health_score"] == 100
    assert row["bounce_rate"] == -1.0
    assert row["source_created_at"] == "2026-03-04T03:17:51+00:00"
    assert json.loads(row["tags_json"]) == [
        "provider_inboxkit-azure", "capacity_high", "popcam_all", "popcam_segment_a",
    ]
    for smtp_col in ("smtp_username", "smtp_host", "imap_host", "username"):
        assert smtp_col not in row


def test_parse_sender_accounts_csv_matches_real_sample_export():
    import pipeline_sender_accounts as psa

    if not SAMPLE_CSV.exists():
        return  # sample export lives outside this repo; skip if unavailable
    rows = psa.parse_sender_accounts_csv(str(SAMPLE_CSV))
    assert len(rows) > 0
    assert all(r["email"] for r in rows)


def test_import_sender_accounts_is_idempotent_on_reimport(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    csv_path = str(_write_fixture_csv(tmp_path))

    first = psa.import_sender_accounts(csv_path)
    assert first == {"status": "ok", "total": 1, "created": 1, "updated": 0, "workspace_links": 0}

    second = psa.import_sender_accounts(csv_path)
    assert second == {"status": "ok", "total": 1, "created": 0, "updated": 1, "workspace_links": 0}

    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM sender_accounts").fetchone()[0]
    conn.close()
    assert count == 1


def test_import_sender_accounts_infers_workspace_from_tags(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace

    pm.init_db()
    create_workspace("Popcam", slug="popcam")
    csv_path = str(_write_fixture_csv(tmp_path))

    result = psa.import_sender_accounts(csv_path)
    assert result["workspace_links"] == 1

    conn = get_conn()
    links = conn.execute(
        """SELECT w.slug FROM workspace_sender_accounts wsa
           INNER JOIN workspaces w ON w.id = wsa.workspace_id"""
    ).fetchall()
    conn.close()
    assert [r["slug"] for r in links] == ["popcam"]


def test_import_sender_accounts_explicit_workspace_overrides_tag_inference(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace

    pm.init_db()
    create_workspace("Popcam", slug="popcam")
    create_workspace("Other Client", slug="other-client")
    csv_path = str(_write_fixture_csv(tmp_path))

    result = psa.import_sender_accounts(csv_path, workspace="other-client")
    assert result["workspace_links"] == 1

    conn = get_conn()
    links = conn.execute(
        """SELECT w.slug FROM workspace_sender_accounts wsa
           INNER JOIN workspaces w ON w.id = wsa.workspace_id"""
    ).fetchall()
    conn.close()
    assert [r["slug"] for r in links] == ["other-client"]


def test_entity_key_and_sync_payload_round_trip(tmp_path):
    """A payload built on one client must reproduce identical data on another."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path)

    conn = get_conn()
    sa_id = conn.execute(
        "SELECT id FROM sender_accounts WHERE email = 'gabriel@rentpopcam.com'"
    ).fetchone()["id"]

    entity_key = psa.sender_account_entity_key(conn, sa_id)
    assert entity_key == "sender_account:gabriel@rentpopcam.com"

    payload = psa.build_sender_account_sync_payload(conn, sa_id)
    conn.close()

    # Simulate a different client applying the pulled payload to a bare row
    # it just created for this entity_key (mirrors ingest_agent_entry()'s
    # resolve-then-apply flow for sender_account_update).
    conn2 = get_conn()
    conn2.execute("DELETE FROM sender_accounts")
    conn2.execute("DELETE FROM workspace_sender_accounts")
    conn2.commit()

    sa_id2 = psa.resolve_sender_account_from_entity_key(conn2, entity_key)
    psa.apply_agent_sender_account_sync_payload(sa_id2, payload, conn=conn2)
    conn2.commit()

    row = dict(conn2.execute("SELECT * FROM sender_accounts WHERE id = ?", (sa_id2,)).fetchone())
    conn2.close()

    assert row["email"] == "gabriel@rentpopcam.com"
    assert row["overall_health_score"] == 100
    assert row["bounce_rate"] == -1.0
    assert row["provider"] == "MICROSOFT365"
    assert json.loads(row["tags_json"]) == [
        "provider_inboxkit-azure", "capacity_high", "popcam_all", "popcam_segment_a",
    ]


def test_compute_sender_stats_reply_and_bounce_rate(tmp_path):
    import pipeline as om
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from bounces import record_bounce_event
    from db_conn import get_conn
    from pipeline_tags import log_event

    pm.init_db()
    lead = om.resolve_lead(name="Recipient", email="recipient@example.com")
    lead_id = int(lead["id"])
    sender = "sender@ourcompany.com"

    conn = get_conn()
    log_event(lead_id, "email_sent", direction="outbound", sender=sender, conn=conn, commit=False)
    log_event(lead_id, "email_sent", direction="outbound", sender=sender, conn=conn, commit=False)
    log_event(lead_id, "email_sent", direction="outbound", sender=sender, conn=conn, commit=False)
    log_event(lead_id, "email_reply", direction="inbound", sender=sender, conn=conn, commit=False)
    conn.commit()

    record_bounce_event(
        conn, lead_id=lead_id, event_id=None, platform="plusvibe",
        sender_email=sender, lead_email="recipient@example.com", payload={},
    )
    conn.commit()

    stats = psa.compute_sender_stats(conn, sender)
    conn.close()

    assert stats["sent_count"] == 3
    assert stats["reply_count"] == 1
    assert stats["reply_rate"] == round(1 / 3, 4)
    assert stats["bounce_count"] == 1
    assert stats["bounce_rate"] == round(1 / 3, 4)


def test_compute_sender_stats_no_events_returns_none_rates(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    conn = get_conn()
    stats = psa.compute_sender_stats(conn, "nobody@example.com")
    conn.close()

    assert stats == {
        "sent_count": 0, "reply_count": 0, "reply_rate": None,
        "bounce_count": 0, "bounce_rate": None,
    }


def test_sender_insights_combines_stored_metrics_with_computed_stats(tmp_path):
    import pipeline as om
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_tags import log_event

    pm.init_db()
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path)

    lead = om.resolve_lead(name="Recipient", email="recipient2@example.com")
    conn = get_conn()
    log_event(
        int(lead["id"]), "email_sent", direction="outbound",
        sender="gabriel@rentpopcam.com", conn=conn, commit=False,
    )
    conn.commit()

    insights = psa.sender_insights(conn)
    conn.close()

    assert len(insights) == 1
    item = insights[0]
    assert item["email"] == "gabriel@rentpopcam.com"
    assert item["overall_health_score"] == 100
    assert item["sent_count"] == 1


def test_log_event_auto_creates_sender_account_for_unknown_sender(tmp_path):
    """Warmup-only PlusVibe accounts get a CSV row, but accounts that have
    never appeared in an import (and every Prosp/LinkedIn account, which has
    no CSV export at all) must still get bootstrapped from event traffic."""
    import pipeline as om
    import pipeline_migration as pm
    from db_conn import get_conn
    from pipeline_tags import log_event

    pm.init_db()
    lead = om.resolve_lead(name="Recipient", email="recipient@example.com")
    conn = get_conn()

    log_event(
        int(lead["id"]), "email_sent", direction="outbound", channel="email",
        sender="newmailbox@ourcompany.com", conn=conn, commit=True,
    )
    log_event(
        int(lead["id"]), "linkedin_message", direction="outbound", channel="linkedin",
        sender="linkedin.com/in/our-rep", conn=conn, commit=True,
    )

    rows = {r["email"]: r["channel"] for r in conn.execute("SELECT email, channel FROM sender_accounts")}
    conn.close()

    assert rows == {"newmailbox@ourcompany.com": "email", "linkedin.com/in/our-rep": "linkedin"}


def test_log_event_links_auto_created_sender_account_to_lead_workspace(tmp_path):
    """Sender accounts bootstrapped from events inherit the workspace the
    triggering lead is already routed to (via the existing campaign_workspace_map
    -> workspace_leads pipeline) -- no separate campaign lookup needed."""
    import pipeline as om
    import pipeline_migration as pm
    from db_conn import get_conn
    from pipeline_tags import log_event
    from pipeline_workspace import create_workspace
    from workspace_routing import upsert_workspace_lead

    pm.init_db()
    ws = create_workspace("Popcam", slug="popcam")
    lead = om.resolve_lead(name="Recipient", email="recipient@example.com")
    lead_id = int(lead["id"])

    conn = get_conn()
    upsert_workspace_lead(conn, org_id="default", workspace_id=ws["id"], lead_id=lead_id)
    conn.commit()

    log_event(
        lead_id, "email_sent", direction="outbound", channel="email",
        sender="newmailbox@ourcompany.com", conn=conn, commit=True,
    )

    links = conn.execute(
        """SELECT w.slug FROM workspace_sender_accounts wsa
           INNER JOIN sender_accounts sa ON sa.id = wsa.sender_account_id
           INNER JOIN workspaces w ON w.id = wsa.workspace_id
           WHERE sa.email = 'newmailbox@ourcompany.com'"""
    ).fetchall()
    conn.close()

    assert [r["slug"] for r in links] == ["popcam"]


def test_csv_import_then_event_does_not_duplicate_or_clobber_rich_data(tmp_path):
    import pipeline as om
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_tags import log_event

    pm.init_db()
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path)

    lead = om.resolve_lead(name="Recipient", email="recipient3@example.com")
    conn = get_conn()
    log_event(
        int(lead["id"]), "email_sent", direction="outbound", channel="email",
        sender="gabriel@rentpopcam.com", conn=conn, commit=True,
    )

    count = conn.execute(
        "SELECT COUNT(*) FROM sender_accounts WHERE email = 'gabriel@rentpopcam.com'"
    ).fetchone()[0]
    row = dict(conn.execute(
        "SELECT overall_health_score, bounce_rate FROM sender_accounts WHERE email = 'gabriel@rentpopcam.com'"
    ).fetchone())
    conn.close()

    assert count == 1
    assert row["overall_health_score"] == 100
    assert row["bounce_rate"] == -1.0
