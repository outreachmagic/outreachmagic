"""Import a LinkedIn "Connections" data export (Settings & Privacy -> Get a
copy of your data -> Connections) as leads, tagging each row with LinkedIn
connection status for the given sender.

This is a thin CSV adapter, not a new matching/upsert engine: it maps
LinkedIn's export columns onto the row shape import_profiles() already
understands (name/email/company/title/linkedin/is_connected_linkedin/tags),
then delegates entirely to the existing tiered-identity import pipeline --
find-or-create by linkedin_url, workspace_lead_linkedin_status upsert, and
sender-profile normalization are all handled there already.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_MONTH_DAY_YEAR_FORMATS = ("%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y")
_RELATIVE_RE = re.compile(
    r"^(\d+)\s*(day|week|month|mo|year|yr)s?\s*ago$", re.IGNORECASE,
)
_RELATIVE_UNIT_DAYS = {
    "day": 1, "week": 7, "month": 30, "mo": 30, "year": 365, "yr": 365,
}


def parse_connected_on(raw: Optional[str]) -> Optional[str]:
    """Parse LinkedIn's inconsistent "Connected On" column into ISO 8601.

    Seen in the wild: "12 Jan 2026", "2026-01-12", and (defensively handled,
    though not seen in LinkedIn's own official export) relative forms like
    "1 mo ago". Returns None on anything unparseable -- the caller falls back
    to import time rather than guessing.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        pass
    for fmt in _MONTH_DAY_YEAR_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    m = _RELATIVE_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * _RELATIVE_UNIT_DAYS.get(unit, 0)
        if days:
            return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return None


def _find_header_row(lines: list[str]) -> int:
    """LinkedIn's export precedes the real header with a variable number of
    Notes/blank lines -- detect the header by content ("first name" as the
    first column) rather than assuming a fixed line count, so a future
    export-format tweak (an extra blank line, a longer notice) doesn't
    silently swallow real rows or misparse the notice as data."""
    for i, line in enumerate(lines):
        first_cell = line.split(",", 1)[0].strip().strip('"').lower()
        if first_cell == "first name":
            return i
    return 0


def parse_linkedin_connections_csv(path: str) -> list[dict]:
    """Read a LinkedIn connections export and return import_profiles()-ready
    rows: name/email/company/title/linkedin/is_connected_linkedin/
    linkedin_connected_at/list_source."""
    raw_text = Path(path).read_text(encoding="utf-8-sig")
    lines = raw_text.splitlines()
    header_idx = _find_header_row(lines)
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))

    rows: list[dict] = []
    skipped_no_linkedin = 0
    for raw in reader:
        url = (raw.get("URL") or "").strip()
        if not url:
            skipped_no_linkedin += 1
            continue
        first = (raw.get("First Name") or "").strip()
        last = (raw.get("Last Name") or "").strip()
        name = f"{first} {last}".strip() or None
        row: dict = {
            "linkedin": url,
            "is_connected_linkedin": "1",
            "list_source": "linkedin_connections",
        }
        if name:
            row["name"] = name
        email = (raw.get("Email Address") or "").strip()
        if email:
            row["email"] = email
        company = (raw.get("Company") or "").strip()
        if company:
            row["company"] = company
        title = (raw.get("Position") or "").strip()
        if title:
            row["title"] = title
        connected_at = parse_connected_on(raw.get("Connected On"))
        if connected_at:
            row["linkedin_connected_at"] = connected_at
        rows.append(row)
    return rows


def import_linkedin_connections(
    path: str,
    *,
    workspace: str,
    sender: str,
    tag: Optional[str] = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict:
    """Parse a LinkedIn connections CSV and import via the existing
    import_profiles() pipeline -- no bespoke matching/upsert logic here."""
    from pipeline import import_profiles

    rows = parse_linkedin_connections_csv(path)
    total_csv_rows = len(rows)
    if tag:
        for row in rows:
            row["tags"] = tag
    summary = import_profiles(
        rows,
        dry_run=dry_run,
        overwrite=overwrite,
        workspace=workspace,
        sender_profile=sender,
        source="linkedin_connections",
        source_detail="linkedin-connections-export",
        import_format="generic",
    )
    summary["csv_rows_with_linkedin_url"] = total_csv_rows
    return summary
