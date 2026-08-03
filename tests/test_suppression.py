"""Suppression lists: rules authored against identifier VALUES, resolved to
leads at match time, and excluded from everything by default.

The two properties worth defending here are the ones the feature exists for:

1. Default-exclude reaches every surface. The contacts list, its stat counts,
   the CSV export and bulk-action id selection all build their WHERE from
   `lead_filter_clause`, so one clause has to cover all four. A suppression
   that hides a contact from the list but ships it in the export is worse than
   no suppression at all.

2. It survives a round trip. Suppressing by value (not by lead_id) has to keep
   working when the lead is deleted and re-imported with a new id -- which is
   exactly what a tag or a lead column would not do.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dashboard_queries as dq  # noqa: E402
import lead_export  # noqa: E402
import pipeline as om  # noqa: E402
import suppression as sp  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _workspace():
    om.create_workspace("Acme Outbound", slug="acme")
    conn = om.get_conn()
    return conn, om.resolve_workspace_identity(conn, "acme")["id"]


def _lead(conn, ws_id, name, email, company):
    lead = om.resolve_lead(name=name, email=email, company=company, source="csv",
                           allow_weak_identity=True, conn=conn)
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead["id"])
    conn.commit()
    return lead["id"]


def _names(conn, ws_id, **filters):
    return sorted(
        r["name"] for r in dq.search_leads(conn, ws_id, limit=50, **filters)["leads"]
    )


def test_company_domain_entry_suppresses_everyone_at_that_domain():
    """The headline case: block a domain, and everyone associated with it goes."""
    conn, ws_id = _workspace()
    _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    _lead(conn, ws_id, "John Roe", "john@acme.com", "Acme Inc")
    _lead(conn, ws_id, "Cy Poe", "cy@clean.com", "Clean Co")

    result = sp.add_entry(conn, entry_type="company_domain",
                          value="https://www.acme.com/careers", workspace_id=ws_id,
                          reason="existing_customer")
    # The value is normalized to a registrable domain, so a URL, an @-prefixed
    # form and a bare domain are all the same rule.
    assert result["value"] == "acme.com"
    assert result["contacts_suppressed"] == 2
    assert _names(conn, ws_id) == ["Cy Poe"]
    conn.close()


def test_default_excludes_from_list_counts_export_and_id_selection():
    """One clause, four surfaces. Any of them leaking is the bug."""
    conn, ws_id = _workspace()
    _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    _lead(conn, ws_id, "Cy Poe", "cy@clean.com", "Clean Co")
    sp.add_entry(conn, entry_type="email", value="jane@acme.com",
                 workspace_id=ws_id, reason="unsubscribed")

    assert _names(conn, ws_id) == ["Cy Poe"]
    assert dq.contacts_stats(conn, ws_id)["overall"]["total"] == 1
    ids = dq.search_leads(conn, ws_id, ids_only=True, limit=50)
    assert len(ids["lead_ids"] if "lead_ids" in ids else ids["leads"]) == 1
    _cols, rows = lead_export.export_rows(conn, ws_id, fields=["name"])
    assert [r["name"] for r in rows] == ["Cy Poe"]
    conn.close()


def test_suppressed_only_and_all_are_the_deliberate_opt_outs():
    conn, ws_id = _workspace()
    _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    _lead(conn, ws_id, "Cy Poe", "cy@clean.com", "Clean Co")
    sp.add_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=ws_id)

    assert _names(conn, ws_id, suppressed="only") == ["Jane Doe"]
    assert _names(conn, ws_id, suppressed="all") == ["Cy Poe", "Jane Doe"]
    conn.close()


def test_suppression_survives_the_lead_being_deleted_and_re_imported():
    """The round-trip requirement, and the reason rules key on values.

    A suppression stored as a tag or a lead column dies with the row. Stored
    against the address, a re-import of the same person is suppressed again the
    moment it lands -- no second action from the operator.
    """
    conn, ws_id = _workspace()
    lead_id = _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    sp.add_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=ws_id)
    assert _names(conn, ws_id) == []

    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    new_id = _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    assert new_id != lead_id

    sp.reconcile(conn, workspace_id=ws_id)
    assert _names(conn, ws_id) == []
    conn.close()


def test_workspace_scoped_suppression_does_not_leak_to_another_workspace():
    """Explicitly required: blocked for one client, available for another."""
    conn, ws_a = _workspace()
    om.create_workspace("Beta Outbound", slug="beta")
    ws_b = om.resolve_workspace_identity(conn, "beta")["id"]
    lead_id = _lead(conn, ws_a, "Jane Doe", "jane@acme.com", "Acme Inc")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_b, lead_id)
    conn.commit()

    sp.add_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=ws_a)
    assert _names(conn, ws_a) == []
    assert _names(conn, ws_b) == ["Jane Doe"]
    conn.close()


def test_org_wide_entry_applies_to_every_workspace():
    conn, ws_a = _workspace()
    om.create_workspace("Beta Outbound", slug="beta")
    ws_b = om.resolve_workspace_identity(conn, "beta")["id"]
    lead_id = _lead(conn, ws_a, "Jane Doe", "jane@acme.com", "Acme Inc")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_b, lead_id)
    conn.commit()

    sp.add_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=None,
                 reason="legal_request")
    assert _names(conn, ws_a) == []
    assert _names(conn, ws_b) == []
    conn.close()


def test_revoke_is_a_soft_delete_that_releases_the_contacts():
    conn, ws_id = _workspace()
    _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    sp.add_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=ws_id)
    assert _names(conn, ws_id) == []

    result = sp.revoke_entry(conn, entry_type="email", value="jane@acme.com",
                             workspace_id=ws_id)
    assert result["contacts_released"] == 1
    assert _names(conn, ws_id) == ["Jane Doe"]
    # The row survives revocation -- "who un-suppressed this, and when" stays
    # answerable.
    assert sp.list_entries(conn, workspace_id=ws_id) == []
    assert len(sp.list_entries(conn, workspace_id=ws_id, include_revoked=True)) == 1
    conn.close()


def test_re_adding_a_revoked_entry_reuses_its_row():
    conn, ws_id = _workspace()
    _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    first = sp.add_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=ws_id)
    sp.revoke_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=ws_id)
    again = sp.add_entry(conn, entry_type="email", value="jane@acme.com", workspace_id=ws_id)

    assert again["status"] == "updated"
    assert again["entry_id"] == first["entry_id"]
    assert _names(conn, ws_id) == []
    conn.close()


def test_values_are_normalized_so_formatting_does_not_create_a_second_rule():
    conn, ws_id = _workspace()
    _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    sp.add_entry(conn, entry_type="email", value="  JANE@Acme.com. ", workspace_id=ws_id)
    # Trailing dot, case and padding all resolve to the stored address -- the
    # same canonicalization the verifier path now uses.
    assert _names(conn, ws_id) == []
    assert len(sp.list_entries(conn, workspace_id=ws_id)) == 1
    conn.close()


def test_check_says_which_rule_is_responsible():
    """The support question this feature generates, answered in one call."""
    conn, ws_id = _workspace()
    _lead(conn, ws_id, "Jane Doe", "jane@acme.com", "Acme Inc")
    sp.add_entry(conn, entry_type="company_domain", value="acme.com",
                 workspace_id=ws_id, reason="competitor")

    result = sp.check(conn, "acme.com", workspace_id=ws_id)
    assert result["suppressed"] is True
    assert result["matching_entries"][0]["reason"] == "competitor"

    assert sp.check(conn, "clean.com", workspace_id=ws_id)["suppressed"] is False
    conn.close()


def test_bad_values_are_rejected_and_bulk_add_reports_the_bad_row():
    conn, ws_id = _workspace()
    with pytest.raises(sp.SuppressionError):
        sp.add_entry(conn, entry_type="email", value="not-an-email", workspace_id=ws_id)
    with pytest.raises(sp.SuppressionError):
        sp.add_entry(conn, entry_type="nonsense", value="x", workspace_id=ws_id)
    with pytest.raises(sp.SuppressionError):
        sp.add_entry(conn, entry_type="email", value="a@b.com", workspace_id=ws_id,
                     reason="because-i-said-so")

    # One bad row must not take the other 399 down with it.
    result = sp.add_entries_bulk(conn, [
        {"type": "email_domain", "value": "acme.com"},
        {"type": "email_domain", "value": "!!!"},
        {"type": "email_domain", "value": "beta.io"},
    ], workspace_id=ws_id)
    assert result["added"] == 2
    assert result["failed"] == 1
    assert result["errors"][0]["row"] == 2
    conn.close()
