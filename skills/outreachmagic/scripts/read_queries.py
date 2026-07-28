"""
Read-only analytics queries for Outreach Magic.

Agents should prefer `pipeline.py query <preset>` over ad-hoc table exploration.
Writes stay in pipeline mutation commands only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Optional

from db_conn import get_conn
from event_classification import normalize_campaign_event_type
from om_paths import get_db_path
from platform_registry import reply_event_sql_condition
from shared import MV_ATTEMPTED_TAG, SCRUBBY_DEEP_ATTEMPTED_TAG, SERPER_ATTEMPTED_TAG
from workspace_routing import resolve_workspace_identity

# Events that carry lead status / sentiment for current-state filters.
STATUS_METADATA_PREDICATE = """(
    json_extract(e.metadata_json, '$.lead_status_sentiment') IS NOT NULL
    OR json_extract(e.metadata_json, '$.lead_status_raw') IS NOT NULL
    OR CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) = 1
)"""

LATEST_STATUS_CTE = f"""
WITH ranked_status AS (
  SELECT
    e.lead_id,
    lower(json_extract(e.metadata_json, '$.lead_status_sentiment')) AS current_sentiment,
    json_extract(e.metadata_json, '$.lead_status_raw') AS current_lead_status_raw,
    json_extract(e.metadata_json, '$.lead_status_display') AS current_lead_status_display,
    CAST(json_extract(e.metadata_json, '$.is_auto_reply') AS INTEGER) AS current_is_auto_reply,
    e.created_at AS status_at,
    e.campaign_id,
    ROW_NUMBER() OVER (
      PARTITION BY e.lead_id
      ORDER BY e.created_at DESC, e.id DESC
    ) AS rn
  FROM events e
  WHERE {STATUS_METADATA_PREDICATE}
)
"""

DEFAULT_ROW_LIMIT = 500
DEFAULT_TIMEOUT_SEC = 30

_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA\s+\w+\s*=)\b",
    re.IGNORECASE,
)


def normalize_since(since: Optional[str]) -> Optional[str]:
    """Return SQLite datetime expression or YYYY-MM-DD date literal for comparisons."""
    if not since:
        return None
    raw = since.strip()
    low = raw.lower()
    if low == "today":
        return datetime.now().strftime("%Y-%m-%d")
    m = re.match(r"^(\d+)\s*h(?:ours?)?$", low)
    if m:
        return f"datetime('now', '-{int(m.group(1))} hours')"
    m = re.match(r"^(\d+)\s*d(?:ays?)?$", low)
    if m:
        return f"datetime('now', '-{int(m.group(1))} days')"
    m = re.match(r"^(\d+)\s*w(?:eeks?)?$", low)
    if m:
        return f"datetime('now', '-{int(m.group(1)) * 7} days')"
    return raw


def workspace_campaign_prefix(workspace: Optional[str], campaign_prefix: Optional[str]) -> str:
    if campaign_prefix:
        return campaign_prefix.strip()
    slug = (workspace or "").strip()
    if not slug:
        return "%"
    return f"{slug} |%"


def _since_clause(since: Optional[str], column: str = "e.created_at") -> tuple[str, list]:
    """Build a safe, parameterized SQL clause for the since filter.

    Relative expressions (e.g. '48h', '7d', '2w') use parameterised
    datetime modifiers so user-supplied strings are never interpolated
    as SQL. Absolute dates ('2026-05-26', 'today') are passed as bound
    parameters.
    """
    raw = (since or "").strip()
    if not raw:
        return "", []
    low = raw.lower()
    if low == "all":  # the "all available" preset: no date bound at all
        return "", []
    # Calendar-day presets. Without these, "today" fell through to the absolute
    # date branch below and compared a timestamp column to the literal string
    # 'today', which matches nothing and reports it as an empty range rather
    # than as an error.
    if low == "today":
        return f" AND {column} >= date('now', 'start of day')", []
    if low == "yesterday":
        # The only closed-interval preset: it emits both bounds itself, so a
        # caller's `until` would be redundant and is simply narrower if given.
        return (
            f" AND {column} >= date('now', '-1 day', 'start of day')"
            f" AND {column} < date('now', 'start of day')"
        ), []
    m = re.match(r"^(\d+)\s*h(?:ours?)?$", low)
    if m:
        return f" AND {column} >= datetime('now', ?)", [f"-{int(m.group(1))} hours"]
    m = re.match(r"^(\d+)\s*d(?:ays?)?$", low)
    if m:
        return f" AND {column} >= datetime('now', ?)", [f"-{int(m.group(1))} days"]
    m = re.match(r"^(\d+)\s*w(?:eeks?)?$", low)
    if m:
        return f" AND {column} >= datetime('now', ?)", [f"-{int(m.group(1)) * 7} days"]
    # Absolute date string — parameterize as a simple value
    return f" AND {column} >= ?", [raw]


def engagement_by_campaign(
    *,
    workspace: Optional[str] = None,
    campaign_prefix: Optional[str] = None,
    since: Optional[str] = None,
    direction: str = "inbound",
    event_types: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Inbound (or custom) engagement counts by campaign name and event_type."""
    prefix = workspace_campaign_prefix(workspace, campaign_prefix)
    since_sql, since_params = _since_clause(since)
    dir_sql = ""
    params: list[Any] = [prefix]
    if direction:
        dir_sql = " AND lower(coalesce(e.direction, '')) = lower(?)"
        params.append(direction)
    type_sql = ""
    if event_types:
        placeholders = ", ".join("?" for _ in event_types)
        type_sql = f" AND e.event_type IN ({placeholders})"
        params.extend(event_types)
    params.extend(since_params)
    sql = f"""
        SELECT c.name AS campaign, e.event_type, COUNT(*) AS count
        FROM events e
        LEFT JOIN campaigns c ON e.campaign_id = c.id
        WHERE c.name LIKE ?
          {dir_sql}
          {type_sql}
          {since_sql}
        GROUP BY c.name, e.event_type
        ORDER BY count DESC, campaign, e.event_type
    """
    return _run_preset(sql, params, preset="engagement")


def replies_by_campaign(
    *,
    workspace: Optional[str] = None,
    campaign_prefix: Optional[str] = None,
    since: Optional[str] = None,
) -> dict[str, Any]:
    """Reply events (platform_registry reply types) by campaign."""
    prefix = workspace_campaign_prefix(workspace, campaign_prefix)
    since_sql, since_params = _since_clause(since)
    reply_where = reply_event_sql_condition()
    params: list[Any] = [prefix, *since_params]
    sql = f"""
        SELECT c.name AS campaign, e.event_type, COUNT(*) AS count
        FROM events e
        LEFT JOIN campaigns c ON e.campaign_id = c.id
        WHERE c.name LIKE ?
          AND ({reply_where})
          {since_sql}
        GROUP BY c.name, e.event_type
        ORDER BY count DESC, campaign, e.event_type
    """
    return _run_preset(sql, params, preset="replies")


def daily_digest(
    *,
    since: Optional[str] = None,
    workspace: Optional[str] = None,
    campaign_prefix: Optional[str] = None,
    reply_limit: int = 8,
) -> dict[str, Any]:
    """Lightweight counts and highlights for a date window (agent daily briefing)."""
    prefix = workspace_campaign_prefix(workspace, campaign_prefix)
    since_sql, since_params = _since_clause(since)
    reply_where = reply_event_sql_condition()
    params: list[Any] = [prefix, *since_params]

    conn = get_conn()
    try:
        sends = conn.execute(
            f"""
            SELECT COUNT(*) FROM events e
            LEFT JOIN campaigns c ON e.campaign_id = c.id
            WHERE c.name LIKE ?
              AND lower(e.event_type) IN ('email_sent', 'email_sent_auto')
              {since_sql}
            """,
            params,
        ).fetchone()[0]

        replies = conn.execute(
            f"""
            SELECT COUNT(*) FROM events e
            LEFT JOIN campaigns c ON e.campaign_id = c.id
            WHERE c.name LIKE ?
              AND ({reply_where})
              {since_sql}
            """,
            params,
        ).fetchone()[0]

        interested = conn.execute(
            f"""
            SELECT COUNT(*) FROM events e
            LEFT JOIN campaigns c ON e.campaign_id = c.id
            WHERE c.name LIKE ?
              AND (
                lower(json_extract(e.metadata_json, '$.lead_status_sentiment')) = 'positive'
                OR lower(json_extract(e.metadata_json, '$.lead_status_raw')) IN ('interested', 'positive')
                OR lower(e.event_type) LIKE '%interested%'
              )
              {since_sql}
            """,
            params,
        ).fetchone()[0]

        bounces = conn.execute(
            f"""
            SELECT COUNT(*) FROM events e
            LEFT JOIN campaigns c ON e.campaign_id = c.id
            WHERE c.name LIKE ?
              AND (
                lower(e.event_type) IN ('email_bounce', 'bounced_email', 'email_bounced')
                OR lower(json_extract(e.metadata_json, '$.lead_status_sentiment')) = 'invalid'
              )
              {since_sql}
            """,
            params,
        ).fetchone()[0]

        top_row = conn.execute(
            f"""
            SELECT c.name AS campaign, COUNT(*) AS sends
            FROM events e
            LEFT JOIN campaigns c ON e.campaign_id = c.id
            WHERE c.name LIKE ?
              AND lower(e.event_type) IN ('email_sent', 'email_sent_auto')
              {since_sql}
            GROUP BY c.name
            ORDER BY sends DESC
            LIMIT 1
            """,
            params,
        ).fetchone()

        reply_rows = conn.execute(
            f"""
            SELECT l.name, l.company, c.name AS campaign
            FROM events e
            JOIN leads l ON l.id = e.lead_id
            LEFT JOIN campaigns c ON e.campaign_id = c.id
            WHERE c.name LIKE ?
              AND ({reply_where})
              {since_sql}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT ?
            """,
            [*params, max(1, int(reply_limit))],
        ).fetchall()

        # One-line blacklist summary: any sender domain whose stored DNSBL scan
        # is not all_clean. Reuses the existing digest delivery path (no new
        # alerting mechanism). Guarded in case dnsbl_status predates migration.
        listed_domains = []
        try:
            for row in conn.execute(
                "SELECT domain, dnsbl_status FROM sender_domains WHERE dnsbl_status IS NOT NULL"
            ).fetchall():
                try:
                    if json.loads(row["dnsbl_status"]).get("all_clean") is False:
                        listed_domains.append(row["domain"])
                except (ValueError, TypeError):
                    continue
        except sqlite3.OperationalError:
            listed_domains = []
    finally:
        conn.close()

    since_label = normalize_since(since) or since or "all time"
    if isinstance(since_label, str) and since_label.startswith("datetime("):
        since_label = since or "recent"

    highlights = []
    for row in reply_rows:
        name = (row["name"] or "").strip() or "Unknown"
        company = (row["company"] or "").strip()
        highlights.append(
            f"{name} ({company})" if company else name
        )

    return {
        "since": since,
        "since_label": since_label,
        "workspace": workspace,
        "campaign_prefix": prefix,
        "emails_sent": int(sends or 0),
        "replies": int(replies or 0),
        "interested": int(interested or 0),
        "bounces": int(bounces or 0),
        "top_campaign": dict(top_row) if top_row else None,
        "new_replies": highlights,
        "blacklisted_domains": listed_domains,
    }


def format_daily_digest(data: dict[str, Any]) -> str:
    since_label = data.get("since_label") or data.get("since") or "all time"
    lines = [
        f"Date: {since_label}",
        (
            f"Emails sent: {data.get('emails_sent', 0)}  |  "
            f"Replies: {data.get('replies', 0)}  |  "
            f"Interested: {data.get('interested', 0)}  |  "
            f"Bounces: {data.get('bounces', 0)}"
        ),
    ]
    top = data.get("top_campaign")
    if top and top.get("campaign"):
        lines.append(f"Top campaign: {top['campaign']} ({top.get('sends', 0)} sends)")
    replies = data.get("new_replies") or []
    if replies:
        lines.append(f"New replies: {', '.join(replies)}")
    blacklisted = data.get("blacklisted_domains") or []
    if blacklisted:
        lines.append(f"Blacklisted domains: {', '.join(blacklisted)}")
    return "\n".join(lines)


def interested_by_campaign(
    *,
    workspace: Optional[str] = None,
    campaign_prefix: Optional[str] = None,
    since: Optional[str] = None,
) -> dict[str, Any]:
    """Leads whose latest status-bearing event is positive/interested, grouped by campaign."""
    prefix = workspace_campaign_prefix(workspace, campaign_prefix)
    since_sql, since_params = _since_clause(since, column="rs.status_at")
    params: list[Any] = [prefix, *since_params]
    sql = (
        LATEST_STATUS_CTE
        + f"""
        SELECT c.name AS campaign,
               rs.current_lead_status_display AS lead_status,
               rs.current_sentiment AS sentiment,
               COUNT(DISTINCT rs.lead_id) AS lead_count
        FROM ranked_status rs
        LEFT JOIN campaigns c ON rs.campaign_id = c.id
        WHERE rs.rn = 1
          AND c.name LIKE ?
          AND (
            lower(rs.current_sentiment) IN ('positive', 'interested')
            OR lower(rs.current_lead_status_raw) IN ('interested', 'positive')
            OR lower(rs.current_lead_status_display) LIKE '%interested%'
          )
          {since_sql}
        GROUP BY c.name, rs.current_lead_status_display, rs.current_sentiment
        ORDER BY lead_count DESC, campaign
        """
    )
    return _run_preset(sql, params, preset="interested")


def _run_preset(sql: str, params: list[Any], *, preset: str) -> dict[str, Any]:
    result = run_readonly_sql(sql, params=params)
    result["preset"] = preset
    return result


def validate_readonly_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise ValueError("SQL is empty")
    if ";" in text.rstrip().rstrip(";"):
        raise ValueError("Only a single SQL statement is allowed")
    if _FORBIDDEN_SQL.search(text):
        raise ValueError("Only read-only SELECT / WITH queries are allowed")
    head = text.lstrip()[:20].upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise ValueError("Query must start with SELECT or WITH")
    return text


def run_readonly_sql(
    sql: str,
    *,
    params: Optional[list[Any]] = None,
    limit: int = DEFAULT_ROW_LIMIT,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Execute one read-only query; returns columns, rows, timing."""
    query = validate_readonly_sql(sql)
    bounded = query.rstrip()
    if not re.search(r"\blimit\b", bounded, re.IGNORECASE):
        bounded = f"{bounded}\nLIMIT {int(limit)}"
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA query_only = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_sec * 1000)}")
        start = time.perf_counter()
        cur = conn.execute(bounded, params or [])
        rows = [dict(r) for r in cur.fetchall()]
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        columns = [d[0] for d in (cur.description or [])]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "elapsed_ms": elapsed_ms,
            "sql": query.strip(),
            "truncated": len(rows) >= limit,
            "limit": limit,
        }
    finally:
        conn.close()


def list_tables() -> dict[str, Any]:
    """All user table names in the connected DB, plus the resolved DB path.

    `db_path` is the cheapest fix for "which database am I even talking to" —
    print it whenever schema looks wrong before assuming a code bug.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return {"tables": [r[0] for r in rows], "db_path": str(get_db_path())}


def table_schema(table: str) -> dict[str, Any]:
    """Column name/type/notnull/default/pk for one table, plus the resolved DB path.

    PRAGMA statements can't bind parameters for identifiers, so `table` is
    validated against sqlite_master (a real catalog lookup) before being
    interpolated into the PRAGMA — this also gives a much better error
    ("table not found, available: ...") than a raw OperationalError.
    """
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            all_tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            raise ValueError(f"Table not found: {table}. Available: {', '.join(all_tables)}")
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    finally:
        conn.close()
    return {
        "table": table,
        "columns": [
            {"name": c[1], "type": c[2], "notnull": bool(c[3]), "default": c[4], "pk": bool(c[5])}
            for c in cols
        ],
        "db_path": str(get_db_path()),
    }


# ---------------------------------------------------------------------------
# Tag-group summary
# ---------------------------------------------------------------------------

# Known {provider}_attempted tags, classified so the summary doesn't lump
# email-finding / email-verification activity in with lead research.
# Unknown "%_attempted" tags fall back to category "other" (see tag_summary)
# so a brand-new provider still shows up automatically — no config needed.
ATTEMPT_TAG_META: dict[str, dict[str, str]] = {
    SERPER_ATTEMPTED_TAG: {"category": "research", "label": "Serper"},
    "trykitt_attempted": {"category": "email_finding", "label": "TryKitt"},
    "icypeas_attempted": {"category": "email_finding", "label": "Icypeas"},
    MV_ATTEMPTED_TAG: {"category": "email_verification", "label": "MillionVerifier"},
    SCRUBBY_DEEP_ATTEMPTED_TAG: {"category": "email_verification", "label": "Scrubby (deep)"},
}

# Internal job-state tag suffixes excluded from the "segments" bucket (e.g.
# scrubby_deep_submitted is a pipeline marker, not a lead segment like
# "speaker" or "exhibitor").
SYSTEM_TAG_SUFFIXES = ("_attempted", "_submitted")


def _attempt_tag_meta(tag: str) -> dict[str, str]:
    meta = ATTEMPT_TAG_META.get(tag)
    if meta:
        return meta
    label = tag[: -len("_attempted")].replace("_", " ").title() if tag.endswith("_attempted") else tag
    return {"category": "other", "label": label}


def tag_summary(*, workspace: str, tag: str) -> dict[str, Any]:
    """Aggregate research/email-quality/segment/status counts for a workspace-scoped tag group.

    `pending` counts reflect current tag-group membership, not attempt
    history — a lead added to the group after a bulk research run will show
    as pending even if it was researched earlier under a different tag.
    """
    conn = get_conn()
    try:
        ws_row = resolve_workspace_identity(conn, workspace)
        if not ws_row:
            raise ValueError(f"workspace not found: {workspace}")
        ws_id = ws_row["id"]

        total_leads = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM workspace_lead_tags WHERE workspace_id = ? AND tag = ?",
            (ws_id, tag),
        ).fetchone()[0]

        attempt_rows = conn.execute(
            """
            SELECT wlt.tag, COUNT(DISTINCT wlt.lead_id) AS lead_count
            FROM workspace_lead_tags wlt
            JOIN workspace_lead_tags tl
              ON tl.workspace_id = wlt.workspace_id AND tl.lead_id = wlt.lead_id
            WHERE wlt.workspace_id = ? AND tl.tag = ?
              AND wlt.tag LIKE '%\\_attempted' ESCAPE '\\'
            GROUP BY wlt.tag
            """,
            (ws_id, tag),
        ).fetchall()

        segment_rows = conn.execute(
            """
            SELECT wlt.tag, COUNT(DISTINCT wlt.lead_id) AS lead_count
            FROM workspace_lead_tags wlt
            JOIN workspace_lead_tags tl
              ON tl.workspace_id = wlt.workspace_id AND tl.lead_id = wlt.lead_id
            WHERE wlt.workspace_id = ? AND tl.tag = ?
              AND wlt.tag NOT LIKE '%\\_attempted' ESCAPE '\\'
              AND wlt.tag NOT LIKE '%\\_submitted' ESCAPE '\\'
              AND wlt.tag != ?
            GROUP BY wlt.tag
            ORDER BY lead_count DESC
            """,
            (ws_id, tag, tag),
        ).fetchall()

        quality_rows = conn.execute(
            """
            SELECT COALESCE(l.email_verification_status, 'unverified') AS status, COUNT(*) AS n
            FROM leads l
            JOIN workspace_lead_tags tl ON tl.lead_id = l.id
            WHERE tl.workspace_id = ? AND tl.tag = ?
            GROUP BY status
            """,
            (ws_id, tag),
        ).fetchall()

        completeness_row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN l.email IS NOT NULL AND l.email != '' THEN 1 ELSE 0 END) AS has_email,
              SUM(CASE WHEN l.linkedin_url IS NOT NULL AND l.linkedin_url != '' THEN 1 ELSE 0 END) AS has_linkedin,
              SUM(CASE WHEN l.company IS NOT NULL AND l.company != '' THEN 1 ELSE 0 END) AS has_company
            FROM leads l
            JOIN workspace_lead_tags tl ON tl.lead_id = l.id
            WHERE tl.workspace_id = ? AND tl.tag = ?
            """,
            (ws_id, tag),
        ).fetchone()

        status_rows = conn.execute(
            """
            SELECT wl.status, COUNT(*) AS n
            FROM workspace_leads wl
            JOIN workspace_lead_tags tl ON tl.workspace_id = wl.workspace_id AND tl.lead_id = wl.lead_id
            WHERE wl.workspace_id = ? AND tl.tag = ?
            GROUP BY wl.status
            """,
            (ws_id, tag),
        ).fetchall()
    finally:
        conn.close()

    research: list[dict[str, Any]] = []
    email_finding: list[dict[str, Any]] = []
    verification_attempts: list[dict[str, Any]] = []
    other_attempts: list[dict[str, Any]] = []
    for row in attempt_rows:
        meta = _attempt_tag_meta(row["tag"])
        attempted = row["lead_count"]
        entry = {
            "tag": row["tag"],
            "label": meta["label"],
            "attempted": attempted,
            "pending": max(0, total_leads - attempted),
        }
        bucket = {
            "research": research,
            "email_finding": email_finding,
            "email_verification": verification_attempts,
        }.get(meta["category"], other_attempts)
        bucket.append(entry)

    return {
        "workspace": ws_row["slug"],
        "tag": tag,
        "total_leads": total_leads,
        "research": research,
        "email_finding": email_finding,
        "email_quality": {
            "verification_status": {row["status"]: row["n"] for row in quality_rows},
            "verification_attempts": verification_attempts,
        },
        "data_completeness": dict(completeness_row) if completeness_row else {},
        "segments": [{"tag": row["tag"], "lead_count": row["lead_count"]} for row in segment_rows],
        "workspace_status": {row["status"]: row["n"] for row in status_rows},
        "other_attempts": other_attempts,
    }


def format_tag_summary(data: dict[str, Any]) -> str:
    total = data.get("total_leads", 0)
    lines = [f"=== {data.get('tag')} Summary ({total} leads, workspace={data.get('workspace')}) ==="]

    def _section(title: str, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        lines.append("")
        lines.append(f"{title}:")
        for e in entries:
            lines.append(f"  {e['label']} attempted: {e['attempted']} / {total}  (pending {e['pending']})")

    _section("Research", data.get("research") or [])
    _section("Email Finding", data.get("email_finding") or [])
    _section("Email Verification", (data.get("email_quality") or {}).get("verification_attempts") or [])
    if data.get("other_attempts"):
        _section("Other", data["other_attempts"])

    vs = (data.get("email_quality") or {}).get("verification_status") or {}
    if vs:
        lines.append("")
        lines.append("Email Verification Status:")
        for status, n in sorted(vs.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {status}: {n}")

    comp = data.get("data_completeness") or {}
    if comp:
        lines.append("")
        lines.append("Data Completeness:")
        lines.append(f"  Has email: {comp.get('has_email', 0)} / {comp.get('total', total)}")
        lines.append(f"  Has LinkedIn: {comp.get('has_linkedin', 0)} / {comp.get('total', total)}")
        lines.append(f"  Has company: {comp.get('has_company', 0)} / {comp.get('total', total)}")

    segments = data.get("segments") or []
    if segments:
        lines.append("")
        lines.append("Segments:")
        for s in segments:
            lines.append(f"  {s['tag']}: {s['lead_count']}")

    ws_status = data.get("workspace_status") or {}
    if ws_status:
        lines.append("")
        lines.append("Workspace Status:")
        for status, n in sorted(ws_status.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {status}: {n}")

    return "\n".join(lines)


def format_query_result_text(result: dict[str, Any]) -> str:
    preset = result.get("preset")
    header = f"Preset: {preset}" if preset else "Query"
    lines = [
        header,
        f"Rows: {result.get('row_count', 0)} ({result.get('elapsed_ms', 0)} ms)",
    ]
    if result.get("truncated"):
        lines.append(f"(truncated at limit {result.get('limit')})")
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if not rows:
        lines.append("No rows.")
        return "\n".join(lines)
    for row in rows[:50]:
        parts = [f"{c}={row.get(c)}" for c in cols]
        lines.append(" | ".join(parts))
    if len(rows) > 50:
        lines.append(f"... and {len(rows) - 50} more rows")
    return "\n".join(lines)
