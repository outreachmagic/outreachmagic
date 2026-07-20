"""company_registrable_domain() (Stage D0): eTLD+1 comparison function for
company-dedup confidence tiering. Pure-function tests, no DB fixture needed."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pipeline_utils import company_registrable_domain as crd  # noqa: E402


def test_mail_subdomain_collapses_to_registrable_domain():
    assert crd("mail.wvu.edu") == "wvu.edu"
    assert crd("mix.wvu.edu") == "wvu.edu"
    assert crd("wvu.edu") == "wvu.edu"


def test_non_mail_subdomains_also_collapse_not_just_mail_prefixes():
    """Proves this isn't a hand-maintained mail-prefix hack -- career./go./
    am./cahs./coe./eservices. are department/portal/regional subdomains,
    nothing mail-related, and must still collapse to the same registrable
    domain as their parent org."""
    assert crd("career.olemiss.edu") == "olemiss.edu"
    assert crd("go.olemiss.edu") == "olemiss.edu"
    assert crd("am.jll.com") == "jll.com"
    assert crd("cahs.colostate.edu") == "colostate.edu"
    assert crd("coe.northeastern.edu") == "northeastern.edu"
    assert crd("eservices.virginia.edu") == "virginia.edu"


def test_genuinely_different_domains_stay_different():
    assert crd("wvu.edu") != crd("wvup.edu")
    assert crd("enterprise.com") != crd("ehi.com") != crd("em.com")
    assert crd("kochcc.com") != crd("kochgs.com") != crd("kochinc.com")


def test_multi_label_suffix_false_positive_guard():
    """The core risk a naive last-two-labels split would introduce: two
    unrelated companies both on .co.uk must not collapse to the same
    "co.uk" registrable domain."""
    assert crd("foo.co.uk") == "foo.co.uk"
    assert crd("bar.co.uk") == "bar.co.uk"
    assert crd("foo.co.uk") != crd("bar.co.uk")
    assert crd("sub.foo.co.uk") == "foo.co.uk"


def test_multi_label_suffix_other_countries():
    assert crd("sub.example.com.au") == "example.com.au"
    assert crd("sub.example.co.nz") == "example.co.nz"
    assert crd("sub.example.co.za") == "example.co.za"


def test_none_and_malformed_input():
    assert crd(None) is None
    assert crd("") is None
    assert crd("nodothere") is None
