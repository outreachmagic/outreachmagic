#!/usr/bin/env python3
"""Build Sales Nav import fixture DB for dark-factory layer 2 tests."""

from __future__ import annotations

import csv
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
CSV_PATH = Path(__file__).resolve().parent / "sales-nav-export.csv"
SALES_HASH = "ACwAABAK84YBQ6cs16Ta-YfqZidA8SX2ywuCxhI"


def _reset_tree() -> Path:
    if FIXTURE_ROOT.exists():
        shutil.rmtree(FIXTURE_ROOT)
    (FIXTURE_ROOT / "skills/outreachmagic/databases").mkdir(parents=True, mode=0o755)
    (FIXTURE_ROOT / "skills/outreachmagic/config").mkdir(parents=True, mode=0o755)
    return FIXTURE_ROOT / DB_REL


def _load_csv_rows() -> list[dict]:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    db_path = _reset_tree()
    set_data_root_override(FIXTURE_ROOT)

    import pipeline as om  # noqa: E402

    om.init_db()
    om.create_workspace("Sales Nav Factory", slug="df-salesnav")
    rows = _load_csv_rows()
    summary = om.import_profiles(
        rows,
        workspace="df-salesnav",
        source="sales_navigator",
        source_detail="DF Sales Nav Batch",
        import_batch_id="df-salesnav-2026",
        import_format="auto",
    )
    lead_id = int(summary["results"][0]["id"])
    conn = om.get_conn()
    row_db = conn.execute(
        "SELECT name, title, linkedin_url FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()
    domain = conn.execute(
        """SELECT c.domain FROM companies c JOIN leads l ON l.company_id = c.id WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()
    sn = conn.execute(
        """SELECT identity_value_normalized FROM lead_identities
           WHERE lead_id = ? AND identity_type = 'linkedin_sales_nav_id'""",
        (lead_id,),
    ).fetchone()
    conn.close()

    meta = {
        "workspace": "df-salesnav",
        "lead_id": lead_id,
        "name": row_db["name"] if row_db else None,
        "title": row_db["title"] if row_db else None,
        "company_domain": domain["domain"] if domain else None,
        "linkedin_url": row_db["linkedin_url"] if row_db else None,
        "sales_nav_identity": sn["identity_value_normalized"] if sn else None,
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
    print(f"Wrote {db_path}")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
