"""Verification candidates: the limit that never reached the query, and the
`--max-age 0` claim it was mistaken for.

The reported symptom was "verify-bulk stops at 5000". The CLI flag and the SQL
bind for `limit` both already existed; `_collect_verify_emails` simply never
forwarded one, so `run_verification_candidates`'s own default was always what
ran. `test_collect_verify_emails_forwards_limit` is the regression that would
have caught it -- without it the fix is one refactor away from reverting.

`test_max_age_zero_includes_verified` checks the *other* half of that report:
the claim that `--max-age 0` still excludes already-valid rows. It does not --
the test passed against the SQL unchanged, because `-0 days` resolves to `now`
and `email_verified_at < now` admits every past verification. The set looked
truncated for the reason above, not this one. The test stays as a guard on
behaviour that is already correct; no `OR max_age_days = 0` branch was added,
because a second code path for one parameter value is how two paths drift.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _seed(n: int = 12) -> None:
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    for i in range(n):
        lead = om.resolve_lead(
            name=f"Lead {i:02d}", source="csv", allow_weak_identity=True, conn=conn)
        conn.execute(
            "UPDATE leads SET email = ? WHERE id = ?", (f"lead{i:02d}@acme.com", lead["id"]))
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
    conn.commit()
    conn.close()


def test_limit_zero_returns_all():
    """0 means no cap -- the whole point of the flag."""
    _seed(12)
    res = om.leads_needing_verification("storefront", limit=0)
    assert res["count"] == 12
    assert res["truncated"] is False
    assert res["limit"] == 0


def test_limit_caps_and_reports_truncation():
    """A capped result says so. A silent truncation is how this survived."""
    _seed(12)
    res = om.leads_needing_verification("storefront", limit=5)
    assert res["count"] == 5
    assert res["truncated"] is True


def test_negative_limit_is_uncapped_too():
    _seed(3)
    assert om.leads_needing_verification("storefront", limit=-1)["count"] == 3


def test_max_age_zero_includes_verified():
    """`--max-age 0` must include rows already marked valid.

    Both leads are verified: one long ago, one a moment ago. With max_age_days=0
    the age filter is `email_verified_at < datetime('now', '-0 days')` -- i.e.
    `< now` -- which admits every past verification.
    """
    _seed(0)
    om.create_workspace("Storefront", slug="storefront")
    conn = om.get_conn()
    ws = om.resolve_workspace_identity(conn, "storefront")
    for name, email, verified_at in [
        ("Old Val", "old@acme.com", "datetime('now', '-400 days')"),
        ("New Val", "new@acme.com", "datetime('now', '-10 seconds')"),
    ]:
        lead = om.resolve_lead(
            name=name, source="csv", allow_weak_identity=True, conn=conn)
        conn.execute(
            f"""UPDATE leads
                   SET email = ?, email_verified_at = {verified_at},
                       email_verification_status = 'valid'
                 WHERE id = ?""",
            (email, lead["id"]))
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
    conn.commit()
    conn.close()

    res = om.leads_needing_verification(
        "storefront", max_age_days=0, skip_mv_days=0,
        skip_mv_attempted_tag=False, limit=100)
    assert {r["name"] for r in res["leads"]} == {"Old Val", "New Val"}


def test_collect_verify_emails_forwards_limit(monkeypatch, tmp_path):
    """The missing link: the limit has to survive the subprocess bridge."""
    import email_finder

    seen = {}

    def fake_candidates(om_dir, workspace, **kw):
        seen.update(kw)
        return {"status": "ok", "count": 0, "leads": []}

    monkeypatch.setattr(email_finder.cc, "run_verification_candidates", fake_candidates)
    monkeypatch.setattr(email_finder, "find_outreachmagic", lambda cfg: tmp_path)
    email_finder._collect_verify_emails(workspace="storefront", limit=0, cfg={})
    assert seen["limit"] == 0
