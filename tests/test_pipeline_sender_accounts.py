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
    "69a7a45f466b7294895bb770,Gabriel,Price,MICROSOFT365,2,ACTIVE,gabriel@acmemail.com,"
    "Wed Mar 04 2026 03:17:51 GMT+0000 (Coordinated Universal Time),ACTIVE,PASS,PASS,PASS,"
    "Sat Mar 07 2026 07:17:33 GMT+0000 (Coordinated Universal Time),,,,,,,5,joyous-excited,"
    "100,100,100,100,10,10,9.76,8.51,-1,0,provider_inboxkit-azure;capacity_high;acme_all;acme_segment_a"
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
    assert row["email"] == "gabriel@acmemail.com"
    assert row["external_id"] == "69a7a45f466b7294895bb770"
    assert row["overall_health_score"] == 100
    assert row["bounce_rate"] == -1.0
    assert row["source_created_at"] == "2026-03-04T03:17:51+00:00"
    assert json.loads(row["tags_json"]) == [
        "provider_inboxkit-azure", "capacity_high", "acme_all", "acme_segment_a",
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


def test_import_sender_accounts_no_workspace_leaves_unlinked(tmp_path):
    """Tag-based workspace inference was removed -- tags in the CSV export
    (e.g. 'acme_all') are never parsed for workspace linking. Linking is
    always explicit, either via --workspace at import time or afterward via
    `sender-accounts link`."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace

    pm.init_db()
    create_workspace("Acme", slug="acme")
    csv_path = str(_write_fixture_csv(tmp_path))

    result = psa.import_sender_accounts(csv_path)
    assert result["workspace_links"] == 0

    conn = get_conn()
    links = conn.execute("SELECT * FROM workspace_sender_accounts").fetchall()
    conn.close()
    assert links == []


def test_import_sender_accounts_explicit_workspace_links(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace

    pm.init_db()
    create_workspace("Acme", slug="acme")
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


def test_set_sender_account_workspace_link_link_and_unlink(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace

    pm.init_db()
    create_workspace("Acme", slug="acme")
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path)

    result = psa.set_sender_account_workspace_link("gabriel@acmemail.com", "acme", linked=True)
    assert result["status"] == "ok"

    conn = get_conn()
    links = conn.execute(
        """SELECT w.slug FROM workspace_sender_accounts wsa
           INNER JOIN workspaces w ON w.id = wsa.workspace_id"""
    ).fetchall()
    assert [r["slug"] for r in links] == ["acme"]

    result = psa.set_sender_account_workspace_link("gabriel@acmemail.com", "acme", linked=False)
    assert result["status"] == "ok"
    links = conn.execute("SELECT * FROM workspace_sender_accounts").fetchall()
    conn.close()
    assert links == []


def test_set_sender_account_workspace_link_unknown_email_or_workspace(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from pipeline_workspace import create_workspace

    pm.init_db()
    create_workspace("Acme", slug="acme")

    result = psa.set_sender_account_workspace_link("nobody@nowhere.com", "acme")
    assert result["status"] == "error"

    result = psa.set_sender_account_workspace_link("gabriel@acmemail.com", "unknown-workspace")
    assert result["status"] == "error"


def test_update_sender_account_edits_only_provided_fields(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path)

    result = psa.update_sender_account("gabriel@acmemail.com", provider="azure", daily_limit=5)
    assert result["status"] == "ok"
    assert sorted(result["updated"]) == ["daily_limit", "provider"]

    conn = get_conn()
    row = dict(conn.execute(
        "SELECT provider, daily_limit, first_name FROM sender_accounts WHERE email = 'gabriel@acmemail.com'"
    ).fetchone())
    conn.close()
    assert row["provider"] == "azure"
    assert row["daily_limit"] == 5
    assert row["first_name"] == "Gabriel"  # untouched


def test_update_sender_account_rejects_non_editable_field(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    result = psa.update_sender_account("gabriel@acmemail.com", overall_health_score=100)
    assert result["status"] == "error"
    assert "overall_health_score" in result["error"]


def test_update_sender_account_unknown_email(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    result = psa.update_sender_account("nobody@nowhere.com", provider="azure")
    assert result["status"] == "error"


def test_update_sender_account_no_fields(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    result = psa.update_sender_account("gabriel@acmemail.com")
    assert result["status"] == "error"


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
        "SELECT id FROM sender_accounts WHERE email = 'gabriel@acmemail.com'"
    ).fetchone()["id"]

    entity_key = psa.sender_account_entity_key(conn, sa_id)
    assert entity_key == "sender_account:gabriel@acmemail.com"

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

    assert row["email"] == "gabriel@acmemail.com"
    assert row["overall_health_score"] == 100
    assert row["bounce_rate"] == -1.0
    assert row["provider"] == "MICROSOFT365"
    assert json.loads(row["tags_json"]) == [
        "provider_inboxkit-azure", "capacity_high", "acme_all", "acme_segment_a",
    ]


def test_sync_payload_includes_workspace_slugs(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace

    pm.init_db()
    create_workspace("Acme", slug="acme")
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path, workspace="acme")

    conn = get_conn()
    sa_id = psa.find_sender_account_id_by_email(conn, "gabriel@acmemail.com")
    payload = psa.build_sender_account_sync_payload(conn, sa_id)
    conn.close()

    assert payload["workspace_slugs"] == ["acme"]


def test_apply_sync_payload_reconciles_workspace_links_full_set(tmp_path):
    """Pull applies the incoming workspace_slugs as the full current state --
    links present locally but absent from the incoming set get removed,
    links present in the incoming set but missing locally get added."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace

    pm.init_db()
    ws_acme = create_workspace("Acme", slug="acme")
    ws_other = create_workspace("Other Client", slug="other-client")
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path, workspace="acme")

    conn = get_conn()
    sa_id = psa.find_sender_account_id_by_email(conn, "gabriel@acmemail.com")

    # Incoming payload says this account is now linked to other-client only.
    psa.apply_agent_sender_account_sync_payload(
        sa_id, {"workspace_slugs": ["other-client"]}, conn=conn,
    )
    conn.commit()

    links = {
        r["workspace_id"] for r in conn.execute(
            "SELECT workspace_id FROM workspace_sender_accounts WHERE sender_account_id = ?", (sa_id,),
        ).fetchall()
    }
    conn.close()
    assert links == {ws_other["id"]}
    assert ws_acme["id"] not in links


def test_apply_sync_payload_skips_unknown_workspace_slug(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path)

    conn = get_conn()
    sa_id = psa.find_sender_account_id_by_email(conn, "gabriel@acmemail.com")
    # Should not raise even though "not-yet-synced" isn't a local workspace.
    psa.apply_agent_sender_account_sync_payload(
        sa_id, {"workspace_slugs": ["not-yet-synced"]}, conn=conn,
    )
    conn.commit()
    links = conn.execute(
        "SELECT * FROM workspace_sender_accounts WHERE sender_account_id = ?", (sa_id,),
    ).fetchall()
    conn.close()
    assert links == []


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


def test_compute_sender_stats_since_shorthand_and_param_binding(tmp_path):
    """Regression test: `since` params must line up with their placeholders
    (previously the event_type IN(...) params and the since param were
    concatenated in the wrong order, so any --since value silently forced
    sent_count to 0), and shorthand like '7d'/'48h' must be understood
    rather than compared as a raw string against an ISO timestamp."""
    import pipeline as om
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_tags import log_event

    pm.init_db()
    lead = om.resolve_lead(name="Recipient", email="recipient@example.com")
    lead_id = int(lead["id"])
    sender = "sender@ourcompany.com"

    conn = get_conn()
    log_event(lead_id, "email_sent", direction="outbound", sender=sender, conn=conn, commit=False)
    log_event(lead_id, "email_sent", direction="outbound", sender=sender, conn=conn, commit=False)
    log_event(lead_id, "email_reply", direction="inbound", sender=sender, conn=conn, commit=False)
    conn.commit()

    stats_7d = psa.compute_sender_stats(conn, sender, since="7d")
    assert stats_7d["sent_count"] == 2
    assert stats_7d["reply_count"] == 1

    stats_48h = psa.compute_sender_stats(conn, sender, since="48h")
    assert stats_48h["sent_count"] == 2

    # An absolute since-date in the future must exclude everything.
    stats_future = psa.compute_sender_stats(conn, sender, since="2099-01-01")
    conn.close()
    assert stats_future["sent_count"] == 0
    assert stats_future["reply_count"] == 0


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


def test_ensure_sender_account_classifies_linkedin_profile_url(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    conn = get_conn()
    sa_id = psa.ensure_sender_account(conn, "https://linkedin.com/in/bill_smith", channel="linkedin")
    conn.commit()
    row = dict(conn.execute(
        "SELECT email, linkedin_url, linkedin_sales_nav_id, email_domain FROM sender_accounts WHERE id = ?",
        (sa_id,),
    ).fetchone())
    conn.close()
    assert row["linkedin_url"] == "linkedin.com/in/bill_smith"
    assert row["linkedin_sales_nav_id"] is None
    assert row["email_domain"] is None


def test_ensure_sender_account_classifies_sales_nav_url(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    conn = get_conn()
    sa_id = psa.ensure_sender_account(
        conn,
        "https://www.linkedin.com/sales/lead/ACwAAESGK48Bob7WfQ1v_tXsyB6LCbqVCD5dvUg,NAME_SEARCH,4S2R",
        channel="linkedin",
    )
    conn.commit()
    row = dict(conn.execute(
        "SELECT linkedin_url, linkedin_sales_nav_id FROM sender_accounts WHERE id = ?", (sa_id,),
    ).fetchone())
    conn.close()
    assert row["linkedin_url"] is None
    assert row["linkedin_sales_nav_id"] == "ACwAAESGK48Bob7WfQ1v_tXsyB6LCbqVCD5dvUg"


def test_ensure_sender_account_linkedin_channel_with_email_identifier(tmp_path):
    """A LinkedIn seat can be identified by its login email in some sources --
    email_domain should still populate even though channel is 'linkedin'."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    conn = get_conn()
    sa_id = psa.ensure_sender_account(conn, "rep-seat@ourcompany.com", channel="linkedin")
    conn.commit()
    row = dict(conn.execute(
        "SELECT email_domain, linkedin_url FROM sender_accounts WHERE id = ?", (sa_id,),
    ).fetchone())
    conn.close()
    assert row["email_domain"] == "ourcompany.com"
    assert row["linkedin_url"] is None


def test_import_sender_accounts_sets_email_domain(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    csv_path = str(_write_fixture_csv(tmp_path))
    psa.import_sender_accounts(csv_path)

    conn = get_conn()
    domain = conn.execute(
        "SELECT email_domain FROM sender_accounts WHERE email = 'gabriel@acmemail.com'"
    ).fetchone()["email_domain"]
    conn.close()
    assert domain == "acmemail.com"


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
        sender="gabriel@acmemail.com", conn=conn, commit=False,
    )
    conn.commit()

    insights = psa.sender_insights(conn)
    conn.close()

    assert len(insights) == 1
    item = insights[0]
    assert item["email"] == "gabriel@acmemail.com"
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
    ws = create_workspace("Acme", slug="acme")
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

    assert [r["slug"] for r in links] == ["acme"]


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
        sender="gabriel@acmemail.com", conn=conn, commit=True,
    )

    count = conn.execute(
        "SELECT COUNT(*) FROM sender_accounts WHERE email = 'gabriel@acmemail.com'"
    ).fetchone()[0]
    row = dict(conn.execute(
        "SELECT overall_health_score, bounce_rate FROM sender_accounts WHERE email = 'gabriel@acmemail.com'"
    ).fetchone())
    conn.close()

    assert count == 1
    assert row["overall_health_score"] == 100
    assert row["bounce_rate"] == -1.0


def _import_two_accounts_on_domain(tmp_path, domain="acmemail.com"):
    """Two sender accounts on the same domain, for domain-level cost tests."""
    import pipeline_sender_accounts as psa

    header = _CSV_HEADER
    row_a = _CSV_ROW.replace("gabriel@acmemail.com", f"gabriel@{domain}")
    row_b = (
        row_a.replace("69a7a45f466b7294895bb770", "other-id")
        .replace(f"gabriel@{domain}", f"samuel@{domain}")
    )
    path = tmp_path / "two_accounts.csv"
    path.write_text(header + "\n" + row_a + "\n" + row_b + "\n", encoding="utf-8")
    return psa.import_sender_accounts(str(path))


def test_sender_domains_report_computes_live_count_and_cost_per_account(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    _import_two_accounts_on_domain(tmp_path)
    psa.set_sender_domain_cost("acmemail.com", reseller="inboxkit", domain_cost=7.0)

    report = psa.sender_domains_report()
    assert len(report) == 1
    domain = report[0]
    assert domain["domain"] == "acmemail.com"
    assert domain["sender_count"] == 2
    assert domain["reseller"] == "inboxkit"
    assert domain["domain_cost"] == 7.0
    assert domain["cost_per_account"] == 3.5


def test_sender_domains_report_reflects_live_count_as_accounts_change(tmp_path):
    """Sender count is computed live, not stored -- adding a third account
    on the same domain must change the reported count/cost-per-account
    without any manual update to sender_domains."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    _import_two_accounts_on_domain(tmp_path)
    psa.set_sender_domain_cost("acmemail.com", domain_cost=7.0)

    conn = get_conn()
    conn.execute(
        "INSERT INTO sender_accounts (org_id, email, channel, email_domain) VALUES ('default', ?, 'email', ?)",
        ("third@acmemail.com", "acmemail.com"),
    )
    conn.commit()
    conn.close()

    domain = psa.sender_domains_report()[0]
    assert domain["sender_count"] == 3
    assert round(domain["cost_per_account"], 4) == round(7.0 / 3, 4)


def test_sender_domains_report_includes_zero_account_domains(tmp_path):
    """A domain registered ahead of any sender accounts (owned but not yet
    in use) must still show up in the report, with sender_count=0 --
    previously invisible since the query started FROM sender_accounts."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    psa.set_sender_domain_cost("futuredomain.com", reseller="inboxkit", notes="reserved for Q3")

    report = psa.sender_domains_report()
    assert len(report) == 1
    domain = report[0]
    assert domain["domain"] == "futuredomain.com"
    assert domain["sender_count"] == 0
    assert domain["reseller"] == "inboxkit"
    assert domain["notes"] == "reserved for Q3"
    assert domain["cost_per_account"] is None  # no accounts to divide by


def test_sender_domains_report_includes_domains_from_both_sides(tmp_path):
    """Union covers domains only known via sender_accounts (not yet cost-tracked)
    and domains only known via sender_domains (no accounts yet)."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    _import_two_accounts_on_domain(tmp_path)  # acmemail.com, no cost set
    psa.set_sender_domain_cost("futuredomain.com", reseller="inboxkit")  # no accounts

    domains = {d["domain"]: d for d in psa.sender_domains_report()}
    assert set(domains) == {"acmemail.com", "futuredomain.com"}
    assert domains["acmemail.com"]["sender_count"] == 2
    assert domains["acmemail.com"]["reseller"] is None
    assert domains["futuredomain.com"]["sender_count"] == 0


def test_sender_domain_notes_set_and_overwrite(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    psa.set_sender_domain_cost("acmemail.com", notes="blacklisted in Azure")
    report = psa.sender_domains_report()
    assert report[0]["notes"] == "blacklisted in Azure"

    psa.set_sender_domain_cost("acmemail.com", notes="delisted, back to normal")
    report = psa.sender_domains_report()
    assert report[0]["notes"] == "delisted, back to normal"


def test_workspace_sender_cost_report_all_time_vs_windowed_differ_for_older_domain(tmp_path):
    """A domain tracked for ~3 months should show all_time cost roughly 3x
    the per-month (windowed, months=1) figure, since domain_cost is a
    monthly rate and all_time projects it across elapsed months."""
    import pipeline as om
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace
    from workspace_routing import upsert_workspace_lead

    pm.init_db()
    ws = create_workspace("Acme", slug="acme")
    _import_two_accounts_on_domain(tmp_path, domain="acmemail.com")
    psa.set_sender_domain_cost("acmemail.com", domain_cost=10.0)

    conn = get_conn()
    conn.execute(
        "UPDATE sender_domains SET created_at = datetime('now', '-91 days') WHERE domain = 'acmemail.com'"
    )
    sa_id = psa.find_sender_account_id_by_email(conn, "gabriel@acmemail.com")
    psa.link_sender_account_to_workspace(conn, ws["id"], sa_id)
    conn.commit()
    lead = om.resolve_lead(name="Lead One", email="lead1@example.com")
    upsert_workspace_lead(
        conn, org_id="default", workspace_id=ws["id"], lead_id=int(lead["id"]),
        current_status_sentiment="positive",
    )
    conn.commit()
    conn.close()

    report = psa.workspace_sender_cost_report("acme")
    # monthly share = 10.0 / 2 accounts = 5.0; ~91 days elapsed = 3 months
    assert report["windowed"]["total_cost"] == 5.0
    assert report["all_time"]["total_cost"] == 15.0
    assert report["all_time"]["total_cost"] > report["windowed"]["total_cost"]


def test_workspace_sender_cost_report(tmp_path):
    import pipeline as om
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace
    from workspace_routing import upsert_workspace_lead

    pm.init_db()
    ws = create_workspace("Acme", slug="acme")
    _import_two_accounts_on_domain(tmp_path)
    psa.set_sender_domain_cost("acmemail.com", reseller="inboxkit", domain_cost=7.0)

    conn = get_conn()
    for email in ("gabriel@acmemail.com", "samuel@acmemail.com"):
        sa_id = psa.find_sender_account_id_by_email(conn, email)
        psa.link_sender_account_to_workspace(conn, ws["id"], sa_id)
    conn.commit()

    lead1 = om.resolve_lead(name="Lead One", email="lead1@example.com")
    lead2 = om.resolve_lead(name="Lead Two", email="lead2@example.com")
    upsert_workspace_lead(
        conn, org_id="default", workspace_id=ws["id"], lead_id=int(lead1["id"]),
        current_status_sentiment="positive",
    )
    upsert_workspace_lead(
        conn, org_id="default", workspace_id=ws["id"], lead_id=int(lead2["id"]),
        current_status_sentiment="neutral",
    )
    conn.commit()
    conn.close()

    report = psa.workspace_sender_cost_report("acme")
    assert report["status"] == "ok"
    assert report["sender_account_count"] == 2
    # Domain was just created, so "all time" (elapsed <1mo, floored to 1)
    # and "windowed" (default months=1) match exactly here.
    for key in ("all_time", "windowed"):
        assert report[key]["total_cost"] == 7.0
        assert report[key]["positive_sentiment_leads"] == 1
        assert report[key]["cost_per_positive"] == 7.0
    assert report["windowed"]["months"] == 1

    report_3mo = psa.workspace_sender_cost_report("acme", months=3)
    assert report_3mo["windowed"]["months"] == 3
    assert report_3mo["windowed"]["total_cost"] == 21.0
    assert report_3mo["windowed"]["positive_sentiment_leads"] == 1


def test_reseller_cost_report_across_domains(tmp_path):
    import pipeline as om
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn
    from pipeline_workspace import create_workspace
    from workspace_routing import upsert_workspace_lead

    pm.init_db()
    ws = create_workspace("Acme", slug="acme")
    _import_two_accounts_on_domain(tmp_path, domain="acmemail.com")
    psa.set_sender_domain_cost("acmemail.com", reseller="inboxkit", domain_cost=7.0)
    psa.set_sender_domain_cost("otherdomain.com", reseller="inboxkit", domain_cost=10.0)

    conn = get_conn()
    sa_id = psa.find_sender_account_id_by_email(conn, "gabriel@acmemail.com")
    psa.link_sender_account_to_workspace(conn, ws["id"], sa_id)
    conn.commit()
    lead = om.resolve_lead(name="Lead One", email="lead1@example.com")
    upsert_workspace_lead(
        conn, org_id="default", workspace_id=ws["id"], lead_id=int(lead["id"]),
        current_status_sentiment="positive",
    )
    conn.commit()
    conn.close()

    report = psa.reseller_cost_report("inboxkit")
    assert report["status"] == "ok"
    assert set(report["domains"]) == {"acmemail.com", "otherdomain.com"}
    assert report["workspaces_served"] == ["acme"]
    for key in ("all_time", "windowed"):
        assert report[key]["total_cost"] == 17.0
        assert report[key]["positive_sentiment_leads"] == 1
    assert report["windowed"]["months"] == 1

    report_3mo = psa.reseller_cost_report("inboxkit", months=3)
    assert report_3mo["windowed"]["total_cost"] == 51.0


def test_reseller_cost_report_unknown_reseller(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa

    pm.init_db()
    report = psa.reseller_cost_report("nonexistent")
    assert report["status"] == "error"


def test_sender_domain_entity_key_and_sync_payload_round_trip(tmp_path):
    """A domain's cost/reseller set on one client must reproduce identical
    data on another via the sender_domain_update sync action."""
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    psa.set_sender_domain_cost("acmemail.com", reseller="inboxkit", domain_cost=7.0, currency="USD")

    conn = get_conn()
    entity_key = psa.sender_domain_entity_key("acmemail.com")
    assert entity_key == "sender_domain:acmemail.com"

    payload = psa.build_sender_domain_sync_payload(conn, "acmemail.com")
    conn.close()
    # is_active travels so a decommissioned domain can be distinguished from a
    # never-registered one on the far side; a live domain ships is_active=1.
    assert payload == {"reseller": "inboxkit", "domain_cost": 7.0, "currency": "USD", "is_active": 1}

    # Simulate a different client applying the pulled payload to a bare row
    # it just created for this entity_key (mirrors ingest_agent_entry()'s
    # resolve-then-apply flow for sender_domain_update).
    conn2 = get_conn()
    conn2.execute("DELETE FROM sender_domains")
    conn2.commit()

    domain2 = psa.resolve_sender_domain_from_entity_key(conn2, entity_key)
    psa.apply_agent_sender_domain_sync_payload(domain2, payload, conn=conn2)
    conn2.commit()

    row = dict(conn2.execute("SELECT * FROM sender_domains WHERE domain = ?", (domain2,)).fetchone())
    conn2.close()

    assert row["domain"] == "acmemail.com"
    assert row["reseller"] == "inboxkit"
    assert row["domain_cost"] == 7.0
    assert row["currency"] == "USD"


def test_apply_sender_domain_sync_payload_only_updates_present_fields(tmp_path):
    import pipeline_migration as pm
    import pipeline_sender_accounts as psa
    from db_conn import get_conn

    pm.init_db()
    psa.set_sender_domain_cost("acmemail.com", reseller="inboxkit", domain_cost=7.0)

    conn = get_conn()
    psa.apply_agent_sender_domain_sync_payload("acmemail.com", {"domain_cost": 9.5}, conn=conn)
    conn.commit()
    row = dict(conn.execute("SELECT reseller, domain_cost FROM sender_domains WHERE domain = 'acmemail.com'").fetchone())
    conn.close()

    assert row["reseller"] == "inboxkit"
    assert row["domain_cost"] == 9.5
