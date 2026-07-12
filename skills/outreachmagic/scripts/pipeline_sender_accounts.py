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
from datetime import datetime
from typing import Optional

from campaign_stats import reply_event_sql_condition
from db_conn import get_conn
from pipeline_utils import email_domain, normalize_email
from workspace_routing import DEFAULT_ORG_ID, parse_linkedin_value

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
    email = row["email"]
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


def ensure_sender_account(
    conn: sqlite3.Connection, identifier: str, channel: str = "email", org_id: str = DEFAULT_ORG_ID,
) -> Optional[int]:
    """Bootstrap a bare sender_accounts row from an event's sender, if it doesn't exist yet.

    Cheap INSERT OR IGNORE -- never overwrites richer data from a CSV import
    (warmup/health scores etc), only fills in accounts we've never seen
    before (this is the primary creation path for Prosp/LinkedIn accounts,
    which have no CSV export).
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO sender_accounts (org_id, email, channel) VALUES (?, ?, ?)",
        (org_id, identifier, channel),
    )
    row = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, identifier)
    ).fetchone()
    if not row:
        return None
    sender_account_id = int(row["id"])
    _classify_and_store_identifier(conn, sender_account_id, identifier)
    return sender_account_id


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
        (org_id, (email or "").strip().lower()),
    ).fetchone()
    return int(row["id"]) if row else None


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
    return f"sender_account:{row['email'].strip().lower()}"


def resolve_sender_account_from_entity_key(
    conn: sqlite3.Connection, entity_key: str, org_id: str = DEFAULT_ORG_ID
) -> Optional[int]:
    if not entity_key.startswith("sender_account:"):
        return None
    email = entity_key.split(":", 1)[1]
    if not email:
        return None
    row = conn.execute(
        "SELECT id FROM sender_accounts WHERE org_id = ? AND email = ?", (org_id, email)
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO sender_accounts (org_id, email) VALUES (?, ?)", (org_id, email)
    )
    return int(cur.lastrowid)


_SYNC_PAYLOAD_COLUMNS = sorted(
    set(_CSV_TO_COLUMN.values()) | {"tags_json", "linkedin_url", "linkedin_sales_nav_id"}
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
    """Reply/bounce rates for a sender, computed from local events/bounce_events."""
    params: list = [email]
    since_clause = ""
    if since:
        since_clause = " AND e.created_at >= ?"
        params.append(since)

    sent_placeholders = ", ".join("?" for _ in _SENT_EVENT_TYPES)
    sent_count = conn.execute(
        f"""SELECT COUNT(*) FROM events e
            WHERE e.sender = ? AND e.event_type IN ({sent_placeholders}){since_clause}""",
        params + list(_SENT_EVENT_TYPES),
    ).fetchone()[0]

    reply_params = [email] + ([since] if since else [])
    reply_count = conn.execute(
        f"""SELECT COUNT(*) FROM events e
            WHERE e.sender = ? AND {reply_event_sql_condition()}{since_clause}""",
        reply_params,
    ).fetchone()[0]

    bounce_params: list = [email]
    bounce_since_clause = ""
    if since:
        bounce_since_clause = " AND first_seen_at >= ?"
        bounce_params.append(since)
    bounce_count = conn.execute(
        f"SELECT COUNT(*) FROM bounce_events WHERE sender_email = ?{bounce_since_clause}",
        bounce_params,
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
    currency: Optional[str] = None,
) -> dict:
    """Set/update the flat cost + reseller for a domain's sender accounts.

    domain_cost is a single hand-computed number covering every mailbox on
    that domain (e.g. $3.50/mailbox x 2 mailboxes = $7), not a per-account
    rate -- there's no billing-model split here, just one number per domain.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"status": "error", "error": "domain is required"}
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
            conn.execute(f"UPDATE sender_domains SET {', '.join(sets)} WHERE domain = ?", params + [domain])
        else:
            conn.execute(
                "INSERT INTO sender_domains (domain, reseller, domain_cost, currency) VALUES (?, ?, ?, ?)",
                (domain, reseller, domain_cost, currency or "USD"),
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "domain": domain}


def sender_domains_report() -> list[dict]:
    """Per-domain sender count (live, never stored) alongside hand-entered cost/reseller."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT
                sa.email_domain AS domain,
                sd.reseller AS reseller,
                sd.domain_cost AS domain_cost,
                COALESCE(sd.currency, 'USD') AS currency,
                COUNT(sa.id) AS sender_count
            FROM sender_accounts sa
            LEFT JOIN sender_domains sd ON sd.domain = sa.email_domain
            WHERE sa.email_domain IS NOT NULL
            GROUP BY sa.email_domain
            ORDER BY sa.email_domain
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


def workspace_sender_cost_report(workspace: str) -> dict:
    """Total sender-account cost for a workspace + cost per positive-sentiment lead.

    Each domain's flat cost is split evenly across every sender account
    currently on that domain (live count); an account linked to more than
    one workspace counts its full share toward each workspace it serves.
    """
    conn = get_conn()
    try:
        ws = conn.execute("SELECT id FROM workspaces WHERE slug = ?", (workspace,)).fetchone()
        if not ws:
            return {"status": "error", "error": f"unknown workspace: {workspace}"}
        ws_id = ws["id"]

        rows = conn.execute("""
            SELECT sa.id, sd.domain_cost,
                   (SELECT COUNT(*) FROM sender_accounts sa2
                    WHERE sa2.email_domain = sa.email_domain) AS domain_sender_count
            FROM sender_accounts sa
            INNER JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
            LEFT JOIN sender_domains sd ON sd.domain = sa.email_domain
            WHERE wsa.workspace_id = ?
        """, (ws_id,)).fetchall()

        total_cost = 0.0
        priced_accounts = 0
        for r in rows:
            if r["domain_cost"] is not None and r["domain_sender_count"]:
                total_cost += r["domain_cost"] / r["domain_sender_count"]
                priced_accounts += 1

        positive_count = conn.execute(
            """SELECT COUNT(*) FROM workspace_leads
               WHERE workspace_id = ? AND lower(current_status_sentiment) = 'positive'""",
            (ws_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "ok",
        "workspace": workspace,
        "sender_account_count": len(rows),
        "priced_sender_account_count": priced_accounts,
        "total_cost": round(total_cost, 2),
        "positive_sentiment_leads": positive_count,
        "cost_per_positive": round(total_cost / positive_count, 2) if positive_count else None,
    }


def reseller_cost_report(reseller: str) -> dict:
    """Total cost across a reseller's domains + cost per positive-sentiment lead
    across every workspace those domains' sender accounts serve.

    Approximate when a workspace mixes accounts from multiple resellers --
    that workspace's positive-lead count gets attributed in full to each
    reseller it uses, matching the same "count in full per workspace" choice
    used for per-workspace cost.
    """
    conn = get_conn()
    try:
        domains = conn.execute(
            "SELECT domain, domain_cost FROM sender_domains WHERE reseller = ?", (reseller,)
        ).fetchall()
        if not domains:
            return {"status": "error", "error": f"no domains found for reseller: {reseller}"}
        domain_list = [d["domain"] for d in domains]
        total_cost = sum(d["domain_cost"] or 0 for d in domains)

        placeholders = ", ".join("?" for _ in domain_list)
        ws_rows = conn.execute(
            f"""SELECT DISTINCT w.id, w.slug
                FROM sender_accounts sa
                INNER JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
                INNER JOIN workspaces w ON w.id = wsa.workspace_id
                WHERE sa.email_domain IN ({placeholders})""",
            domain_list,
        ).fetchall()

        positive_count = 0
        if ws_rows:
            ws_ids = [w["id"] for w in ws_rows]
            ws_placeholders = ", ".join("?" for _ in ws_ids)
            positive_count = conn.execute(
                f"""SELECT COUNT(*) FROM workspace_leads
                    WHERE workspace_id IN ({ws_placeholders}) AND lower(current_status_sentiment) = 'positive'""",
                ws_ids,
            ).fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "ok",
        "reseller": reseller,
        "domains": domain_list,
        "total_cost": round(total_cost, 2),
        "workspaces_served": sorted(w["slug"] for w in ws_rows),
        "positive_sentiment_leads": positive_count,
        "cost_per_positive": round(total_cost / positive_count, 2) if positive_count else None,
    }
