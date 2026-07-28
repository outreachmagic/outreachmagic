#!/usr/bin/env python3
"""Sender account (PlusVibe deliverability) import, sync, and stats.

Sender accounts are the mailboxes we send *from* -- warmup/health/deliverability
metadata imported from PlusVibe's CSV export, plus reply/bounce rates computed
from our own local `events`/`bounce_events` data. Org-wide, mirroring the
leads/workspace_leads split (a mailbox can be shared across workspaces).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import blacklist_monitor
from campaign_stats import reply_event_sql_condition
from db_conn import get_conn
from pipeline_utils import email_domain, normalize_email
from read_queries import _since_clause
from workspace_routing import DEFAULT_ORG_ID, parse_linkedin_value


def normalize_domain(domain: Optional[str]) -> str:
    """Lowercase + trim a domain and strip stray surrounding quotes.

    register/upsert paths only did .strip().lower(), so a value pasted with
    quotes (e.g. `"example.com"`) leaked the quote chars into the primary key —
    the two dead `meetpopcam.com"` / `popcam.net"` tombstones are exactly that
    bug. Strip quotes defensively everywhere a domain is keyed or stored.
    """
    return (domain or "").strip().strip('"').strip("'").strip().lower()


# Domain uses the company panel can label/group additional domains by.
SENDER_DOMAIN_PURPOSES = ("sending", "branch", "email_finding")

# PlusVibe export columns we intentionally ignore -- SMTP/IMAP connection
# config, not deliverability data, and we don't use those.
_IGNORED_CSV_COLUMNS = frozenset({
    "username", "smtp_username", "smtp_host", "smtp_port",
    "imap_host", "imap_port", "warmup_custom_words",
})

_SENT_EVENT_TYPES = ("email_sent", "email_sent_auto")

# CSV column -> (sender_accounts column, coercion)
_INT_FIELDS = frozenset({
    "daily_limit", "warmup_max_daily_limit",
    "overall_hscore", "google_hscore", "microsoft_hscore", "other_hscore",
})
_REAL_FIELDS = frozenset({"ooo_rr", "ooo_rr_14", "ooo_rr_30", "ooo_rr_90", "bounce_r", "miss_warmup_r"})

_CSV_TO_COLUMN = {
    "_id": "external_id",
    "first_name": "first_name",
    "last_name": "last_name",
    "provider": "provider",
    "daily_limit": "daily_limit",
    "warmup_status": "warmup_status",
    "email": "email",
    "created_at": "source_created_at",
    "status": "status",
    "SPF": "spf_status",
    "DKIM": "dkim_status",
    "DMARC": "dmarc_status",
    "warmup_enabled_date": "warmup_enabled_date",
    "warmup_max_daily_limit": "warmup_max_daily_limit",
    "overall_hscore": "overall_health_score",
    "google_hscore": "google_health_score",
    "microsoft_hscore": "microsoft_health_score",
    "other_hscore": "other_health_score",
    "ooo_rr": "ooo_rr",
    "ooo_rr_14": "ooo_rr_14",
    "ooo_rr_30": "ooo_rr_30",
    "ooo_rr_90": "ooo_rr_90",
    "bounce_r": "bounce_rate",
    "miss_warmup_r": "miss_warmup_rate",
}

_JS_DATE_TRAILING_PAREN_RE = re.compile(r"\s*\(.*\)\s*$")


def _parse_plusvibe_date(raw: Optional[str]) -> Optional[str]:
    """PlusVibe stamps JS Date.toString() output, e.g.
    'Wed Mar 04 2026 03:17:51 GMT+0000 (Coordinated Universal Time)'."""
    if not raw or not str(raw).strip():
        return None
    cleaned = _JS_DATE_TRAILING_PAREN_RE.sub("", str(raw).strip())
    try:
        return datetime.strptime(cleaned, "%a %b %d %Y %H:%M:%S GMT%z").isoformat()
    except ValueError:
        return raw


def parse_sender_accounts_csv(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            email = normalize_email(raw_row.get("email"))
            if not email:
                continue
            row: dict = {"email": email}
            for csv_col, value in raw_row.items():
                if csv_col in _IGNORED_CSV_COLUMNS or csv_col in ("email",):
                    continue
                if csv_col == "tags":
                    tags = [t.strip() for t in (value or "").split(";") if t.strip()]
                    row["tags_json"] = json.dumps(tags)
                    continue
                column = _CSV_TO_COLUMN.get(csv_col)
                if not column:
                    continue
                if value is None or value == "":
                    row[column] = None
                    continue
                if csv_col in _INT_FIELDS:
                    try:
                        row[column] = int(float(value))
                    except ValueError:
                        row[column] = None
                elif csv_col in _REAL_FIELDS:
                    try:
                        row[column] = float(value)
                    except ValueError:
                        row[column] = None
                elif csv_col in ("created_at", "warmup_enabled_date"):
                    row[column] = _parse_plusvibe_date(value)
                else:
                    row[column] = value
            rows.append(row)
    return rows


def upsert_sender_account(
    conn: sqlite3.Connection, row: dict, org_id: str = DEFAULT_ORG_ID, channel: str = "email",
) -> int:
    email = normalize_sender_identity(row["email"])
    columns = [c for c in _CSV_TO_COLUMN.values() if c in row] + (["tags_json"] if "tags_json" in row else [])
    columns = sorted(set(columns))
    existing = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, email)
    ).fetchone()
    if existing:
        set_clause = ", ".join(f"{c} = ?" for c in columns)
        conn.execute(
            f"UPDATE sender_accounts SET {set_clause}, channel = ?, updated_at = datetime('now') WHERE id = ?",
            [row.get(c) for c in columns] + [channel, existing["id"]],
        )
        sender_account_id = int(existing["id"])
    else:
        insert_cols = ["org_id", "email", "channel"] + columns
        placeholders = ", ".join("?" for _ in insert_cols)
        cur = conn.execute(
            f"INSERT INTO sender_accounts ({', '.join(insert_cols)}) VALUES ({placeholders})",
            [org_id, email, channel] + [row.get(c) for c in columns],
        )
        sender_account_id = int(cur.lastrowid)
    _classify_and_store_identifier(conn, sender_account_id, email)
    return sender_account_id


def _classify_and_store_identifier(conn: sqlite3.Connection, sender_account_id: int, identifier: str) -> None:
    """Fill in linkedin_url / linkedin_sales_nav_id / email_domain from a raw
    identifier -- `identifier` may be a public LinkedIn profile URL, a Sales
    Navigator URL/token, or a real email (a LinkedIn seat can be identified by
    its login email in some sources). Never overwrites an already-set value.
    """
    parsed = dict(parse_linkedin_value(identifier))
    linkedin_url = parsed.get("linkedin_url")
    sales_nav_id = parsed.get("linkedin_sales_nav_id")
    domain = email_domain(identifier) if "@" in identifier else None
    if not (linkedin_url or sales_nav_id or domain):
        return
    conn.execute(
        """UPDATE sender_accounts
           SET linkedin_url = COALESCE(linkedin_url, ?),
               linkedin_sales_nav_id = COALESCE(linkedin_sales_nav_id, ?),
               email_domain = COALESCE(email_domain, ?)
           WHERE id = ?""",
        (linkedin_url, sales_nav_id, domain, sender_account_id),
    )


def normalize_sender_identity(identifier: Optional[str]) -> str:
    """Canonical form of the sender_accounts.email identity key.

    `email` is the identity column, not necessarily an address -- a LinkedIn seat
    has no email, so its profile URL goes here -- and (org_id, email) is the unique
    key on the table.

    Three code paths used to normalize it three different ways: the relay entity_key
    lowercased it, ensure_sender_account only stripped it, and
    find_sender_account_id_by_email forced lowercase. So a sender arriving with any
    uppercase created a *second* row, which the third path could then never find.
    One rule, applied on every read and write.
    """
    return (identifier or "").strip().lower()


def infer_sender_channel(identifier: str) -> str:
    """Guess a seat's channel from its identity, for paths that aren't told one."""
    return "email" if "@" in (identifier or "") else "linkedin"


def ensure_sender_account(
    conn: sqlite3.Connection, identifier: str, channel: str = "email", org_id: str = DEFAULT_ORG_ID,
) -> Optional[int]:
    """Bootstrap a bare sender_accounts row from an event's sender, if it doesn't exist yet.

    Cheap INSERT OR IGNORE -- never overwrites richer data from a CSV import
    (warmup/health scores etc), only fills in accounts we've never seen
    before (this is the primary creation path for Prosp/LinkedIn accounts,
    which have no CSV export).
    """
    raw = (identifier or "").strip()
    key = normalize_sender_identity(raw)
    if not key:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO sender_accounts (org_id, email, channel) VALUES (?, ?, ?)",
        (org_id, key, channel),
    )
    row = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, key)
    ).fetchone()
    if not row:
        return None
    sender_account_id = int(row["id"])
    # Classify from the raw identifier, not the lowercased key: Sales Navigator
    # tokens (ACwAA...) are case-sensitive.
    _classify_and_store_identifier(conn, sender_account_id, raw)
    return sender_account_id


def touch_sender_account_activity(
    conn: sqlite3.Connection,
    sender_account_id: int,
    *,
    direction: str,
    event_at: Optional[str] = None,
) -> None:
    """Advance a sender's last outbound/inbound timestamp for one event.

    events.sender is always one of our own mailboxes, on inbound as well as
    outbound -- an inbound reply records the seat that *received* it -- so
    direction alone decides which column moves.

    Only ever moves forward. A pull replays history in relay order, not
    chronological order, so a plain assignment would let an old event overwrite a
    newer timestamp; MAX() makes the write order irrelevant.
    """
    col = "last_inbound_at" if direction == "inbound" else "last_outbound_at"
    ts = event_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"""UPDATE sender_accounts
               SET {col} = MAX(COALESCE({col}, ''), ?),
                   updated_at = datetime('now')
             WHERE id = ? AND COALESCE({col}, '') < ?""",
        (ts, sender_account_id, ts),
    )


def sender_domain_activity(
    conn: sqlite3.Connection,
    org_id: str = DEFAULT_ORG_ID,
) -> list[dict]:
    """Per-domain send/reply activity, rolled up from the sender accounts on it.

    Deliberately a rollup rather than columns on sender_domains: the domain's
    activity *is* its senders' activity, and a second copy would be one more thing
    to keep in step. idx_sender_accounts_email_domain makes the GROUP BY cheap.

    LinkedIn seats have a NULL email_domain (they have no domain) and are excluded.
    """
    rows = conn.execute(
        """SELECT email_domain AS domain,
                  COUNT(*) AS senders,
                  MAX(last_outbound_at) AS last_outbound_at,
                  MAX(last_inbound_at)  AS last_inbound_at,
                  SUM(CASE WHEN last_outbound_at IS NOT NULL THEN 1 ELSE 0 END) AS senders_active
             FROM sender_accounts
            WHERE org_id = ? AND email_domain IS NOT NULL AND email_domain != ''
            GROUP BY email_domain
            ORDER BY MAX(last_outbound_at) DESC NULLS LAST""",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def link_sender_account_to_workspace(conn: sqlite3.Connection, workspace_id: str, sender_account_id: int) -> None:
    _link_workspace(conn, workspace_id, sender_account_id)


def unlink_sender_account_from_workspace(conn: sqlite3.Connection, workspace_id: str, sender_account_id: int) -> None:
    conn.execute(
        "DELETE FROM workspace_sender_accounts WHERE workspace_id = ? AND sender_account_id = ?",
        (workspace_id, sender_account_id),
    )


def _link_workspace(conn: sqlite3.Connection, workspace_id: str, sender_account_id: int) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO workspace_sender_accounts (workspace_id, sender_account_id)
           VALUES (?, ?)""",
        (workspace_id, sender_account_id),
    )


def find_sender_account_id_by_email(conn: sqlite3.Connection, email: str, org_id: str = DEFAULT_ORG_ID) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?",
        (org_id, normalize_sender_identity(email)),
    ).fetchone()
    return int(row["id"]) if row else None


_SENDER_ACCOUNT_EDITABLE_FIELDS = (
    "provider", "first_name", "last_name", "daily_limit", "status", "warmup_status", "channel",
    "is_active",
)


def update_sender_account(email: str, *, org_id: str = DEFAULT_ORG_ID, **fields) -> dict:
    """Manually edit a sender account's own fields (not sync/import-owned metrics).

    Only accepts `_SENDER_ACCOUNT_EDITABLE_FIELDS` -- health scores, warmup
    rates, and other PlusVibe-reported metrics stay CSV-import/sync-owned.
    """
    unknown = set(fields) - set(_SENDER_ACCOUNT_EDITABLE_FIELDS)
    if unknown:
        return {"status": "error", "error": f"not editable: {', '.join(sorted(unknown))}"}
    updates = {k: v for k, v in fields.items() if v is not None}
    if not updates:
        return {"status": "error", "error": "no fields to update"}

    conn = get_conn()
    try:
        sender_account_id = find_sender_account_id_by_email(conn, email, org_id=org_id)
        if not sender_account_id:
            return {"status": "error", "error": f"unknown sender account: {email}"}
        set_clause = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(
            f"UPDATE sender_accounts SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            list(updates.values()) + [sender_account_id],
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "email": email, "updated": sorted(updates)}


def set_sender_account_workspace_link(
    email: str, workspace: str, *, linked: bool = True, org_id: str = DEFAULT_ORG_ID,
) -> dict:
    from pipeline_workspace import list_workspaces

    slug_to_id = {w["slug"]: w["id"] for w in list_workspaces()}
    ws_id = slug_to_id.get(workspace)
    if not ws_id:
        return {"status": "error", "error": f"unknown workspace: {workspace}"}

    conn = get_conn()
    try:
        sender_account_id = find_sender_account_id_by_email(conn, email, org_id=org_id)
        if not sender_account_id:
            return {"status": "error", "error": f"unknown sender account: {email}"}
        if linked:
            _link_workspace(conn, ws_id, sender_account_id)
        else:
            unlink_sender_account_from_workspace(conn, ws_id, sender_account_id)
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "email": email, "workspace": workspace, "linked": linked}


def sender_account_workspaces(
    email: str, *, org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Which workspaces a mailbox belongs to, and which exist to choose from."""
    from pipeline_workspace import list_workspaces

    conn = get_conn()
    try:
        sender_account_id = find_sender_account_id_by_email(conn, email, org_id=org_id)
        if not sender_account_id:
            return {"status": "error", "error": f"unknown sender account: {email}"}
        member = _sender_account_workspace_slugs(conn, sender_account_id)
    finally:
        conn.close()
    return {
        "status": "ok", "email": email, "workspaces": member,
        "available": [w["slug"] for w in list_workspaces()],
    }


def set_sender_account_workspaces(
    email: str, workspaces: list[str], *, org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Replace a mailbox's whole workspace membership with `workspaces`.

    A full set, not a delta, matching the sync payload's semantic
    (build_sender_account_sync_payload sends membership in full, so an empty
    list means "removed from everywhere" rather than "no change"). Sending a
    delta from the UI and a snapshot on the wire is how the two drift apart.
    """
    from pipeline_workspace import list_workspaces

    known = {w["slug"] for w in list_workspaces()}
    wanted = [s for s in dict.fromkeys(workspaces or []) if s]
    unknown = [s for s in wanted if s not in known]
    if unknown:
        return {"status": "error", "error": f"unknown workspace(s): {', '.join(unknown)}"}

    conn = get_conn()
    try:
        sender_account_id = find_sender_account_id_by_email(conn, email, org_id=org_id)
        if not sender_account_id:
            return {"status": "error", "error": f"unknown sender account: {email}"}
        _reconcile_workspace_links(conn, sender_account_id, wanted)
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "email": email, "workspaces": wanted}


def set_domain_workspaces(
    domain: str, workspaces: list[str], *, org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Apply a workspace membership to every mailbox on a sending domain.

    A domain has no membership of its own -- its mailboxes do. This is a bulk
    edit over them, not a new fact about the domain: inventing a domain-level
    membership row that nothing else reads would be a second source of truth
    for the same question.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT email FROM sender_accounts WHERE LOWER(email_domain) = LOWER(?)",
            (domain.strip(),),
        ).fetchall()
        emails = [r["email"] for r in rows]
    finally:
        conn.close()
    if not emails:
        return {"status": "error", "error": f"no mailboxes on domain: {domain}"}
    results = [set_sender_account_workspaces(e, workspaces, org_id=org_id) for e in emails]
    failed = [r for r in results if r.get("status") != "ok"]
    return {
        "status": "error" if failed else "ok",
        "domain": domain, "mailboxes": len(emails),
        "updated": len(results) - len(failed),
        "errors": [r.get("error") for r in failed],
    }


def domain_workspace_summary(domain: str) -> dict:
    """Workspace membership across a domain's mailboxes, counted.

    Read-only and derived, so the domain pane can show what its mailboxes say
    without pretending the domain itself carries the fact.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT w.slug, COUNT(*) AS mailboxes
                 FROM sender_accounts sa
                 JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
                 JOIN workspaces w ON w.id = wsa.workspace_id
                WHERE LOWER(sa.email_domain) = LOWER(?)
                GROUP BY w.slug ORDER BY mailboxes DESC, w.slug""",
            (domain.strip(),),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) n FROM sender_accounts WHERE LOWER(email_domain) = LOWER(?)",
            (domain.strip(),),
        ).fetchone()["n"]
    finally:
        conn.close()
    return {"domain": domain, "mailboxes": total,
            "workspaces": [dict(r) for r in rows]}


def import_sender_accounts(file_path: str, workspace: Optional[str] = None, org_id: str = DEFAULT_ORG_ID) -> dict:
    from pipeline_workspace import list_workspaces

    slug_to_id = {w["slug"]: w["id"] for w in list_workspaces()}
    explicit_ws_id = None
    if workspace:
        explicit_ws_id = slug_to_id.get(workspace)
        if not explicit_ws_id:
            return {"status": "error", "error": f"unknown workspace: {workspace}"}

    rows = parse_sender_accounts_csv(file_path)
    conn = get_conn()
    created = 0
    updated = 0
    workspace_links = 0
    try:
        for row in rows:
            existing = conn.execute(
                "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?",
                (org_id, row["email"]),
            ).fetchone()
            sender_account_id = upsert_sender_account(conn, row, org_id=org_id, channel="email")
            if existing:
                updated += 1
            else:
                created += 1

            if explicit_ws_id:
                _link_workspace(conn, explicit_ws_id, sender_account_id)
                workspace_links += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "ok",
        "total": len(rows),
        "created": created,
        "updated": updated,
        "workspace_links": workspace_links,
    }


def sender_account_entity_key(conn: sqlite3.Connection, sender_account_id: int) -> Optional[str]:
    row = conn.execute("SELECT email FROM sender_accounts WHERE id = ?", (sender_account_id,)).fetchone()
    if not row or not row["email"]:
        return None
    return f"sender_account:{normalize_sender_identity(row['email'])}"


def resolve_sender_account_from_entity_key(
    conn: sqlite3.Connection, entity_key: str, org_id: str = DEFAULT_ORG_ID
) -> Optional[int]:
    if not entity_key.startswith("sender_account:"):
        return None
    email = normalize_sender_identity(entity_key.split(":", 1)[1])
    if not email:
        return None
    row = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, email)
    ).fetchone()
    if row:
        return int(row["id"])
    # The entity_key carries no channel, and the column defaults to 'email' -- which
    # would label a relay-created LinkedIn seat as an email one. Infer it from the
    # identity instead (a LinkedIn seat's identity is a profile URL, not an address).
    cur = conn.execute(
        "INSERT INTO sender_accounts (org_id, email, channel) VALUES (?, ?, ?)",
        (org_id, email, infer_sender_channel(email)),
    )
    return int(cur.lastrowid)


_SYNC_PAYLOAD_COLUMNS = sorted(
    set(_CSV_TO_COLUMN.values())
    | {"tags_json", "linkedin_url", "linkedin_sales_nav_id", "is_active"}
)


def _sender_account_workspace_slugs(conn: sqlite3.Connection, sender_account_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT w.slug FROM workspace_sender_accounts wsa
           INNER JOIN workspaces w ON w.id = wsa.workspace_id
           WHERE wsa.sender_account_id = ?
           ORDER BY w.slug""",
        (sender_account_id,),
    ).fetchall()
    return [r["slug"] for r in rows]


def build_sender_account_sync_payload(conn: sqlite3.Connection, sender_account_id: int) -> dict:
    row = conn.execute(
        f"SELECT {', '.join(_SYNC_PAYLOAD_COLUMNS)} FROM sender_accounts WHERE id = ?",
        (sender_account_id,),
    ).fetchone()
    if not row:
        return {}
    payload: dict = {}
    for col in _SYNC_PAYLOAD_COLUMNS:
        val = row[col]
        if val is not None and val != "":
            payload[col] = val
    # Always present, even empty -- this is a full snapshot of current
    # workspace membership, not a delta, so an empty list is itself
    # meaningful (every link was removed) and must reconcile on pull.
    payload["workspace_slugs"] = _sender_account_workspace_slugs(conn, sender_account_id)
    return payload


def inspect_sync_sender_account(conn: sqlite3.Connection, sender_account_id: int) -> dict:
    """Full sender_account_update payload for one sender account, for sync auditing/troubleshooting."""
    row = conn.execute(
        "SELECT id, email FROM sender_accounts WHERE id = ?", (sender_account_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        "sender_account_id": row["id"],
        "email": row["email"],
        "full_sync_payload": build_sender_account_sync_payload(conn, sender_account_id),
    }


def apply_agent_sender_account_sync_payload(sender_account_id: int, payload: dict, *, conn=None) -> None:
    own_conn = conn is None
    conn = conn or get_conn()
    columns = [c for c in _SYNC_PAYLOAD_COLUMNS if c in payload]
    if columns:
        set_clause = ", ".join(f"{c} = ?" for c in columns)
        conn.execute(
            f"UPDATE sender_accounts SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            [payload.get(c) for c in columns] + [sender_account_id],
        )
    if payload.get("email"):
        _classify_and_store_identifier(conn, sender_account_id, payload["email"])
    if "workspace_slugs" in payload:
        _reconcile_workspace_links(conn, sender_account_id, payload["workspace_slugs"])
    if own_conn:
        conn.commit()
        conn.close()


def _reconcile_workspace_links(conn: sqlite3.Connection, sender_account_id: int, workspace_slugs: list[str]) -> None:
    """Make local workspace_sender_accounts links match the incoming full set.

    Unknown slugs (a workspace not yet synced to this DB) are skipped, not
    errored -- they'll reconcile correctly on a later pull once that
    workspace exists locally. Queries `workspaces` directly on the caller's
    connection rather than list_workspaces(), which opens its own
    connection and would deadlock against an open write transaction here.
    """
    slug_to_id = {
        r["slug"]: r["id"] for r in conn.execute("SELECT id, slug FROM workspaces").fetchall()
    }
    incoming_ids = {slug_to_id[s] for s in workspace_slugs if s in slug_to_id}

    current_rows = conn.execute(
        "SELECT workspace_id FROM workspace_sender_accounts WHERE sender_account_id = ?",
        (sender_account_id,),
    ).fetchall()
    current_ids = {r["workspace_id"] for r in current_rows}

    for ws_id in current_ids - incoming_ids:
        unlink_sender_account_from_workspace(conn, ws_id, sender_account_id)
    for ws_id in incoming_ids - current_ids:
        _link_workspace(conn, ws_id, sender_account_id)


def compute_sender_stats(
    conn: sqlite3.Connection, email: str, *, workspace: Optional[str] = None, since: Optional[str] = None
) -> dict:
    """Reply/bounce rates for a sender, computed from local events/bounce_events.

    `since` accepts the same shorthand as the rest of the CLI (`48h`, `7d`,
    `2w`, or an absolute `YYYY-MM-DD`), via read_queries._since_clause --
    which also keeps every clause properly parameterized (params always
    appended in the same order they appear in the SQL text).
    """
    since_clause, since_params = _since_clause(since, column="e.created_at")

    sent_placeholders = ", ".join("?" for _ in _SENT_EVENT_TYPES)
    sent_count = conn.execute(
        f"""SELECT COUNT(*) FROM events e
            WHERE e.sender = ? AND e.event_type IN ({sent_placeholders}){since_clause}""",
        [email] + list(_SENT_EVENT_TYPES) + since_params,
    ).fetchone()[0]

    reply_count = conn.execute(
        f"""SELECT COUNT(*) FROM events e
            WHERE e.sender = ? AND {reply_event_sql_condition()}{since_clause}""",
        [email] + since_params,
    ).fetchone()[0]

    bounce_since_clause, bounce_since_params = _since_clause(since, column="first_seen_at")
    bounce_count = conn.execute(
        f"SELECT COUNT(*) FROM bounce_events WHERE sender_email = ?{bounce_since_clause}",
        [email] + bounce_since_params,
    ).fetchone()[0]

    return {
        "sent_count": sent_count,
        "reply_count": reply_count,
        "reply_rate": round(reply_count / sent_count, 4) if sent_count else None,
        "bounce_count": bounce_count,
        "bounce_rate": round(bounce_count / sent_count, 4) if sent_count else None,
    }


def sender_insights(conn: sqlite3.Connection, workspace: Optional[str] = None, since: Optional[str] = None) -> list[dict]:
    if workspace:
        rows = conn.execute(
            """SELECT sa.* FROM sender_accounts sa
               INNER JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
               INNER JOIN workspaces w ON w.id = wsa.workspace_id
               WHERE w.slug = ?
               ORDER BY sa.email""",
            (workspace,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM sender_accounts ORDER BY email").fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json", None) or "[]")
        item.update(compute_sender_stats(conn, row["email"], since=since))
        results.append(item)
    return results


def set_sender_domain_cost(
    domain: str, *, reseller: Optional[str] = None, domain_cost: Optional[float] = None,
    currency: Optional[str] = None, notes: Optional[str] = None,
    sending_ip: Optional[str] = None, purpose: Optional[str] = None,
    company_id: Optional[int] = None, is_active: Optional[int] = None,
) -> dict:
    """Set/update the flat cost, reseller, notes, purpose, and/or owning company
    for a domain.

    domain_cost is a single hand-computed number covering every mailbox on
    that domain (e.g. $3.50/mailbox x 2 mailboxes = $7), not a per-account
    rate -- there's no billing-model split here, just one number per domain.

    Registers the domain even with every field left unset (bare
    `--domain X`) -- this is how you track a domain you already own but
    haven't set any sender accounts up on yet. `notes` is a single
    freeform field (e.g. "blacklisted in Azure") -- setting it again
    overwrites the previous note, it isn't a history log.

    `purpose` labels what the domain is for (sending / branch / email_finding);
    `company_id` optionally links it to an owning company so the company panel
    can list multiple domains.
    """
    domain = normalize_domain(domain)
    if not domain:
        return {"status": "error", "error": "domain is required"}
    if purpose is not None and purpose not in SENDER_DOMAIN_PURPOSES:
        return {"status": "error",
                "error": f"purpose must be one of {', '.join(SENDER_DOMAIN_PURPOSES)}"}
    conn = get_conn()
    try:
        existing = conn.execute("SELECT domain FROM sender_domains WHERE domain = ?", (domain,)).fetchone()
        if existing:
            sets = ["updated_at = datetime('now')"]
            params: list = []
            if reseller is not None:
                sets.append("reseller = ?")
                params.append(reseller)
            if domain_cost is not None:
                sets.append("domain_cost = ?")
                params.append(domain_cost)
            if currency is not None:
                sets.append("currency = ?")
                params.append(currency)
            if notes is not None:
                sets.append("notes = ?")
                params.append(notes)
            if sending_ip is not None:
                sets.append("sending_ip = ?")
                params.append(sending_ip)
            if purpose is not None:
                sets.append("purpose = ?")
                params.append(purpose)
            if company_id is not None:
                sets.append("company_id = ?")
                params.append(company_id)
            if is_active is not None:
                sets.append("is_active = ?")
                params.append(int(is_active))
            conn.execute(f"UPDATE sender_domains SET {', '.join(sets)} WHERE domain = ?", params + [domain])
        else:
            conn.execute(
                "INSERT INTO sender_domains (domain, reseller, domain_cost, currency, notes, sending_ip, purpose, company_id, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (domain, reseller, domain_cost, currency or "USD", notes, sending_ip,
                 purpose or "sending", company_id, 1 if is_active is None else int(is_active)),
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "domain": domain}


# company_domains() used to live here: it read sender_domains WHERE company_id,
# and the company pane rendered its result alongside company_detail's
# identity-derived list, both under the heading "this company's domains". They
# are not the same thing. sender_domains is YOUR cold-email sending
# infrastructure; a prospect's domains live in company_identities, which is what
# email finding walks and dedup matches on. The company pane now shows only the
# latter (see dashboard_queries.company_detail).
#
# sender_domains.company_id / .purpose stay as columns -- no production row ever
# used the link (every one is company_id IS NULL), so there is nothing to
# migrate, and dropping columns means a table rebuild. They are simply not
# surfaced as a company's domains anywhere.


def get_sender_domains_for_scan(domain: Optional[str] = None) -> list[dict]:
    """Return {domain, sending_ip, dnsbl_status} for one or all registered domains.

    Covers the union of domains in sender_domains and domains only known via
    sender_accounts (same pattern as sender_domains_report), so a domain with
    mailboxes but no cost row is still scannable.
    """
    conn = get_conn()
    try:
        if domain:
            target = normalize_domain(domain)
            rows = conn.execute(
                """SELECT d.domain AS domain, sd.sending_ip AS sending_ip, sd.dnsbl_status AS dnsbl_status
                   FROM (
                       SELECT ? AS domain
                   ) d
                   LEFT JOIN sender_domains sd ON sd.domain = d.domain""",
                (target,),
            ).fetchall()
        else:
            # Decommissioned domains (sender_domains.is_active = 0) and domains
            # known only through decommissioned mailboxes drop out of the bulk
            # scan; an explicit `--domain X` above still scans on demand.
            rows = conn.execute(
                """WITH all_domains AS (
                       SELECT domain FROM sender_domains WHERE is_active = 1
                       UNION
                       SELECT DISTINCT email_domain AS domain FROM sender_accounts
                       WHERE email_domain IS NOT NULL AND is_active = 1
                   )
                   SELECT d.domain AS domain, sd.sending_ip AS sending_ip, sd.dnsbl_status AS dnsbl_status
                   FROM all_domains d
                   LEFT JOIN sender_domains sd ON sd.domain = d.domain
                   WHERE sd.is_active IS NULL OR sd.is_active = 1
                   ORDER BY d.domain""",
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def update_sender_domain_blacklist_status(domain: str, dnsbl_block: dict) -> dict:
    """Write only the dnsbl_status column for a domain (own column, no read-merge with notes)."""
    domain = normalize_domain(domain)
    if not domain:
        return {"status": "error", "error": "domain is required"}
    payload = json.dumps(dnsbl_block)
    conn = get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO sender_domains (domain) VALUES (?)", (domain,))
        # (domain already quote-stripped above via normalize_domain)
        conn.execute(
            "UPDATE sender_domains SET dnsbl_status = ?, updated_at = datetime('now') WHERE domain = ?",
            (payload, domain),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "domain": domain}


def run_blacklist_check(domain: Optional[str] = None, tier: str = "all") -> dict:
    """Scan one or all registered domains against DNSBLs, persist results, flag newly-listed.

    Reads each domain's prior dnsbl_status before overwriting so a clean->listed
    (all_clean true->false) transition can be reported as newly-listed.
    """
    tiers = blacklist_monitor.select_tiers(tier)
    domains = get_sender_domains_for_scan(domain)
    results = []
    newly_listed = []
    any_listed = False
    for d in domains:
        prior_all_clean = None
        raw_prior = d.get("dnsbl_status")
        if raw_prior:
            try:
                prior_all_clean = json.loads(raw_prior).get("all_clean")
            except (ValueError, TypeError):
                prior_all_clean = None
        block = blacklist_monitor.scan_domain(d["domain"], d.get("sending_ip"), tiers)
        update_sender_domain_blacklist_status(d["domain"], block)
        if not block["all_clean"]:
            any_listed = True
            # Only a genuine clean->listed flip is "newly listed"; a first-ever
            # scan or a still-listed domain is not a transition.
            if prior_all_clean is True:
                newly_listed.append(d["domain"])
        results.append(
            {
                "domain": d["domain"],
                "sending_ip": d.get("sending_ip"),
                "all_clean": block["all_clean"],
                "summary": block["summary"],
            }
        )
    return {
        "status": "ok",
        "tier": tier,
        "domains_checked": len(results),
        "any_listed": any_listed,
        "newly_listed": newly_listed,
        "results": results,
    }


def blacklist_status_report(domain: Optional[str] = None, stale_hours: Optional[int] = None) -> dict:
    """Read stored dnsbl_status without rescanning: clean/listed/unchecked/stale counts."""
    domains = get_sender_domains_for_scan(domain)
    now = datetime.now(timezone.utc)
    clean = listed = unchecked = stale = 0
    items = []
    for d in domains:
        raw = d.get("dnsbl_status")
        block = None
        if raw:
            try:
                block = json.loads(raw)
            except (ValueError, TypeError):
                block = None
        if not block:
            unchecked += 1
            items.append({"domain": d["domain"], "state": "unchecked", "checked_at": None})
            continue
        all_clean = block.get("all_clean")
        checked_at = block.get("checked_at")
        is_stale = False
        if stale_hours and checked_at:
            try:
                ts = datetime.fromisoformat(checked_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now - ts).total_seconds() > stale_hours * 3600:
                    is_stale = True
                    stale += 1
            except ValueError:
                pass
        if all_clean:
            clean += 1
            state = "clean"
        else:
            listed += 1
            state = "listed"
        items.append(
            {
                "domain": d["domain"],
                "state": state,
                "checked_at": checked_at,
                "stale": is_stale,
                "summary": block.get("summary"),
            }
        )
    return {
        "status": "ok",
        "counts": {"clean": clean, "listed": listed, "unchecked": unchecked, "stale": stale},
        "domains": items,
    }


_SENDER_DOMAIN_SYNC_COLUMNS = ("reseller", "domain_cost", "currency", "notes", "sending_ip", "dnsbl_status", "is_active")


def sender_domain_entity_key(domain: str) -> str:
    return f"sender_domain:{normalize_domain(domain)}"


def resolve_sender_domain_from_entity_key(conn: sqlite3.Connection, entity_key: str) -> Optional[str]:
    if not entity_key.startswith("sender_domain:"):
        return None
    domain = normalize_domain(entity_key.split(":", 1)[1])
    if not domain:
        return None
    row = conn.execute("SELECT domain FROM sender_domains WHERE domain = ?", (domain,)).fetchone()
    if row:
        return row["domain"]
    conn.execute("INSERT INTO sender_domains (domain) VALUES (?)", (domain,))
    return domain


def build_sender_domain_sync_payload(conn: sqlite3.Connection, domain: str) -> dict:
    row = conn.execute(
        f"SELECT {', '.join(_SENDER_DOMAIN_SYNC_COLUMNS)} FROM sender_domains WHERE domain = ?",
        (domain,),
    ).fetchone()
    if not row:
        return {}
    payload: dict = {}
    for col in _SENDER_DOMAIN_SYNC_COLUMNS:
        val = row[col]
        if val is not None and val != "":
            payload[col] = val
    return payload


def inspect_sync_sender_domain(conn: sqlite3.Connection, domain: str) -> dict:
    """Full sender_domain_update payload for one domain, for sync auditing/troubleshooting."""
    payload = build_sender_domain_sync_payload(conn, domain)
    if not payload:
        return {}
    return {
        "domain": domain,
        "full_sync_payload": payload,
    }


def apply_agent_sender_domain_sync_payload(domain: str, payload: dict, *, conn=None) -> None:
    own_conn = conn is None
    conn = conn or get_conn()
    columns = [c for c in _SENDER_DOMAIN_SYNC_COLUMNS if c in payload]
    if columns:
        set_clause = ", ".join(f"{c} = ?" for c in columns)
        conn.execute(
            f"UPDATE sender_domains SET {set_clause}, updated_at = datetime('now') WHERE domain = ?",
            [payload.get(c) for c in columns] + [domain],
        )
    if own_conn:
        conn.commit()
        conn.close()


def sender_domains_report() -> list[dict]:
    """Per-domain sender count (live, never stored) alongside hand-entered cost/reseller/notes.

    Covers the union of domains registered in sender_domains (including
    ones with zero accounts -- e.g. a domain you own but haven't set any
    mailboxes up on yet) and domains only known via sender_accounts (not
    yet cost-tracked) -- either side alone would silently drop the other.
    """
    conn = get_conn()
    try:
        rows = conn.execute("""
            WITH all_domains AS (
                SELECT domain FROM sender_domains
                UNION
                SELECT DISTINCT email_domain AS domain FROM sender_accounts WHERE email_domain IS NOT NULL
            )
            SELECT
                d.domain AS domain,
                sd.reseller AS reseller,
                sd.domain_cost AS domain_cost,
                COALESCE(sd.currency, 'USD') AS currency,
                sd.notes AS notes,
                (SELECT COUNT(*) FROM sender_accounts sa
                 WHERE sa.email_domain = d.domain AND sa.is_active = 1) AS sender_count
            FROM all_domains d
            LEFT JOIN sender_domains sd ON sd.domain = d.domain
            ORDER BY d.domain
        """).fetchall()
    finally:
        conn.close()
    results = []
    for row in rows:
        item = dict(row)
        item["cost_per_account"] = (
            round(item["domain_cost"] / item["sender_count"], 4)
            if item["domain_cost"] is not None and item["sender_count"]
            else None
        )
        results.append(item)
    return results


def _months_elapsed(since_iso: Optional[str]) -> int:
    """Whole months between an ISO timestamp and now, minimum 1.

    Used to project an "all time" total from a monthly recurring
    domain_cost. Approximate -- anchored to when the cost was first
    recorded (sender_domains.created_at), not necessarily true billing
    start, since we have no other signal for that locally.
    """
    if not since_iso:
        return 1
    try:
        since_dt = datetime.fromisoformat(str(since_iso).replace("Z", "+00:00"))
    except ValueError:
        return 1
    now = datetime.now(since_dt.tzinfo) if since_dt.tzinfo else datetime.now()
    days = (now - since_dt).total_seconds() / 86400
    return max(1, round(days / 30.4375))


def _positive_lead_count_sql(months: Optional[int]) -> tuple[str, list]:
    """WHERE-clause fragment + params for a positive-sentiment lead count.

    months=None -> lifetime (no time filter). months=N -> only leads
    touched (workspace_leads.updated_at) in the trailing N months --
    this is what makes a windowed cost_per_positive an apples-to-apples
    rate rather than a stale lifetime count divided by a fresh cost.
    """
    if months is None:
        return "", []
    return " AND updated_at >= datetime('now', ?)", [f"-{months} months"]


def _domain_cost_rows_for_workspace(conn: sqlite3.Connection, ws_id: str) -> list:
    return conn.execute("""
        SELECT sa.id, sd.domain_cost, sd.created_at AS domain_created_at,
               (SELECT COUNT(*) FROM sender_accounts sa2
                WHERE sa2.email_domain = sa.email_domain
                  AND sa2.is_active = 1) AS domain_sender_count
        FROM sender_accounts sa
        INNER JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
        LEFT JOIN sender_domains sd ON sd.domain = sa.email_domain
        WHERE wsa.workspace_id = ? AND sa.is_active = 1
    """, (ws_id,)).fetchall()


def _cost_window(rows: list, *, months: Optional[int]) -> dict:
    """Sum each priced account's monthly share x its window multiplier.

    months=None -> "all time": each domain's own elapsed-months-since-tracked
    (so domains tracked for different lengths of time don't get treated the
    same). months=N -> every domain multiplied by the same N.
    """
    total_cost = 0.0
    priced_accounts = 0
    for r in rows:
        if r["domain_cost"] is not None and r["domain_sender_count"]:
            monthly_share = r["domain_cost"] / r["domain_sender_count"]
            window = months if months is not None else _months_elapsed(r["domain_created_at"])
            total_cost += monthly_share * window
            priced_accounts += 1
    return {"total_cost": round(total_cost, 2), "priced_accounts": priced_accounts}


def workspace_sender_cost_report(workspace: str, *, months: Optional[int] = None) -> dict:
    """Sender-account cost for a workspace + cost per positive-sentiment lead,
    reported both all-time and per-month (or over a custom N-month window).

    domain_cost is treated as a recurring MONTHLY rate, split evenly across
    every sender account currently on that domain (live count); an account
    linked to more than one workspace counts its full share toward each
    workspace it serves.

    `all_time` multiplies each domain's rate by however long that domain's
    cost has been tracked, and counts positive leads lifetime. `windowed`
    (months=1 by default, i.e. "per month") multiplies the rate by
    `months` and counts positive leads only from the trailing `months`
    months, so cost and results are always compared over the same period.
    """
    conn = get_conn()
    try:
        ws = conn.execute("SELECT id FROM workspaces WHERE slug = ?", (workspace,)).fetchone()
        if not ws:
            return {"status": "error", "error": f"unknown workspace: {workspace}"}
        ws_id = ws["id"]
        rows = _domain_cost_rows_for_workspace(conn, ws_id)

        window_months = months if months is not None else 1
        priced_accounts = 0
        results = {}
        for label, m in (("all_time", None), ("windowed", window_months)):
            cost = _cost_window(rows, months=m)
            priced_accounts = cost["priced_accounts"]
            clause, params = _positive_lead_count_sql(m)
            positive_count = conn.execute(
                f"""SELECT COUNT(*) FROM workspace_leads
                    WHERE workspace_id = ? AND lower(current_status_sentiment) = 'positive'{clause}""",
                [ws_id] + params,
            ).fetchone()[0]
            entry = {
                "total_cost": cost["total_cost"],
                "positive_sentiment_leads": positive_count,
                "cost_per_positive": (
                    round(cost["total_cost"] / positive_count, 2) if positive_count else None
                ),
            }
            if label == "windowed":
                entry["months"] = window_months
            results[label] = entry
    finally:
        conn.close()

    return {
        "status": "ok",
        "workspace": workspace,
        "sender_account_count": len(rows),
        "priced_sender_account_count": priced_accounts,
        **results,
    }


def reseller_cost_report(reseller: str, *, months: Optional[int] = None) -> dict:
    """Cost across a reseller's domains + cost per positive-sentiment lead,
    reported both all-time and per-month (or over a custom N-month window).

    Approximate when a workspace mixes accounts from multiple resellers --
    that workspace's positive-lead count gets attributed in full to each
    reseller it uses, matching the same "count in full per workspace" choice
    used for per-workspace cost.
    """
    conn = get_conn()
    try:
        domains = conn.execute(
            "SELECT domain, domain_cost, created_at FROM sender_domains WHERE reseller = ?", (reseller,)
        ).fetchall()
        if not domains:
            return {"status": "error", "error": f"no domains found for reseller: {reseller}"}
        domain_list = [d["domain"] for d in domains]

        placeholders = ", ".join("?" for _ in domain_list)
        ws_rows = conn.execute(
            f"""SELECT DISTINCT w.id, w.slug
                FROM sender_accounts sa
                INNER JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
                INNER JOIN workspaces w ON w.id = wsa.workspace_id
                WHERE sa.email_domain IN ({placeholders})""",
            domain_list,
        ).fetchall()
        ws_ids = [w["id"] for w in ws_rows]

        window_months = months if months is not None else 1
        results = {}
        for label, m in (("all_time", None), ("windowed", window_months)):
            if m is None:
                total_cost = sum((d["domain_cost"] or 0) * _months_elapsed(d["created_at"]) for d in domains)
            else:
                total_cost = sum((d["domain_cost"] or 0) * m for d in domains)
            positive_count = 0
            if ws_ids:
                clause, params = _positive_lead_count_sql(m)
                ws_placeholders = ", ".join("?" for _ in ws_ids)
                positive_count = conn.execute(
                    f"""SELECT COUNT(*) FROM workspace_leads
                        WHERE workspace_id IN ({ws_placeholders})
                          AND lower(current_status_sentiment) = 'positive'{clause}""",
                    ws_ids + params,
                ).fetchone()[0]
            entry = {
                "total_cost": round(total_cost, 2),
                "positive_sentiment_leads": positive_count,
                "cost_per_positive": (
                    round(total_cost / positive_count, 2) if positive_count else None
                ),
            }
            if label == "windowed":
                entry["months"] = window_months
            results[label] = entry
    finally:
        conn.close()

    return {
        "status": "ok",
        "reseller": reseller,
        "domains": domain_list,
        "workspaces_served": sorted(w["slug"] for w in ws_rows),
        **results,
    }
