"""The structural pass over a staff page, and the ICP filter applied to it.

Every fixture here is synthetic. The layouts are real -- they reproduce the four
shapes observed across dealer CMS platforms (heading-name, plain-line name,
underscore-italic title, section-banner) -- but the names, titles, domains and
phone numbers are invented, because this repo is public.

The pass is deliberately generous: it answers "who is on this page", and the ICP
profile answers "who do we care about". Tests are grouped that way.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import contact_extract as ce  # noqa: E402


def _pairs(markdown):
    return [(c.name, c.title) for c in ce.regex_pass(markdown) if c.title]


# ── layout: name as a heading, title as the next heading ─────────────────────

HEADING_LAYOUT = """
## Meet Our Team

- ![Dana Whitfield](https://cdn.example.test/dana.png)

### Dana Whitfield

#### General Manager

Ext. 3107

[Email Me](mailto:dwhitfield@example.test)

- ![Marco Bell](https://cdn.example.test/marco.png)

### Marco Bell

#### Fixed Operations Director

[Email Me](mailto:mbell@example.test)
"""


def test_heading_layout_pairs_name_with_the_following_title():
    assert _pairs(HEADING_LAYOUT) == [
        ("Dana Whitfield", "General Manager"),
        ("Marco Bell", "Fixed Operations Director"),
    ]


def test_heading_layout_recovers_the_email_from_the_link_target():
    """The address lives in `[Email Me](mailto:…)`, which is a link-only line --
    invisible to the name/title pass and exactly where the details are."""
    contacts = {c.name: c for c in ce.regex_pass(HEADING_LAYOUT)}
    assert contacts["Dana Whitfield"].email == "dwhitfield@example.test"
    assert contacts["Marco Bell"].email == "mbell@example.test"


# ── layout: plain name line, plain title line ────────────────────────────────

PLAIN_LAYOUT = """
#### Dealer Principal

![](https://cdn.example.test/1.png)

Priya Raman

Dealer Principal

Read More

#### Management Staff

![Sales Manager](https://cdn.example.test/2.png)

Owen Castellanos

Sales Manager

[Call Me](tel:555-201-4400)
555-201-4400
"""


def test_plain_layout_pairs_adjacent_lines():
    assert _pairs(PLAIN_LAYOUT) == [
        ("Priya Raman", "Dealer Principal"),
        ("Owen Castellanos", "Sales Manager"),
    ]


def test_plain_layout_carries_the_section_heading():
    contacts = {c.name: c for c in ce.regex_pass(PLAIN_LAYOUT)}
    assert contacts["Priya Raman"].section == "Dealer Principal"
    assert contacts["Owen Castellanos"].section == "Management Staff"


def test_phone_is_recovered_from_the_block():
    contacts = {c.name: c for c in ce.regex_pass(PLAIN_LAYOUT)}
    assert contacts["Owen Castellanos"].phone == "555-201-4400"


# ── the underscore-italic trap ───────────────────────────────────────────────

ITALIC_LAYOUT = """
![Curtis Bowen](https://cdn.example.test/staff.png)

Curtis Bowen

_New Car Sales Manager_

[Phone](tel:5552138000 "Phone")[Email](mailto:bowen@example.test "Email")

![Renata Vlk](https://cdn.example.test/staff.png)

Renata Vlk

_Pre-Owned Sales Manager_
"""


def test_underscore_italic_titles_are_not_invisible():
    """`_` is a regex word character, so `\\b(sales manager)\\b` does not match
    `_Sales Manager_`. A whole page scored zero titles for this reason during
    the vendor bake-off and read as a fetch failure."""
    assert _pairs(ITALIC_LAYOUT) == [
        ("Curtis Bowen", "New Car Sales Manager"),
        ("Renata Vlk", "Pre-Owned Sales Manager"),
    ]


def test_strip_markdown_removes_emphasis_and_keeps_link_labels():
    assert ce.strip_markdown("_**Sales Manager**_") == "Sales Manager"
    assert ce.strip_markdown("### [Dana Whitfield](/staff/dana)") == "Dana Whitfield"


def test_an_italicised_title_still_matches_an_icp_term():
    """The trap again, one layer up: canonical ICP terms have no underscores, so
    the matcher's boundaries must not treat `_` as a word character either."""
    scored = ce.score_against_icp(
        ce.regex_pass(ITALIC_LAYOUT), {"whitelist": ["sales manager"]})
    assert [s.candidate.name for s in scored if s.kept] == ["Curtis Bowen", "Renata Vlk"]


# ── navigation is not staff ──────────────────────────────────────────────────

NAV_LAYOUT = """
- [Special Offers](https://example.test/offers "Special Offers")
- [Guest Amenities](https://example.test/amenities)
- [Recall Information](https://example.test/recalls)

## SALES TEAM

### Ivy Trelawney

Sales Manager

[More Info](https://example.test/bio-1) [Schedule Service](https://example.test/service)
"""


def test_link_only_lines_are_not_people():
    """A dealer's nav is a dozen two-capitalised-word phrases. Structural, not a
    stoplist: no vocabulary of navigation phrases is ever finished."""
    assert _pairs(NAV_LAYOUT) == [("Ivy Trelawney", "Sales Manager")]


def test_link_only_lines_are_not_titles_either():
    contacts = ce.regex_pass(NAV_LAYOUT)
    assert [c.title for c in contacts] == ["Sales Manager"]


def test_an_all_caps_banner_becomes_the_section_not_a_person():
    contacts = ce.regex_pass(NAV_LAYOUT)
    assert contacts[0].section == "SALES TEAM"


def test_a_role_heading_with_no_name_before_it_is_a_section():
    markdown = "#### Finance Managers\n\nHugo Almeida\n\nFinance Manager\n"
    contact = ce.regex_pass(markdown)[0]
    assert contact.section == "Finance Managers"
    assert contact.title == "Finance Manager"


@pytest.mark.parametrize("line", [
    "Dealer Principal", "Sales Manager", "MANAGEMENT TEAM", "Meet Our Team",
    "Read More", "Contact Us", "Hours & Directions", "All Dealer Trades",
])
def test_role_and_navigation_phrases_are_not_names(line):
    assert not ce.looks_like_name(line)


@pytest.mark.parametrize("line", [
    "Dana Whitfield", "Ana R Delgado", "AJ Fenwick", "Mira Van-Hoorn",
    "Renata Vlk", "Owen Castellanos",
])
def test_person_shaped_lines_are_names(line):
    assert ce.looks_like_name(line)


def test_a_title_with_digits_is_a_phone_or_an_hours_block():
    """"Sales: 555-746-0500" and "Sales: 9am-8pm" both contain a role word."""
    assert not ce.looks_like_title("Sales: 555-746-0500")
    assert not ce.looks_like_title("Sales: 9am-8pm Service: 7am-7pm")
    assert ce.looks_like_title("General Manager")


def test_a_prose_paragraph_mentioning_the_sales_team_is_not_a_title():
    long_line = (
        "Every department has one thing in common: a dedicated sales team ready "
        "to solve all of your automotive needs today."
    )
    assert not ce.looks_like_title(long_line)


# ── block boundaries ─────────────────────────────────────────────────────────

def test_a_title_does_not_drift_onto_the_previous_person():
    markdown = "### Nadia Okonkwo\n\n### Bram Sadler\n\nService Director\n"
    assert _pairs(markdown) == [("Bram Sadler", "Service Director")]


def test_details_do_not_leak_from_the_next_persons_block():
    markdown = (
        "### Nadia Okonkwo\n\nParts Manager\n\n"
        "### Bram Sadler\n\nService Director\n\n[Email](mailto:bram@example.test)\n"
    )
    contacts = {c.name: c for c in ce.regex_pass(markdown)}
    assert contacts["Nadia Okonkwo"].email is None
    assert contacts["Bram Sadler"].email == "bram@example.test"


def test_the_same_person_listed_twice_is_one_candidate():
    """Staff pages repeat people across department sections."""
    block = "### Dana Whitfield\n\nGeneral Manager\n\n"
    assert len(ce.regex_pass(block * 3)) == 1


def test_an_empty_page_yields_nothing():
    assert ce.regex_pass("") == []
    assert ce.regex_pass("   \n\n  ") == []


# ── ICP scoring ──────────────────────────────────────────────────────────────

ICP = {
    "whitelist": ["general manager", "service manager"],
    "blocklist": ["assistant general manager"],
    "min_contacts": 1,
}

MIXED = """
### Dana Whitfield

General Manager

### Theo Brandt

Assistant General Manager

### Ines Fournier

Sales Consultant
"""


def test_the_blocklist_is_checked_before_the_whitelist():
    """"assistant general manager" contains "general manager" -- whitelist-first
    keeps exactly the people the blocklist was written to exclude."""
    scored = {s.candidate.name: s for s in ce.score_against_icp(ce.regex_pass(MIXED), ICP)}
    assert scored["Theo Brandt"].kept is False
    assert scored["Theo Brandt"].reason == "blocklist"
    assert scored["Dana Whitfield"].kept is True


def test_off_profile_titles_are_rejected_not_dropped():
    scored = {s.candidate.name: s for s in ce.score_against_icp(ce.regex_pass(MIXED), ICP)}
    assert scored["Ines Fournier"].reason == "not_in_whitelist"
    assert len(scored) == 3, "rejections stay in the result so the whitelist can be judged"


def test_kept_contacts_returns_only_the_survivors():
    kept = ce.kept_contacts(ce.score_against_icp(ce.regex_pass(MIXED), ICP))
    assert [c.name for c in kept] == ["Dana Whitfield"]


def test_an_empty_whitelist_keeps_everything_the_blocklist_did_not_take():
    """A profile with no positive rule is not a profile that rejects everyone."""
    scored = ce.score_against_icp(ce.regex_pass(MIXED), {"blocklist": ["sales consultant"]})
    assert [s.candidate.name for s in scored if s.kept] == ["Dana Whitfield", "Theo Brandt"]


def test_no_icp_at_all_keeps_everything():
    assert len(ce.kept_contacts(ce.score_against_icp(ce.regex_pass(MIXED), None))) == 3


def test_a_titleless_candidate_cannot_satisfy_a_whitelist():
    scored = ce.score_against_icp([ce.ContactCandidate(name="Dana Whitfield")], ICP)
    assert scored[0].kept is False
    assert scored[0].reason == "no_title"


def test_the_longest_matching_term_is_the_one_reported():
    scored = ce.score_against_icp(
        [ce.ContactCandidate(name="Dana Whitfield", title="General Sales Manager")],
        {"whitelist": ["manager", "general sales manager"]})
    assert scored[0].matched == "general sales manager"


def test_section_headers_scope_which_part_of_the_page_counts():
    markdown = (
        "## Senior Staff\n\n### Dana Whitfield\n\nGeneral Manager\n\n"
        "## Our Vendors\n\n### Rui Salgado\n\nGeneral Manager\n"
    )
    scored = {s.candidate.name: s for s in ce.score_against_icp(
        ce.regex_pass(markdown), {"whitelist": ["general manager"],
                                  "section_headers": ["senior staff"]})}
    assert scored["Dana Whitfield"].kept is True
    assert scored["Rui Salgado"].reason == "section"


def test_the_dealerships_own_name_is_not_a_person():
    scored = ce.score_against_icp(
        [ce.ContactCandidate(name="Carlton Motorcars", title="Sales Manager"),
         ce.ContactCandidate(name="Rodney Carlton", title="Sales Manager")],
        {"whitelist": ["sales manager"]},
        company_name="Carlton Motorcars, Inc.")
    assert scored[0].kept is False and scored[0].reason == "company_name"
    assert scored[1].kept is True, "a real person sharing one word with the brand survives"


# ── the fall-through signal ──────────────────────────────────────────────────

def test_a_page_the_pass_could_not_crack_goes_to_the_agent():
    assert ce.needs_agent([], {"min_contacts": 1}) is True
    assert ce.needs_agent(ce.regex_pass(MIXED), {"min_contacts": 5}) is True
    assert ce.needs_agent(ce.regex_pass(MIXED), {"min_contacts": 3}) is False


def test_the_fall_through_counts_extraction_not_icp_fit():
    """A page full of people none of whom fit the ICP is a page extraction
    handled perfectly. Routing on ICP-fit would send every off-profile page to
    the agent forever, on every run."""
    off_profile = {"whitelist": ["chief executive officer"], "min_contacts": 2}
    assert ce.needs_agent(ce.regex_pass(MIXED), off_profile) is False
    assert ce.kept_contacts(ce.score_against_icp(ce.regex_pass(MIXED), off_profile)) == []


def test_candidates_without_a_title_do_not_count_toward_the_minimum():
    nameless_only = [ce.ContactCandidate(name="Dana Whitfield")]
    assert ce.needs_agent(nameless_only, {"min_contacts": 1}) is True


def test_needs_llm_is_the_same_function():
    assert ce.needs_llm is ce.needs_agent


# ── the purity contract ──────────────────────────────────────────────────────

def test_the_module_touches_no_database_and_no_network():
    """Extraction rules must stay testable without a key, a connection or a
    credit -- the same split serper_candidates.py keeps from serper_review.py."""
    source = (SCRIPTS / "contact_extract.py").read_text(encoding="utf-8")
    for forbidden in ("import sqlite3", "from db_conn", "import urllib", "import requests"):
        assert forbidden not in source, f"contact_extract.py must not {forbidden}"
