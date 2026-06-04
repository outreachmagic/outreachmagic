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
from platform_registry import reply_event_sql_condition

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
    expr = normalize_since(since)
    if not expr:
        return "", []
    if expr.startswith("datetime("):
        return f" AND {column} >= {expr}", []
    return f" AND {column} >= ?", [expr]


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
