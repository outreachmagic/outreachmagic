#!/usr/bin/env python3
"""Build generic dedup fixture DB for dark-factory layer 2 tests (no campaign-specific data)."""

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
    import pipeline_dedup  # noqa: E402

    om.init_db()
    ws = om.create_workspace("Dedup Factory", slug="df-dedup")
    ws_id = f"ws_{ws['slug']}"

    def add(name: str, company: str, *, email: str | None = None, linkedin: str | None = None) -> int:
        r = om.resolve_lead(name=name, company=company, email=email, linkedin_url=linkedin)
        lead_id = int(r["id"])
        conn = om.get_conn()
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
        conn.commit()
        conn.close()
        om.tag_add(ws_id, lead_id, "dedup-test")
        return lead_id

    ids: dict[str, int] = {}
    ids["abby_full"] = add("Abby Schoenbeck", "Acme University")
    ids["abby_mba"] = add("Abby Schoenbeck, MBA", "Acme University")
    ids["copper_keep"] = add(
        "Adam Sambrano",
        "Copper Mountain Community College",
        email="adam.sambrano@example.edu",
    )
    ids["copper_merge"] = add("Adam Sambrano", "Copper Mountain College")
    ids["amd_short"] = add("Jane Chip", "AMD")
    ids["amd_long"] = add("Jane Chip", "Advanced Micro Devices, Inc.")
    ids["unf_short"] = add("Bob Gator", "UNF")
    ids["unf_long"] = add("Bob Gator", "University of North Florida")
    ids["vanessa_first"] = add("Vanessa", "M.C. Dean")
    ids["vanessa_full"] = add("Vanessa Fountaine", "M.C. Dean")
    ids["job_a"] = add("Adam Reese", "Reese Agency")
    ids["job_b"] = add("Adam Reese", "Insight Global")
    ids["solo"] = add("Unique Person", "Solo Corp")

    conn = om.get_conn()
    payload = pipeline_dedup.find_duplicates(
        conn,
        workspace_slug="df-dedup",
        tag_filter="dedup-test",
        min_confidence="ALL",
        resolve_workspace_fn=om.resolve_workspace_identity,
        normalize_tag_fn=om.normalize_tag,
    )
    conn.close()

    # Candidates file for merge tests (includes synthetic orphan pair)
    candidates_path = Path(__file__).resolve().parent / "candidates.json"
    orphan_pair = {
        "keep_id": ids["copper_keep"],
        "merge_id": 99999,
        "keep_name": "Adam Sambrano",
        "merge_name": "Phantom",
        "keep_company": "Copper Mountain Community College",
        "merge_company": "",
        "keep_email": "adam.sambrano@example.edu",
        "merge_email": None,
        "keep_linkedin": None,
        "merge_linkedin": None,
        "keep_tags": ["dedup-test"],
        "merge_tags": ["dedup-test"],
        "confidence": "HIGH",
        "match_method": "exact_company",
        "name_variations": ["Adam Sambrano"],
    }
    merge_payload = {
        "workspace": "df-dedup",
        "tag_filter": "dedup-test",
        "generated_at": payload["generated_at"],
        "stats": payload["stats"],
        "candidates": [orphan_pair],
    }
    candidates_path.write_text(json.dumps(merge_payload, indent=2) + "\n", encoding="utf-8")

    meta = {
        "workspace": "df-dedup",
        "tag": "dedup-test",
        "lead_ids": ids,
        "find_stats": payload["stats"],
        "candidates_found": payload["stats"]["candidates_found"],
    }
    (Path(__file__).resolve().parent / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Wrote {db_path}")
    print(f"Candidates: {payload['stats']}")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
