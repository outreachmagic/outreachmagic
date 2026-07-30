"""Contacts export: presets, a field picker, and one filter set shared with the
contacts list.

The requirement is "whatever is on screen right now, all N of it" — which is
only true if the export and the list build the same WHERE. So this module takes
`dashboard_queries.LEAD_FILTER_KEYS` verbatim and calls
`dashboard_queries.lead_filter_clause()`; there is no second filter set here to
drift out of step. (`pipeline_tags.export_leads` was exactly that second set,
and is now a shim over this one.)

Server-side by design: writes a CSV under `om_paths.get_export_dir()` and hands
back a path. Streaming 100k rows into the browser to assemble a Blob is how an
export of the size these actually are takes the tab down with it.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Optional

import dashboard_queries as dq
from constants import COMPANY_DOMAIN_SQL
from om_paths import get_export_dir

# COMPANY_DOMAIN_SQL ships with its own "AS company_domain"; strip it so it
# composes like every other column expression here.
_COMPANY_DOMAIN_EXPR = COMPANY_DOMAIN_SQL.rsplit(" AS company_domain", 1)[0]

# Base columns available to any export: key -> (SQL expression, header).
# The aliases `l` (leads), `wl` (workspace_leads) and `co` (companies) are in
# scope for all of them.
BASE_COLUMNS: dict[str, str] = {
    "lead_id": "l.id",
    "name": "l.name",
    "first_name": "TRIM(SUBSTR(l.name, 1, INSTR(l.name || ' ', ' ') - 1))",
    "last_name": "TRIM(SUBSTR(l.name, INSTR(l.name || ' ', ' ')))",
    "email": "l.email",
    "email_verification_status": "l.email_verification_status",
    "email_domain": "l.email_domain",
    "title": "l.title",
    "company": "COALESCE(co.name, l.company)",
    "company_domain": _COMPANY_DOMAIN_EXPR,
    "industry": "l.industry",
    "headcount": "l.headcount",
    # A complete, openable URL — same value the dashboard's LinkedIn column
    # shows, so an export matches what was on screen. Mirrors
    # workspace_routing.linkedin_display_url(): prefer the public profile,
    # scheme-prefix it when the stored value is bare (Sales Navigator exports
    # omit it), else synthesize the Sales Navigator URL from the member token.
    # The raw token is still available on its own as linkedin_sales_nav_id.
    "linkedin": ("CASE"
                 " WHEN TRIM(COALESCE(l.linkedin_url, '')) != ''"
                 "  THEN CASE WHEN l.linkedin_url LIKE 'http%' THEN TRIM(l.linkedin_url)"
                 "            ELSE 'https://' || LTRIM(TRIM(l.linkedin_url), '/') END"
                 " WHEN TRIM(COALESCE(l.linkedin_sales_nav_id, '')) != ''"
                 "  THEN 'https://www.linkedin.com/sales/lead/' || TRIM(l.linkedin_sales_nav_id)"
                 " ELSE NULL END"),
    "linkedin_sales_nav_id": "l.linkedin_sales_nav_id",
    "location_city": "l.location_city",
    "location_state": "l.location_state",
    "location_country": "l.location_country",
    "hq_city": "co.hq_city",
    "hq_state": "co.hq_state",
    "hq_country": "co.hq_country",
    "record_type": "l.record_type",
    "stage": "wl.status",
    "status_label": "wl.current_status_label",
    "sentiment": "wl.current_status_sentiment",
    "sentiment_since": "wl.current_sentiment_since",
    "last_activity_at": "wl.last_activity_at",
    "email_sent_count": "wl.email_sent_count",
    "total_replies_count": "wl.total_replies_count",
    "latest_sender": "wl.latest_sender",
    "original_source": "l.original_source",
    "original_source_detail": "l.original_source_detail",
    "latest_source": "l.latest_source",
    "notes": "l.notes",
    "tags": ("(SELECT GROUP_CONCAT(t.tag, ';') FROM workspace_lead_tags t"
             " WHERE t.workspace_id = wl.workspace_id AND t.lead_id = wl.lead_id)"),
    "campaign": ("(SELECT c.name FROM campaign_leads cl JOIN campaigns c ON c.id = cl.campaign_id"
                 " WHERE cl.lead_id = l.id ORDER BY cl.added_at DESC LIMIT 1)"),
    "phone": ("(SELECT p.phone_e164 FROM phone_numbers p"
              " WHERE p.owner_type = 'lead' AND p.owner_id = l.id"
              " ORDER BY p.is_primary DESC, p.id LIMIT 1)"),
}

# Computed message blocks — expensive correlated subqueries, so they are only
# added to the query when actually selected.
MESSAGE_COLUMNS = (
    "last_message_sent_at", "last_message_sent_subject", "last_message_sent_body",
    "last_message_received_at", "last_message_received_subject", "last_message_received_body",
)

# The five a lead-gen agency actually needs. Each is a starting point for the
# field picker, not a fixed shape.
PRESETS: dict[str, list[str]] = {
    "sequencer-upload": [
        "email", "first_name", "last_name", "name", "company", "title",
        "linkedin", "company_domain", "phone",
    ],
    "enrichment-input": [
        "lead_id", "name", "company", "company_domain", "linkedin", "title",
        "location_city", "location_state", "location_country",
    ],
    "client-report": [
        "name", "title", "company", "stage", "sentiment", "status_label",
        "last_activity_at", "campaign", "latest_sender",
    ],
    "replies-review": [
        "name", "email", "company", "title", "stage", "sentiment", "status_label",
        "last_message_sent_at", "last_message_sent_subject", "last_message_sent_body",
        "last_message_received_at", "last_message_received_subject",
        "last_message_received_body",
    ],
    "full": [
        *BASE_COLUMNS.keys(),
        *MESSAGE_COLUMNS,
    ],
}

# Presets that mean "and every personalization field too". sequencer-upload
# needs them because that is the merge data the sequencer sends with.
PRESETS_WITH_ALL_PERSONALIZATION = frozenset({"sequencer-upload", "full"})

_SAFE_FIELD_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class LeadExportError(ValueError):
    """User-facing failure (unknown preset, unknown field, nothing selected)."""


def personalization_fields(conn: sqlite3.Connection, workspace_id: str) -> list[str]:
    """Every personalization field present on this workspace's leads.

    Read from the data rather than a fixed list, so the picker is always
    accurate for the workspace you are actually looking at.
    """
    rows = conn.execute(
        """SELECT DISTINCT p.field_name FROM lead_personalization p
             JOIN workspace_leads wl ON wl.lead_id = p.lead_id
            WHERE wl.workspace_id = ? AND p.field_name IS NOT NULL
            ORDER BY p.field_name""",
        (workspace_id,),
    ).fetchall()
    return [r["field_name"] for r in rows if _SAFE_FIELD_RE.match(r["field_name"] or "")]


def export_field_options(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """What the field dropdown offers, and what each preset starts from."""
    pers = personalization_fields(conn, workspace_id)
    return {
        "base": list(BASE_COLUMNS.keys()),
        "messages": list(MESSAGE_COLUMNS),
        "personalization": [f"personalized_{f}" for f in pers],
        "presets": {
            name: resolve_fields(name, None, pers)
            for name in PRESETS
        },
    }


def resolve_fields(
    preset: Optional[str],
    fields: Optional[list[str]],
    available_personalization: list[str],
) -> list[str]:
    """Explicit `fields` win outright; otherwise expand the preset.

    An explicit list is the field picker's output — the user started from a
    preset and then added or removed columns, so the list they ended up with is
    the answer, not the preset they started from.
    """
    if fields:
        out, seen = [], set()
        for f in fields:
            f = str(f or "").strip()
            if not f or f in seen:
                continue
            _validate_field(f, available_personalization)
            seen.add(f)
            out.append(f)
        if not out:
            raise LeadExportError("no columns selected")
        return out

    name = (preset or "sequencer-upload").strip()
    if name not in PRESETS:
        raise LeadExportError(
            f"unknown preset {name!r}. Valid: {', '.join(sorted(PRESETS))}")
    out = list(PRESETS[name])
    if name in PRESETS_WITH_ALL_PERSONALIZATION:
        out += [f"personalized_{f}" for f in available_personalization]
    return out


def _validate_field(field: str, available_personalization: list[str]) -> None:
    if field in BASE_COLUMNS or field in MESSAGE_COLUMNS:
        return
    if field.startswith("personalized_"):
        # Not restricted to `available_personalization`: a field that is empty
        # for every lead in the current filter is a legitimate column to ask
        # for (it keeps the CSV shape stable across exports). Only the NAME has
        # to be safe, since it reaches SQL as a bind value and a header.
        if _SAFE_FIELD_RE.match(field[len("personalized_"):]):
            return
    raise LeadExportError(f"unknown export column: {field}")


def _build_query(
    workspace_id: str, fields: list[str], filters: dict, limit: int,
) -> tuple[str, list]:
    where_sql, params = dq.lead_filter_clause(workspace_id, **filters)

    selects, pers_fields = [], []
    for f in fields:
        if f in BASE_COLUMNS:
            selects.append(f'{BASE_COLUMNS[f]} AS "{f}"')
        elif f in MESSAGE_COLUMNS:
            continue  # emitted once per direction, below
        else:
            pers_fields.append(f[len("personalized_"):])

    for direction, prefix in (("outbound", "last_message_sent"),
                              ("inbound", "last_message_received")):
        if any(f.startswith(prefix) for f in fields):
            selects.append(dq.lead_message_block_sql(direction))

    # Personalization is one row per (lead, field), so it is pulled per column
    # rather than joined -- a join per field would multiply the result set.
    for name in pers_fields:
        selects.append(
            "(SELECT p.field_value FROM lead_personalization p"
            " WHERE p.lead_id = l.id AND p.field_name = ?)"
            f' AS "personalized_{name}"')

    # SQLite numbers `?` by position in the statement text: the personalization
    # binds sit in the SELECT list, so they precede the WHERE clause's params.
    ordered_params = [*pers_fields, *params, limit]
    sql = f"""SELECT {', '.join(selects)}
                FROM workspace_leads wl
                JOIN leads l ON l.id = wl.lead_id
                LEFT JOIN companies co ON co.id = l.company_id
               WHERE {where_sql}
               ORDER BY l.id
               LIMIT ?"""
    return sql, ordered_params


def export_rows(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    preset: Optional[str] = None,
    fields: Optional[list[str]] = None,
    limit: int = 50000,
    **filters: Any,
) -> tuple[list[str], list[dict]]:
    """(columns, rows) for the current filter set. The query path the CSV and
    any future format both go through."""
    unknown = sorted(set(filters) - set(dq.LEAD_FILTER_KEYS))
    if unknown:
        raise LeadExportError(f"unknown filter(s): {', '.join(unknown)}")
    cols = resolve_fields(preset, fields, personalization_fields(conn, workspace_id))
    sql, params = _build_query(workspace_id, cols, filters, limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return cols, rows


def export_to_csv(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    workspace_slug: Optional[str] = None,
    preset: Optional[str] = None,
    fields: Optional[list[str]] = None,
    file_path: Optional[str] = None,
    limit: int = 50000,
    **filters: Any,
) -> dict:
    """Write the export and return {file, count, columns, truncated}."""
    cols, rows = export_rows(
        conn, workspace_id, preset=preset, fields=fields, limit=limit, **filters)
    out_dir = get_export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    if file_path:
        name = re.sub(r"[^A-Za-z0-9._-]", "-", str(file_path).strip()) or "contacts.csv"
    else:
        slug = re.sub(r"[^a-z0-9-]", "-", (workspace_slug or workspace_id).lower())
        name = f"{slug}-{preset or 'contacts'}-{datetime.now():%Y-%m-%d-%H%M%S}.csv"
    if not name.endswith(".csv"):
        name += ".csv"
    out = out_dir / name
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                k: "" if row.get(k) is None else _csv_value(row.get(k)) for k in cols
            })
    return {
        "status": "exported",
        "file": str(out),
        "filename": out.name,
        "count": len(rows),
        "columns": cols,
        "truncated": len(rows) >= limit,
        "limit": limit,
    }


def _csv_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
