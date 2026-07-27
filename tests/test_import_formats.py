"""Tests for Sales Nav / Vayne CSV import normalization."""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import import_formats as impfmt  # noqa: E402
import pipeline as om  # noqa: E402


VAYNE_ROW = {
    "first name": "Renata",
    "last name": "Vukčević",
    "job title": "Marketing Director",
    "company": "Vandelay Games",
    "linkedin url": "https://www.linkedin.com/in/lucia-stankovic",
    "corporate website": "http://www.vandelaygames.com",
    "linkedin industry": "Computer Games",
    "linkedin employees": "1001-5000",
    "location": "Laguna Beach, California, United States",
    "linkedin company location": "Los Angeles, California, United States",
    "member linkedin id": "urn:li:member:22728810",
    "member linkedin sales nav id": "urn:li:fs_salesProfile:(ACwAAA,NAME_SEARCH,x)",
}


def test_camelcase_linkedinurl_header_maps_to_canonical_linkedin_field():
    """A CSV generator that emits "LinkedInUrl" (camelCase, no separator) --
    seen from Modern Storefront's Apify categorization script -- used to be
    silently dropped: normalize_header_key() only lowercases, so "LinkedInUrl"
    collapses to "linkedinurl", which wasn't in HEADER_ALIASES or the
    _pick_best_linkedin_from_raw() alias tuple (both only had the space/
    underscore-delimited spellings). See OM-IMPORT-FIELD-MAPPING-DESIGN.md."""
    row = impfmt.normalize_import_row({"Name": "Jane", "LinkedInUrl": "https://www.linkedin.com/in/real-handle"})
    assert row["linkedin"] == "https://www.linkedin.com/in/real-handle"

    picked = impfmt._pick_best_linkedin_from_raw({"LinkedInUrl": "https://www.linkedin.com/in/real-handle"})
    assert picked == "https://www.linkedin.com/in/real-handle"


def test_detect_sales_nav_format():
    fmt, conf = impfmt.detect_import_format(set(VAYNE_ROW.keys()))
    assert fmt == "sales_navigator"
    assert conf == "high"


def test_normalize_vayne_row():
    row = impfmt.normalize_import_row(VAYNE_ROW)
    assert row["name"] == "Renata Vukčević"
    assert row["title"] == "Marketing Director"
    assert row["company"] == "Vandelay Games"
    assert row["company_domain"] == "http://www.vandelaygames.com"
    assert row["industry"] == "Computer Games"
    assert row["headcount"] == "1001-5000"
    assert row["location_city"] == "Laguna Beach"
    assert row["location_state"] == "California"
    assert row["location_country"] == "United States"
    assert row["hq_city"] == "Los Angeles"
    # personalization fields are only set when explicitly present in the CSV
    assert "personalized_first_name" not in row
    assert "personalized_company_name" not in row
    assert row["external_id"] == "sales_navigator:urn:li:member:22728810"


def _reset_db() -> None:
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def test_import_profiles_vayne_row_not_unknown():
    _reset_db()
    summary = om.import_profiles([VAYNE_ROW], import_format="auto")
    assert summary["processed"] == 1
    lead_id = int(summary["results"][0]["id"])
    conn = om.get_conn()
    lead = conn.execute(
        "SELECT name, title, industry, headcount FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    domain = conn.execute(
        "SELECT domain FROM companies c JOIN leads l ON l.company_id = c.id WHERE l.id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert lead["name"] != "Unknown"
    assert "Renata" in lead["name"]
    assert lead["title"] == "Marketing Director"
    assert lead["industry"] == "Computer Games"
    assert domain and domain["domain"] == "vandelaygames.com"


def test_import_dry_run_preview_fields():
    _reset_db()
    summary = om.import_profiles([VAYNE_ROW], dry_run=True, import_format="auto")
    assert summary["import_format"] == "sales_navigator"
    assert "first name" in summary["fields_mapped"]
    assert summary.get("sample_preview", {}).get("name") == "Renata Vukčević"


def test_dry_run_suggests_fields_missing_from_this_csv():
    """fields_available_but_not_present (OM-IMPORT-FIELD-MAPPING-DESIGN.md,
    Fix D): fields the pipeline supports that this CSV doesn't have, so
    someone building an import CSV can see "you could also send industry /
    linkedin_bio / hq_city" without reading source."""
    _reset_db()
    row = {"name": "Jane Doe", "title": "VP Sales", "company": "Acme", "email": "jane@acme.com"}
    summary = om.import_profiles([row], dry_run=True, import_format="auto")
    missing = set(summary["fields_available_but_not_present"])
    assert "industry" in missing
    assert "linkedin_bio" in missing
    assert "hq_city" in missing
    # Present in the row -- must not also be "suggested".
    assert "name" not in missing
    assert "title" not in missing
    assert "company" not in missing


def test_dry_run_suggestion_resolves_aliases_not_raw_headers():
    """The original design-doc sketch diffed KNOWN_IMPORTABLE_FIELDS against
    raw un-aliased header strings -- a CSV column like "job title" would
    still show canonical "title" as missing even though it's already present
    via alias. Must diff against the resolved/normalized row keys instead."""
    _reset_db()
    row = dict(VAYNE_ROW)  # uses "job title", "linkedin url", "linkedin industry", etc.
    summary = om.import_profiles([row], dry_run=True, import_format="sales_navigator")
    missing = set(summary["fields_available_but_not_present"])
    assert "title" not in missing
    assert "linkedin" not in missing
    assert "industry" not in missing


def test_dry_run_suggestions_exclude_activity_and_duplicate_fields():
    """IMPORT_EXTRA_FIELDS also has activity/derived fields (last_message_sent,
    last_message_received, linkedin_connected_at) and duplicate Sales-Nav-ID
    aliases (member linkedin sales nav id, sales_nav_id) -- suggesting those
    as "you could add this column" would be noise, not guidance."""
    _reset_db()
    row = {"name": "Jane Doe"}
    summary = om.import_profiles([row], dry_run=True, import_format="auto")
    missing = set(summary["fields_available_but_not_present"])
    assert "last_message_sent" not in missing
    assert "last_message_received" not in missing
    assert "linkedin_connected_at" not in missing
    assert "member linkedin sales nav id" not in missing
    assert "sales_nav_id" not in missing
    assert "linkedin_sales_nav_id" in missing


def test_plain_canonical_headers_not_reported_dropped():
    """name/title pass straight through normalize_import_row unchanged, but used to be
    misreported as fields_dropped since OM_MAPPED_FIELDS wasn't consulted."""
    rows = [{"name": "Jane Doe", "title": "VP Sales", "company": "Acme", "email": "jane@acme.com"}]
    _, meta = impfmt.preprocess_import_rows(rows)
    assert "name" not in meta["fields_dropped"]
    assert "title" not in meta["fields_dropped"]
    assert meta["sample_preview"]["name"] == "Jane Doe"
    assert meta["sample_preview"]["title"] == "VP Sales"


def test_tags_and_preserved_extras_not_reported_dropped():
    """tags (and the other PRESERVED_EXTRA_FIELDS: list_source, import_name,
    lead_status, lead_sentiment, contact_order, is_connected_linkedin,
    is_linkedin_request_pending) are preserved verbatim by normalize_import_row
    and actually applied during import -- but OM_MAPPED_FIELDS never listed them,
    so the dry-run's fields_dropped wrongly claimed they were dropped. That
    false alarm is what a real bug report mistook for tags being silently
    discarded by the sales_navigator import format."""
    row = dict(VAYNE_ROW)
    row["tags"] = "career services 07-11-26"
    rows, meta = impfmt.preprocess_import_rows([row], import_format="sales_navigator")
    assert "tags" not in meta["fields_dropped"]
    assert "tags" in meta["fields_mapped"]
    assert rows[0]["tags"] == "career services 07-11-26"


def test_csv_roundtrip_headers():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(VAYNE_ROW.keys()))
    writer.writeheader()
    writer.writerow(VAYNE_ROW)
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    normalized, meta = impfmt.preprocess_import_rows(rows)
    assert meta["detected_format"] == "sales_navigator"
    assert normalized[0]["name"] == "Renata Vukčević"
