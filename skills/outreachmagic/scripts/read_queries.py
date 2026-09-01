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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

#: Wall-clock ceiling on one ad-hoc query. SQLite has no native statement
#: timeout, so a join with no row-reducing predicate on `events` runs until the
#: user kills the process -- which is exactly what happened on a 4-way join over
#: a 5 GB database. Ten seconds is far past any query worth waiting for
#: interactively; `--timeout 0` opts out for a deliberate long scan.
DEFAULT_TIMEOUT_SEC = 10

#: Lock-wait budget. Unrelated to the statement timeout above: this is how long
#: we wait to START, that is how long we allow to FINISH.
BUSY_TIMEOUT_SEC = 30


class QueryTimeout(RuntimeError):
    """An ad-hoc query ran past its wall-clock budget and was cancelled."""


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


def resolve_window(since: Optional[str], until: Optional[str] = None) -> dict:
    """The UTC bounds a since/until pair actually resolves to, right now.

    Echoed in reporting payloads so a consumer never has to re-derive "what does
    14d mean" -- and so two answers taken minutes apart can be told apart. UTC
    because that is what SQLite's datetime('now') compares against; `until` is
    inclusive of its whole day, matching _range_clause.
    """
    now = datetime.now(timezone.utc)
    start: Optional[datetime] = None
    raw = (since or "").strip().lower()
    if raw and raw != "all":
        if raw == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif m := re.match(r"^(\d+)\s*h(?:ours?)?$", raw):
            start = now - timedelta(hours=int(m.group(1)))
        elif m := re.match(r"^(\d+)\s*d(?:ays?)?$", raw):
            start = now - timedelta(days=int(m.group(1)))
        elif m := re.match(r"^(\d+)\s*w(?:eeks?)?$", raw):
            start = now - timedelta(weeks=int(m.group(1)))
        else:
            try:
                start = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            except ValueError:
                start = None
    end = now
    if until and until.strip():
        try:
            end = (datetime.fromisoformat(until.strip()).replace(tzinfo=timezone.utc)
                   + timedelta(days=1))
        except ValueError:
            pass

    def _iso(dt: Optional[datetime]) -> Optional[str]:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None

    return {
        "since": since,
        "until": until,
        "since_resolved_utc": _iso(start),
        "until_resolved_utc": _iso(end),
        "window_days": round((end - start).total_seconds() / 86400, 4) if start else None,
    }


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
        raise ValueError(
            "Query must start with SELECT or WITH. "
            "For table/column discovery use: pipeline.py schema [table] [--relations]"
        )
    return text


#: CTEs a --workspace query gets for free. `leads` has no workspace_id (the
#: relationship lives in workspace_leads) and `events` reaches its workspace
#: through campaigns, so those two joins are what every agent gets wrong first.
#: They are handed over as pre-bound CTEs rather than by rewriting the caller's
#: SQL: silently editing someone's query is a footgun, giving them a scoped
#: table to join against is not.
WORKSPACE_CTES = (
    ("ws_workspace", "SELECT * FROM workspaces WHERE id = '{wid}'"),
    ("ws_leads",
     "SELECT l.* FROM leads l JOIN workspace_leads wl ON wl.lead_id = l.id"
     " WHERE wl.workspace_id = '{wid}'"),
    ("ws_campaigns", "SELECT * FROM campaigns WHERE workspace_id = '{wid}'"),
    ("ws_events",
     "SELECT e.* FROM events e JOIN campaigns c ON c.id = e.campaign_id"
     " WHERE c.workspace_id = '{wid}'"),
    ("ws_sender_accounts",
     "SELECT sa.* FROM sender_accounts sa"
     " JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id"
     " WHERE wsa.workspace_id = '{wid}'"),
)


def resolve_workspace_id(workspace: str) -> str:
    """Slug or name or id -> workspace id, with a listing when it doesn't match."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM workspaces WHERE id = ? OR slug = ? OR name = ?"
            " ORDER BY (id = ?) DESC LIMIT 1",
            (workspace, workspace, workspace, workspace),
        ).fetchone()
        if row:
            return row[0]
        slugs = [r[0] for r in conn.execute(
            "SELECT slug FROM workspaces ORDER BY slug").fetchall()]
    finally:
        conn.close()
    raise ValueError(f"Unknown workspace: {workspace}. Available: {', '.join(slugs) or 'none'}")


def with_workspace_ctes(sql: str, workspace_id: str) -> str:
    """Prefix the query with the workspace-scoped CTEs.

    The id is interpolated rather than bound because prepending bound
    parameters would renumber the caller's own placeholders. It is validated
    against the workspaces table first and matched to an id shape here, so it
    is never free text from the outside.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", workspace_id or ""):
        raise ValueError("workspace id has an unexpected shape")
    ctes = ",\n".join(
        f"{name} AS ({body.format(wid=workspace_id)})" for name, body in WORKSPACE_CTES)
    text = sql.strip()
    if text[:4].upper() == "WITH":
        return f"WITH {ctes},\n{text[4:].lstrip()}"
    return f"WITH {ctes}\n{text}"


#: sqlite3 calls the progress handler every N virtual-machine instructions.
#: 5,000 is frequent enough that a runaway scan is cut within milliseconds of
#: its deadline and rare enough that the callback costs nothing measurable.
_PROGRESS_INSTRUCTIONS = 5_000


@contextmanager
def _statement_timeout(conn: sqlite3.Connection, timeout_sec: float, start: float):
    """Abort the statement running on `conn` once `timeout_sec` has elapsed.

    SQLite has no statement timeout, but it does call a progress handler mid-
    execution, and a non-zero return from that handler cancels the statement --
    the standard-library way to bound a query. It needs no signals, so it works
    off the main thread and inside the dashboard server, and it leaves the
    connection usable afterwards. `timeout_sec = 0` disables the ceiling for a
    deliberate long scan.
    """
    if not timeout_sec or timeout_sec <= 0:
        yield
        return
    deadline = start + float(timeout_sec)
    conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0,
                              _PROGRESS_INSTRUCTIONS)
    try:
        yield
    except sqlite3.OperationalError as exc:
        # "interrupted" is what the cancelled statement surfaces as; anything
        # else is a real SQL error and must not be relabelled as a timeout.
        if "interrupt" not in str(exc).lower():
            raise
        raise QueryTimeout(
            f"Query exceeded the {timeout_sec:g}s timeout and was cancelled. "
            "Add a WHERE filter on the largest table, pre-filter `events` in a "
            "CTE, or use a canonical command (campaign-audit / attribute-audit / "
            "deliverability). Raise the ceiling with --timeout N, or --timeout 0 "
            "for no ceiling. See references/sql-pitfalls.md."
        ) from exc
    finally:
        conn.set_progress_handler(None, 0)


#: Tables big enough that scanning one unfiltered is the difference between a
#: query that answers and a query that hangs.
_LARGE_TABLES = ("events", "relay_ingested", "workspace_lead_events")

_TIME_PREDICATE = re.compile(
    r"\b(created_at|event_at|sent_at|occurred_at|campaign_id|lead_id)\b", re.IGNORECASE)


def lint_sql(sql: str) -> list[str]:
    """Advisory warnings about a query's shape. Never blocks.

    Every rule here corresponds to a way this schema has actually produced a
    wrong or non-terminating answer. They are warnings and not errors on
    purpose: a deliberate full scan is legitimate, and a tool that refuses
    legitimate queries gets routed around rather than fixed.
    """
    text = " ".join((sql or "").split())
    low = text.lower()
    out: list[str] = []

    if re.search(r"\bcross\s+join\b", low):
        out.append(
            "CROSS JOIN: every row of each side is paired with every row of the "
            "other. If you meant a relationship, name it in an ON clause.")

    joins = len(re.findall(r"\bjoin\b", low))
    if joins >= 3 and not re.search(r"\bwhere\b", low):
        out.append(
            f"{joins} joins and no WHERE: nothing reduces the row count before "
            "the join. Pre-filter the largest table in a CTE first.")

    for table in _LARGE_TABLES:
        if not re.search(rf"\b(from|join)\s+{table}\b", low):
            continue
        # The workspace CTEs (ws_events) are pre-scoped, so a query using those
        # is not the pattern being warned about.
        if not _TIME_PREDICATE.search(text):
            out.append(
                f"`{table}` is joined with no date, campaign or lead predicate. "
                "On a large workspace that is a full scan; filter it in a CTE "
                "before joining.")

    if re.search(r"\b(campaigns|workspace_leads)\b", low) and "workspace_id" not in low:
        out.append(
            "`campaigns` / `workspace_leads` without a workspace_id filter counts "
            "every workspace at once. Pass --workspace to get the ws_* CTEs.")

    # The trap that turned 106 sends into 15,565: a lifetime, workspace-wide
    # counter read as if it were per-campaign.
    if "email_sent_count" in low and "campaign" in low:
        out.append(
            "workspace_leads.email_sent_count is a LIFETIME, WORKSPACE-WIDE "
            "counter -- it is not sends in this campaign. For per-campaign "
            "sends use `campaign-audit` or the v_campaign_lead_events view.")

    return out


def explain_plan(sql: str, params: Optional[list[Any]] = None,
                 workspace: Optional[str] = None) -> dict[str, Any]:
    """`EXPLAIN QUERY PLAN` for a query, plus lint warnings. Does not run it."""
    query = validate_readonly_sql(sql)
    workspace_id = None
    if workspace:
        workspace_id = resolve_workspace_id(workspace)
        query = with_workspace_ctes(query, workspace_id)
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(f"EXPLAIN QUERY PLAN {query}", params or []).fetchall()
    finally:
        conn.close()
    return {
        "plan": [r["detail"] for r in rows],
        "warnings": lint_sql(query),
        "sql": query.strip(),
        "workspace_id": workspace_id,
    }


def run_readonly_sql(
    sql: str,
    *,
    params: Optional[list[Any]] = None,
    limit: int = DEFAULT_ROW_LIMIT,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    workspace: Optional[str] = None,
) -> dict[str, Any]:
    """Execute one read-only query; returns columns, rows, timing.

    `limit=0` means no cap. Otherwise the cap is appended when the query has no
    LIMIT of its own, and `truncated` says whether it bit -- silent truncation
    at 500 rows is a wrong-answer generator.
    """
    query = validate_readonly_sql(sql)
    workspace_id = None
    if workspace:
        workspace_id = resolve_workspace_id(workspace)
        query = with_workspace_ctes(query, workspace_id)
    bounded = query.rstrip()
    if limit and not re.search(r"\blimit\b", bounded, re.IGNORECASE):
        bounded = f"{bounded}\nLIMIT {int(limit)}"
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SEC * 1000)}")
        start = time.perf_counter()
        with _statement_timeout(conn, timeout_sec, start):
            cur = conn.execute(bounded, params or [])
            rows = [dict(r) for r in cur.fetchall()]
            columns = [d[0] for d in (cur.description or [])]
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "elapsed_ms": elapsed_ms,
            "sql": query.strip(),
            "truncated": bool(limit) and len(rows) >= limit,
            "limit": limit,
            "timeout_sec": timeout_sec,
            "warnings": lint_sql(query),
            "workspace_id": workspace_id,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------

#: One line per table, so `schema` answers "what is this for" and not just
#: "what columns does it have". A table missing from here shows as TODO and
#: fails release-check (tests/test_schema_reference.py) -- the point is that a
#: new table cannot quietly arrive undocumented.
TABLE_PURPOSE: dict[str, str] = {
    "leads": "One row per lead (email and/or LinkedIn identity). Global — workspace scoping lives in workspace_leads.",
    "companies": "Canonical company records. Linked from leads.company_id.",
    "lead_links": (
        "Two people in one conversation (someone replied and forwarded us to a "
        "colleague). Directional: from_lead_id introduced us to to_lead_id. "
        "Non-destructive — both leads stay whole; reporting counts distinct "
        "conversation roots so the thread counts once."),
    "company_identities": "A company's domains and other identifiers, with provenance. Where a company's extra domains live.",
    "events": "Primary timeline: sends, replies, bounces, status labels. Use this for analytics.",
    "campaigns": "Campaign names and their workspace. events.campaign_id joins here.",
    "workspaces": "Multi-workspace routing. Slug column is `slug`, not `workspace_slug`.",
    "workspace_leads": "Per-workspace lead status, sentiment anchor and activity. The leads-to-workspace join.",
    "workspace_lead_events": "Workspace-scoped ingest audit — a subset, not full relay volume.",
    "workspace_lead_tags": "Workspace-scoped tags on a lead.",
    "campaign_leads": "Which leads belong to which campaign.",
    "campaign_workspace_map": "Relay-platform campaign routing (campaign_platform_id = the sequencer's own id). NOT a join path for events.",
    "unmapped_campaign_queue": "Quarantined webhook events awaiting routing.",
    "relay_ingested": "Inbound dedupe ledger — a hash per ingested relay event.",
    "event_push_log": "Outbound push state: has this event been sent to the relay.",
    "bounce_events": "Deduplicated bounces with message, SMTP code and recipient MX. Never pruned — full history.",
    "sender_accounts": "Your sending mailboxes. provider is sequencer-owned.",
    "sender_domains": "Your sending domains: reseller, provider, cost, DNSBL status, registration date.",
    "workspace_sender_accounts": "Which workspaces a mailbox serves.",
    "lead_emails": "Additional email addresses for a lead.",
    "lead_personalization": "Per-lead personalization values.",
    "company_personalization": "Per-company personalization values.",
    "lead_merges": "Audit trail of merged lead records.",
    "outbox": "Pending entity changes queued for the relay.",
    "phone_numbers": "Phone numbers owned by a lead or company.",
    "suppression_rules": "Contacts excluded from lists, counts and exports.",
    "suppression_entries": "Materialized matches of a suppression rule to specific contacts.",
    "workspace_lead_suppressions": "Per-workspace suppression of one lead.",
    "company_contact_observations": "Contacts seen on a company's own pages during sourcing.",
    "company_merge_candidates": "Proposed company merges awaiting a decision.",
    "company_merges": "Audit trail of executed company merges.",
    "company_page_cache": "Fetched company pages, cached so sourcing does not pay twice.",
    "crm_entity_map": "Local entity to CRM record id mapping.",
    "crm_sync_log": "What was pushed to the CRM, and when.",
    "crm_workspace_config": "Per-workspace CRM connection settings.",
    "lead_identities": "A lead's identifying values (email, LinkedIn) with provenance.",
    "lead_merge_jobs": "Proposed lead merges awaiting a decision.",
    "lead_provider_observations": "Per-provider enrichment attempts and their outcomes.",
    "leads_junk_quarantine": "Rows held back as junk rather than imported.",
    "migration_flags": "One-shot migration markers, so a backfill runs once.",
    "organizations": "The org a database belongs to (single row in normal use).",
    "personalization_fields": "Registry of personalization field names and their scope.",
    "provider_batch_jobs": "Submitted provider batch jobs awaiting download.",
    "sync_audit": "Record of sync pushes and pulls for debugging.",
    "sync_shadow": "Last-synced copy of an entity, for change detection.",
    "workspace_icp_profiles": "Saved ICP definitions per workspace.",
    "workspace_lead_linkedin_status": "Per-workspace LinkedIn connection state for a lead.",
    "lead_email_verification": "Email verification results per address.",
    "email_verification_status": "Consolidated verification verdict per lead.",
    "unmapped_campaign_resolutions": "Decisions made on quarantined campaign events.",
    "workspace_sender_domains": "Which workspaces a sending domain serves.",
    "public_emails": "Published email addresses found on a company's site.",
    "campaign_workspace_rules": "Name/pattern rules routing campaigns to workspaces.",
}


#: Columns whose NAME does not tell you what they mean, keyed (table, column).
#: Rendered into references/schema.md and printed by `pipeline.py schema
#: <table>`, so the warning reaches whoever is about to use the column rather
#: than living in a docstring nobody opens.
#:
#: Every entry here is a wrong answer that has already been shipped. Reading
#: email_sent_count as "sends in this campaign" once reported 15,565 sends for
#: a campaign that had sent 106.
COLUMN_NOTE: dict[tuple[str, str], str] = {
    ("workspace_leads", "email_sent_count"):
        "LIFETIME and WORKSPACE-WIDE — not sends in one campaign. "
        "For per-campaign sends use `campaign-audit` or the v_campaign_lead_events view.",
    ("workspace_leads", "linkedin_sent_count"):
        "LIFETIME and WORKSPACE-WIDE, like email_sent_count.",
    ("workspace_leads", "total_replies_count"):
        "LIFETIME and WORKSPACE-WIDE — not replies in one campaign or window.",
    ("workspace_leads", "current_sentiment_since"):
        "When the status LAST CHANGED. Not first contact, not first positive. "
        "Windowing on this to find recent positives silently drops every lead "
        "whose status has not moved since; use first_positive_at.",
    ("workspace_leads", "current_status_sentiment"):
        "The lead's sentiment RIGHT NOW. A lead that went positive and later "
        "cold is not counted here; `campaign-audit --positive-anchor first` "
        "answers the historical question.",
    ("workspace_leads", "first_positive_at"):
        "First time this lead ever turned positive — the anchor behind "
        "`--positive-anchor first`, and the column to window on.",
    ("workspace_leads", "last_activity_at"):
        "Any event in any campaign, inbound or outbound.",
    ("events", "campaign_id"):
        "The campaign the PLATFORM stamped on the event. A booking webhook "
        "stamps the calendar slot, so meetings counted off this column credit "
        "the calendar rather than the sequence that earned them.",
}


def column_note(table: str, column: str) -> Optional[str]:
    return COLUMN_NOTE.get((table, column))


#: How each entity is scoped to a workspace. The single most common wrong first
#: guess is `leads.workspace_id`, which does not exist.
WORKSPACE_SCOPING: dict[str, str] = {
    "leads": "via workspace_leads(workspace_id, lead_id) — no column on leads",
    "campaigns": "direct campaigns.workspace_id",
    "events": "via events.campaign_id -> campaigns.workspace_id",
    "sender_accounts": "via workspace_sender_accounts(workspace_id, sender_account_id)",
    "bounce_events": "bounce_events.workspace_id (older rows may be null)",
    "campaign_workspace_map": "relay-platform routing only — not a join path for events",
}


def table_relations(tables: Optional[list[str]] = None) -> dict[str, Any]:
    """Foreign-key graph, so an agent can find the join instead of guessing it."""
    conn = get_conn()
    try:
        names = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        wanted = [t for t in names if not tables or t in set(tables)]
        edges = []
        for table in wanted:
            for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                edges.append({
                    "from_table": table,
                    "from_column": fk[3],
                    "to_table": fk[2],
                    "to_column": fk[4] or "id",
                })
    finally:
        conn.close()
    return {
        "relations": edges,
        "workspace_scoping": WORKSPACE_SCOPING,
        "db_path": str(get_db_path()),
    }


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
    tables = [r[0] for r in rows]
    return {
        "tables": tables,
        "purposes": {t: TABLE_PURPOSE.get(t, "TODO — undocumented table") for t in tables},
        "db_path": str(get_db_path()),
    }


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
        fks = [
            {"column": fk[3], "references": f"{fk[2]}.{fk[4] or 'id'}"}
            for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        ]
    finally:
        conn.close()
    return {
        "table": table,
        "purpose": TABLE_PURPOSE.get(table, "TODO — undocumented table"),
        "workspace_scoping": WORKSPACE_SCOPING.get(table),
        "columns": [
            {"name": c[1], "type": c[2], "notnull": bool(c[3]), "default": c[4],
             "pk": bool(c[5]), "note": COLUMN_NOTE.get((table, c[1]))}
            for c in cols
        ],
        "foreign_keys": fks,
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
