"""Coverage for the dashboard CRM-depth additions: Sales-Navigator URL
synthesis, lead-email add/promote (+ dedup identity), the provider re-run
guard, and the first-entry 'interested' daily aggregation."""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import dashboard_queries as dq  # noqa: E402
import lead_emails  # noqa: E402
import pipeline as om  # noqa: E402
import workspace_routing as wr  # noqa: E402
from pipeline_provider_attempts import provider_run_summary  # noqa: E402
from workspace_routing import (  # noqa: E402
    DEFAULT_ORG_ID,
    WORKSPACE_ROUTING_MULTI,
    append_workspace_event,
    resolve_workspace_identity,
)


@pytest.fixture(autouse=True)
def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Alpha", slug="alpha")


# ---- A1: Sales Navigator URL --------------------------------------------------

def test_build_sales_nav_url_from_token():
    assert wr.build_sales_nav_url("ACwAABc123def456ghi789") == \
        "https://www.linkedin.com/sales/lead/ACwAABc123def456ghi789"
    assert wr.build_sales_nav_url("not-a-token") is None


def test_linkedin_display_url_prefers_public_then_salesnav():
    assert wr.linkedin_display_url("linkedin.com/in/jane", None) == "https://linkedin.com/in/jane"
    assert wr.linkedin_display_url(None, "ACwAABc123def456ghi789") == \
        "https://www.linkedin.com/sales/lead/ACwAABc123def456ghi789"
    assert wr.linkedin_display_url(None, None) is None


# ---- A4: lead emails ----------------------------------------------------------

def test_add_and_promote_lead_email_swaps_primary():
    lid = om.add_lead("Jane", company="Acme", email="jane@acme.com")["id"]
    lead_emails.add_lead_email(lid, "jane.alt@acme.com")
    lead_emails.promote_lead_email(lid, "jane.alt@acme.com")
    emails = {e["email"]: e["is_primary"] for e in lead_emails.list_lead_emails(lid)["emails"]}
    assert emails["jane.alt@acme.com"] is True
    assert emails["jane@acme.com"] is False


def test_add_email_owned_by_another_lead_conflicts():
    a = om.add_lead("Jane", company="Acme", email="jane@acme.com")["id"]
    b = om.add_lead("Bob", company="Acme", email="bob@acme.com")["id"]
    with pytest.raises(lead_emails.LeadEmailError):
        lead_emails.add_lead_email(b, "jane@acme.com")
    assert a  # the address belongs to lead a


# ---- A5: provider re-run guard ------------------------------------------------

def test_provider_run_summary_flags_prior_attempt():
    lid = om.add_lead("Jane", company="Acme")["id"]
    conn = om.get_conn()
    from pipeline_provider_attempts import record_provider_attempt
    record_provider_attempt(conn, lid, "serper", status="not_found")
    conn.commit()
    summary = provider_run_summary(conn, lid)
    conn.close()
    assert summary["research"]["ran"] is True
    assert summary["email_finding"]["ran"] is False


# ---- E6: first-entry interested ----------------------------------------------

def test_interested_first_entry_resets_on_reentry():
    lid = om.add_lead("Jane", company="Acme", email="jane@acme.com")["id"]
    c0 = om.get_conn()
    wsid = resolve_workspace_identity(c0, "alpha")["id"]
    c0.close()

    def status(raw, at):
        eid = om.log_event(lead_id=lid, event_type="lead_status_updated",
                           direction="inbound", metadata={"lead_status_raw": raw})
        conn = om.get_conn()
        append_workspace_event(conn, DEFAULT_ORG_ID, wsid, lid, event_id=eid,
                               event_type="lead_status_updated", event_at=at,
                               idempotency_key=f"k{raw}{at}")
        conn.commit()
        conn.close()

    status("interested", "2026-07-01T10:00:00")
    status("not_interested", "2026-07-03T10:00:00")
    status("interested", "2026-07-04T10:00:00")
    conn = om.get_conn()
    by_day = dq._interested_first_entry_by_day(conn, wsid)
    conn.close()
    # counted once, on the re-entry date, not the original entry
    assert by_day == {"2026-07-04": 1}


def test_interested_not_counted_when_current_status_is_not_interested():
    lid = om.add_lead("Bob", company="Beta", email="bob@beta.com")["id"]
    c0 = om.get_conn()
    wsid = resolve_workspace_identity(c0, "alpha")["id"]
    c0.close()

    def status(raw, at):
        eid = om.log_event(lead_id=lid, event_type="lead_status_updated",
                           direction="inbound", metadata={"lead_status_raw": raw})
        conn = om.get_conn()
        append_workspace_event(conn, DEFAULT_ORG_ID, wsid, lid, event_id=eid,
                               event_type="lead_status_updated", event_at=at,
                               idempotency_key=f"k{raw}{at}")
        conn.commit()
        conn.close()

    status("interested", "2026-07-01T10:00:00")
    status("not_interested", "2026-07-02T10:00:00")
    conn = om.get_conn()
    by_day = dq._interested_first_entry_by_day(conn, wsid)
    conn.close()
    assert by_day == {}
