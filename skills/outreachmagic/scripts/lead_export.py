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
    # No first_name / last_name. There is no such column on `leads`; both used
    # to be a SQL split of l.name on the first space, which silently mangled
    # every name that isn't exactly two words -- "Mary Anne Okonkwo" exported a
    # last name of "Anne Okonkwo", a mononym exported an empty one, and a
    # company placeholder was shredded into a fake person. A wrong first name
    # in a mail merge is worse than an absent one, so the split is gone. Use
    # `name`, or `personalized_first_name` for a curated merge value.
    "email": "l.email",
    "email_verification_status": "l.email_verification_status",
    "email_domain": "l.email_domain",
    "title": "l.title",
    "company": "COALESCE(co.name, l.company)",
    "company_domain": _COMPANY_DOMAIN_EXPR,
    "industry": "l.industry",
    "headcount": "l.headcount",
    # The account's classification, denormalized onto companies so segmenting
    # by it is a column and not a three-hop join through the placeholder lead.
    "company_category": "co.category",
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
        "email", "name", "company", "title",
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

# Personalization column namespaces. A column says which scope it means, so the
# picker can group them and an agent can name one without guessing.
LEAD_PREFIX = "personalized_"
COMPANY_PREFIX = "company_personalized_"


class LeadExportError(ValueError):
    """User-facing failure (unknown preset, unknown field, nothing selected)."""


def personalization_fields(conn: sqlite3.Connection, workspace_id: str) -> list[str]:
    """Every LEAD-scoped personalization field present on this workspace.

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


def company_personalization_fields(conn: sqlite3.Connection, workspace_id: str) -> list[str]:
    """Every COMPANY-scoped personalization field reachable from this workspace.

    company_personalization is the most-populated personalization table in the
    database and none of it was exportable -- `personalized_company_name` was
    not even offered by the picker, so "full export" quietly meant "full export
    of half the personalization" and the join had to be done by hand afterwards.
    """
    rows = conn.execute(
        """SELECT DISTINCT p.field_name FROM company_personalization p
             JOIN leads l ON l.company_id = p.company_id
             JOIN workspace_leads wl ON wl.lead_id = l.id
            WHERE wl.workspace_id = ? AND p.field_name IS NOT NULL
            ORDER BY p.field_name""",
        (workspace_id,),
    ).fetchall()
    return [r["field_name"] for r in rows if _SAFE_FIELD_RE.match(r["field_name"] or "")]


def export_field_options(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """What the field dropdown offers, and what each preset starts from.

    Grouped, because "is this column the contact's or the company's?" is the
    question the picker actually has to answer -- a company value is shared by
    every contact at that company, and picking the wrong one is silent.
    """
    pers = personalization_fields(conn, workspace_id)
    co_pers = company_personalization_fields(conn, workspace_id)
    lead_cols = [f"{LEAD_PREFIX}{f}" for f in pers]
    co_cols = [f"{COMPANY_PREFIX}{f}" for f in co_pers]
    return {
        "groups": [
            {"key": "base", "label": "Contact", "fields": list(BASE_COLUMNS.keys())},
            {"key": "messages", "label": "Messages", "fields": list(MESSAGE_COLUMNS)},
            {"key": "personalization_lead", "label": "Contact personalization",
             "scope": "lead", "fields": lead_cols},
            {"key": "personalization_company", "label": "Company personalization",
             "scope": "company", "fields": co_cols},
        ],
        # Legacy flat keys, kept for one release so an older dashboard or a
        # saved script does not break on the shape change.
        "base": list(BASE_COLUMNS.keys()),
        "messages": list(MESSAGE_COLUMNS),
        "personalization": lead_cols,
        "personalization_company": co_cols,
        "presets": {
            name: resolve_fields(name, None, pers, co_pers)
            for name in PRESETS
        },
    }


def resolve_fields(
    preset: Optional[str],
    fields: Optional[list[str]],
    available_personalization: list[str],
    available_company_personalization: Optional[list[str]] = None,
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
        out += [f"{LEAD_PREFIX}{f}" for f in available_personalization]
        # Company personalization belongs in "everything" too. Leaving it out is
        # what made `full` a misnomer: the most-populated personalization table
        # in the database was unreachable from the preset whose whole promise is
        # completeness.
        out += [f"{COMPANY_PREFIX}{f}" for f in (available_company_personalization or [])]
    return out


# Columns that used to exist and now deliberately do not. Naming one is almost
# always a saved script or a remembered preset, so say what to use instead
# rather than the bare "unknown export column".
_RETIRED_COLUMNS = {
    "first_name": "no such column: `leads.name` was split on the first space, "
                  "which mangles anything that isn't exactly two words. "
                  "Use `name`, or `personalized_first_name` for a curated value.",
    "last_name": "no such column: `leads.name` was split on the first space, "
                 "which mangles anything that isn't exactly two words. Use `name`.",
}


def _validate_field(field: str, available_personalization: list[str]) -> None:
    if field in BASE_COLUMNS or field in MESSAGE_COLUMNS:
        return
    if field in _RETIRED_COLUMNS:
        raise LeadExportError(f"{field}: {_RETIRED_COLUMNS[field]}")
    for prefix in (COMPANY_PREFIX, LEAD_PREFIX):
        # COMPANY_PREFIX first: it ends in the lead prefix, so testing the
        # shorter one first would classify every company column as a lead one.
        if field.startswith(prefix):
            # Not restricted to what is currently populated: a field that is
            # empty for every lead in the current filter is a legitimate column
            # to ask for (it keeps the CSV shape stable across exports). Only
            # the NAME has to be safe, since it reaches SQL as a bind value and
            # a header.
            if _SAFE_FIELD_RE.match(field[len(prefix):]):
                return
    raise LeadExportError(f"unknown export column: {field}")


def _build_query(
    workspace_id: str, fields: list[str], filters: dict, limit: int,
) -> tuple[str, dict]:
    where_sql, params = dq.lead_filter_clause(workspace_id, **filters)

    selects = []
    # Named binds, not positional. Personalization binds live in the SELECT
    # list and the filter binds live in the WHERE, so with `?` the two groups
    # have to be concatenated in exactly the order SQLite scans the statement
    # text -- correct today only because the message blocks happen to carry no
    # binds. Adding a second bound subquery group (company personalization)
    # makes that a landmine, so the ordering dependency goes away instead.
    named: dict[str, Any] = {}

    def _bind(value: Any) -> str:
        key = f"p{len(named)}"
        named[key] = value
        return f":{key}"

    for f in fields:
        if f in BASE_COLUMNS:
            selects.append(f'{BASE_COLUMNS[f]} AS "{f}"')

    for direction, prefix in (("outbound", "last_message_sent"),
                              ("inbound", "last_message_received")):
        if any(f.startswith(prefix) for f in fields):
            selects.append(dq.lead_message_block_sql(direction))

    # Personalization is one row per (entity, field), so it is pulled per column
    # rather than joined -- a join per field would multiply the result set.
    for f in fields:
        if f in BASE_COLUMNS or f in MESSAGE_COLUMNS:
            continue
        if f.startswith(COMPANY_PREFIX):
            name = f[len(COMPANY_PREFIX):]
            selects.append(
                "(SELECT p.field_value FROM company_personalization p"
                f" WHERE p.company_id = l.company_id AND p.field_name = {_bind(name)})"
                f' AS "{f}"')
        else:
            name = f[len(LEAD_PREFIX):]
            # A bare `personalized_<f>` for a field that only exists in company
            # scope resolves there. `personalized_company_name` is the name
            # every existing script and preset already uses, and the value has
            # always lived on the company -- so it reads through rather than
            # coming back empty.
            selects.append(
                "COALESCE("
                "(SELECT p.field_value FROM lead_personalization p"
                f"  WHERE p.lead_id = l.id AND p.field_name = {_bind(name)}),"
                "(SELECT p.field_value FROM company_personalization p"
                f"  WHERE p.company_id = l.company_id AND p.field_name = {_bind(name)})"
                f') AS "{f}"')

    where_named = {}
    for i, value in enumerate(params):
        where_named[f"w{i}"] = value
    # lead_filter_clause emits `?`; renumber them into the named namespace in
    # the order they appear so both halves can share one parameter mapping.
    parts = where_sql.split("?")
    where_sql = "".join(
        part + (f":w{i}" if i < len(parts) - 1 else "")
        for i, part in enumerate(parts))
    named.update(where_named)
    named["lim"] = limit

    sql = f"""SELECT {', '.join(selects)}
                FROM workspace_leads wl
                JOIN leads l ON l.id = wl.lead_id
                LEFT JOIN companies co ON co.id = l.company_id
               WHERE {where_sql}
               ORDER BY l.id
               LIMIT :lim"""
    return sql, named


def export_rows(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    preset: Optional[str] = None,
    fields: Optional[list[str]] = None,
    limit: int = 50000,
    **filters: Any,
) -> tuple[list[str], list[dict]]:
    """(columns, rows) for the current filter set, fully materialized.

    Only for callers that genuinely need the whole list in memory. The CSV
    writer uses iter_export_rows() instead -- see why there.
    """
    cols, cursor = iter_export_rows(
        conn, workspace_id, preset=preset, fields=fields, limit=limit, **filters)
    return cols, [dict(r) for r in cursor]


def iter_export_rows(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    preset: Optional[str] = None,
    fields: Optional[list[str]] = None,
    limit: int = 50000,
    **filters: Any,
) -> tuple[list[str], Any]:
    """(columns, open cursor) for the current filter set.

    A cursor rather than a list because `full` is genuinely enormous: every one
    of tags / campaign / phone / the six message columns / each personalization
    field is an independent correlated subquery, so a wide export is ~30
    subquery evaluations per row -- and materializing 50k of those rows,
    message bodies included, into a list of dicts before writing a single byte
    is what took the dashboard down rather than the query itself.
    """
    unknown = sorted(set(filters) - set(dq.LEAD_FILTER_KEYS))
    if unknown:
        raise LeadExportError(f"unknown filter(s): {', '.join(unknown)}")
    cols = resolve_fields(
        preset, fields,
        personalization_fields(conn, workspace_id),
        company_personalization_fields(conn, workspace_id))
    sql, params = _build_query(workspace_id, cols, filters, limit)
    return cols, conn.execute(sql, params)


def count_suppressed_excluded(
    conn: sqlite3.Connection, workspace_id: str, filters: dict,
) -> Optional[dict]:
    """How many contacts this export's filters matched but suppression removed.

    "Give me everyone with this tag" returning 1,873 when the tag holds 1,875 is
    a silent surprise: the requester reads `count` as "all contacts" and only a
    DB cross-check reveals the difference. Returns None when the caller asked
    for suppressed rows anyway (nothing was excluded to report).
    """
    if filters.get("suppressed") in ("only", "all"):
        return None
    probe = {**filters, "suppressed": "only"}
    where_sql, params = dq.lead_filter_clause(workspace_id, **probe)
    rows = conn.execute(
        f"""SELECT l.id FROM workspace_leads wl
              JOIN leads l ON l.id = wl.lead_id
              LEFT JOIN companies co ON co.id = l.company_id
             WHERE {where_sql}
             ORDER BY l.id LIMIT 101""",
        params,
    ).fetchall()
    if not rows:
        return None
    return {
        "count": len(rows) if len(rows) <= 100 else None,
        "at_least": 100 if len(rows) > 100 else len(rows),
        "lead_ids": [r["id"] for r in rows[:100]],
    }


def export_to_csv(
    conn: sqlite3.Connection,
    workspace_id: str,
    *,
    workspace_slug: Optional[str] = None,
    preset: Optional[str] = None,
    fields: Optional[list[str]] = None,
    file_path: Optional[str] = None,
    limit: int = 50000,
    progress: Optional[Any] = None,
    **filters: Any,
) -> dict:
    """Write the export and return {file, count, columns, truncated, ...}.

    Rows stream from the cursor straight into the writer -- nothing holds the
    result set. `progress`, when given, is called with (rows_written) every
    2,000 rows so a caller running this as a background job can say how far
    along it is instead of going quiet for minutes.
    """
    cols, cursor = iter_export_rows(
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
    written = 0
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in cursor:
            writer.writerow({
                k: "" if row[k] is None else _csv_value(row[k]) for k in cols
            })
            written += 1
            if progress is not None and written % 2000 == 0:
                progress(written)
    result = {
        "status": "exported",
        "file": str(out),
        "filename": out.name,
        "count": written,
        "columns": cols,
        "truncated": written >= limit,
        "limit": limit,
    }
    suppressed = count_suppressed_excluded(conn, workspace_id, filters)
    if suppressed:
        result["suppressed_excluded"] = suppressed
    return result


def _csv_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
