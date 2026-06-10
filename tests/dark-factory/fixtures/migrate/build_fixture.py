#!/usr/bin/env python3
"""Build pre-upgrade fixture DB for migrate / SQLite lock dark-factory tests."""

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
CFG_REL = Path("skills/outreachmagic/config/outreachmagic_config.json")


def _reset_tree() -> tuple[Path, Path]:
    if FIXTURE_ROOT.exists():
        shutil.rmtree(FIXTURE_ROOT)
    (FIXTURE_ROOT / "skills/outreachmagic/databases").mkdir(parents=True, mode=0o755)
    (FIXTURE_ROOT / "skills/outreachmagic/config").mkdir(parents=True, mode=0o755)
    return FIXTURE_ROOT / DB_REL, FIXTURE_ROOT / CFG_REL


def main() -> None:
    db_path, cfg_path = _reset_tree()
    set_data_root_override(FIXTURE_ROOT)

    import pipeline as om  # noqa: E402

    om.init_db()
    conn = om.get_conn()
    conn.execute(
        "INSERT INTO leads (name, email, company) VALUES ('Legacy Lead', 'legacy@fixture.test', 'Fixture Co')"
    )
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, metadata_json, campaign_id)
           VALUES (?, 'email_reply', 'inbound', ?, NULL)""",
        (lead_id, json.dumps({"platform": "plusvibe", "relay_id": 99001, "body": "Interested"})),
    )
    conn.execute(
        """INSERT INTO events (lead_id, event_type, direction, metadata_json, campaign_id)
           VALUES (?, 'email_sent', 'outbound', ?, NULL)""",
        (lead_id, json.dumps({"platform": "smartlead", "relay_id": 99002})),
    )
    conn.commit()
    conn.close()

    cfg = om.load_config()
    cfg.pop("null_campaign_backfill_at", None)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote migrate fixture: {db_path}")
    print(f"  null-campaign events: 2")
    print(f"  null_campaign_backfill_at: cleared")


if __name__ == "__main__":
    main()
