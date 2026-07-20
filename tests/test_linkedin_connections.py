"""linkedin_connections.py: parsing LinkedIn's Connections.csv export and
importing it through the existing import_profiles() pipeline (no bespoke
matching/upsert logic of its own)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402
from linkedin_connections import (  # noqa: E402
    import_linkedin_connections,
    parse_connected_on,
    parse_linkedin_connections_csv,
)

_SAMPLE_CSV = (
    "Notes:\n"
    '"When exporting your connection data, you may notice some information is missing."\n'
    "\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Jane,Doe,https://www.linkedin.com/in/janedoe,jane@acme.com,Acme Corp,CEO,12 Jan 2026\n"
    "John,Smith,https://www.linkedin.com/in/johnsmith,,Beta Inc,VP Sales,2026-02-03\n"
    "No,LinkedIn,,noone@example.com,Gamma LLC,Analyst,15 Mar 2026\n"
)


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def test_parse_connected_on_month_day_year():
    assert parse_connected_on("12 Jan 2026").startswith("2026-01-12")


def test_parse_connected_on_iso_passthrough():
    assert parse_connected_on("2026-02-03").startswith("2026-02-03")


def test_parse_connected_on_relative():
    result = parse_connected_on("1 mo ago")
    assert result is not None


def test_parse_connected_on_empty_returns_none():
    assert parse_connected_on("") is None
    assert parse_connected_on(None) is None


def test_parse_connected_on_garbage_returns_none():
    assert parse_connected_on("not a date at all") is None


def test_parse_csv_skips_preamble_and_finds_header(tmp_path):
    f = tmp_path / "Connections.csv"
    f.write_text(_SAMPLE_CSV, encoding="utf-8-sig")
    rows = parse_linkedin_connections_csv(str(f))
    assert len(rows) == 2, "the row with no LinkedIn URL must be skipped"
    assert rows[0]["name"] == "Jane Doe"
    assert rows[0]["linkedin"] == "https://www.linkedin.com/in/janedoe"
    assert rows[0]["email"] == "jane@acme.com"
    assert rows[0]["company"] == "Acme Corp"
    assert rows[0]["title"] == "CEO"
    assert rows[0]["is_connected_linkedin"] == "1"
    assert rows[0]["list_source"] == "linkedin_connections"
    assert rows[0]["linkedin_connected_at"].startswith("2026-01-12")


def test_parse_csv_row_without_email_still_included(tmp_path):
    f = tmp_path / "Connections.csv"
    f.write_text(_SAMPLE_CSV, encoding="utf-8-sig")
    rows = parse_linkedin_connections_csv(str(f))
    john = next(r for r in rows if r["name"] == "John Smith")
    assert "email" not in john


def test_import_linkedin_connections_creates_leads_and_sets_status(tmp_path):
    f = tmp_path / "Connections.csv"
    f.write_text(_SAMPLE_CSV, encoding="utf-8-sig")

    conn = om.get_conn()
    om.resolve_workspace_identity(conn, "default")
    ws_row = conn.execute("SELECT id FROM workspaces WHERE slug = 'default'").fetchone()
    conn.close()

    summary = import_linkedin_connections(
        str(f), workspace="default", sender="linkedin.com/in/senderprofile",
    )
    assert summary["created"] == 2
    assert summary["csv_rows_with_linkedin_url"] == 2

    conn = om.get_conn()
    rows = conn.execute(
        """SELECT wl.sender_profile, wl.is_connected FROM workspace_lead_linkedin_status wl
           JOIN leads l ON l.id = wl.lead_id WHERE l.name = 'Jane Doe'""",
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["sender_profile"] == "linkedin.com/in/senderprofile"
    assert rows[0]["is_connected"] == 1
    conn.close()


def test_import_linkedin_connections_uses_real_connected_at_not_import_time():
    conn = om.get_conn()
    lead = om.resolve_lead(
        name="Jane Doe", linkedin_url="https://www.linkedin.com/in/janedoe", conn=conn,
    )
    conn.commit()
    conn.close()

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as tf:
        tf.write(_SAMPLE_CSV)
        path = tf.name
    try:
        import_linkedin_connections(path, workspace="default", sender="linkedin.com/in/sender2")
    finally:
        Path(path).unlink()

    conn = om.get_conn()
    row = conn.execute(
        "SELECT connected_at FROM workspace_lead_linkedin_status WHERE lead_id = ?", (lead["id"],),
    ).fetchone()
    assert row["connected_at"].startswith("2026-01-12"), (
        "must use the CSV's real Connected On date, not the import timestamp"
    )
    conn.close()


def test_import_linkedin_connections_applies_optional_tag(tmp_path):
    f = tmp_path / "Connections.csv"
    f.write_text(_SAMPLE_CSV, encoding="utf-8-sig")

    summary = import_linkedin_connections(
        str(f), workspace="default", sender="linkedin.com/in/senderprofile", tag="janedoe_1st",
    )
    assert summary["tagged"] == 2

    conn = om.get_conn()
    tag_rows = conn.execute(
        "SELECT DISTINCT tag FROM workspace_lead_tags WHERE tag = 'janedoe_1st'",
    ).fetchall()
    assert len(tag_rows) == 1
    conn.close()


def test_import_linkedin_connections_matches_existing_lead_by_linkedin_url():
    conn = om.get_conn()
    existing = om.resolve_lead(
        name="Jane Doe", linkedin_url="https://www.linkedin.com/in/janedoe", conn=conn,
    )
    conn.commit()
    conn.close()

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as tf:
        tf.write(_SAMPLE_CSV)
        path = tf.name
    try:
        summary = import_linkedin_connections(path, workspace="default", sender="linkedin.com/in/senderprofile")
    finally:
        Path(path).unlink()

    assert summary["matched"] >= 1
    conn = om.get_conn()
    status = conn.execute(
        "SELECT is_connected FROM workspace_lead_linkedin_status WHERE lead_id = ?", (existing["id"],),
    ).fetchone()
    assert status["is_connected"] == 1
    conn.close()


def test_import_linkedin_connections_dry_run_does_not_write():
    conn = om.get_conn()
    before = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    conn.close()

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as tf:
        tf.write(_SAMPLE_CSV)
        path = tf.name
    try:
        import_linkedin_connections(
            path, workspace="default", sender="linkedin.com/in/senderprofile", dry_run=True,
        )
    finally:
        Path(path).unlink()

    conn = om.get_conn()
    after = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    conn.close()
    assert after == before
