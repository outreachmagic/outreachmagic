#!/usr/bin/env python3
"""Build review-sheet fixture DB for dark-factory layer 2 tests."""

from __future__ import annotations

import hashlib
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


def _tag(conn, ws_id: str, lead_id: int, tag: str) -> None:
    norm = tag.strip().lower().replace(" ", "_")
    tag_id = f"wlt_{ws_id}_{lead_id}_{hashlib.md5(norm.encode()).hexdigest()[:8]}"
    conn.execute(
        """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
           VALUES (?, ?, ?, ?)""",
        (tag_id, ws_id, lead_id, norm),
    )


def main() -> None:
    db_path = _reset_tree()
    set_data_root_override(FIXTURE_ROOT)

    import pipeline as om  # noqa: E402

    om.init_db()
    ws = om.create_workspace("Review Factory", slug="df-review")
    ws_id = f"ws_{ws['slug']}"

    r = om.resolve_lead(
        name="Pat Review",
        company="Review Corp",
        email="pat.review@example.edu",
        linkedin_url="https://www.linkedin.com/in/pat-review-handle",
        source="sales_navigator",
        source_detail="DF Review Batch",
    )
    lead_id = int(r["id"])
    r2 = om.resolve_lead(
        name="Other Lead",
        company="Other Co",
        email="other@example.edu",
        source="csv_import",
        source_detail="Other Batch",
    )
    lead2 = int(r2["id"])

    conn = om.get_conn()
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead2)
    _tag(conn, ws_id, lead_id, "review-test")
    _tag(conn, ws_id, lead2, "review-test")
    conn.execute(
        """INSERT INTO lead_personalization (lead_id, field_name, field_value, cloud_pending)
           VALUES (?, 'first_name', 'Patricia', 0)
           ON CONFLICT (lead_id, field_name) DO UPDATE SET field_value = excluded.field_value""",
        (lead_id,),
    )
    conn.execute(
        """INSERT INTO workspace_lead_linkedin_status
           (id, workspace_id, lead_id, sender_profile, is_connected, is_request_pending, updated_at)
           VALUES (?, ?, ?, ?, 0, 0, datetime('now'))""",
        (f"lis_{ws_id}_{lead_id}_sender", ws_id, lead_id, "sender one"),
    )
    conn.commit()
    conn.close()

    meta = {
        "workspace": "df-review",
        "tag": "review-test",
        "lead_ids": {"pat": lead_id, "other": lead2},
    }
    (Path(__file__).resolve().parent / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {db_path}")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
