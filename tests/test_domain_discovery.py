"""domain_discovery.py: Serper-based company domain/email discovery
(find-domains). Covers the three things the feature exists for:

1. Query text never quotes the company name, and strips trailing legal-entity
   suffixes (LLC/Inc/Corp/...) -- a company registered as "X LLC" whose own
   site never says "LLC" must not lose the hit to an over-strict quoted
   phrase match.
2. A domain with a real attached email always outranks a same-or-higher
   string-matched domain with no email -- a company can have more than one
   domain, and the one email_finder should try first is the one with proven
   mail delivery, not just the one that looks like the website.
3. The targeted waterfall spends the minimum queries: 1 by default, a 2nd
   only when query 1 found a domain but no email, a 3rd only when query 1
   returned results that merely failed to name-match -- and a company already
   resolved (or already Serper-searched within the freshness window,
   regardless of which workspace triggered it) is never re-queried.
4. It survives the real world: no search operators (free Serper rejects
   them), no uncaught provider error taking down a batch, no free-provider
   domain becoming companies.domain, and no unbounded raw payload crossing
   the relay wire.
"""

import json
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


# ── Query construction ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("Modern Storefront LLC", "Modern Storefront"),
    ("Empire Care Centers, Inc.", "Empire Care Centers"),
    ("Acme Corp", "Acme"),
    ("Smith & Sons Ltd", "Smith & Sons"),
    ("Widgets Co.", "Widgets"),
    ("Acme", "Acme"),  # no suffix -- unchanged
])
def test_strip_entity_suffix(raw, expected):
    assert dd.strip_entity_suffix(raw) == expected


def test_build_discovery_query_never_quotes_the_company_name():
    query = dd.build_discovery_query("Modern Storefront LLC")
    assert query == "Modern Storefront email"
    assert '"' not in query


def test_build_discovery_query_alt_domain_mode():
    assert dd.build_discovery_query("Acme Corp", "alt_domain") == "Acme @"


# ── Extraction ───────────────────────────────────────────────────────────────

def _serper_result(company_name="Modern Storefront LLC", with_email=True, with_kg=False):
    organic = [{
        "link": "https://www.linkedin.com/company/modern-storefront",
        "title": f"{company_name} | LinkedIn",
        "snippet": f"{company_name} on LinkedIn",
    }]
    if with_email:
        organic.insert(0, {
            "link": "https://www.modernstorefront.com/contact",
            "title": f"{company_name} - Contact",
            "snippet": "Reach us at info@modernstorefront.com any time.",
        })
    else:
        organic.insert(0, {
            "link": "https://www.modernstorefront.com/about",
            "title": f"{company_name} - About",
            "snippet": "We build modern storefronts.",
        })
    result = {"organic": organic, "knowledgeGraph": {}}
    if with_kg:
        result["knowledgeGraph"] = {"website": "https://modernstorefront.com"}
    return result


def test_extract_domains_excludes_linkedin_and_other_aggregators():
    result = _serper_result()
    scored = dd.extract_domains(result, "Modern Storefront LLC")
    domains = {d["domain"] for d in scored}
    assert "linkedin.com" not in domains
    assert "modernstorefront.com" in domains


def test_extract_domains_prefers_knowledge_graph_website():
    result = _serper_result(with_kg=True)
    scored = dd.extract_domains(result, "Modern Storefront LLC")
    assert scored[0]["domain"] == "modernstorefront.com"
    assert scored[0]["score"] >= 20


def test_extract_emails_flags_role_addresses():
    result = _serper_result(with_email=True)
    emails = dd.extract_emails(result)
    assert emails[0]["email"] == "info@modernstorefront.com"
    assert emails[0]["is_role"] is True


def test_classify_domains_email_attached_breaks_a_tie():
    """An attached email decides between EQUALLY name-matched domains -- a
    company owns several (website vs email-sending vs per-branch) and the one
    with proven mail delivery is the better bet."""
    scored = [
        {"domain": "acmehq.com", "score": 12, "reason": "acronym"},
        {"domain": "acme-mail.net", "score": 12, "reason": "acronym"},
    ]
    emails = [{"email": "info@acme-mail.net", "is_role": True, "is_free_provider": False}]
    ranked = dd.classify_domains(scored, emails)
    assert ranked[0]["domain"] == "acme-mail.net"
    assert ranked[0]["has_email"] is True


def test_classify_domains_does_not_let_an_email_override_a_better_name_match():
    """Corrected contract. This previously asserted the opposite -- that any
    attached email outranks any name score -- which is how a score-0 address
    scraped off a page displaced the company's own domain. Measured across 213
    real observations, that ordering picked the wrong winner 48 times:
    psychatlanta.com (0) over hightophealth.com (34), email4pr.com (0) over
    precioushospice.com (34). Mail-delivery preference is preserved where it
    belongs -- on the identity's role='email' flag, which rank_company_domains()
    reads when the email waterfall picks what to try first."""
    scored = [
        {"domain": "acmehq.com", "score": 20, "reason": "exact"},
        {"domain": "acme-mail.net", "score": 3, "reason": "token_overlap_1"},
    ]
    emails = [{"email": "info@acme-mail.net", "is_role": True, "is_free_provider": False}]
    ranked = dd.classify_domains(scored, emails)
    assert ranked[0]["domain"] == "acmehq.com"
    assert ranked[1]["domain"] == "acme-mail.net"
    assert ranked[1]["has_email"] is True


def test_classify_domains_adds_email_only_domain_missed_by_organic_scoring():
    scored = [{"domain": "acmehq.com", "score": 20, "reason": "exact"}]
    emails = [{"email": "info@acme-mail.net", "is_role": True, "is_free_provider": False}]
    ranked = dd.classify_domains(scored, emails)
    domains = {d["domain"] for d in ranked}
    assert "acme-mail.net" in domains


def test_summarize_source_matches_legacy_notation():
    ranked = [{"domain": "a.com", "score": 5, "has_email": True}]
    emails = [{"email": "x@a.com", "is_role": False}]
    assert dd.summarize_source(emails, ranked) == "email+url_single (1e, 1u)"


def test_compute_confidence_prioritizes_has_email_over_score():
    with_email = [{"domain": "a.com", "score": 5, "has_email": True}]
    without_email = [{"domain": "b.com", "score": 20, "has_email": False}]
    assert dd.compute_confidence(with_email) > dd.compute_confidence(without_email)


# ── Orchestrator: run_company_domain_discovery ──────────────────────────────

@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()


def _company_with_lead(conn, name="Modern Storefront LLC", person="Jane Doe"):
    cid = om.ensure_company(conn, name=name)
    lead = om.resolve_lead(name=person, source="csv", allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    conn.commit()
    return cid, lead["id"]


def test_stops_after_one_query_when_domain_and_email_found():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    calls = []
    def fake_search(query, config):
        calls.append(query)
        return _serper_result(with_email=True)

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert len(calls) == 1
    assert outcome["status"] == "found"
    assert outcome["domain"] == "modernstorefront.com"

    identity = conn.execute(
        "SELECT role FROM company_identities WHERE company_id = ? AND identity_type = 'domain'", (cid,),
    ).fetchone()
    assert identity["role"] == "email"
    company = conn.execute("SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["domain"] == "modernstorefront.com"


def test_runs_targeted_second_query_only_when_domain_found_without_email():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    calls = []
    def fake_search(query, config):
        calls.append(query)
        if len(calls) > 1:
            return _serper_result(with_email=True)
        return _serper_result(with_email=False)

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert len(calls) == 2
    # Operator-free: `site:` is rejected outright by free Serper accounts.
    assert "site:" not in calls[1]
    assert "modernstorefront.com" in calls[1]
    assert outcome["status"] == "found"
    identity = conn.execute(
        "SELECT role FROM company_identities WHERE company_id = ? AND identity_type = 'domain'", (cid,),
    ).fetchone()
    assert identity["role"] == "email"


def test_third_query_fires_when_query_one_had_results_but_no_name_match():
    """Query 1 returned real results that simply didn't match by name -- query
    2 is targeted at a domain we don't have, so it's skipped; query 3
    (alt-domain, last resort) is the very next thing that runs."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    calls = []
    def fake_search(query, config):
        calls.append(query)
        return {
            "organic": [{
                "link": "https://www.linkedin.com/company/modern-storefront",
                "title": "Modern Storefront | LinkedIn",
                "snippet": "A company.",
            }],
            "knowledgeGraph": {},
        }

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert len(calls) == 2
    assert calls[0] == "Modern Storefront email"
    assert calls[1] == "Modern Storefront @"
    assert outcome["status"] == "not_found"


def test_zero_organic_results_spends_exactly_one_query():
    """A query that came back completely empty is a dead end, not a near miss:
    a second generic query buys nothing and, at thousands of companies, doubles
    the credit cost of every dead end."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    calls = []
    def fake_search(query, config):
        calls.append(query)
        return {"organic": [], "knowledgeGraph": {}}

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert calls == ["Modern Storefront email"]
    assert outcome["status"] == "not_found"


def test_second_run_is_served_from_cache_without_new_serper_calls():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    with mock.patch.object(enrich, "serper_search", return_value=_serper_result(with_email=True)):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not re-query")) as fake:
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
        fake.assert_not_called()
    # Served from the identity row the first run wrote, which is checked
    # before the observation cache. Either way the point is zero credits.
    assert outcome["status"] in ("resolved_from_db", "cached")


def test_force_bypasses_the_cache():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    with mock.patch.object(enrich, "serper_search", return_value=_serper_result(with_email=True)):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    calls = []
    def fake_search(query, config):
        calls.append(query)
        return _serper_result(with_email=True)

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id, force=True,
        )
    assert len(calls) == 1


def test_domain_owned_by_another_company_is_not_misattached():
    """Both companies name-match modernstorefront.com strongly enough to clear
    the confidence floor, so the ownership guard -- not the floor -- is what
    stops the second one."""
    conn = om.get_conn()
    cid_a, lead_a = _company_with_lead(conn, name="Modern Storefront LLC", person="Jane Doe")
    cid_b, lead_b = _company_with_lead(conn, name="Modern Storefront Group", person="John Smith")

    with mock.patch.object(enrich, "serper_search", return_value=_serper_result(with_email=True)):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid_a, company_name="Modern Storefront LLC", rep_lead_id=lead_a,
        )
    conn.commit()

    with mock.patch.object(enrich, "serper_search", return_value=_serper_result(with_email=True)):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid_b, company_name="Modern Storefront Group", rep_lead_id=lead_b,
        )
    conn.commit()

    assert outcome["attach"]["attached"] is False
    assert outcome["attach"]["reason"] == "domain_owned_by_other_company"
    company_b = conn.execute("SELECT domain FROM companies WHERE id = ?", (cid_b,)).fetchone()
    assert company_b["domain"] is None


def test_skips_non_company_names_without_spending_a_credit():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Self-Employed")

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not query")) as fake:
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Self-Employed", rep_lead_id=lead_id,
        )
        fake.assert_not_called()
    assert outcome["status"] == "skipped"


# ── Regression fixtures from the live storefront run ─────────────────────────
# Source: OM-DOMAIN-DISCOVERY-DEBUG-REPORT.md. These are the exact shapes that
# produced wrong answers in production; they exist so the specific failure
# cannot silently come back.

def _amada_serper():
    """Franchise/branch case: the LinkedIn company name carries a location
    suffix the brand domain doesn't share. The correct domain held organic
    slots 1-4 and 9; a directory site at slot 10 won anyway."""
    company = "Amada Senior Care North Atlanta"
    return {
        "organic": [
            {"link": "https://amadaseniorcare.com/marietta-senior-care/", "title": "Marietta Senior Care", "snippet": "Amada Senior Care of Marietta."},
            {"link": "https://amadaseniorcare.com/home-care-locations/", "title": "Locations", "snippet": "Find a location."},
            {"link": "https://amadaseniorcare.com/duluth-senior-care/", "title": "Duluth", "snippet": "Duluth senior care."},
            {"link": "https://amadaseniorcare.com/", "title": "Amada Senior Care", "snippet": "In-home care."},
            {"link": "https://www.aplaceformom.com/community/x", "title": "A Place For Mom", "snippet": f"{company} reviews."},
            {"link": "https://myseniorcarefinder.com/x", "title": "Finder", "snippet": f"{company} listing."},
            {"link": "https://www.mapquest.com/us/georgia/x", "title": "MapQuest", "snippet": f"{company} directions."},
            {"link": "https://healthfinder.fl.gov/x", "title": "HealthFinder", "snippet": "State registry."},
            {"link": "https://amadaseniorcare.com/atlanta-southwest/", "title": "Atlanta Southwest", "snippet": "Southwest Atlanta."},
            {"link": "https://carelistings.com/ga/atlanta/x", "title": "Care Listings", "snippet": f"{company} is listed here."},
        ],
        "knowledgeGraph": {},
    }


def _agape_serper():
    """The company's only published contact is a gmail address. gmail.com used
    to win the domain slot outright: email-derived candidates skipped
    validation, and has_email=True sorts ahead of score."""
    return {
        "organic": [
            {"link": "https://agapesenior.org/contact", "title": "Agape Senior Solutions - Contact",
             "snippet": "Email us at agapeseniorsolutions@gmail.com."},
            {"link": "https://agapesenior.org/", "title": "Agape Senior Solutions",
             "snippet": "Assisted living services."},
        ],
        "knowledgeGraph": {},
    }


def test_score_domain_match_handles_branch_suffixed_franchise_names():
    score, reason = dd.score_domain_match("Amada Senior Care North Atlanta", "amadaseniorcare.com")
    assert score >= 18, reason
    assert reason == "domain_is_name_prefix"


def test_amada_brand_domain_beats_directory_site():
    ranked = dd.extract_domains(_amada_serper(), "Amada Senior Care North Atlanta")
    assert ranked[0]["domain"] == "amadaseniorcare.com"
    assert "carelistings.com" not in {d["domain"] for d in ranked[:1]}


def test_score_domain_match_does_not_depend_on_stripped_generic_words():
    """normalize_company_name() strips senior/care/solutions/group -- words
    that are routinely part of the real brand AND the real domain. Scoring the
    normalized form alone gave the correct domain 0."""
    assert dd.score_domain_match("Amada Senior Care North Atlanta", "amadaseniorcare.com")[0] > \
        dd.score_domain_match("Amada Senior Care North Atlanta", "carelistings.com")[0]


def test_free_provider_email_never_becomes_the_company_domain():
    raw = _agape_serper()
    emails = dd.extract_emails(raw)
    ranked = dd.classify_domains(dd.extract_domains(raw, "Agape Senior Solutions"), emails)
    domains = {d["domain"] for d in ranked}
    assert "gmail.com" not in domains
    assert ranked[0]["domain"] == "agapesenior.org"
    # ...but the address itself is kept, flagged, and stored.
    assert any(e["email"] == "agapeseniorsolutions@gmail.com" and e["is_free_provider"] for e in emails)


def test_email_regex_does_not_swallow_sentence_ending_period():
    emails = dd.extract_emails({"organic": [
        {"link": "https://x.com", "title": "", "snippet": "Write to info@ahfohio.com."},
    ]})
    assert emails[0]["email"] == "info@ahfohio.com"


def test_normalize_company_domain_strips_trailing_dot():
    from pipeline_utils import normalize_company_domain
    assert normalize_company_domain("ahfohio.com.") == "ahfohio.com"
    assert normalize_company_domain("https://www.gmail.com./") == "gmail.com"


# ── Orchestrator: resilience, budget, storage ────────────────────────────────

def test_serper_error_on_second_query_does_not_crash_the_batch():
    """A free-tier account rejecting a query used to raise an uncaught
    ValueError that killed the whole run mid-batch."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    calls = []
    def fake_search(query, config):
        calls.append(query)
        if len(calls) > 1:
            raise ValueError('Serper HTTP 400: {"message":"Query pattern not allowed for free accounts."}')
        return _serper_result(with_email=False)

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert len(calls) == 2
    assert outcome["status"] == "found_no_email"
    assert outcome["domain"] == "modernstorefront.com"
    row = conn.execute(
        """SELECT status, metadata_json FROM lead_provider_observations
           WHERE lead_id = ? AND kind = 'domain_lookup' ORDER BY rowid DESC LIMIT 1""",
        (lead_id,),
    ).fetchone()
    assert row["status"] == "error"
    assert "400" in json.loads(row["metadata_json"])["error"]


def test_low_confidence_result_is_recorded_but_never_attached():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Zzz Unrelated Holdings")

    def fake_search(query, config):
        return {"organic": [
            {"link": "https://carelistings.com/x", "title": "Care Listings",
             "snippet": "Zzz Unrelated Holdings is listed."},
        ], "knowledgeGraph": {}}

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Zzz Unrelated Holdings", rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["status"] in ("low_confidence", "not_found")
    assert outcome.get("attach", {}).get("attached") is not True
    company = conn.execute("SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()
    assert not company["domain"]
    assert conn.execute(
        "SELECT COUNT(*) c FROM company_identities WHERE company_id = ? AND identity_type = 'domain'", (cid,),
    ).fetchone()["c"] == 0


def test_public_emails_land_as_company_identities():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Agape Senior Solutions")

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _agape_serper()):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Agape Senior Solutions", rep_lead_id=lead_id,
        )
    conn.commit()

    rows = conn.execute(
        """SELECT identity_value_normalized, role, label FROM company_identities
           WHERE company_id = ? AND identity_type = 'public_email'""",
        (cid,),
    ).fetchall()
    by_email = {r["identity_value_normalized"]: r for r in rows}
    assert "agapeseniorsolutions@gmail.com" in by_email
    assert by_email["agapeseniorsolutions@gmail.com"]["role"] == "free_provider"
    assert by_email["agapeseniorsolutions@gmail.com"]["label"] == "https://agapesenior.org/contact"


def test_raw_serper_is_omitted_by_default_and_present_with_debug():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _serper_result()):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()
    lean = json.loads(conn.execute(
        "SELECT metadata_json FROM lead_provider_observations WHERE lead_id = ? ORDER BY rowid DESC LIMIT 1",
        (lead_id,),
    ).fetchone()["metadata_json"])
    assert "raw_serper" not in lean
    # The summary still carries everything needed to explain the pick.
    assert lean["ranked_domains"][0]["reason"]
    assert lean["top_links"] and len(lean["top_links"]) <= 3

    # A distinct company: the same one would be an org-wide cache hit and
    # never re-query (which is the intended credit discipline, tested above).
    cid2, lead2 = _company_with_lead(conn, name="Modern Storefront Two LLC", person="John Roe")
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _serper_result()):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid2, company_name="Modern Storefront Two LLC", rep_lead_id=lead2,
            debug=True,
        )
    conn.commit()
    debugged = json.loads(conn.execute(
        "SELECT metadata_json FROM lead_provider_observations WHERE lead_id = ? ORDER BY rowid DESC LIMIT 1",
        (lead2,),
    ).fetchone()["metadata_json"])
    assert "raw_serper" in debugged


def test_query_budget_stops_the_second_query():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    calls = []
    def fake_search(query, config):
        calls.append(query)
        return _serper_result(with_email=False)

    with mock.patch.object(enrich, "serper_search", side_effect=fake_search):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
            query_budget=1,
        )
    conn.commit()

    assert len(calls) == 1
    assert outcome["status"] == "found_no_email"


def test_every_query_gets_its_own_observation_row():
    """compute_obs_uid() hashes the content columns, and metadata_json is not
    one of them -- two queries for the same company returning the same domain
    in the same wall-clock second used to hash identically, so the second
    INSERT silently no-op'd and the log undercounted credits actually spent."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn)

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _serper_result(with_email=False)):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert len(outcome["queries_run"]) == 2
    rows = conn.execute(
        """SELECT source_detail FROM lead_provider_observations
           WHERE lead_id = ? AND kind = 'domain_lookup'""",
        (lead_id,),
    ).fetchall()
    assert len(rows) == 2, "one observation per Serper query actually spent"
    assert {r["source_detail"].split()[0] for r in rows} == {"q1", "q2"}


def test_amada_full_waterfall_picks_the_brand_domain():
    """End-to-end on the exact fixture that stored carelistings.com."""
    conn = om.get_conn()
    name = "Amada Senior Care North Atlanta"
    cid, lead_id = _company_with_lead(conn, name=name, person="Pat Roe")

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _amada_serper()):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name=name, rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["domain"] == "amadaseniorcare.com"
    company = conn.execute("SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()
    assert company["domain"] == "amadaseniorcare.com"


def test_domain_already_primary_for_another_company_does_not_crash():
    """companies.domain is UNIQUE, and companies can carry a primary domain
    with no company_identities row (imported/legacy), so the identity
    ownership check does not cover this. The bare UPDATE raised
    sqlite3.IntegrityError and killed the whole batch on the first duplicate."""
    conn = om.get_conn()
    other = om.ensure_company(conn, name="Some Other Co")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("modernstorefront.com", other))
    conn.commit()

    cid, lead_id = _company_with_lead(conn)
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _serper_result()):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["status"] == "found"
    attach = outcome["attach"]
    assert attach["attached"] is True
    assert attach["primary_backfilled"] is False
    assert attach["other_company_id"] == other
    # The discovery is still recorded as an identity, just not as the primary.
    assert conn.execute(
        """SELECT COUNT(*) c FROM company_identities
           WHERE company_id = ? AND identity_type = 'domain'""", (cid,),
    ).fetchone()["c"] == 1


def test_public_emails_round_trip_the_company_sync_payload():
    """company_identities rows are only emitted by build_company_sync_payload,
    which filters identity_type='domain' -- public_email rows would otherwise
    exist solely on the machine that found them and be lost on a fresh pull."""
    from pipeline_personalize import apply_agent_company_sync_payload, build_company_sync_payload

    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Agape Senior Solutions", person="Ann Poe")
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _agape_serper()):
        dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Agape Senior Solutions", rep_lead_id=lead_id,
        )
    conn.commit()

    payload = build_company_sync_payload(conn, cid)
    emails = {e["email"]: e for e in payload.get("public_emails") or []}
    assert "agapeseniorsolutions@gmail.com" in emails
    assert emails["agapeseniorsolutions@gmail.com"]["role"] == "free_provider"

    # Replay onto a company with no identities -- simulates a fresh install
    # pulling this snapshot down.
    conn.execute(
        "DELETE FROM company_identities WHERE company_id = ? AND identity_type = 'public_email'", (cid,))
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) c FROM company_identities WHERE company_id=? AND identity_type='public_email'",
        (cid,)).fetchone()["c"] == 0

    apply_agent_company_sync_payload(cid, payload, conn=conn)
    conn.commit()
    back = conn.execute(
        """SELECT identity_value_normalized, role, label FROM company_identities
           WHERE company_id = ? AND identity_type = 'public_email'""", (cid,)).fetchall()
    restored = {r["identity_value_normalized"]: r for r in back}
    assert "agapeseniorsolutions@gmail.com" in restored
    assert restored["agapeseniorsolutions@gmail.com"]["role"] == "free_provider"
    assert restored["agapeseniorsolutions@gmail.com"]["label"] == "https://agapesenior.org/contact"


# ── Public-email quality ─────────────────────────────────────────────────────

@pytest.mark.parametrize("email", [
    "jane.doe@stonex.com", "john.doe@coxinc.com", "first.last@coxinc.com",
    "jdoe@fusionacademy.com", "flast@alloyllc.com", "fl@alloyllc.com",
    "mi@stonex.com", "firstname.lastname@acme.io", "someone@example.com",
    "info@yourcompany.com",
])
def test_email_format_examples_are_rejected(email):
    """Serper surfaces 'what is <company>'s email format?' pages; those spell
    the pattern out with stand-in names that look like real addresses."""
    assert dd.is_placeholder_email(email) is True


@pytest.mark.parametrize("email", [
    "info@rockco.com", "service@greensky.com", "board@stonex.com",
    "jgladden@fusionacademy.com", "customersupport@alloyapparel.com",
])
def test_real_addresses_are_not_rejected(email):
    assert dd.is_placeholder_email(email) is False


def test_classify_public_email_separates_on_domain_from_someone_elses():
    assert dd.classify_public_email("info@coxenterprises.com", "coxenterprises.com") == "corporate"
    # Surfaced under Cox Enterprises but belongs to an unrelated company.
    assert dd.classify_public_email("customer.service@mercer.com", "coxenterprises.com") == "off_domain"
    assert dd.classify_public_email("agapeseniorsolutions@gmail.com", "agapesenior.org") == "free_provider"
    assert dd.classify_public_email("jane.doe@coxenterprises.com", "coxenterprises.com") == "placeholder"
    # Subdomain/registrable-domain equivalence still counts as corporate.
    assert dd.classify_public_email("info@mail.coxenterprises.com", "coxenterprises.com") == "corporate"


def test_truncated_scrape_artifact_is_dropped():
    emails = [
        {"email": "accessibility@greensky.com"},
        {"email": "ccessibility@greensky.com"},
        {"email": "service@greensky.com"},
    ]
    kept = {e["email"] for e in dd.drop_truncated_duplicates(emails)}
    assert kept == {"accessibility@greensky.com", "service@greensky.com"}


def test_only_trustworthy_emails_become_identities():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Cox Enterprises", person="Cid Poe")
    raw = {"organic": [{
        "link": "https://coxenterprises.com/contact",
        "title": "Cox Enterprises - Contact",
        "snippet": ("Reach media@coxenterprises.com. Format is jane.doe@coxenterprises.com. "
                    "Benefits via customer.service@mercer.com."),
    }], "knowledgeGraph": {}}

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: raw):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Cox Enterprises", rep_lead_id=lead_id,
        )
    conn.commit()

    stored = {r["identity_value_normalized"] for r in conn.execute(
        "SELECT identity_value_normalized FROM company_identities WHERE company_id=? AND identity_type='public_email'",
        (cid,)).fetchall()}
    assert stored == {"media@coxenterprises.com"}
    # The rejected ones are still recorded in the observation for reference.
    md = json.loads(conn.execute(
        "SELECT metadata_json FROM lead_provider_observations WHERE lead_id=? ORDER BY rowid DESC LIMIT 1",
        (lead_id,)).fetchone()["metadata_json"])
    found = {e["email"] for e in md["public_emails"]}
    assert "customer.service@mercer.com" in found
    assert "jane.doe@coxenterprises.com" in found
    assert outcome["status"] in ("found", "found_no_email")


# ── Free answers before paid ones ────────────────────────────────────────────

def test_lead_email_domains_are_not_an_evidence_source():
    """Deliberately unused. A lead's email domain says where they work NOW,
    not which company row they are attached to -- measured 50/50 wrong on real
    data. Even a strict name check does not rescue it: normalize_company_name
    strips Partners/Group/Company, so "Regent Partners" -> regent.edu and
    "Artisan Partners" -> artisan.co both pass and are both wrong."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Widgets Industrial", person="Dee Poe")
    conn.execute(
        "UPDATE leads SET email = ?, email_domain = ? WHERE id = ?",
        ("dee@widgetsindustrial.com", "widgetsindustrial.com", lead_id))
    conn.commit()
    assert dd.domain_from_local_evidence(conn, cid, "Widgets Industrial") is None


def test_duplicate_company_row_resolves_without_a_credit():
    conn = om.get_conn()
    twin = om.ensure_company(conn, name="Widgets Industrial, Inc.")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("widgetsindustrial.com", twin))
    conn.commit()
    cid, lead_id = _company_with_lead(conn, name="Widgets Industrial", person="Fay Poe")

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not query")):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Widgets Industrial", rep_lead_id=lead_id,
        )
    conn.commit()
    assert outcome["status"] == "resolved_from_db"
    assert outcome["domain"] == "widgetsindustrial.com"
    assert outcome["evidence"].startswith("duplicate_company")


def test_unrelated_company_name_does_not_resolve_from_a_duplicate():
    conn = om.get_conn()
    other = om.ensure_company(conn, name="Something Else Entirely")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("somethingelse.com", other))
    conn.commit()
    cid, _ = _company_with_lead(conn, name="Widgets Industrial", person="Gil Poe")
    assert dd.domain_from_local_evidence(conn, cid, "Widgets Industrial") is None


def test_exhausted_budget_still_allows_free_resolution():
    """A spent query budget must not block a resolution that costs nothing --
    the budget caps Serper spend, not the DB."""
    conn = om.get_conn()
    twin = om.ensure_company(conn, name="Widgets Industrial Inc")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("widgetsindustrial.com", twin))
    conn.commit()
    cid, lead_id = _company_with_lead(conn, name="Widgets Industrial", person="Hal Poe")

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not query")):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Widgets Industrial",
            rep_lead_id=lead_id, query_budget=0,
        )
    conn.commit()
    assert outcome["status"] == "resolved_from_db"
    assert outcome["domain"] == "widgetsindustrial.com"


def test_exhausted_budget_reports_instead_of_querying():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Nothing Known Co", person="Ivy Poe")
    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not query")):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Nothing Known Co",
            rep_lead_id=lead_id, query_budget=0,
        )
    assert outcome["status"] == "budget_exhausted"
    assert outcome["queries_run"] == []


@pytest.mark.parametrize("company, lead_domain", [
    ("Dragon Con, Inc", "trellahealth.com"),
    ("Berkeley Partners", "berkeleycollege.edu"),
    ("CARROLL", "cc.edu"),
    ("Cedar Ridge Services, LLC", "crdistillery.com"),
    ("The Woodruff Arts Center", "deloitte.com"),
])
def test_lead_email_domain_unrelated_to_company_name_is_rejected(company, lead_domain):
    """A lead's email domain says where they work NOW, not which company row
    they are attached to. Unguarded, this was wrong 50/50 on real data."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name=company, person=f"P {lead_domain}")
    conn.execute(
        "UPDATE leads SET email = ?, email_domain = ? WHERE id = ?",
        (f"p@{lead_domain}", lead_domain, lead_id))
    conn.commit()
    assert dd.domain_from_local_evidence(conn, cid, company) is None


@pytest.mark.parametrize("name_a, name_b, same", [
    ("Acme Widgets, Inc.", "Acme Widgets", True),      # entity suffix only
    ("Acme Widgets LLC", "Acme Widgets", True),
    ("Sterling Group", "Sterling Partners", False),    # generic word IS the difference
    ("Regent Partners", "Regent University", False),
    ("Hanover Company", "Hanover College", False),
    ("Berkeley Partners", "Berkeley College", False),
])
def test_duplicate_name_key_keeps_distinct_companies_apart(name_a, name_b, same):
    """normalize_company_name() strips Partners/Group/Company, collapsing
    genuinely different companies onto one key -- fine for search ranking,
    wrong for deciding two rows are the same company."""
    assert (dd.duplicate_name_key(name_a) == dd.duplicate_name_key(name_b)) is same


def test_duplicate_company_lookup_does_not_confuse_similar_names():
    conn = om.get_conn()
    other = om.ensure_company(conn, name="Sterling Partners")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("sterlingpartners.com", other))
    conn.commit()
    cid, _ = _company_with_lead(conn, name="Sterling Group", person="Stu Poe")
    assert dd.domain_from_local_evidence(conn, cid, "Sterling Group") is None


def test_shared_domain_is_queued_for_human_merge_review():
    """Two company rows resolving to one domain are one company recorded
    twice. Research consensus (and this codebase's own merge-review queue) is
    to route that to a human, never to auto-merge or guess."""
    conn = om.get_conn()
    other = om.ensure_company(conn, name="Some Other Co")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("modernstorefront.com", other))
    conn.commit()

    cid, lead_id = _company_with_lead(conn)
    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: _serper_result()):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Modern Storefront LLC", rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["attach"]["merge_candidate_logged"] is True
    row = conn.execute(
        """SELECT existing_company_id, candidate_company_id, reason, status, payload_json
           FROM company_merge_candidates WHERE reason = 'domain_discovery_shared_domain'""",
    ).fetchone()
    assert row is not None
    assert row["existing_company_id"] == other
    assert row["candidate_company_id"] == cid
    assert row["status"] == "pending"          # never auto-merged
    assert json.loads(row["payload_json"])["domain"] == "modernstorefront.com"


def test_duplicate_name_resolution_is_also_queued_for_review():
    conn = om.get_conn()
    twin = om.ensure_company(conn, name="Widgets Industrial Inc")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("widgetsindustrial.com", twin))
    conn.commit()
    cid, lead_id = _company_with_lead(conn, name="Widgets Industrial", person="Jo Poe")

    with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not query")):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Widgets Industrial", rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["status"] == "resolved_from_db"
    row = conn.execute(
        """SELECT existing_company_id, status FROM company_merge_candidates
           WHERE reason = 'domain_discovery_duplicate_name'""").fetchone()
    assert row is not None and row["existing_company_id"] == twin
    assert row["status"] == "pending"


def test_twin_row_with_a_wrong_domain_is_not_propagated():
    """The duplicate row's own domain can be wrong -- this DB contains a
    company named "Dragon Con" carrying domain trellahealth.com. Name-key
    agreement alone would faithfully copy that error onto every namesake."""
    conn = om.get_conn()
    twin = om.ensure_company(conn, name="Dragon Con")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("trellahealth.com", twin))
    conn.commit()
    cid, _ = _company_with_lead(conn, name="Dragon Con, Inc", person="Kim Poe")

    # Same duplicate_name_key, so the row IS found -- and rejected on the domain.
    assert dd.duplicate_name_key("Dragon Con, Inc") == dd.duplicate_name_key("Dragon Con")
    assert dd.domain_from_local_evidence(conn, cid, "Dragon Con, Inc") is None


def test_merge_candidate_is_queued_once_per_pair():
    """Both code paths can see the same pair in one company, and every later
    run sees it again. Unchecked, a 3,000-company pass buries the review queue
    in thousands of rows describing a few hundred real merges."""
    conn = om.get_conn()
    twin = om.ensure_company(conn, name="Widgets Industrial Inc")
    conn.execute("UPDATE companies SET domain = ? WHERE id = ?", ("widgetsindustrial.com", twin))
    conn.commit()
    cid, lead_id = _company_with_lead(conn, name="Widgets Industrial", person="Lee Poe")

    for _ in range(3):
        with mock.patch.object(enrich, "serper_search", side_effect=AssertionError("must not query")):
            dd.run_company_domain_discovery(
                conn, {}, company_id=cid, company_name="Widgets Industrial", rep_lead_id=lead_id,
            )
        conn.commit()

    n = conn.execute(
        """SELECT COUNT(*) c FROM company_merge_candidates
           WHERE existing_company_id = ? AND candidate_company_id = ?""", (twin, cid)).fetchone()["c"]
    assert n == 1


def test_email_derived_domain_unrelated_to_the_company_is_not_attached():
    """Live regression: "Hightop Health" was assigned psychatlanta.com purely
    because an address on that domain appeared in a snippet. An attached email
    proves a domain receives mail, not whose domain it is."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Hightop Health", person="Hi Poe")
    raw = {"organic": [{
        "link": "https://directory.example.org/listing/hightop",
        "title": "Hightop Health - Provider Directory",
        "snippet": "Hightop Health. Billing handled by intake@psychatlanta.com.",
    }], "knowledgeGraph": {}}

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: raw):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Hightop Health", rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["status"] == "low_confidence"
    assert conn.execute(
        "SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()["domain"] is None
    assert conn.execute(
        """SELECT COUNT(*) c FROM company_identities
           WHERE company_id = ? AND identity_type = 'domain'""", (cid,)).fetchone()["c"] == 0


def test_email_derived_domain_matching_the_company_name_still_wins():
    """The guard must not cost us the good case: SANZIE HEALTHCARE SERVICES
    resolved via an address on sanziehealthcareservices.com, a domain that
    never appeared as an organic link."""
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Sanzie Healthcare Services, Inc.", person="Sa Poe")
    raw = {"organic": [{
        "link": "https://directory.example.org/listing/sanzie",
        "title": "Sanzie Healthcare Services - Directory",
        "snippet": "Contact info@sanziehealthcareservices.com for details.",
    }], "knowledgeGraph": {}}

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: raw):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Sanzie Healthcare Services, Inc.",
            rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["status"] == "found"
    assert outcome["domain"] == "sanziehealthcareservices.com"


# ── Subdomains ───────────────────────────────────────────────────────────────
# Live regression: "Autumn Breeze Healthcare" was assigned health.usnews.com.
# Two independent failures had to line up -- the aggregator list matched only
# exact hosts, and scoring used the leftmost label ("health"), a substring of
# "autumn breeze healthcare".

@pytest.mark.parametrize("host", [
    "health.usnews.com", "money.usnews.com", "www.health.usnews.com",
    "jobs.linkedin.com", "blog.medium.com", "business.yelp.com",
])
def test_aggregator_subdomains_are_rejected(host):
    cleaned, warning = enrich.validate_company_domain(host, "Some Company")
    assert cleaned == "", warning
    assert "ggregator" in warning


def test_scoring_uses_the_registrable_label_not_the_leftmost():
    assert dd.score_domain_match("Autumn Breeze Healthcare", "health.usnews.com")[0] == 0
    assert dd.score_domain_match("Some Health Co", "health.example-directory.com")[0] == 0
    # A real company's own subdomain still scores on its brand.
    assert dd.score_domain_match("Acme Widgets", "mail.acmewidgets.com")[1] == "exact"


def test_aggregator_subdomain_never_becomes_the_company_domain():
    conn = om.get_conn()
    cid, lead_id = _company_with_lead(conn, name="Autumn Breeze Healthcare", person="Au Poe")
    raw = {"organic": [{
        "link": "https://health.usnews.com/best-senior-living/autumn-breeze",
        "title": "Autumn Breeze Healthcare | US News",
        "snippet": "Autumn Breeze Healthcare ratings and reviews.",
    }], "knowledgeGraph": {}}

    with mock.patch.object(enrich, "serper_search", side_effect=lambda q, c: raw):
        outcome = dd.run_company_domain_discovery(
            conn, {}, company_id=cid, company_name="Autumn Breeze Healthcare", rep_lead_id=lead_id,
        )
    conn.commit()

    assert outcome["domain"] is None
    assert conn.execute(
        "SELECT domain FROM companies WHERE id = ?", (cid,)).fetchone()["domain"] is None


# ── Recall patterns found by measuring 213 real observations ─────────────────
# Synthetic names, real shapes: the repo is public, so no client company names
# live here. What is under test is the matching pattern, not the customer.

@pytest.mark.parametrize("name, domain, reason", [
    # Long descriptive facility names whose domain keeps a subset of the words.
    ("Fairview Center for Nursing and Healing", "fairviewnursing.com", "word_subset"),
    ("Northgate Center For Nursing and Healing", "northgatenursing.com", "word_subset"),
    ("Harbor Estates Senior Living", "harborestatesliving.com", "word_subset"),
    # Acronym plus a suffix the registered name never shows.
    ("Village Park Senior Living, LLC", "vpsl.com", "acronym"),
    ("Premier Senior Living", "pslgroupllc.com", "acronym_prefix"),
    ("Refrigerated Warehousing Inc", "rwizero.com", "acronym_prefix"),
])
def test_recall_patterns_from_real_data(name, domain, reason):
    score, got = dd.score_domain_match(name, domain)
    assert got == reason, f"{name} -> {domain} gave {got}"
    assert score > 0


@pytest.mark.parametrize("name, domain", [
    # A single generic word must never trigger word_subset, or it would match
    # half of any senior-living dataset.
    ("Harbor Estates Senior Living", "livingmagazine.com"),
    ("Summit Health Partners", "health.com"),
    ("Grove Nursing Center", "nursing-directory.net"),
    # Two-letter acronyms collide with far too much.
    ("Modern Storefront", "ms.com"),
])
def test_recall_patterns_do_not_over_match(name, domain):
    score, reason = dd.score_domain_match(name, domain)
    assert reason not in ("word_subset", "acronym", "acronym_prefix"), f"{name} -> {domain} ({reason})"
