"""The human triage queue, and the gate this phase exists for.

Two things are under test:

  * **`contact-review`.** It follows `serper_review.py`'s three rules -- nothing
    pre-selected, "none of these" is an answer, rejections are kept -- over a
    candidate list derived from the cached page rather than stored. The id is a
    position in `regex_pass` output, so the tests that matter are the ones that
    pin down what moves it and what does not.

  * **The Phase 8 gate.** An ICP edit followed by `--reparse` changes what a run
    keeps, at zero fetches and zero credits. That property is the entire reason
    `company_page_cache` never auto-invalidates, and it is only worth anything if
    something fails when it stops being true.

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
import page_cache  # noqa: E402
import pipeline as om  # noqa: E402

# Four people, three titles the dealer ICP has an opinion about and one it does
# not. The "no title" case is the one a human is most often needed for.
STAFF_PAGE = """
## Meet Our Team

### Dana Whitfield

#### General Manager

[Email Me](mailto:dwhitfield@example.test)

### Theo Brandt

#### Assistant General Manager

### Ines Fournier

#### Sales Consultant

### Rowan Petrel
"""


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()
    om.create_workspace("Storefront", slug="storefront")


def _company(name="Modern Storefront", domain="modernstorefront.test",
             page=STAFF_PAGE, ws="storefront"):
    """A company with a cached staff page, tied to the workspace that fetched it."""
    conn = om.get_conn()
    try:
        cid = om.ensure_company(conn, name=name, domain=domain)
        url = f"https://{domain}/staff"
        if page is not None:
            page_cache.store_page(conn, url, page, company_id=cid, http_status=200)
            if ws:
                ws_row = om.resolve_workspace_identity(conn, ws)
                contact_review.record_observation(
                    conn, cid, outcome="fetched", workspace_id=ws_row["id"],
                    url=url, extractor="regex")
        conn.commit()
        return cid
    finally:
        conn.close()


def _company_in_workspace(name="Modern Storefront", domain="modernstorefront.test",
                          page=STAFF_PAGE, slug="storefront"):
    """A company `find-contacts` can target: reachable from the workspace via a
    lead, and carrying a cached page for `--reparse` to re-read."""
    cid = _company(name, domain, page=page, ws=slug)
    conn = om.get_conn()
    try:
        lead = om.resolve_lead(name=f"Seed {name}", company=name,
                               allow_weak_identity=True, conn=conn)
        conn.execute("UPDATE leads SET company_id = ? WHERE id = ?", (cid, lead["id"]))
        ws = om.resolve_workspace_identity(conn, slug)
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws["id"], lead["id"])
        conn.commit()
        return cid
    finally:
        conn.close()


def _lead_names():
    """Sourced contacts, without the seed lead that ties a company to a workspace."""
    conn = om.get_conn()
    try:
        return sorted(r["name"] for r in conn.execute("SELECT name FROM leads")
                      if not r["name"].startswith("Seed "))
    finally:
        conn.close()


def _observations(company_id):
    conn = om.get_conn()
    try:
        return conn.execute(
            "SELECT * FROM company_contact_observations WHERE company_id = ? "
            "ORDER BY id", (company_id,)).fetchall()
    finally:
        conn.close()


def _ops_icp(**kwargs):
    kwargs.setdefault("whitelist", "general manager")
    kwargs.setdefault("blocklist", "assistant general manager")
    return contact_icp.cli_set("storefront", "ops", **kwargs)


# ── what the queue offers ────────────────────────────────────────────────────

def test_every_candidate_is_offered_kept_and_rejected_alike():
    """The rejects are the reason a human is looking. A queue showing only the
    ICP's keeps is a queue that can never correct the ICP."""
    _ops_icp()
    _company()
    result = contact_review.cli_review("storefront", icp_name="ops")
    candidates = result["companies"][0]["candidates"]

    assert [c["name"] for c in candidates] == [
        "Dana Whitfield", "Theo Brandt", "Ines Fournier", "Rowan Petrel"]
    assert [c["reason"] for c in candidates] == [
        "whitelist", "blocklist", "not_in_whitelist", "no_title"]
    assert [c["kept"] for c in candidates] == [True, False, False, False]


def test_nothing_is_pre_selected():
    _ops_icp()
    _company()
    candidates = contact_review.cli_review(
        "storefront", icp_name="ops")["companies"][0]["candidates"]
    assert all("chosen" not in c for c in candidates)
    assert all("selected" not in c for c in candidates)


def test_ids_are_page_positions_and_do_not_move_when_the_icp_changes():
    """The load-bearing property of deriving candidates instead of storing them:
    an ICP edit changes verdicts, never numbering."""
    _ops_icp()
    cid = _company()
    before = contact_review.cli_review(
        "storefront", company_id=cid, icp_name="ops")["companies"][0]["candidates"]

    _ops_icp(whitelist="sales consultant")
    after = contact_review.cli_review(
        "storefront", company_id=cid, icp_name="ops")["companies"][0]["candidates"]

    assert [(c["id"], c["name"]) for c in before] == [(c["id"], c["name"]) for c in after]
    assert [c["kept"] for c in before] == [True, False, False, False]
    assert [c["kept"] for c in after] == [False, False, True, False]


def test_a_candidate_already_attached_is_labelled_not_hidden():
    _ops_icp()
    cid = _company()
    contact_review.apply_batch(
        [{"company_id": cid, "contacts": [{"name": "Dana Whitfield", "title": "GM"}]}],
        workspace="storefront", icp_name="ops")
    candidates = contact_review.cli_review(
        "storefront", company_id=cid, icp_name="ops")["companies"][0]["candidates"]
    assert [c["attached"] for c in candidates] == [True, False, False, False]


def test_a_company_with_nothing_left_to_choose_leaves_the_queue():
    cid = _company()
    contact_review.apply_batch([{"company_id": cid, "contacts": [
        {"name": "Dana Whitfield"}, {"name": "Theo Brandt"},
        {"name": "Ines Fournier"}, {"name": "Rowan Petrel"},
    ]}], workspace="storefront")
    assert contact_review.cli_review("storefront")["count"] == 0


def test_a_company_with_no_cached_page_is_an_error():
    cid = _company(page=None)
    with pytest.raises(contact_review.ContactReviewError, match="no cached page"):
        contact_review.cli_review("storefront", company_id=cid)


def test_the_workspace_that_paid_for_the_fetch_gets_the_company():
    om.create_workspace("Other", slug="other")
    _company(ws="storefront")
    assert contact_review.cli_review("storefront")["count"] == 1
    assert contact_review.cli_review("other")["count"] == 0


def test_the_queue_respects_limit_and_offset():
    for i in range(4):
        _company(f"Company {i}", f"c{i}.test")
    first = contact_review.cli_review("storefront", limit=2)
    rest = contact_review.cli_review("storefront", limit=2, offset=2)
    assert len(first["companies"]) == 2
    assert len(rest["companies"]) == 2
    assert {c["company_id"] for c in first["companies"]} != \
           {c["company_id"] for c in rest["companies"]}


def test_a_missing_named_icp_profile_is_an_error():
    _company()
    with pytest.raises(contact_review.ContactReviewError, match="no ICP profile"):
        contact_review.cli_review("storefront", icp_name="absent")


# ── applying picks ───────────────────────────────────────────────────────────

def test_applying_ids_attaches_exactly_those_people():
    _ops_icp()
    cid = _company()
    result = contact_review.cli_apply_ids(
        cid, [2, 3], workspace="storefront", icp_name="ops")
    assert result["attached"] == 2
    assert _lead_names() == ["Ines Fournier", "Rowan Petrel"]


def test_an_applied_contact_keeps_the_details_scraped_next_to_the_name():
    cid = _company()
    contact_review.cli_apply_ids(cid, [0], workspace="storefront")
    conn = om.get_conn()
    try:
        lead = conn.execute("SELECT title, email, company_id FROM leads").fetchone()
        assert lead["title"] == "General Manager"
        assert lead["email"] == "dwhitfield@example.test"
        assert lead["company_id"] == cid
    finally:
        conn.close()


def test_the_blocklist_still_wins_over_a_human_pick():
    """The one ICP rule that means "never contact this person". A reviewer can
    disagree with the whitelist; the blocklist is enforced on every write path."""
    _ops_icp()
    cid = _company()
    result = contact_review.cli_apply_ids(
        cid, [1], workspace="storefront", icp_name="ops")
    assert result["attached"] == 0
    assert result["rejections"][0]["reason"] == "blocklist"
    assert _lead_names() == []


def test_an_unknown_id_is_refused_and_writes_nothing():
    cid = _company()
    with pytest.raises(contact_review.ContactReviewError, match="no candidate with id 9"):
        contact_review.cli_apply_ids(cid, [0, 9], workspace="storefront")
    assert _lead_names() == [], "the whole pick rolls back, not just the bad id"
    assert _observations(cid)[-1]["extractor"] == "regex", "no decision was recorded"


def test_a_changed_page_invalidates_the_ids_when_the_hash_is_given():
    """Ids are positions in the page, so a re-fetch renumbers them. The guard is
    opt-in because the common path is review-then-apply seconds later."""
    cid = _company()
    view = contact_review.cli_review("storefront", company_id=cid)["companies"][0]
    conn = om.get_conn()
    page_cache.store_page(conn, view["url"], "### Someone Else\n\n#### Sales Manager\n",
                          company_id=cid)
    conn.commit()
    conn.close()

    with pytest.raises(contact_review.ContactReviewError, match="page changed"):
        contact_review.cli_apply_ids(
            cid, [0], workspace="storefront", content_hash=view["content_hash"])
    # Without the guard the apply proceeds against whatever is cached now.
    assert contact_review.cli_apply_ids(cid, [0], workspace="storefront")["attached"] == 1
    assert _lead_names() == ["Someone Else"]


def test_a_dry_run_writes_neither_lead_nor_decision():
    cid = _company()
    result = contact_review.cli_apply_ids(
        cid, [0], workspace="storefront", dry_run=True)
    assert result["status"] == "dry_run"
    assert _lead_names() == []
    assert [o["extractor"] for o in _observations(cid)] == ["regex"]


def test_applying_the_same_pick_twice_creates_no_duplicate():
    cid = _company()
    contact_review.cli_apply_ids(cid, [0], workspace="storefront")
    contact_review.cli_apply_ids(cid, [0], workspace="storefront", )
    assert _lead_names() == ["Dana Whitfield"]


# ── the decision, and what it records ────────────────────────────────────────

def test_a_decision_keeps_what_was_offered_and_refused():
    """Rule three. Without the rejections there is no way to tell a good
    ordering from a lucky one."""
    _ops_icp()
    cid = _company()
    contact_review.cli_apply_ids(cid, [0], workspace="storefront", icp_name="ops")

    obs = _observations(cid)[-1]
    decision = json.loads(obs["decision_json"])
    assert obs["extractor"] == "human"
    assert obs["outcome"] == "reviewed"
    assert [c["name"] for c in decision["chosen"]] == ["Dana Whitfield"]
    assert [c["name"] for c in decision["rejected"]] == [
        "Theo Brandt", "Ines Fournier", "Rowan Petrel"]
    assert [c["reason"] for c in decision["rejected"]] == [
        "blocklist", "not_in_whitelist", "no_title"]


def test_none_of_these_is_recorded_as_a_decision():
    """Rule two. The absence of a decision means "not looked at yet", which is a
    different state and must stay one."""
    _ops_icp()
    cid = _company()
    result = contact_review.cli_review(
        "storefront", company_id=cid, icp_name="ops", none_of_these=True)

    assert result["status"] == "dismissed"
    assert result["attached"] == 0
    assert _lead_names() == []
    obs = _observations(cid)[-1]
    assert obs["outcome"] == "dismissed"
    assert len(json.loads(obs["decision_json"])["rejected"]) == 4


def test_a_decided_company_is_not_offered_again():
    _ops_icp()
    cid = _company()
    contact_review.cli_review("storefront", company_id=cid, icp_name="ops",
                              none_of_these=True)
    assert contact_review.cli_review("storefront", icp_name="ops")["count"] == 0
    assert contact_review.cli_review("storefront", icp_name="ops", force=True)["count"] == 1


def test_editing_the_icp_puts_a_decided_company_back_in_the_queue():
    """Changing the profile is exactly when re-deciding is worth doing -- the
    same rule the agent queue follows, keyed on the same hash."""
    _ops_icp()
    cid = _company()
    contact_review.cli_review("storefront", company_id=cid, icp_name="ops",
                              none_of_these=True)
    assert contact_review.cli_review("storefront", icp_name="ops")["count"] == 0

    _ops_icp(whitelist="sales consultant")
    assert contact_review.cli_review("storefront", icp_name="ops")["count"] == 1
    assert cid


def test_none_of_these_needs_a_company():
    with pytest.raises(contact_review.ContactReviewError, match="needs --company-id"):
        contact_review.cli_review("storefront", none_of_these=True)


def test_an_empty_pick_is_an_error_not_a_silent_dismissal():
    """"I chose nobody" and "none of these fit" look identical in the data and
    are not the same statement, so the ambiguous one is refused."""
    cid = _company()
    with pytest.raises(contact_review.ContactReviewError, match="none-of-these"):
        contact_review.cli_apply_ids(cid, [], workspace="storefront")


# ── the Phase 8 gate: an ICP edit + --reparse, at zero credits ───────────────

def test_reparse_after_an_icp_edit_changes_the_result_and_spends_nothing(monkeypatch):
    """The gate. `company_page_cache` never auto-invalidates precisely so that
    re-scoring a page against a changed profile costs nothing -- and the only way
    that stays true is if a fetch during a reparse is a test failure."""
    def _no_fetching(*args, **kwargs):
        raise AssertionError("--reparse must never reach the network")

    _ops_icp(whitelist="general manager")
    cid = _company_in_workspace()
    monkeypatch.setattr(page_cache, "_firecrawl_post", _no_fetching)

    first = om.find_contacts_for_workspace(
        "storefront", icp_name="ops", company_ids=[cid], reparse=True)
    assert first["contacts_attached"] == 1
    assert _lead_names() == ["Dana Whitfield"]

    _ops_icp(whitelist="sales consultant")
    second = om.find_contacts_for_workspace(
        "storefront", icp_name="ops", company_ids=[cid], reparse=True)

    assert second["contacts_attached"] == 1
    assert second["firecrawl_credits_spent"] == 0, "a reparse spends no Firecrawl credit"
    assert second["serper_queries_spent"] == 0, "and no Serper query either"
    assert _lead_names() == ["Dana Whitfield", "Ines Fournier"]


def test_reparse_stamps_the_observation_with_the_icp_that_produced_it():
    """Precision-per-campaign only means something if a finding joins to the
    config version that produced it."""
    first_profile = _ops_icp(whitelist="general manager")
    cid = _company_in_workspace()
    om.find_contacts_for_workspace(
        "storefront", icp_name="ops", company_ids=[cid], reparse=True)

    second_profile = _ops_icp(whitelist="sales consultant")
    om.find_contacts_for_workspace(
        "storefront", icp_name="ops", company_ids=[cid], reparse=True)

    hashes = [o["icp_config_hash"] for o in _observations(cid) if o["extractor"] == "regex"]
    assert first_profile["config_hash"] != second_profile["config_hash"]
    assert hashes[-2:] == [first_profile["config_hash"], second_profile["config_hash"]]


def test_reparse_with_no_cached_page_reports_it_rather_than_fetching():
    cid = _company_in_workspace(page=None)
    result = om.find_contacts_for_workspace(
        "storefront", company_ids=[cid], reparse=True)
    assert result["firecrawl_credits_spent"] == 0
    assert _observations(cid)[-1]["outcome"] == "no_cached_page"


# ── CLI wiring ───────────────────────────────────────────────────────────────

def _run_cli(monkeypatch, capsys, *argv):
    import pipeline_cli

    monkeypatch.setattr(sys, "argv", ["pipeline.py", *argv])
    code = pipeline_cli.main()
    return code, capsys.readouterr().out


def test_cli_review_then_apply_round_trip(monkeypatch, capsys):
    _ops_icp()
    cid = _company()
    _, out = _run_cli(monkeypatch, capsys, "contact-review",
                      "--workspace", "storefront", "--icp", "ops", "--json")
    company = json.loads(out)["companies"][0]
    assert company["company_id"] == cid

    _, out = _run_cli(monkeypatch, capsys, "contact-apply",
                      "--company-id", str(cid), "--contact-ids", "0,2",
                      "--workspace", "storefront", "--icp", "ops",
                      "--content-hash", company["content_hash"])
    assert json.loads(out)["attached"] == 2
    assert _lead_names() == ["Dana Whitfield", "Ines Fournier"]


def test_cli_none_of_these(monkeypatch, capsys):
    cid = _company()
    _, out = _run_cli(monkeypatch, capsys, "contact-review",
                      "--company-id", str(cid), "--none-of-these",
                      "--workspace", "storefront", "--json")
    assert json.loads(out)["status"] == "dismissed"
    assert _lead_names() == []


def test_cli_human_output_prints_every_id_and_marks_nothing(monkeypatch, capsys):
    _ops_icp()
    cid = _company()
    _, out = _run_cli(monkeypatch, capsys, "contact-review",
                      "--workspace", "storefront", "--icp", "ops")
    assert f"[{cid}]" in out
    for name in ("Dana Whitfield", "Theo Brandt", "Ines Fournier", "Rowan Petrel"):
        assert name in out
    assert "blocklist" in out and "not_in_whitelist" in out
    assert "*" not in out, "no candidate is starred; the operator picks"


def test_cli_apply_needs_batch_or_a_company(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, capsys, "contact-apply", "--json", "[]")
    assert code == 1
    assert "--company-id" in json.loads(out)["error"]


def test_cli_apply_rejects_non_numeric_ids(monkeypatch, capsys):
    cid = _company()
    code, out = _run_cli(monkeypatch, capsys, "contact-apply",
                         "--company-id", str(cid), "--contact-ids", "0,oops")
    assert code == 1
    assert "oops" in json.loads(out)["error"]
