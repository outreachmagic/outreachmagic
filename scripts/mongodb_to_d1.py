"""
mongodb_to_d1.py — Import historical popcam events from MongoDB into D1.

Converts MongoDB event documents to the unified 5-field envelope and
batch-inserts them into D1 relay_events. Handles all 5 platforms:
smartlead, plusvibe, prosp, api_calls, mcp.

Usage:
   export MONGO_URI="mongodb+srv://..."
   export D1_ORG_ID="cmplyyu9k0002weok1pa3k4dy"
   export D1_TOKEN="popcam_relay_token"
   export MONGO_DB="outreachmagic"
   python3 scripts/mongodb_to_d1.py [--dry-run] [--resume-from OID]
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

import pymongo
from bson.objectid import ObjectId

BATCH_SIZE = 25
POPCAM_WORKSPACE_OID = ObjectId("656ca79a51d6665235637b74")
POPCAM_ORG_OID = ObjectId("654ea699f77c17838f19dfae")


def main():
    dry_run = "--dry-run" in sys.argv
    resume_oid = _parse_resume_oid()

    client = pymongo.MongoClient(os.environ["MONGO_URI"])
    db = client[os.environ["MONGO_DB"]]
    events_coll = db["events"]
    leads_coll = db["lead_attributes"]
    senders_coll = db["sender_accounts"]

    D1_ORG_ID = os.environ["D1_ORG_ID"]
    D1_TOKEN = os.environ["D1_TOKEN"]

    # Phase A: Count + collect reference IDs
    query = {"workspace_id": POPCAM_WORKSPACE_OID, "org_id": POPCAM_ORG_OID}
    if resume_oid:
        query["_id"] = {"$gt": resume_oid}

    print("Scanning MongoDB events...")
    total_expected = events_coll.count_documents(query)
    print(f"  Total events to process: {total_expected}")

    print("Collecting reference IDs...")
    cursor = events_coll.find(query, projection={
        "_id": 1, "unified_lead_id": 1, "sender_id": 1,
    }).batch_size(1000)
    lead_ids, sender_ids = set(), set()
    for doc in cursor:
        if doc.get("unified_lead_id"):
            lead_ids.add(doc["unified_lead_id"])
        if doc.get("sender_id"):
            sender_ids.add(doc["sender_id"])
    print(f"  {len(lead_ids)} unique leads, {len(sender_ids)} unique senders")

    # Phase B: Batch-fetch lookups
    print("Fetching leads...")
    leads_map = {
        doc["_id"]: doc.get("primary_email") or (doc.get("emails") or [""])[0]
        for doc in leads_coll.find(
            {"_id": {"$in": list(lead_ids)}, "org_id": POPCAM_ORG_OID},
            projection={"_id": 1, "primary_email": 1, "emails": 1},
        )
    }
    print(f"  Resolved {len(leads_map)} lead emails")

    print("Fetching senders...")
    senders_map = {
        doc["_id"]: doc.get("sender") or ""
        for doc in senders_coll.find(
            {"_id": {"$in": list(sender_ids)}, "org_id": POPCAM_ORG_OID},
            projection={"_id": 1, "sender": 1},
        )
    }
    print(f"  Resolved {len(senders_map)} sender emails")

    # Phase C: Load existing D1 message_ids for dedup
    known_message_ids = _load_d1_message_ids(D1_ORG_ID)
    print(f"  {len(known_message_ids)} known message_ids in D1")

    # Phase D: Transform and insert
    print("\nStarting migration...")
    cursor = events_coll.find(query).sort("_id", 1).batch_size(200)
    last_oid = resume_oid
    batch, total = [], 0
    start_time = time.time()

    for doc in cursor:
        ev = _transform_event(doc, leads_map, senders_map)

        # Dedup: skip if message_id known, otherwise track it
        if _is_duplicate(ev, known_message_ids):
            continue

        batch.append(ev)
        if len(batch) >= BATCH_SIZE:
            _insert_batch(batch, D1_TOKEN, D1_ORG_ID, dry_run)
            total += len(batch)
            _report_progress(total, total_expected, start_time)
            last_oid = doc["_id"]
            batch.clear()

    if batch:
        _insert_batch(batch, D1_TOKEN, D1_ORG_ID, dry_run)
        total += len(batch)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Inserted {total} events")

    if not dry_run:
        _prepopulate_relay_ingested(D1_ORG_ID)

    client.close()


def _is_duplicate(ev: dict, known_message_ids: set) -> bool:
    """Check if event is a known duplicate by message_id."""
    msg_id = ev["payload"].get("message_id")
    if msg_id:
        if msg_id in known_message_ids:
            return True
        known_message_ids.add(msg_id)
    return False


def _transform_event(doc, leads_map, senders_map):
    """Convert ANY MongoDB event to 5-field unified envelope.

    Handles all 5 platforms (smartlead, plusvibe, prosp, api_calls, mcp).
    Pipl events are written as plusvibe — same platform, rebranded.
    """
    lead_email = leads_map.get(doc.get("unified_lead_id"), "")
    sender_email = senders_map.get(doc.get("sender_id"), "")
    event_type = (doc.get("event_type") or "unknown").lower()
    content = doc.get("content") or {}
    metadata = doc.get("metadata") or {}
    platform = doc.get("platform", "unknown")

    # Pipl is a rebrand of plusvibe — treat them identically
    if platform == "pipl":
        platform = "plusvibe"

    received_at = _ts(metadata.get("created_at") or doc.get("event_timestamp"))

    # Build payload based on what's available
    payload = {
        "campaign_id": doc.get("campaign_id", ""),
        "campaign_name": doc.get("campaign_name", ""),
        "sender": sender_email,
        "lead_email": lead_email,
        "message_id": doc.get("message_id") or "",
        "sent_on": _ts(doc.get("event_timestamp")),
    }

    # Platforms with email content (plusvibe, smartlead)
    if platform in ("plusvibe", "smartlead"):
        payload["subject"] = content.get("subject", "")
        payload["body_preview"] = content.get("body_preview", "")
        payload["body"] = content.get("body_text") or content.get("body_preview", "")
        payload["step"] = str(content.get("step", ""))
        if content.get("view_url"):
            payload["view_url"] = content["view_url"]

    # LinkedIn platform (prosp) — less structured, no message_id
    elif platform == "prosp":
        payload["body_preview"] = content.get("body_preview", "")

    # System platforms (api_calls, mcp) — payload already has the basics

    return {
        "platform": platform,
        "entity_key": lead_email,
        "event_type": event_type,
        "received_at": received_at,
        "payload": {k: v for k, v in payload.items() if v and v not in ("", "None")},
    }


def _insert_batch(events, token, org_id, dry_run):
    """Batch-insert events into D1 via wrangler d1 execute."""
    values = []
    for ev in events:
        event_json = json.dumps(ev, ensure_ascii=False, default=str)
        escaped = event_json.replace("'", "''")
        values.append(
            f"('{token}', '{ev['platform']}', '{org_id}', '{escaped}', 'delivered')"
        )
    sql = (
        "INSERT INTO relay_events "
        "(token, platform, organization_id, event_json, billing_state) VALUES "
        + ",\n".join(values) + ";"
    )
    if dry_run:
        return
    subprocess.run(
        ["wrangler", "d1", "execute", "outreach-magic-relay",
         "--remote", "--command", sql],
        check=True, capture_output=True, text=True,
    )


def _prepopulate_relay_ingested(org_id):
    """Mark migrated events as already-ingested in local SQLite."""
    import sqlite3

    sqlite_db = os.environ.get("SQLITE_DB", "outreachmagic.db")
    conn = sqlite3.connect(sqlite_db)

    conn.execute("""
        INSERT OR IGNORE INTO relay_ingested (dedupe_key)
        SELECT 'msg:' || json_extract(event_json, '$.payload.message_id')
        FROM relay_events
        WHERE organization_id = ?
          AND json_extract(event_json, '$.payload.message_id') IS NOT NULL
    """, (org_id,))

    conn.execute("""
        INSERT OR IGNORE INTO relay_ingested (dedupe_key)
        SELECT 'fp:' || platform || '|' || entity_key || '|' || event_type || '|' || received_at
        FROM relay_events
        WHERE organization_id = ?
          AND (json_extract(event_json, '$.payload.message_id') IS NULL
               OR json_extract(event_json, '$.payload.message_id') = '')
          AND json_extract(event_json, '$.relay_id') IS NULL
    """, (org_id,))

    conn.commit()
    conn.close()
    print("  Pre-populated relay_ingested for migrated events")


def _load_d1_message_ids(org_id):
    """Fetch existing message_ids from D1 for dedup checking."""
    result = subprocess.run(
        ["wrangler", "d1", "execute", "outreach-magic-relay",
         "--remote", "--command",
         (f"SELECT DISTINCT json_extract(event_json, '$.payload.message_id') AS mid"
          f" FROM relay_events"
          f" WHERE organization_id = '{org_id}'"
          f"   AND json_extract(event_json, '$.payload.message_id') IS NOT NULL"),
         "--json"],
        capture_output=True, text=True,
    )
    try:
        rows = json.loads(result.stdout)
        return {row.get("mid") for row in rows if row.get("mid")}
    except (json.JSONDecodeError, KeyError):
        return set()


def _ts(val):
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, dict) and "$date" in val:
        return val["$date"]
    return str(val or "")


def _report_progress(current, total, start):
    elapsed = time.time() - start
    rate = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / rate if rate > 0 else 0
    print(f"  {current}/{total} ({rate:.1f}/s, ETA {eta:.0f}s)")


def _parse_resume_oid():
    for i, arg in enumerate(sys.argv):
        if arg == "--resume-from" and i + 1 < len(sys.argv):
            return ObjectId(sys.argv[i + 1])
    return None


if __name__ == "__main__":
    main()
