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
    "first name": "Lucia",
    "last name": "Stanković",
    "job title": "Marketing Director",
    "company": "Riot Games",
    "linkedin url": "https://www.linkedin.com/in/lucia-stankovic",
    "corporate website": "http://www.riotgames.com",
    "linkedin industry": "Computer Games",
    "linkedin employees": "1001-5000",
    "location": "Laguna Beach, California, United States",
    "linkedin company location": "Los Angeles, California, United States",
    "member linkedin id": "urn:li:member:22728810",
    "member linkedin sales nav id": "urn:li:fs_salesProfile:(ACwAAA,NAME_SEARCH,x)",
}


def test_detect_sales_nav_format():
    fmt, conf = impfmt.detect_import_format(set(VAYNE_ROW.keys()))
    assert fmt == "sales_navigator"
    assert conf == "high"


def test_normalize_vayne_row():
    row = impfmt.normalize_import_row(VAYNE_ROW)
    assert row["name"] == "Lucia Stanković"
    assert row["title"] == "Marketing Director"
    assert row["company"] == "Riot Games"
    assert row["company_domain"] == "http://www.riotgames.com"
    assert row["industry"] == "Computer Games"
    assert row["headcount"] == "1001-5000"
    assert row["location_city"] == "Laguna Beach"
    assert row["location_state"] == "California"
    assert row["location_country"] == "United States"
    assert row["hq_city"] == "Los Angeles"
    assert row["mailmerge_first_name"] == "Lucia"
    assert row["mailmerge_company_name"] == "Riot Games"
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
    assert "Lucia" in lead["name"]
    assert lead["title"] == "Marketing Director"
    assert lead["industry"] == "Computer Games"
    assert domain and domain["domain"] == "riotgames.com"


def test_import_dry_run_preview_fields():
    _reset_db()
    summary = om.import_profiles([VAYNE_ROW], dry_run=True, import_format="auto")
    assert summary["import_format"] == "sales_navigator"
    assert "first name" in summary["fields_mapped"]
    assert summary.get("sample_preview", {}).get("name") == "Lucia Stanković"


def test_csv_roundtrip_headers():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(VAYNE_ROW.keys()))
    writer.writeheader()
    writer.writerow(VAYNE_ROW)
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    normalized, meta = impfmt.preprocess_import_rows(rows)
    assert meta["detected_format"] == "sales_navigator"
    assert normalized[0]["name"] == "Lucia Stanković"
