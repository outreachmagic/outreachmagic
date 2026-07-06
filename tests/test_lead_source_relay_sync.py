#!/usr/bin/env python3
"""Lead source attribution round-trip via relay sync payloads (bug 8)."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
from lead_sync import (  # noqa: E402
    apply_agent_lead_core_payload,
    apply_agent_lead_workspace_payload,
    apply_attribution_from_sync_payload,
    build_lead_core_sync_payload,
    resolve_lead_from_agent_sync,
)
from workspace_routing import DEFAULT_ORG_ID  # noqa: E402


def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()


def test_sync_payload_includes_attribution_fields():
    _fresh_db()
    result = om.resolve_lead(
        email="attr@example.com",
        name="Attr Lead",
        company="Acme",
        source="sales_navigator",
        source_detail="Headshot Lounge batch",
        source_platform="csv",
    )
    lead_id = result["id"]
    conn = om.get_conn()
    payload = build_lead_core_sync_payload(conn, DEFAULT_ORG_ID, lead_id)
    conn.close()
    assert payload["original_source"] == "sales_navigator"
    assert payload["original_source_detail"] == "Headshot Lounge batch"
    assert payload["latest_source"] == "sales_navigator"
    assert payload["list_source"] == "Headshot Lounge batch"


def test_pull_restores_attribution_not_agent_sync():
    _fresh_db()
    payload = {
        "email": "restore@example.com",
        "name": "Restore Lead",
        "company": "Acme",
        "original_source": "nace_enrich",
        "original_source_detail": "lead-enrich/june-2026",
        "original_source_platform": "agent",
        "original_source_at": "2026-06-01T10:00:00Z",
        "latest_source": "nace_enrich",
        "latest_source_detail": "lead-enrich/june-2026",
        "latest_source_platform": "agent",
        "latest_source_at": "2026-06-01T10:00:00Z",
        "list_source": "lead-enrich/june-2026",
    }
    result = resolve_lead_from_agent_sync("restore@example.com", payload)
    assert result["status"] in ("created", "matched", "ok", "updated")
    lead_id = result["id"]
    conn = om.get_conn()
    row = conn.execute(
        "SELECT original_source, original_source_detail FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["original_source"] == "nace_enrich"
    assert row["original_source_detail"] == "lead-enrich/june-2026"


def test_apply_attribution_coalesce_preserves_original():
    _fresh_db()
    result = om.resolve_lead(
        email="keep@example.com",
        name="Keep Lead",
        source="csv_import",
        source_detail="first touch",
    )
    lead_id = result["id"]
    conn = om.get_conn()
    apply_attribution_from_sync_payload(
        conn,
        lead_id,
        {
            "original_source": "agent_sync",
            "original_source_detail": "relay replay",
            "latest_source": "sales_navigator",
            "latest_source_detail": "new list",
        },
    )
    conn.commit()
    row = conn.execute(
        "SELECT original_source, original_source_detail, latest_source, latest_source_detail "
        "FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["original_source"] == "csv_import"
    assert row["original_source_detail"] == "first touch"
    assert row["latest_source"] == "sales_navigator"
    assert row["latest_source_detail"] == "new list"


def test_apply_agent_core_payload_restores_attribution():
    _fresh_db()
    result = om.resolve_lead(email="core@example.com", name="Core Lead", source="agent_sync")
    lead_id = result["id"]
    conn = om.get_conn()
    apply_agent_lead_core_payload(
        lead_id,
        {
            "original_source": "csv_import",
            "original_source_detail": "email-finder/2.0",
            "latest_source": "csv_import",
            "latest_source_detail": "email-finder/2.0",
        },
        org_id=DEFAULT_ORG_ID,
        entity_key="core@example.com",
        conn=conn,
    )
    conn.commit()
    row = conn.execute(
        "SELECT original_source, original_source_detail FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["original_source"] == "csv_import"
    assert row["original_source_detail"] == "email-finder/2.0"


def test_reapplying_unchanged_location_does_not_bump_updated_at():
    """bug-pending-sync-self-bump.md Issue A: applying an identical location
    payload (a relay echo) must not touch updated_at, or the pending-sync
    count never settles."""
    _fresh_db()
    result = om.resolve_lead(email="loc@example.com", name="Loc Lead", source="csv")
    lead_id = result["id"]
    conn = om.get_conn()
    apply_agent_lead_core_payload(
        lead_id,
        {"location_city": "Austin", "location_state": "TX", "location_country": "US"},
        org_id=DEFAULT_ORG_ID,
        entity_key="loc@example.com",
        conn=conn,
    )
    conn.commit()
    conn.execute("UPDATE leads SET updated_at = '2020-01-01 00:00:00' WHERE id = ?", (lead_id,))
    conn.commit()

    apply_agent_lead_core_payload(
        lead_id,
        {"location_city": "Austin", "location_state": "TX", "location_country": "US"},
        org_id=DEFAULT_ORG_ID,
        entity_key="loc@example.com",
        conn=conn,
    )
    conn.commit()
    row = conn.execute("SELECT updated_at FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    assert row["updated_at"] == "2020-01-01 00:00:00"


def test_reapplying_identical_crm_entity_map_does_not_bump_updated_at():
    """bug-pending-sync-self-bump.md: crm_entity_map restore must be a no-op
    (no trigger-driven updated_at bump) when the mapping hasn't changed."""
    _fresh_db()
    result = om.resolve_lead(email="crm@example.com", name="CRM Lead", source="csv")
    lead_id = result["id"]
    conn = om.get_conn()
    ws_row = om.resolve_workspace_identity(conn, "default")
    om.upsert_workspace_lead(conn, DEFAULT_ORG_ID, ws_row["id"], lead_id)
    conn.commit()

    crm_payload = {
        "crm_entity_map": [{
            "platform": "ghl",
            "crm_contact_id": "contact-1",
            "crm_deal_id": "deal-1",
            "last_sync_status": "synced",
        }],
    }
    apply_agent_lead_workspace_payload(
        lead_id, crm_payload, org_id=DEFAULT_ORG_ID, workspace_id=ws_row["id"], conn=conn,
    )
    conn.commit()
    conn.execute("UPDATE leads SET updated_at = '2020-01-01 00:00:00' WHERE id = ?", (lead_id,))
    conn.execute(
        "UPDATE workspace_leads SET updated_at = '2020-01-01 00:00:00' WHERE workspace_id = ? AND lead_id = ?",
        (ws_row["id"], lead_id),
    )
    conn.commit()

    # Re-apply the identical mapping — should be a no-op, no trigger fire.
    apply_agent_lead_workspace_payload(
        lead_id, crm_payload, org_id=DEFAULT_ORG_ID, workspace_id=ws_row["id"], conn=conn,
    )
    conn.commit()
    lead_row = conn.execute("SELECT updated_at FROM leads WHERE id = ?", (lead_id,)).fetchone()
    ws_lead_row = conn.execute(
        "SELECT updated_at FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?",
        (ws_row["id"], lead_id),
    ).fetchone()
    conn.close()
    assert lead_row["updated_at"] == "2020-01-01 00:00:00"
    assert ws_lead_row["updated_at"] == "2020-01-01 00:00:00"

    # Sanity: a genuinely changed mapping DOES bump (trigger still works).
    conn = om.get_conn()
    changed_payload = {
        "crm_entity_map": [{
            "platform": "ghl",
            "crm_contact_id": "contact-2",
            "crm_deal_id": "deal-1",
            "last_sync_status": "synced",
        }],
    }
    apply_agent_lead_workspace_payload(
        lead_id, changed_payload, org_id=DEFAULT_ORG_ID, workspace_id=ws_row["id"], conn=conn,
    )
    conn.commit()
    lead_row_after = conn.execute("SELECT updated_at FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    assert lead_row_after["updated_at"] != "2020-01-01 00:00:00"
