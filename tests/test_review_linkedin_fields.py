"""Review export linkedin_url / linkedin_sales_nav_id field support."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
import pipeline_lead_review as review  # noqa: E402
from workspace_routing import upsert_identity_alias  # noqa: E402

SALES = "ACwAABAK84YBQ6cs16Ta-YfqZidA8SX2ywuCxhI"


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_export_payload_custom_linkedin_fields():
    _reset_db()
    ws = om.create_workspace("Review LinkedIn", slug="df-li-review")
    ws_id = f"ws_{ws['slug']}"
    r = om.resolve_lead(
        name="Sam Nav",
        email="sam@example.com",
        linkedin_url="https://www.linkedin.com/in/sam-nav-handle",
    )
    lead_id = int(r["id"])
    conn = om.get_conn()
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
    upsert_identity_alias(
        conn, om.DEFAULT_ORG_ID, lead_id,
        "linkedin_sales_nav_id", SALES, source="csv",
    )
    conn.commit()
    conn.close()

    conn = om.get_conn()
    payload = review.build_export_payload(
        conn,
        workspace="df-li-review",
        detail="custom",
        title="test",
        custom_fields=["lead_id", "name", "linkedin_url", "linkedin_sales_nav_id"],
        enrich_fn=om.enrich_lead_rows,
    )
    conn.close()

    keys = [col["key"] for col in payload["columns"]]
    row = payload["rows"][0]
    data = dict(zip(keys, row))
    assert "linkedin.com/in/sam-nav-handle" in (data.get("linkedin_url") or "")
    assert data.get("linkedin_sales_nav_id") == SALES


def test_export_payload_field_key_headers():
    _reset_db()
    ws = om.create_workspace("Field Key Headers", slug="df-field-hdr")
    ws_id = f"ws_{ws['slug']}"
    r = om.resolve_lead(name="Raw Test", email="raw@example.com")
    lead_id = int(r["id"])
    conn = om.get_conn()
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
    conn.commit()
    conn.close()

    conn = om.get_conn()
    payload = review.build_export_payload(
        conn,
        workspace="df-field-hdr",
        detail="custom",
        title="test",
        custom_fields=["lead_id", "email", "linkedin_url"],
        enrich_fn=om.enrich_lead_rows,
    )
    conn.close()
    assert payload["headers"] == ["🔒 Lead Id", "✏️ Email", "✏️ Linkedin Url"]


def test_apply_sync_linkedin_sales_nav_id():
    _reset_db()
    ws = om.create_workspace("Sync SN", slug="df-sn-sync")
    ws_id = f"ws_{ws['slug']}"
    r = om.resolve_lead(name="Sync Test", email="sync@example.com")
    lead_id = int(r["id"])
    conn = om.get_conn()
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
    conn.commit()

    summary = review.apply_lead_review_sync(
        conn,
        ws_id,
        [{"lead_id": lead_id, "linkedin_sales_nav_id": SALES}],
        upsert_workspace_lead_fn=om.upsert_workspace_lead,
        org_id=om.DEFAULT_ORG_ID,
        dry_run=False,
    )
    row = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE lead_id = ? AND identity_type = 'linkedin_sales_nav_id'""",
        (lead_id,),
    ).fetchone()
    conn.close()

    assert summary["updated"] == 1
    assert row["identity_value_normalized"] == SALES


if __name__ == "__main__":
    test_export_payload_custom_linkedin_fields()
    test_export_payload_field_key_headers()
    test_apply_sync_linkedin_sales_nav_id()
    print("OK")
