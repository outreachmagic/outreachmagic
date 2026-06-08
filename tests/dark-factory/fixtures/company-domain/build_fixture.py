#!/usr/bin/env python3
"""Build company-domain fallback fixture for dark-factory export tests."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from om_paths import set_data_root_override  # noqa: E402

FIXTURE_ROOT = Path(__file__).resolve().parent / "data-root"
DB_REL = Path("skills/outreachmagic/databases/outreachmagic.db")


def _reset_tree() -> Path:
    if FIXTURE_ROOT.exists():
        shutil.rmtree(FIXTURE_ROOT)
    (FIXTURE_ROOT / "skills/outreachmagic/databases").mkdir(parents=True, mode=0o755)
    (FIXTURE_ROOT / "skills/outreachmagic/config").mkdir(parents=True, mode=0o755)
    return FIXTURE_ROOT / DB_REL


def main() -> None:
    db_path = _reset_tree()
    set_data_root_override(FIXTURE_ROOT)

    import pipeline as om  # noqa: E402

    om.init_db()
    ws = om.create_workspace("Domain Factory", slug="df-domain")
    ws_id = f"ws_{ws['slug']}"

    def add_professional() -> int:
        r = om.resolve_lead(
            email="teresa.stock@purdueglobal.edu",
            name="Teresa Stock",
            company="Purdue University Global",
            linkedin_url="https://www.linkedin.com/in/teresa-stock-df",
        )
        lead_id = int(r["id"])
        conn = om.get_conn()
        conn.execute(
            "UPDATE companies SET domain = NULL WHERE id = (SELECT company_id FROM leads WHERE id = ?)",
            (lead_id,),
        )
        conn.commit()
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
        conn.commit()
        conn.close()
        om.tag_add(ws_id, lead_id, "domain-test")
        return lead_id

    def add_gmail_control() -> int:
        r = om.resolve_lead(
            email="mike.test@gmail.com",
            name="Mike Gmail",
            company="StartupCo",
            linkedin_url="https://www.linkedin.com/in/mike-gmail-df",
        )
        lead_id = int(r["id"])
        conn = om.get_conn()
        conn.execute(
            "UPDATE companies SET domain = NULL WHERE id = (SELECT company_id FROM leads WHERE id = ?)",
            (lead_id,),
        )
        conn.commit()
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
        conn.commit()
        conn.close()
        om.tag_add(ws_id, lead_id, "domain-test")
        return lead_id

    def add_no_email_with_email_domain() -> int:
        """LinkedIn-only lead with email_domain on file (email-finder candidate)."""
        r = om.resolve_lead(
            name="Alex No Email",
            company="Purdue University Global",
            linkedin_url="https://www.linkedin.com/in/alex-no-email-df",
        )
        lead_id = int(r["id"])
        conn = om.get_conn()
        conn.execute(
            "UPDATE leads SET email = NULL, email_domain = ? WHERE id = ?",
            ("purdueglobal.edu", lead_id),
        )
        conn.execute(
            "UPDATE companies SET domain = NULL WHERE id = (SELECT company_id FROM leads WHERE id = ?)",
            (lead_id,),
        )
        conn.commit()
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
        conn.commit()
        conn.close()
        om.tag_add(ws_id, lead_id, "email-finder-test")
        return lead_id

    meta = {
        "workspace": "df-domain",
        "tag": "domain-test",
        "leads": {
            "professional": add_professional(),
            "gmail_control": add_gmail_control(),
            "no_email_finder": add_no_email_with_email_domain(),
        },
    }
    (Path(__file__).resolve().parent / "meta.json").write_text(
        json.dumps(meta, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"db": str(db_path), **meta}, indent=2))


if __name__ == "__main__":
    main()
