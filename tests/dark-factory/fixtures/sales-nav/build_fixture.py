#!/usr/bin/env python3
"""Build Sales Nav import fixture DB for dark-factory layer 2 tests."""

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
SALES_HASH = "ACwAABAK84YBQ6cs16Ta-YfqZidA8SX2ywuCxhI"


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
    ws = om.create_workspace("Sales Nav Factory", slug="df-salesnav")
    ws_id = f"ws_{ws['slug']}"

    row = {
        "name": "Sam Nav",
        "email": "sam.nav@example.edu",
        "company": "Nav Corp",
        "linkedin": f"https://www.linkedin.com/in/{SALES_HASH.lower()}",
        "member linkedin sales nav id": (
            f"urn:li:fs_salesProfile:({SALES_HASH},NAME_SEARCH,cMI4)"
        ),
        "linkedin url": "https://www.linkedin.com/in/sam-nav-handle",
        "list_source": "sales_navigator",
        "import_name": "DF Sales Nav Batch",
    }
    summary = om.import_profiles(
        [row],
        workspace="df-salesnav",
        source="sales_navigator",
        source_detail="DF Sales Nav Batch",
        import_batch_id="df-salesnav-2026",
    )
    lead_id = int(summary["results"][0]["id"])
    conn = om.get_conn()
    row_db = conn.execute(
        "SELECT linkedin_url FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()
    sn = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE lead_id = ? AND identity_type = 'linkedin_sales_nav_id'""",
        (lead_id,),
    ).fetchone()
    pub = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE lead_id = ? AND identity_type = 'linkedin_url'""",
        (lead_id,),
    ).fetchone()
    conn.close()

    meta = {
        "workspace": "df-salesnav",
        "lead_id": lead_id,
        "linkedin_url": row_db["linkedin_url"] if row_db else None,
        "sales_nav_identity": sn["identity_value_normalized"] if sn else None,
        "public_identity": pub["identity_value_normalized"] if pub else None,
        "import_summary": {
            "processed": summary.get("processed"),
            "created": summary.get("created"),
            "matched": summary.get("matched"),
        },
    }
    (Path(__file__).resolve().parent / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n",
        encoding="utf-8",
    )
    (Path(__file__).resolve().parent / "import-row.json").write_text(
        json.dumps([row], indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {db_path}")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
