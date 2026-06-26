#!/usr/bin/env python3
"""Remove test workspaces and set production campaign routing rules."""
from __future__ import annotations

import sys
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

import pipeline as om  # noqa: E402
from workspace_routing import DEFAULT_ORG_ID, assign_campaign_map  # noqa: E402

DELETE_SLUGS = ("acme", "alpha", "repairtagws2")

RULES = [
    # (workspace_slug, match_strategy, campaign_name, campaign_id, priority)
    ("popcam", "rule_contains", "popcam", None, 10),
    ("leadgenph", "rule_contains", "leadgenph", None, 20),
    ("m2n", "rule_contains", "m2n", None, 30),
    ("popcam", "id_exact", None, "ca373942-cc0b-4b9a-b342-ec7c94d27895", 5),
    ("leadgenph", "name_exact", "leadgenph scholarship", None, 15),
]

DISPLAY_NAMES = {
    "popcam": "PopCam",
    "leadgenph": "LeadGenPH",
    "m2n": "M2N",
}


def main() -> None:
    conn = om.get_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    org_id = DEFAULT_ORG_ID

    for slug in DELETE_SLUGS:
        row = conn.execute(
            "SELECT id, name FROM workspaces WHERE org_id = ? AND slug = ?",
            (org_id, slug),
        ).fetchone()
        if not row:
            print(f"skip delete workspace (missing): {slug}")
            continue
        ws_id = row["id"]
        conn.execute("DELETE FROM campaign_workspace_map WHERE workspace_id = ?", (ws_id,))
        conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        print(f"deleted workspace: {slug} ({row['name']})")

    for slug, display in DISPLAY_NAMES.items():
        conn.execute(
            "UPDATE workspaces SET name = ?, updated_at = datetime('now') WHERE org_id = ? AND slug = ?",
            (display, org_id, slug),
        )

    m2n = conn.execute(
        "SELECT id FROM workspaces WHERE org_id = ? AND slug = 'm2n'", (org_id,)
    ).fetchone()
    if not m2n:
        conn.close()
        created = om.create_workspace("M2N", slug="m2n")
        print("created workspace:", created)
        conn = om.get_conn()
    else:
        print("workspace exists: m2n")

    removed = conn.execute(
        "DELETE FROM campaign_workspace_map WHERE org_id = ?", (org_id,)
    ).rowcount
    print(f"removed {removed} campaign map rule(s)")

    for ws_slug, strategy, cname, cid, priority in RULES:
        ws = conn.execute(
            "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
            (org_id, ws_slug),
        ).fetchone()
        if not ws:
            raise SystemExit(f"workspace not found: {ws_slug}")
        map_id = assign_campaign_map(
            conn,
            org_id,
            source_platform="*",
            workspace_id=ws["id"],
            campaign_id=cid,
            campaign_name=cname,
            match_strategy=strategy,
            priority=priority,
        )
        conn.execute(
            "UPDATE campaign_workspace_map SET cloud_synced = 0 WHERE id = ?", (map_id,)
        )
        label = cid or cname
        print(f"rule: {strategy} {label!r} -> {ws_slug} (priority {priority}, id={map_id})")

    conn.commit()
    conn.close()

    print("\n--- final workspaces ---")
    conn = om.get_conn()
    for r in conn.execute(
        "SELECT slug, name FROM workspaces WHERE org_id = ? ORDER BY slug", (org_id,)
    ):
        print(f"  {r['slug']}: {r['name']}")
    print("\n--- final rules ---")
    for r in conn.execute(
        """SELECT m.match_strategy, m.campaign_id, m.campaign_name_normalized,
                  m.priority, w.slug
           FROM campaign_workspace_map m
           JOIN workspaces w ON w.id = m.workspace_id
           WHERE m.org_id = ? AND m.is_active = 1
           ORDER BY m.priority""",
        (org_id,),
    ):
        target = r["campaign_id"] or r["campaign_name_normalized"]
        print(f"  [{r['priority']}] {r['match_strategy']} {target!r} -> {r['slug']}")
    conn.close()
    print("\nDone. Run: pipeline.py sync  (when batch sync is idle) to push rules to cloud.")


if __name__ == "__main__":
    main()
