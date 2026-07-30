"""The agent-delegated extraction contract: the pending queue, and applying a
batch of extracted contacts.

The property this phase exists for is idempotency. 676 staff pages surface the
same person on several pages and across dealer groups, so "applied twice" is
the normal case -- and a duplicate lead is not something a later run corrects.

Fixtures are synthetic; the layouts are real.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import contact_icp  # noqa: E402
import contact_review  # noqa: E402
import pipeline as om  # noqa: E402

STAFF_PAGE = """
## Meet Our Team

### Dana Whitfield

#### General Manager

[Email Me](mailto:dwhitfield@example.test)
"""

# A page the regex pass cannot crack: the names are inside a prose blob, which
# is exactly the tail the agent is spent on.
OPAQUE_PAGE = """
## Our Team

Our leadership team has served the region for thirty years and is led today by
a general manager who joined in 2004, supported by a service director and a
parts manager who between them have four decades on the drive.
"""


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()
    om.create_workspace("Storefront", slug="storefront")


def _company(name="Modern Storefront", domain="modernstorefront.test", page=None,
             ws="storefront"):
    """A company, optionally with a cached staff page.

    Caching a page also records the fetch observation, because that is what
    `find-contacts` does and it is what ties the company to the workspace that
    paid for the fetch -- a sourced company has no lead there yet.
    """
    conn = om.get_conn()
    try:
        cid = om.ensure_company(conn, name=name, domain=domain)
        if page is not None:
            conn.execute(
                """INSERT INTO company_page_cache
                       (company_id, url, url_hash, http_status, content_hash,
                        char_count, markdown)
                   VALUES (?, ?, ?, 200, ?, ?, ?)""",
                (cid, f"https://{domain}/staff", f"hash-{cid}",
                 f"content-{cid}", len(page), page),
            )
            if ws:
                ws_row = om.resolve_workspace_identity(conn, ws)
                contact_review.record_observation(
                    conn, cid, outcome="fetched", workspace_id=ws_row["id"],
                    url=f"https://{domain}/staff", extractor="regex")
        conn.commit()
        return cid
    finally:
        conn.close()


def _ws_id(slug="storefront"):
    conn = om.get_conn()
    try:
        return om.resolve_workspace_identity(conn, slug)["id"]
    finally:
        conn.close()


def _lead_count(name=None):
    conn = om.get_conn()
    try:
        if name:
            return conn.execute(
                "SELECT COUNT(*) c FROM leads WHERE lower(name) = ?", (name.lower(),)
            ).fetchone()["c"]
        return conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    finally:
        conn.close()


def _batch(cid, contacts):
    return [{"company_id": cid, "contacts": contacts}]


# ── the gate: applying the same batch twice ──────────────────────────────────

def test_applying_the_same_batch_twice_creates_no_duplicate_leads():
    cid = _company()
    batch = _batch(cid, [
        {"name": "Dana Whitfield", "title": "General Manager"},
        {"name": "Marco Bell", "title": "Service Director"},
    ])
    first = contact_review.apply_batch(batch, workspace="storefront")
    second = contact_review.apply_batch(batch, workspace="storefront")

    assert first["attached"] == 2
    assert second["attached"] == 2, "the second run still reports what it matched"
    assert _lead_count() == 2, "but it must not have created anything"
    assert [c["created"] for c in first["results"][0]["contacts"]] == [True, True]
    assert [c["created"] for c in second["results"][0]["contacts"]] == [False, False]


def test_a_changed_title_matches_the_same_lead():
    """The specific trap: build_import_identities keys on name|domain|title when
    a title is present, so a title scraped one word differently on the next run
    would mint a second lead for the same person."""
    cid = _company()
    contact_review.apply_batch(
        _batch(cid, [{"name": "Dana Whitfield", "title": "Sales Manager"}]))
    contact_review.apply_batch(
        _batch(cid, [{"name": "Dana Whitfield", "title": "Sales Manager, Bilingual"}]))
    assert _lead_count("Dana Whitfield") == 1


def test_the_same_person_at_two_companies_is_two_leads():
    """Idempotency must not collapse people across companies -- the key is the
    person *at a company*, not the name."""
    a = _company("Alpha Motors", "alpha.test")
    b = _company("Beta Motors", "beta.test")
    contact_review.apply_batch(_batch(a, [{"name": "Dana Whitfield", "title": "General Manager"}]))
    contact_review.apply_batch(_batch(b, [{"name": "Dana Whitfield", "title": "General Manager"}]))
    assert _lead_count("Dana Whitfield") == 2


def test_a_page_listing_one_person_twice_attaches_them_once():
    cid = _company()
    result = contact_review.apply_batch(_batch(cid, [
        {"name": "Dana Whitfield", "title": "General Manager"},
        {"name": "Dana Whitfield", "title": "Sales Manager"},
    ]))
    assert result["attached"] == 1
    assert result["results"][0]["rejections"][0]["reason"] == "duplicate_in_batch"
    assert _lead_count("Dana Whitfield") == 1


def test_an_email_found_later_still_resolves_to_the_same_lead():
    cid = _company()
    contact_review.apply_batch(_batch(cid, [{"name": "Dana Whitfield", "title": "General Manager"}]))
    contact_review.apply_batch(_batch(cid, [
        {"name": "Dana Whitfield", "title": "General Manager",
         "email": "dwhitfield@modernstorefront.test"},
    ]))
    assert _lead_count("Dana Whitfield") == 1


# ── what applying does ───────────────────────────────────────────────────────

def test_contacts_are_linked_to_the_company_and_the_workspace():
    cid = _company()
    contact_review.apply_batch(
        _batch(cid, [{"name": "Dana Whitfield", "title": "General Manager"}]),
        workspace="storefront")
    conn = om.get_conn()
    try:
        lead = conn.execute("SELECT id, company_id, title FROM leads").fetchone()
        assert lead["company_id"] == cid
        assert lead["title"] == "General Manager"
        assert conn.execute(
            "SELECT COUNT(*) c FROM workspace_leads WHERE lead_id = ? AND workspace_id = ?",
            (lead["id"], _ws_id())).fetchone()["c"] == 1
    finally:
        conn.close()


def test_a_phone_is_attached_and_a_junk_one_does_not_lose_the_contact():
    cid = _company()
    result = contact_review.apply_batch(_batch(cid, [
        {"name": "Dana Whitfield", "title": "General Manager", "phone": "555-201-4400"},
        {"name": "Marco Bell", "title": "Service Director", "phone": "ext. 12"},
    ]))
    assert result["attached"] == 2
    conn = om.get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM phone_numbers WHERE owner_type = 'lead'"
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_a_contact_with_no_name_is_rejected_not_created():
    cid = _company()
    result = contact_review.apply_batch(_batch(cid, [{"title": "General Manager"}]))
    assert result["attached"] == 0
    assert result["results"][0]["rejections"][0]["reason"] == "no_name"
    assert _lead_count() == 0


def test_an_unknown_company_fails_the_whole_batch():
    """All-or-nothing: a subagent posting twenty companies and getting back
    "some of it worked" leaves nobody able to say which half."""
    cid = _company()
    result = contact_review.apply_batch([
        {"company_id": cid, "contacts": [{"name": "Dana Whitfield", "title": "GM"}]},
        {"company_id": 999999, "contacts": [{"name": "Marco Bell", "title": "GM"}]},
    ])
    assert result["status"] == "error"
    assert _lead_count() == 0, "the good half must roll back too"


def test_dry_run_writes_nothing():
    cid = _company()
    result = contact_review.apply_batch(
        _batch(cid, [{"name": "Dana Whitfield", "title": "General Manager"}]),
        dry_run=True)
    assert result["status"] == "dry_run"
    assert result["attached"] == 1
    assert _lead_count() == 0


def test_an_unknown_workspace_is_an_error():
    cid = _company()
    result = contact_review.apply_batch(_batch(cid, [{"name": "Dana Whitfield"}]),
                                        workspace="nope")
    assert result["status"] == "error"


# ── the ICP guard ────────────────────────────────────────────────────────────

def test_the_blocklist_is_enforced_even_though_the_agent_had_it():
    """A rule that says "never contact this person" should not depend on a
    model having honoured it."""
    contact_icp.cli_set("storefront", "ops",
                        whitelist="general manager",
                        blocklist="assistant general manager")
    cid = _company()
    result = contact_review.apply_batch(_batch(cid, [
        {"name": "Dana Whitfield", "title": "General Manager"},
        {"name": "Theo Brandt", "title": "Assistant General Manager"},
    ]), workspace="storefront", icp_name="ops")
    assert result["attached"] == 1
    assert result["results"][0]["rejections"][0]["reason"] == "blocklist"
    assert _lead_count("Theo Brandt") == 0


def test_an_off_whitelist_title_is_attached_but_recorded_as_such():
    """The whitelist is not enforced here: an adjacent title the agent judged
    worth keeping is the judgement the agent was asked for."""
    contact_icp.cli_set("storefront", "ops", whitelist="general manager")
    cid = _company()
    result = contact_review.apply_batch(
        _batch(cid, [{"name": "Ines Fournier", "title": "Sales Consultant"}]),
        workspace="storefront", icp_name="ops")
    assert result["attached"] == 1
    assert result["results"][0]["contacts"][0]["icp"] == "not_in_whitelist"


def test_a_missing_named_icp_profile_is_an_error():
    cid = _company()
    result = contact_review.apply_batch(_batch(cid, [{"name": "Dana Whitfield"}]),
                                        workspace="storefront", icp_name="absent")
    assert result["status"] == "error"


# ── observations ─────────────────────────────────────────────────────────────

def test_applying_records_an_observation_stamped_with_the_icp_version():
    profile = contact_icp.cli_set("storefront", "ops", whitelist="general manager")
    cid = _company()
    contact_review.apply_batch(
        _batch(cid, [{"name": "Dana Whitfield", "title": "General Manager"}]),
        workspace="storefront", icp_name="ops")
    conn = om.get_conn()
    try:
        obs = conn.execute(
            "SELECT * FROM company_contact_observations WHERE company_id = ?", (cid,)
        ).fetchone()
        assert obs["outcome"] == "applied"
        assert obs["extractor"] == "agent"
        assert obs["contacts_attached"] == 1
        assert obs["icp_config_hash"] == profile["config_hash"]
    finally:
        conn.close()


def test_a_batch_with_no_usable_contacts_still_records_the_attempt():
    """A page that yielded nothing is the fact that stops the next run paying
    to find out the same thing again."""
    cid = _company()
    contact_review.apply_batch(_batch(cid, []))
    conn = om.get_conn()
    try:
        assert conn.execute(
            "SELECT outcome FROM company_contact_observations WHERE company_id = ?", (cid,)
        ).fetchone()["outcome"] == "no_contacts"
    finally:
        conn.close()


def test_a_dry_run_records_no_observation():
    cid = _company()
    contact_review.apply_batch(_batch(cid, [{"name": "Dana Whitfield"}]), dry_run=True)
    conn = om.get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM company_contact_observations").fetchone()["c"] == 0
    finally:
        conn.close()


# ── the pending queue ────────────────────────────────────────────────────────

def test_only_pages_the_regex_pass_could_not_crack_are_pending():
    easy = _company("Easy Motors", "easy.test", page=STAFF_PAGE)
    hard = _company("Hard Motors", "hard.test", page=OPAQUE_PAGE)
    result = contact_review.cli_extract_pending()
    assert [p["company_id"] for p in result["pending"]] == [hard]
    assert easy not in [p["company_id"] for p in result["pending"]]


def test_a_pending_page_carries_the_markdown_and_the_icp():
    contact_icp.cli_set("storefront", "ops", whitelist="general manager", min_contacts=1)
    cid = _company(page=OPAQUE_PAGE)
    page = contact_review.cli_extract_pending("storefront", icp_name="ops")["pending"][0]
    assert page["company_id"] == cid
    assert page["markdown"] == OPAQUE_PAGE
    assert page["icp"]["whitelist"] == ["general manager"]
    assert page["regex_found"] == 0
    assert page["url"].endswith("/staff")


def test_min_contacts_decides_what_counts_as_cracked():
    """One good pair is enough at min_contacts=1 and not enough at 5."""
    contact_icp.cli_set("storefront", "lenient", min_contacts=1)
    contact_icp.cli_set("storefront", "strict", min_contacts=5)
    _company(page=STAFF_PAGE)
    assert contact_review.cli_extract_pending("storefront", icp_name="lenient")["count"] == 0
    assert contact_review.cli_extract_pending("storefront", icp_name="strict")["count"] == 1


def test_a_page_already_extracted_under_this_icp_is_not_offered_again():
    profile = contact_icp.cli_set("storefront", "ops", min_contacts=5)
    cid = _company(page=STAFF_PAGE)
    assert contact_review.cli_extract_pending("storefront", icp_name="ops")["count"] == 1
    contact_review.apply_batch(
        _batch(cid, [{"name": "Dana Whitfield", "title": "General Manager"}]),
        workspace="storefront", icp_name="ops")
    assert contact_review.cli_extract_pending("storefront", icp_name="ops")["count"] == 0
    assert contact_review.cli_extract_pending(
        "storefront", icp_name="ops", force=True)["count"] == 1
    assert profile["config_hash"]


def test_editing_the_icp_makes_an_extracted_page_pending_again():
    """Changing the profile is exactly when re-deciding is worth doing."""
    contact_icp.cli_set("storefront", "ops", min_contacts=5)
    cid = _company(page=STAFF_PAGE)
    contact_review.apply_batch(
        _batch(cid, [{"name": "Dana Whitfield", "title": "General Manager"}]),
        workspace="storefront", icp_name="ops")
    assert contact_review.cli_extract_pending("storefront", icp_name="ops")["count"] == 0
    contact_icp.cli_set("storefront", "ops", whitelist="dealer principal")
    assert contact_review.cli_extract_pending("storefront", icp_name="ops")["count"] == 1


def test_the_workspace_that_paid_for_the_fetch_gets_the_page():
    """A sourced company has no contact in the workspace yet -- that is why it
    is being sourced -- so the fetch observation is what ties the two."""
    om.create_workspace("Other", slug="other")
    _company(page=OPAQUE_PAGE, ws="storefront")
    assert contact_review.cli_extract_pending("storefront")["count"] == 1
    assert contact_review.cli_extract_pending("other")["count"] == 0


def test_an_existing_lead_in_the_workspace_also_scopes_the_page():
    om.create_workspace("Other", slug="other")
    cid = _company(page=OPAQUE_PAGE, ws=None)
    assert contact_review.cli_extract_pending("storefront")["count"] == 0

    conn = om.get_conn()
    lead = om.resolve_lead(name="Seed Contact", company="Modern Storefront",
                           allow_weak_identity=True, conn=conn)
    conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, _ws_id(), lead["id"])
    conn.commit()
    conn.close()

    assert contact_review.cli_extract_pending("storefront")["count"] == 1
    assert contact_review.cli_extract_pending("other")["count"] == 0


def test_pending_respects_the_limit():
    for i in range(4):
        _company(f"Company {i}", f"c{i}.test", page=OPAQUE_PAGE)
    assert contact_review.cli_extract_pending(limit=2)["count"] == 2


def test_an_unknown_workspace_raises():
    with pytest.raises(contact_review.ContactReviewError, match="workspace not found"):
        contact_review.cli_extract_pending("nope")


# ── CLI wiring ───────────────────────────────────────────────────────────────

def _run_cli(monkeypatch, capsys, *argv):
    import pipeline_cli

    monkeypatch.setattr(sys, "argv", ["pipeline.py", *argv])
    code = pipeline_cli.main()
    return code, capsys.readouterr().out


def test_cli_pending_then_apply_round_trip(monkeypatch, capsys):
    cid = _company(page=OPAQUE_PAGE)
    _, out = _run_cli(monkeypatch, capsys, "contact-extract-pending", "--json")
    pending = json.loads(out)["pending"]
    assert pending[0]["company_id"] == cid

    batch = json.dumps([{"company_id": cid, "contacts": [
        {"name": "Dana Whitfield", "title": "General Manager"}]}])
    _, out = _run_cli(monkeypatch, capsys, "contact-apply", "--batch",
                      "--json", batch, "--workspace", "storefront")
    assert json.loads(out)["attached"] == 1
    assert _lead_count() == 1


def test_cli_apply_without_batch_is_an_error(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, capsys, "contact-apply", "--json", "[]")
    assert code == 1
    assert json.loads(out)["status"] == "error"


def test_cli_apply_rejects_unparseable_json(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, capsys, "contact-apply", "--batch", "--json", "{oops")
    assert code == 1
    assert "JSON" in json.loads(out)["error"]


def test_cli_pending_human_output_omits_the_markdown(monkeypatch, capsys):
    """A batch is ~200k tokens; printing it into the main thread is the exact
    mistake the subagent guidance exists to prevent."""
    cid = _company(page=OPAQUE_PAGE)
    _, out = _run_cli(monkeypatch, capsys, "contact-extract-pending")
    assert f"[{cid}]" in out
    assert "thirty years" not in out
