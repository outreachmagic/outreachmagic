"""Invariants for find-domains: properties that must hold for EVERY result,
checked over an adversarial corpus rather than one example at a time.

Written because four defects shipped past example-based tests -- each was
correct for the tidy data the fixtures used and wrong for the data the
production DB actually holds: aggregators on vertical subdomains, businesses
whose only contact is Gmail, a billing vendor's address on the page, and
company names that are 3 letters, ALL CAPS, or carry a ®.

The rule these encode: a wrong domain is worse than no domain, because
nothing downstream ever corrects companies.domain once it is set. So every
invariant below is one-sided -- it constrains what may be WRITTEN, and never
asserts that a particular company must resolve.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domain_discovery as dd  # noqa: E402
import enrich  # noqa: E402
import pipeline as om  # noqa: E402
from constants import SHARED_EMAIL_DOMAINS  # noqa: E402
from pipeline_utils import normalize_company_domain  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _result(links, snippet="", title="", kg=None):
    return {
        "organic": [
            {"link": l, "title": title or "A Company", "snippet": snippet}
            for l in links
        ],
        "knowledgeGraph": {"website": kg} if kg else {},
    }


# Each case is (company_name, serper_response). Deliberately hostile: the
# point is that NOTHING wrong may be written, not that these resolve.
CORPUS = [
    # Aggregators, including the vertical-subdomain shape that shipped a bug.
    ("Autumn Breeze Healthcare", _result(
        ["https://health.usnews.com/best/autumn-breeze"], "Autumn Breeze Healthcare reviews")),
    ("Dragon Con, Inc", _result(
        ["https://www.mapquest.com/us/ga/dragon-con"], "Dragon Con, Inc listing")),
    ("Some Startup", _result(
        ["https://www.linkedin.com/company/some-startup"], "Some Startup on LinkedIn")),
    ("Blog Co", _result(["https://blog.medium.com/blog-co"], "Blog Co writes here")),
    # Free providers as the only contact.
    ("Agape Senior Solutions", _result(
        ["https://directory.example.org/agape"], "Email agapeseniorsolutions@gmail.com.")),
    ("Tiny Shop", _result(["https://directory.example.org/tiny"], "Contact tinyshop@yahoo.com")),
    # An unrelated company's address on the page.
    ("Hightop Health", _result(
        ["https://directory.example.org/hightop"], "Billing via intake@psychatlanta.com.")),
    # Email-format example pages.
    ("StoneX Group Inc.", _result(
        ["https://rocketreach.co/stonex"], "Format is jane.doe@stonex.com and first.last@stonex.com")),
    # Awkward real-world names.
    ("greensky®", _result(["https://www.greensky.com/"], "GreenSky")),
    ("ATLANTA DOWNTOWN IMPROVEMENT DISTRI CT", _result(["https://www.atlantadowntown.com/"], "ADID")),
    ("COG", _result(["https://www.coghomes.com/"], "COG Homes")),
    ("MX", _result(["https://www.mx.com/"], "MX")),
    ("Clay & Co.", _result(["https://www.clay.com/"], "Clay")),
    # Nothing useful at all.
    ("Zzz Unrelated Holdings", _result(["https://carelistings.com/x"], "Zzz Unrelated Holdings listed")),
    ("Nothing Findable", _result([])),
    # A clean, unambiguous win -- the corpus must not be all-negative, or the
    # invariants would pass trivially on a build that attaches nothing.
    ("Modern Storefront LLC", _result(
        ["https://www.modernstorefront.com/contact"], "Reach info@modernstorefront.com")),
    ("Peachtree Hills Place", _result(
        ["https://www.peachtreehillsplace.com/"], "Peachtree Hills Place")),
]


def _run_corpus(conn):
    """Run every case; return the outcomes."""
    outcomes = []
    for i, (name, payload) in enumerate(CORPUS):
        cid = om.ensure_company(conn, name=name)
        lead = om.resolve_lead(name=f"Person {i}", source="csv",
                               allow_weak_identity=True, conn=conn)
        conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
        conn.commit()
        with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c, p=payload: p):
            out = dd.run_company_domain_discovery(
                conn, {}, company_id=cid, company_name=name, rep_lead_id=lead["id"])
        out["company_id"] = cid
        out["company_name"] = name
        outcomes.append(out)
        conn.commit()
    return outcomes


@pytest.fixture
def corpus_run():
    conn = om.get_conn()
    outcomes = _run_corpus(conn)
    yield conn, outcomes
    conn.close()


def _attached_domains(conn):
    return conn.execute(
        f"""SELECT ci.company_id, c.name, ci.identity_value_normalized AS domain
            FROM company_identities ci JOIN companies c ON c.id = ci.company_id
            WHERE ci.identity_type = 'domain'
              AND ci.source IN ({",".join("?" * len(dd.DISCOVERY_SOURCES))})""",
        dd.DISCOVERY_SOURCES,
    ).fetchall()


# ── Invariants on what may be WRITTEN ────────────────────────────────────────

def test_no_attached_domain_is_an_aggregator_or_its_subdomain(corpus_run):
    conn, _ = corpus_run
    for row in _attached_domains(conn):
        cleaned, warning = enrich.validate_company_domain(row["domain"], row["name"])
        assert cleaned, f"{row['name']} -> {row['domain']}: {warning}"


def test_no_attached_domain_is_a_free_email_provider(corpus_run):
    conn, _ = corpus_run
    for row in _attached_domains(conn):
        assert row["domain"] not in SHARED_EMAIL_DOMAINS, f"{row['name']} -> {row['domain']}"


def test_every_attached_domain_has_a_name_relation(corpus_run):
    """score 0 means nothing tied this domain to this company. That is how
    carelistings.com, psychatlanta.com and health.usnews.com all got in."""
    conn, _ = corpus_run
    for row in _attached_domains(conn):
        score, reason = dd.score_domain_match(row["name"], row["domain"])
        assert score > 0, f"{row['name']} -> {row['domain']} scored {score} ({reason})"


def test_every_attached_domain_is_stored_normalized(corpus_run):
    """Trailing dots and www. produce a second, duplicate identity for one
    real domain."""
    conn, _ = corpus_run
    for row in _attached_domains(conn):
        assert normalize_company_domain(row["domain"]) == row["domain"], row["domain"]


def test_nothing_below_the_confidence_floor_is_attached(corpus_run):
    conn, outcomes = corpus_run
    for out in outcomes:
        if out.get("confidence") is not None and out["confidence"] < dd.MIN_ATTACH_CONFIDENCE:
            assert out.get("attach", {}).get("attached") is not True, out


def test_every_stored_public_email_is_trustworthy(corpus_run):
    """placeholder (reaches nobody) and off_domain (someone else's company)
    may be recorded in the observation but never stored as an identity."""
    conn, _ = corpus_run
    rows = conn.execute(
        """SELECT c.name, c.domain, ci.identity_value_normalized AS email
           FROM company_identities ci JOIN companies c ON c.id = ci.company_id
           WHERE ci.identity_type = 'public_email'""").fetchall()
    for row in rows:
        assert dd.classify_public_email(row["email"], row["domain"]) in (
            "corporate", "free_provider"), f"{row['name']} -> {row['email']}"


def test_the_audit_reports_a_clean_corpus(corpus_run):
    """The audit command is the production safety net; if it disagrees with
    the invariants above, one of them is lying."""
    conn, _ = corpus_run
    report = dd.audit_attached_domains(conn)
    assert report["suspect"] == 0, report["findings"]


def test_corpus_is_not_trivially_clean(corpus_run):
    """Guards the guards: a build that attached nothing at all would satisfy
    every invariant above."""
    conn, outcomes = corpus_run
    assert len(_attached_domains(conn)) >= 3
    assert any(o["status"] == "found" for o in outcomes)


# ── Invariants on repeat runs ────────────────────────────────────────────────

def test_rerunning_the_corpus_changes_nothing(corpus_run):
    """Idempotence: a second pass must not duplicate identities, re-spend
    credits, or flip an answer."""
    conn, _ = corpus_run
    before = sorted((r["company_id"], r["domain"]) for r in _attached_domains(conn))
    n_candidates = conn.execute(
        "SELECT COUNT(*) c FROM company_merge_candidates").fetchone()["c"]

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not re-query")):
        for i, (name, _payload) in enumerate(CORPUS):
            cid = om.ensure_company(conn, name=name)
            lead = conn.execute(
                "SELECT id FROM leads WHERE company_id = ? ORDER BY id LIMIT 1", (cid,)).fetchone()
            if lead:
                dd.run_company_domain_discovery(
                    conn, {}, company_id=cid, company_name=name, rep_lead_id=lead["id"])
    conn.commit()

    assert sorted((r["company_id"], r["domain"]) for r in _attached_domains(conn)) == before
    assert conn.execute(
        "SELECT COUNT(*) c FROM company_merge_candidates").fetchone()["c"] == n_candidates


# ── Contacts follow the domain that was actually established ─────────────────

def test_contacts_are_not_stored_when_the_domain_went_to_a_duplicate_row():
    """Live regression: "Great Oaks Senior Living" ended up with five
    greatoaks.net contacts and no domain, because the duplicate row "Great
    Oaks Assisted Living" already owned that identity. Classifying against the
    merely top-ranked domain -- rather than the one that actually attached --
    stored contacts for a company row that owns nothing."""
    conn = om.get_conn()
    twin = om.ensure_company(conn, name="Great Oaks Assisted Living")
    conn.execute(
        """INSERT INTO company_identities
               (org_id, company_id, identity_type, identity_value_normalized, source)
           VALUES ('default', ?, 'domain', 'greatoaks.net', 'seed')""", (twin,))
    conn.commit()

    cid = om.ensure_company(conn, name="Great Oaks Senior Living")
    lead = om.resolve_lead(name="Go Poe", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()

    raw = _result(["https://www.greatoaks.net/"],
                  snippet="Great Oaks Senior Living. Contact marketing@greatoaks.net.",
                  title="Great Oaks Senior Living")
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: raw):
        out = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Great Oaks Senior Living",
            rep_lead_id=lead["id"])
    conn.commit()

    assert out["attach"]["attached"] is False
    assert out["attach"]["reason"] == "domain_owned_by_other_company"
    # No domain established for this row -> no contacts filed against it.
    assert conn.execute(
        """SELECT COUNT(*) c FROM company_identities
           WHERE company_id = ? AND identity_type = 'public_email'""", (cid,)).fetchone()["c"] == 0
    # ...and the duplicate is queued for a human instead of silently ignored.
    assert conn.execute(
        """SELECT COUNT(*) c FROM company_merge_candidates
           WHERE existing_company_id = ? AND candidate_company_id = ?""",
        (twin, cid)).fetchone()["c"] == 1
    conn.close()


def test_free_provider_contact_survives_even_with_no_domain():
    """The one address type that never depended on the domain: for many small
    businesses the Gmail address is the only published contact there is."""
    conn = om.get_conn()
    cid = om.ensure_company(conn, name="Zzz Unfindable Services")
    lead = om.resolve_lead(name="Zz Poe", source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()

    raw = _result(["https://carelistings.com/zzz"],
                  snippet="Zzz Unfindable Services -- email zzzunfindable@gmail.com.")
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: raw):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Zzz Unfindable Services",
            rep_lead_id=lead["id"])
    conn.commit()

    stored = conn.execute(
        """SELECT identity_value_normalized AS e, role FROM company_identities
           WHERE company_id = ? AND identity_type = 'public_email'""", (cid,)).fetchall()
    assert [(r["e"], r["role"]) for r in stored] == [("zzzunfindable@gmail.com", "free_provider")]
    conn.close()
