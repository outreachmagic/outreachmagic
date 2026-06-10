#!/usr/bin/env python3
"""Build PlusVibe triple-webhook replay fixture for dark-factory dedup tests."""

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
EVENTS_REL = Path("relay_events.json")


def main() -> None:
    if FIXTURE_ROOT.exists():
        shutil.rmtree(FIXTURE_ROOT)
    (FIXTURE_ROOT / "skills/outreachmagic/databases").mkdir(parents=True, mode=0o755)
    (FIXTURE_ROOT / "skills/outreachmagic/config").mkdir(parents=True, mode=0o755)
    set_data_root_override(FIXTURE_ROOT)

    import pipeline as om  # noqa: E402

    om.init_db()
    ws = om.create_workspace("PlusVibe Factory", slug="pv-factory")
    ws_id = f"ws_{ws['slug']}"
    conn = om.get_conn()
    conn.execute(
        """INSERT INTO campaign_workspace_map
           (id, org_id, source_platform, campaign_id, campaign_name_normalized, workspace_id)
           VALUES ('pvmap1', ?, 'plusvibe', 'camp-1', 'test campaign', ?)""",
        (om.DEFAULT_ORG_ID, ws_id),
    )
    conn.commit()
    conn.close()

    body = "Yes, let's schedule a call next week."
    events = [
        {
            "relay_id": 501,
            "platform": "plusvibe",
            "event_type": "all_email_replies",
            "lead": "lead@fixture.test",
            "received_at": "2026-06-10T12:00:01Z",
            "raw": {
                "campaign_name": "Test Campaign",
                "campaign_id": "camp-1",
                "text_body": body,
            },
        },
        {
            "relay_id": 502,
            "platform": "plusvibe",
            "event_type": "lead_marked_as_interested",
            "lead": "lead@fixture.test",
            "received_at": "2026-06-10T12:00:02Z",
            "raw": {"campaign_name": "Test Campaign", "campaign_id": "camp-1", "label": "interested"},
        },
        {
            "relay_id": 503,
            "platform": "plusvibe",
            "event_type": "all_positive_replies",
            "lead": "lead@fixture.test",
            "received_at": "2026-06-10T12:00:03Z",
            "raw": {
                "campaign_name": "Test Campaign",
                "campaign_id": "camp-1",
                "text_body": body,
            },
        },
    ]
    events_path = FIXTURE_ROOT / EVENTS_REL
    events_path.write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(events)} relay events to {events_path}")


if __name__ == "__main__":
    main()
