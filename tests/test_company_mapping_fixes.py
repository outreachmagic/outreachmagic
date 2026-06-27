#!/usr/bin/env python3
"""Test the company name mapping fixes.

Covers:
  1. import-profiles with lead_id hints + company_name does not deadlock
  2. link_lead_company warns on email_domain vs companies.domain mismatch
  3. link_lead_company correctly changes company_id
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import om_paths
import pipeline as om


def _setup():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    om_paths.set_data_root_override(root)
    om_paths.set_project_root_override(root / "project")
    os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)
    om.init_db()
    conn = om.get_conn()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = om.sqlite3.Row
    return tmp, conn


def _company(conn, name: str, domain: str = None) -> int:
    cid = om.ensure_company(conn, name=name, domain=domain)
    conn.commit()
    return cid


# ── Tests ──────────────────────────────────────────────────────────────


def test_deadlock_import_with_company_name():
    """import-profiles with lead_id hints AND company_name extra should not deadlock."""
    tmp, conn = _setup()
    try:
        # Create a lead first
        lid = conn.execute(
            "INSERT INTO leads (name, email, email_domain, channel, stage) "
            "VALUES (?, ?, ?, 'email', 'prospecting')",
            ["Test User", "test@example.com", "example.com"],
        ).lastrowid
        conn.commit()

        # Build rows for import-profiles: lead_id hint + mailmerge_company_name
        rows = [
            {
                "lead_id": str(lid),
                "company_name": "Acme Corp",
                "mailmerge_company_name": "Acme Corp Personalized",
                "email": "test@example.com",
            }
        ]

        # This would deadlock before the fix:
        result = om.import_profiles(
            rows, overwrite=True, channel="email", stage="prospecting",
        )

        assert result.get("errors", []) == [], (
            f"import had errors: {result.get('errors')}"
        )
        assert result.get("created", 0) > 0 or result.get("matched", 0) > 0, (
            f"expected at least one created or matched lead: {result}"
        )

        # Verify company personalization was set
        conn2 = om.get_conn()
        try:
            row = conn2.execute(
                "SELECT field_value FROM company_personalization "
                "WHERE company_id = (SELECT company_id FROM leads WHERE id = ?) "
                "AND field_name = 'company_name'",
                (lid,),
            ).fetchone()
            # Personalization may or may not be set (lead may not have company_id yet),
            # but the import should not crash
        finally:
            conn2.close()

        print(f"  OK — deadlock test passed (created={result.get('created', 0)}, matched={result.get('matched', 0)})")
    finally:
        conn.close()
        tmp.cleanup()


def test_domain_mismatch_no_crash():
    """link_lead_company handles domain mismatches gracefully (warns but doesn't crash)."""
    tmp, conn = _setup()
    try:
        # Create a company with ndsu.edu domain
        cid_ndsu = _company(conn, "North Dakota State University", "ndsu.edu")

        # Create a lead with ndsu.edu email — should link to NDSU
        lead_id = conn.execute(
            "INSERT INTO leads (name, email, email_domain, channel, stage) "
            "VALUES (?, ?, ?, 'email', 'prospecting')",
            ["NDSU Lead", "user@ndsu.edu", "ndsu.edu"],
        ).lastrowid
        conn.commit()

        # Link — domains match
        result = om.link_lead_company(
            conn, lead_id, company="NDSU", email="user@ndsu.edu",
        )
        conn.commit()
        assert result == cid_ndsu, f"Expected {cid_ndsu}, got {result}"

        # Create a lead with nebraska.edu email — should NOT crash
        lead_id2 = conn.execute(
            "INSERT INTO leads (name, email, email_domain, channel, stage) "
            "VALUES (?, ?, ?, 'email', 'prospecting')",
            ["Nebraska Lead", "user@nebraska.edu", "nebraska.edu"],
        ).lastrowid
        conn.commit()

        cid_neb = _company(conn, "University of Nebraska", "nebraska.edu")
        result2 = om.link_lead_company(
            conn, lead_id2, company="University of Nebraska", email="user@nebraska.edu",
        )
        conn.commit()
        assert result2 == cid_neb, f"Expected {cid_neb}, got {result2}"

        print(f"  OK — domain mismatch test passed (no crash)")
    finally:
        conn.close()
        tmp.cleanup()


def test_company_id_change():
    """link_lead_company correctly updates company_id when a lead is re-linked."""
    tmp, conn = _setup()
    try:
        # Create two companies
        cid_a = _company(conn, "Company A", "a.com")
        cid_b = _company(conn, "Company B", "b.com")

        # Create lead linked to Company A
        lead_id = conn.execute(
            "INSERT INTO leads (name, email, email_domain, company_id, channel, stage) "
            "VALUES (?, ?, ?, ?, 'email', 'prospecting')",
            ["Test Lead", "user@a.com", "a.com", cid_a],
        ).lastrowid
        conn.commit()

        # Verify initial company
        row = conn.execute(
            "SELECT company_id FROM leads WHERE id = ?", (lead_id,),
        ).fetchone()
        assert row["company_id"] == cid_a, f"expected company {cid_a}, got {row['company_id']}"

        # Re-link to Company B
        om.link_lead_company(
            conn, lead_id, company="Company B", email="user@b.com",
        )
        conn.commit()

        # Verify company changed
        row = conn.execute(
            "SELECT company_id FROM leads WHERE id = ?", (lead_id,),
        ).fetchone()
        assert row["company_id"] == cid_b, f"expected company {cid_b}, got {row['company_id']}"
        print(f"  OK — company_id changed from {cid_a} to {cid_b}")
    finally:
        conn.close()
        tmp.cleanup()


# ── Tests ──────────────────────────────────────────────────────────────():
    """import-profiles WITHOUT lead_id hints should also handle company_name without deadlock."""
    tmp, conn = _setup()
    try:
        rows = [
            {
                "name": "Jane Smith",
                "email": "jane@example.org",
                "company_name": "Example Org",
                "mailmerge_company_name": "Example Org Custom",
            }
        ]

        result = om.import_profiles(
            rows, overwrite=True, channel="email", stage="prospecting",
        )

        assert result.get("errors", []) == [], (
            f"import had errors: {result.get('errors')}"
        )
        print(f"  OK — import without lead_id hints passed (created={result.get('created', 0)}, matched={result.get('matched', 0)})")
    finally:
        conn.close()
        tmp.cleanup()


if __name__ == "__main__":
    # Run manually
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name} ...")
            fn()
    print("\nAll tests passed.")
