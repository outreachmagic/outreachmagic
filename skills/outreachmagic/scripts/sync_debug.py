"""Troubleshooting commands for the relay round trip.

    sync-preview  -- the exact payload we WOULD send for a lead, without sending it
    sync-diff     -- that payload vs. what the relay actually stores, field by field
    sync-audit    -- every payload sent/received for this lead, with errors

sync-diff is the one that answers the question that started all of this:
"I changed a tag / verified an email -- did it actually make it to the relay?"
Today there is no way to ask, so the answer is archaeology. Now it is one command.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from db_conn import get_conn
from sync_audit import canonical_json, content_hash, history_for_entity_keys

RELAY_URL = "https://api.outreachmagic.io"

# lead_core_update carries these; lead_workspace_update carries the rest.
_CORE_ACTION = "lead_core_update"
_WS_ACTION = "lead_workspace_update"


def resolve_lead_id(
    conn, *, lead_id: Optional[int] = None, email: Optional[str] = None
) -> Optional[int]:
    if lead_id:
        row = conn.execute("SELECT id FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
        return int(row["id"]) if row else None
    if email:
        from pipeline import find_lead_by_email
        from workspace_routing import normalize_email
        return find_lead_by_email(conn, normalize_email(email))
    return None


def build_payloads(conn, lead_id: int) -> dict:
    """Every payload this lead would push, keyed by action (+workspace for ws)."""
    from lead_sync import build_lead_core_sync_payload, build_lead_workspace_sync_payload
    from pipeline import DEFAULT_ORG_ID
    from workspace_routing import lead_entity_key

    entity_key = lead_entity_key(conn, DEFAULT_ORG_ID, lead_id)
    out = {
        "lead_id": lead_id,
        "entity_key": entity_key,
        "entries": [],
    }
    core = build_lead_core_sync_payload(conn, DEFAULT_ORG_ID, lead_id)
    if core:
        out["entries"].append({
            "action": _CORE_ACTION,
            "entity_key": entity_key,
            "payload": core,
            "content_hash": content_hash(core),
        })
    slugs = conn.execute(
        """SELECT w.slug FROM workspace_leads wl
           JOIN workspaces w ON w.id = wl.workspace_id
           WHERE wl.lead_id = ?""",
        (lead_id,),
    ).fetchall()
    for r in slugs:
        ws = build_lead_workspace_sync_payload(
            conn, DEFAULT_ORG_ID, lead_id, workspace_slug=r["slug"]
        )
        if ws:
            out["entries"].append({
                "action": _WS_ACTION,
                "entity_key": entity_key,
                "workspace": r["slug"],
                "payload": ws,
                "content_hash": content_hash(ws),
            })
    return out


def fetch_relay_snapshot(
    entity_key: str, kind: str, *, workspace: Optional[str] = None
) -> dict:
    """Ask the relay what it currently stores for this key (GET /debug/snapshot)."""
    from pipeline import __version__
    from pipeline_update import get_agent_key

    key = get_agent_key()
    if not key:
        return {"error": "not connected: run `pipeline.py setup --key om_agent_...`"}
    params = {"entity_key": entity_key, "kind": kind}
    if workspace:
        params["workspace"] = workspace
    url = f"{RELAY_URL}/debug/snapshot?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": f"Outreach Magic/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"found": False}
        body = ""
        try:
            body = (exc.read() or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        return {"error": f"HTTP {exc.code}{': ' + body if body else ''}"}
    except Exception as exc:
        return {"error": str(exc)}


def _relay_payload(snapshot: dict) -> dict:
    """Unwrap the stored envelope. Both shapes exist in production (flat is 85%)."""
    ev = snapshot.get("event_json")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            return {}
    if not isinstance(ev, dict):
        return {}
    envelope = ev.get("payload") or {}
    if "action" in envelope or "data" in envelope:
        return envelope.get("data") or {}
    return envelope


def diff_payloads(local: dict, remote: dict) -> list[dict]:
    """Field-level diff. Returns only fields that differ."""
    rows = []
    for field in sorted(set(local) | set(remote)):
        lv, rv = local.get(field), remote.get(field)
        if lv == rv:
            continue
        if lv is None:
            status = "only on relay"
        elif rv is None:
            status = "not on relay"
        else:
            status = "differs"
        rows.append({"field": field, "local": lv, "relay": rv, "status": status})
    return rows


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_preview(*, lead_id=None, email=None, as_json=False) -> int:
    conn = get_conn()
    try:
        lid = resolve_lead_id(conn, lead_id=lead_id, email=email)
        if not lid:
            print("lead not found")
            return 1
        built = build_payloads(conn, lid)
    finally:
        conn.close()

    if as_json:
        print(json.dumps(built, indent=2, default=str))
        return 0

    print(f"Lead {built['lead_id']}   entity_key: {built['entity_key'] or '(none — would NOT be pushed)'}")
    if not built["entity_key"]:
        print("\n  This lead has no entity_key, so the push loop skips it entirely.")
        print("  That means nothing about it ever reaches the relay.")
    for e in built["entries"]:
        ws = f"  workspace={e['workspace']}" if e.get("workspace") else ""
        print(f"\n─── {e['action']}{ws}   sha256={e['content_hash'][:12]}…")
        print(json.dumps(e["payload"], indent=2, default=str, ensure_ascii=False))
    if not built["entries"]:
        print("\n  (no payloads — nothing would be sent)")
    return 0


def cmd_diff(*, lead_id=None, email=None, as_json=False) -> int:
    conn = get_conn()
    try:
        lid = resolve_lead_id(conn, lead_id=lead_id, email=email)
        if not lid:
            print("lead not found")
            return 1
        built = build_payloads(conn, lid)
    finally:
        conn.close()

    entity_key = built["entity_key"]
    if not entity_key:
        print(f"Lead {lid} has no entity_key — it is never pushed, so the relay has nothing.")
        return 1

    report = {"lead_id": lid, "entity_key": entity_key, "entries": []}
    for e in built["entries"]:
        kind = "core" if e["action"] == _CORE_ACTION else "workspace"
        snap = fetch_relay_snapshot(entity_key, kind, workspace=e.get("workspace"))
        entry = {"action": e["action"], "workspace": e.get("workspace")}
        if snap.get("error"):
            entry["error"] = snap["error"]
        elif not snap.get("found", True) or not snap.get("event_json"):
            entry["status"] = "NOT ON RELAY"
        else:
            remote = _relay_payload(snap)
            local = e["payload"]
            entry["relay_updated_at"] = snap.get("updated_at")
            entry["in_sync"] = content_hash(local) == content_hash(remote)
            entry["diff"] = diff_payloads(local, remote)
        report["entries"].append(entry)

    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Lead {lid}   entity_key: {entity_key}\n")
    for e in report["entries"]:
        ws = f"  workspace={e['workspace']}" if e.get("workspace") else ""
        print(f"─── {e['action']}{ws}")
        if e.get("error"):
            print(f"    error: {e['error']}\n")
            continue
        if e.get("status") == "NOT ON RELAY":
            print("    ✗ the relay has NO snapshot under this key\n")
            continue
        if e.get("in_sync"):
            print(f"    ✓ in sync (relay updated_at {e.get('relay_updated_at')})\n")
            continue
        print(f"    ✗ OUT OF SYNC (relay updated_at {e.get('relay_updated_at')})")
        for d in e["diff"]:
            print(f"      {d['field']:<32} {d['status']}")
            print(f"        local: {_short(d['local'])}")
            print(f"        relay: {_short(d['relay'])}")
        print()
    return 0


def cmd_audit(*, lead_id=None, email=None, limit=20, errors_only=False, as_json=False) -> int:
    conn = get_conn()
    try:
        lid = resolve_lead_id(conn, lead_id=lead_id, email=email)
        if not lid:
            print("lead not found")
            return 1
        from pipeline import DEFAULT_ORG_ID
        from workspace_routing import lead_entity_key
        keys = {lead_entity_key(conn, DEFAULT_ORG_ID, lid)}
        # entity_key is derived from mutable columns today (email wins over
        # linkedin_url), so a lead's history can be split across keys. Gather
        # every key it has ever plausibly used.
        row = conn.execute(
            "SELECT email, linkedin_url FROM leads WHERE id = ?", (lid,)
        ).fetchone()
        if row:
            if row["email"]:
                keys.add(str(row["email"]).strip().lower())
            if row["linkedin_url"]:
                keys.add(str(row["linkedin_url"]).strip())
        keys.discard("")
        rows = history_for_entity_keys(
            sorted(keys), limit=limit, errors_only=errors_only, conn=conn
        )
    finally:
        conn.close()

    if as_json:
        print(json.dumps({"lead_id": lid, "entity_keys": sorted(keys), "history": rows},
                         indent=2, default=str))
        return 0

    print(f"Lead {lid}   keys: {', '.join(sorted(keys))}\n")
    if not rows:
        print("  No audited traffic for this lead.")
        print("  (The audit log only covers syncs run since it was enabled.)")
        return 0
    for r in rows:
        arrow = "→ push" if r["direction"] == "push" else "← pull"
        status = ""
        if r["error"]:
            status = f"  ERROR: {r['error']}"
        elif r["http_status"]:
            status = f"  HTTP {r['http_status']}"
        ws = f" [{r['workspace']}]" if r["workspace"] else ""
        print(f"{r['created_at']}  {arrow}  {r['action']}{ws}"
              f"  sha={(r['content_hash'] or '')[:10]}…{status}")
    return 0


def _short(v, width=110):
    s = v if isinstance(v, str) else canonical_json(v)
    return s if len(s) <= width else s[: width - 1] + "…"
