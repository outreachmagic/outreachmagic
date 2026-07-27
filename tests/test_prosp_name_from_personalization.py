#!/usr/bin/env python3
"""Regression: a CSV row with no name column but a "Personalized First Name"
column (Prosp LinkedIn-match exports carry this) must land in leads.name, not
only in lead_personalization where nothing reads it back into the profile."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402


def _fresh_db():
    db_path = om.get_db_path()
    if db_path.exists():
        db_path.unlink()
    om.init_db()


def test_personalized_first_name_backfills_name_when_row_has_no_name():
    _fresh_db()
    rows = [{
        "email": "aum221@bluthe.edu",
        "company": "Bluthe University",
        "personalized_first_name": "Autumn",
    }]
    summary = om.import_profiles(rows)
    lead_id = summary["results"][0]["id"]
    conn = om.get_conn()
    row = conn.execute("SELECT name FROM leads WHERE id = ?", (lead_id,)).fetchone()
    pers = {r["field_name"] for r in conn.execute(
        "SELECT field_name FROM lead_personalization WHERE lead_id = ?", (lead_id,),
    ).fetchall()}
    conn.close()
    assert row["name"] == "Autumn"
    # Promoted to the real name column -- no longer a separate override.
    assert "first_name" not in pers


def test_personalized_first_name_still_overrides_when_row_has_a_real_name():
    _fresh_db()
    rows = [{
        "email": "import@test.com",
        "name": "Import Test",
        "personalized_first_name": "Imp",
    }]
    summary = om.import_profiles(rows)
    lead_id = summary["results"][0]["id"]
    conn = om.get_conn()
    row = conn.execute("SELECT name FROM leads WHERE id = ?", (lead_id,)).fetchone()
    pers = conn.execute(
        "SELECT field_value FROM lead_personalization WHERE lead_id = ? AND field_name = 'first_name'",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["name"] == "Import Test"
    assert pers["field_value"] == "Imp"
