"""Company domain + public-email discovery via Serper (find-domains).

Turns a company name with no known domain into 1+ candidate domains,
preferring a domain a public email is actually attached to over one that
merely looks like the company's website -- a company frequently owns more
than one real domain (website vs email-sending vs per-branch), and
email_finder should try the one with proven mail delivery first.

Credit discipline (this runs across thousands of companies; every query is a
paid credit): default is ONE Serper query per company (`"<name> email"`,
unquoted -- see strip_entity_suffix()). A second, *targeted* query only fires
when query 1 found a domain but zero emails on it -- aimed at the one thing
still missing, not a generic second opinion, and phrased without search
operators so it works on any Serper tier. A third, alt-domain query fires only
when query 1 returned organic results that merely failed to name-match; zero
results (or an error) means a second generic query is a wasted credit. Callers
may cap total spend for a run via `query_budget`. Every company is cached
org-wide (keyed on company_id via lead_provider_observations, not
per-workspace) so the same company searched from two different workspace
campaigns never re-spends credits -- and because those observations round-trip
the relay, the cache survives a wipe-and-pull.

Discovered domains are written into company_identities (the existing
multi-domain store rank_company_domains()/email_finder already read) --
deliberately NOT a new companies.domains_found column, which would fork the
one place downstream code already looks.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional
from urllib.parse import urlparse

import enrich
from constants import SHARED_EMAIL_DOMAINS, is_non_company_name
from normalize import validate_domain
from pipeline_utils import company_registrable_domain, normalize_company_domain, normalize_company_name
from provider_observations import KIND_DOMAIN_LOOKUP, ORIGIN_ATTEMPT, record_observation
from workspace_routing import DEFAULT_ORG_ID

FRESHNESS_DAYS = 30
# --retry-unresolved bypasses the 30-day cache so a scoring change can be
# re-applied, but NOT all history: a run killed and restarted would otherwise
# re-search every company it had just paid for. Anything searched inside this
# window is still treated as done, which is long enough to cover a restart and
# far shorter than any real "the logic changed" gap.
RETRY_FRESHNESS_HOURS = 6
MIN_SEARCH_NUM_RESULTS = 20

# Below this, the winner is recorded and reviewable but NEVER written to
# companies.domain or company_identities. A wrong domain here is worse than no
# domain: nothing downstream overwrites companies.domain once set, so a
# directory site that squeaked past scoring would poison email_finder for that
# company permanently. 0.40 is compute_confidence()'s "score >= 5" tier -- i.e.
# at least a real token overlap, not just a snippet mention.
MIN_ATTACH_CONFIDENCE = 0.40

# The final group deliberately cannot end in "." or "-": `[\w.-]+` used to run
# one char long on "...@acme.com." at the end of a snippet sentence, producing
# a "gmail.com." domain that no exclusion list matched.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
ROLE_PREFIXES = frozenset({
    "info", "contact", "hello", "support", "sales", "billing",
    "admin", "office", "team", "help", "careers", "jobs",
})

# Serper surfaces a lot of "what is <company>'s email format?" pages
# (RocketReach, LeadIQ, and similar). Those spell the pattern out using
# stand-in names, so the page yields addresses that look real and are not:
# jane.doe@stonex.com, first.last@coxinc.com, jdoe@fusionacademy.com. Matched
# on the local part only, exact after stripping separators, so a real person
# named e.g. "Mi Lastname" at mi@... is the only plausible collision and a
# role-address check would not have caught these anyway.
PLACEHOLDER_LOCALS = frozenset({
    "janedoe", "johndoe", "jandoe", "jdoe", "jsmith", "johnsmith", "janesmith",
    "firstlast", "firstnamelastname", "flast", "firstl", "fl", "fname", "lname",
    "first", "last", "firstname", "lastname", "initiallast", "mi", "ml",
    "name", "yourname", "fullname", "email", "youremail", "emailaddress",
    "user", "username", "someone", "somebody", "example", "sample", "test",
    "abc", "xyz", "aaa", "foo", "bar", "nn", "xx",
})

# Domains that only ever appear in documentation/examples.
PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "company.com", "yourcompany.com", "email.com", "mycompany.com", "acme.com",
})


# score_domain_match() reasons strong enough to stand ALONE, with no
# corroborating search signal. Deliberately excludes the containment tiers:
# normalize_company_name() strips generic words, so "Berkeley Partners"
# reduces to "berkeley", which is a substring of "berkeleycollege" -- fine as
# one signal among several when ranking search results, catastrophic as the
# sole basis for writing a domain onto a company.
STRICT_MATCH_REASONS = frozenset({"exact", "domain_is_name_prefix", "acronym"})


def _local_key(local: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (local or "").lower())


def is_placeholder_email(email: str) -> bool:
    """True for an address that documents a format rather than reaching anyone."""
    local, _, domain = (email or "").lower().partition("@")
    if not domain or domain in PLACEHOLDER_DOMAINS:
        return True
    return _local_key(local) in PLACEHOLDER_LOCALS


def classify_public_email(email: str, company_domain: Optional[str]) -> str:
    """How much this address can be trusted as a contact for the company.

    - 'corporate'     : on the company's own registrable domain -- usable
    - 'free_provider' : gmail/yahoo/etc -- often the only published contact a
                        small business has, so kept as a fallback
    - 'placeholder'   : an email-format example, reaches nobody -- dropped
    - 'off_domain'    : a real-looking address on someone ELSE's domain
                        (customer.service@mercer.com surfaced under Cox).
                        Recorded in the observation for reference, never
                        attached as a company identity.
    """
    email = (email or "").strip().lower()
    domain = email.partition("@")[2]
    if not domain or is_placeholder_email(email):
        return "placeholder"
    if domain in SHARED_EMAIL_DOMAINS:
        return "free_provider"
    company_domain = normalize_company_domain(company_domain) if company_domain else None
    if company_domain and company_registrable_domain(domain) == company_registrable_domain(company_domain):
        return "corporate"
    return "off_domain"


def drop_truncated_duplicates(emails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop an address whose local part is a strict suffix of another address
    on the same domain -- e.g. both 'accessibility@greensky.com' and
    'ccessibility@greensky.com' came back from one page, the second being a
    scrape artifact of the first."""
    by_domain: dict[str, list[str]] = {}
    for e in emails:
        local, _, domain = e["email"].partition("@")
        by_domain.setdefault(domain, []).append(local)
    drop: set[str] = set()
    for domain, locals_ in by_domain.items():
        for a in locals_:
            for b in locals_:
                if a != b and len(a) < len(b) and b.endswith(a):
                    drop.add(f"{a}@{domain}")
    return [e for e in emails if e["email"] not in drop]

# Suffix-only strip so "Modern Storefront LLC" -> "Modern Storefront" for the
# query text itself. Deliberately narrower than pipeline_utils.
# normalize_company_name(), which also strips generic mid-string words
# (systems/solutions/group/...) that are frequently part of the real brand --
# right for name-matching, wrong for a human-readable search string.
_ENTITY_SUFFIX_RE = re.compile(
    r",?\s*\b(l\.?l\.?c\.?|inc\.?|incorporated|corp\.?|corporation|ltd\.?|limited|"
    r"l\.?l\.?p\.?|l\.?p\.?|p\.?l\.?l\.?c\.?|p\.?c\.?|co\.?)\s*$",
    re.IGNORECASE,
)


# Words that make a name a business regardless of how person-like it reads.
# "Jensen Dental Group" is a company; "Rick Jensen" is not.
_BUSINESS_WORDS = frozenset({
    "group", "clinic", "health", "healthcare", "medical", "dental", "law",
    "firm", "associates", "partners", "services", "solutions", "systems",
    "care", "center", "centre", "hospital", "hospice", "agency", "studio",
    "consulting", "management", "properties", "realty", "insurance", "capital",
    "holdings", "ventures", "industries", "labs", "technologies", "software",
    "motors", "auto", "automotive", "dealership", "construction", "supply",
    "company", "enterprises", "institute", "school", "academy", "university",
    "college", "foundation", "society", "association", "council", "bank",
    "financial", "advisors", "media", "marketing", "design", "engineering",
})

_PERSON_SUFFIXES = frozenset({
    "jr", "sr", "ii", "iii", "iv", "md", "dds", "dmd", "phd", "esq", "cpa",
    "pmp", "rn", "np", "pa", "do",
})


def looks_like_person_name(name: str) -> bool:
    """True when a *company* name is really a person's name.

    Group-level contact sourcing sometimes files a person under their own name
    as the company ("Rick Jensen", "Ann Perry"). Those rows then go through
    domain discovery, which correctly scores `drrickjensen.com` as an excellent
    match -- because by string similarity it is one. The scoring is not wrong;
    it is being asked the wrong question. The result was law-firm and personal
    addresses landing on dealership contacts and needing a manual audit.

    Deliberately conservative: two or three alphabetic tokens, no legal suffix,
    no business word, no digits. A false positive here only costs a domain
    going to human review, but a false negative writes a junk domain that
    nothing downstream ever corrects.
    """
    raw = (name or "").strip()
    if not raw or any(ch.isdigit() for ch in raw):
        return False
    if _ENTITY_SUFFIX_RE.search(raw):
        return False
    tokens = [t for t in re.sub(r"[^a-z']+", " ", raw.lower()).split() if t]
    # Drop a trailing credential ("Al-Tarik Samuel, PMP") before counting.
    while tokens and tokens[-1] in _PERSON_SUFFIXES:
        tokens.pop()
    if not 2 <= len(tokens) <= 3:
        return False
    if any(t in _BUSINESS_WORDS for t in tokens):
        return False
    # A middle initial is a strong person signal; a single-letter token
    # anywhere else usually is not a name at all.
    if any(len(t) == 1 for t in (tokens[:1] + tokens[-1:])):
        return False
    return True


def strip_entity_suffix(name: str) -> str:
    """Strip a trailing legal-entity suffix so a quoted-vs-unquoted mismatch
    never costs a hit. Loops because "Company, Inc., LLC" has two."""
    cleaned = (name or "").strip()
    prev = None
    while cleaned and cleaned != prev:
        prev = cleaned
        cleaned = _ENTITY_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or (name or "").strip()


def build_discovery_query(company_name: str, mode: str = "email") -> str:
    """Never wraps the company name in quotes -- a quoted phrase match is
    strictly narrower than an unquoted one and buys nothing here (Serper
    still ranks exact matches highest without forcing them); quoting would
    only risk losing hits when the registered legal name carries words the
    site itself never uses."""
    base = strip_entity_suffix(company_name)
    if mode == "email":
        return f"{base} email"
    if mode == "alt_domain":
        return f"{base} @"
    raise ValueError(f"unknown discovery query mode: {mode}")


_WWW_PREFIX_RE = re.compile(r"^www\d*\.")


def _candidate_domain_from_link(link: str) -> str:
    # www2./www3. are as much a bare host prefix as www. is; leaving them on
    # stored www2.cortland.edu as a company domain in its own right.
    return _WWW_PREFIX_RE.sub("", urlparse(link).netloc.lower())


def extract_emails(serper_json: dict) -> list[dict[str, Any]]:
    """Extract email addresses from organic result snippets/titles.

    `is_free_provider` splits the two things a found email can be good for: a
    corporate-domain address both corroborates a domain candidate and is worth
    contacting, while a gmail/yahoo address corroborates nothing about the
    website but is frequently the only published contact a small business has
    -- so it is kept and stored, just barred from ranking (see
    classify_domains).
    """
    emails: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in serper_json.get("organic") or []:
        for text in (result.get("title", ""), result.get("snippet", "")):
            for match in EMAIL_RE.finditer(text or ""):
                email = match.group().lower()
                if email in seen:
                    continue
                seen.add(email)
                local, _, domain = email.partition("@")
                emails.append({
                    "email": email,
                    "is_role": local in ROLE_PREFIXES,
                    "is_free_provider": domain in SHARED_EMAIL_DOMAINS,
                    "source_url": result.get("link", ""),
                })
    return emails


def _is_word_subset(label: str, name_tokens: list[str]) -> bool:
    """True when `label` is exactly an in-order subset of the name's words,
    concatenated -- "eastcobbnursing" from ["east","cobb","center","for",
    "nursing","and","healing"].

    Requires the label to be consumed EXACTLY (no leftover characters) and at
    least two whole words to participate. Both conditions matter: without the
    exact-consumption rule "eastcobbsomethingelse" would pass, and without the
    two-word rule any domain starting with a single generic word ("living",
    "health", "care") would match half this dataset.
    """
    if not label or len(name_tokens) < 2:
        return False
    pos = 0
    used = 0
    for word in name_tokens:
        if word and label.startswith(word, pos):
            pos += len(word)
            used += 1
    return pos == len(label) and used >= 2


_APOSTROPHE_RE = re.compile(r"['’ʼ]")


def _name_tokens(name: str) -> list[str]:
    """Lowercase word tokens, with possessive apostrophes CLOSED UP rather than
    treated as separators.

    "Children's Healthcare of Atlanta" split on every non-alphanumeric gives
    ["children", "s", "healthcare", "of", "atlanta"], whose initials are
    "cshoa" -- so choa.org scored 0 and a 13-lead health system was recorded as
    having no findable domain. Closing the apostrophe gives "childrens" and the
    acronym the domain actually uses. Same fix carries St. Joseph's/Candler ->
    sjc, which sjchs.org then matches as an acronym prefix.
    """
    collapsed = _APOSTROPHE_RE.sub("", (name or "").lower())
    return [t for t in re.sub(r"[^a-z0-9]+", " ", collapsed).split() if t]


def _trigrams(text: str) -> set[str]:
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def score_domain_match(company_name: str, domain: str) -> tuple[int, str]:
    """Score how strongly `domain` looks like `company_name`'s own domain.

    Returns (score, reason) -- the reason travels into the stored observation
    so a bad pick is explainable months later without re-spending a credit.

    Scores against BOTH name forms on purpose. pipeline_utils.
    normalize_company_name() strips generic business words (senior, care,
    solutions, group, ...) which is right for dedup but wrong here: those words
    are routinely part of the actual brand *and* the actual domain. "Amada
    Senior Care North Atlanta" normalizes to "amada north atlanta", which can
    never match amadaseniorcare.com by token overlap or containment -- that
    exact case scored the correct domain 0 while a directory site won. The raw
    collapsed form ("amadaseniorcarenorthatlanta") does match, as a prefix.
    """
    # Score the REGISTRABLE domain's label, not the leftmost one. On
    # health.usnews.com the leftmost label is "health", which is a substring
    # of "autumn breeze healthcare" -- that scored 15 and handed a U.S. News
    # directory page to a senior-living company. The brand lives in the
    # registrable label ("usnews"), which matches nothing and is correctly
    # rejected.
    registrable = company_registrable_domain((domain or "").lower()) or (domain or "").lower()
    label = re.sub(r"[^a-z0-9]", "", registrable.split(".", 1)[0])
    if not label:
        return (0, "no_domain_label")

    raw_tokens = _name_tokens(company_name)
    collapsed_raw = "".join(raw_tokens)
    name_norm = normalize_company_name(company_name)
    collapsed_norm = re.sub(r"[^a-z0-9]", "", name_norm)
    norm_tokens = set(name_norm.split())

    if not collapsed_raw:
        return (0, "no_company_name")

    if label == collapsed_raw or (collapsed_norm and label == collapsed_norm):
        return (20, "exact")
    # The franchise/branch case: brand domain, name carries a location suffix.
    if collapsed_raw.startswith(label) and len(label) >= 4:
        return (18, "domain_is_name_prefix")
    if collapsed_norm and len(collapsed_norm) >= 4 and collapsed_norm in label:
        return (15, "norm_name_in_domain")
    if len(label) >= 4 and label in collapsed_raw:
        return (15, "domain_in_raw_name")

    # The domain keeps an in-order SUBSET of the name's words and drops the
    # rest: "East Cobb Center for Nursing and Healing" -> eastcobbnursing.
    # Extremely common for facilities whose registered name is a long
    # descriptive phrase. Requires 2+ whole words matched end to end, so a
    # single generic word ("living", "health") can never trigger it.
    if _is_word_subset(label, raw_tokens):
        return (14, "word_subset")

    # Acronym: "American Health Facilities" -> ahf. Only meaningful at 3+
    # letters; 2-letter acronyms collide with far too much.
    #
    # Both conventions occur and neither dominates, so both are tried:
    # "Village Park Senior Living, LLC" -> vpsl.com drops the entity suffix,
    # "Refrigerated Warehousing Inc" -> rwizero.com keeps it (RWI).
    stripped_tokens = _name_tokens(strip_entity_suffix(company_name))
    acronyms = {
        "".join(t[0] for t in raw_tokens),
        "".join(t[0] for t in stripped_tokens),
    }
    acronyms = {a for a in acronyms if len(a) >= 3}
    if label in acronyms:
        return (12, "acronym")
    # ...and the same acronym carrying a suffix the name never shows:
    # "Premier Senior Living" -> pslgroupllc.com. Kept out of
    # STRICT_MATCH_REASONS -- a 3-letter prefix is too thin to write a domain
    # on with no search result backing it.
    if any(label.startswith(a) for a in acronyms):
        return (12, "acronym_prefix")

    shared = _trigrams(label) & _trigrams(collapsed_raw)
    union = _trigrams(label) | _trigrams(collapsed_raw)
    jaccard = len(shared) / len(union) if union else 0.0
    if jaccard >= 0.6:
        return (10, f"trigram_{jaccard:.2f}")
    if jaccard >= 0.4:
        return (6, f"trigram_{jaccard:.2f}")

    label_tokens = set(re.sub(r"[^a-z0-9]+", " ", registrable.split(".", 1)[0]).split())
    overlap = label_tokens & norm_tokens
    if overlap:
        return (3 * len(overlap), f"token_overlap_{len(overlap)}")

    return (0, "no_match")


def extract_domains(serper_json: dict, company_name: str) -> list[dict[str, Any]]:
    """Scored candidate domains from the knowledge graph (free, near-certain
    when present) plus organic results. Rejects aggregator/directory/generic
    domains via enrich.validate_company_domain() rather than maintaining a
    second exclusion list.

    Name similarity comes from score_domain_match(); this function adds the two
    signals that only exist across the *whole* result set:

    - consensus: how many organic slots the domain holds. A company that owns
      the query holds several (amadaseniorcare.com held 5 of 10); a directory
      that merely lists it holds exactly one.
    - position: rank 1-3 is worth more than rank 10, which is where the
      directory sites that used to win were sitting.

    Neither can rescue a zero name match on its own -- both are capped well
    below the containment tiers, so a well-ranked stranger still loses to a
    weakly-ranked name match.
    """
    lowered_name = (company_name or "").lower()
    candidates: dict[str, dict[str, Any]] = {}

    def _bump(domain: str, score: int, reason: str) -> dict[str, Any]:
        entry = candidates.setdefault(
            domain,
            {"domain": domain, "score": 0, "reason": reason, "hits": 0, "best_position": None},
        )
        if score > entry["score"]:
            entry["score"] = score
            entry["reason"] = reason
        return entry

    kg_website = (serper_json.get("knowledgeGraph") or {}).get("website") or ""
    if kg_website:
        candidate = _candidate_domain_from_link(kg_website)
        cleaned, _warning = enrich.validate_company_domain(candidate, company_name)
        if cleaned:
            _bump(cleaned, 20, "knowledge_graph")

    for position, result in enumerate(serper_json.get("organic") or [], start=1):
        candidate = _candidate_domain_from_link(result.get("link", "") or "")
        if not candidate:
            continue
        cleaned, _warning = enrich.validate_company_domain(candidate, company_name)
        if not cleaned:
            continue

        name_score, reason = score_domain_match(company_name, cleaned)
        # A snippet/title mention corroborates a name match; it must never
        # create one. Every directory page that lists a company mentions it by
        # name, which is precisely how carelistings.com (score 2, position 10)
        # beat the brand's own domain sitting at positions 1-4.
        if name_score <= 0:
            continue
        snippet = (result.get("snippet") or "").lower()
        title = (result.get("title") or "").lower()
        if lowered_name and lowered_name in snippet:
            name_score += 2
        if lowered_name and lowered_name in title:
            name_score += 1

        entry = _bump(cleaned, name_score, reason)
        entry["hits"] += 1
        if entry["best_position"] is None or position < entry["best_position"]:
            entry["best_position"] = position

    ranked: list[dict[str, Any]] = []
    for entry in candidates.values():
        bonus = 0
        if entry["hits"] > 1:
            bonus += 2 * min(entry["hits"], 4)
        pos = entry["best_position"]
        if pos is not None:
            bonus += 3 if pos <= 3 else 1
        # A domain nothing matched by name is not promoted into contention by
        # ranking alone -- that is exactly how carelistings.com won before.
        if entry["score"] <= 0:
            continue
        entry["score"] += bonus
        if bonus:
            entry["reason"] = f"{entry['reason']}+consensus{entry['hits']}"
        ranked.append(entry)

    ranked.sort(key=lambda e: (-e["score"], e["best_position"] or 99, e["domain"]))
    return ranked


def classify_domains(
    scored_domains: list[dict[str, Any]],
    emails: list[dict[str, Any]],
    company_name: str = "",
) -> list[dict[str, Any]]:
    """Best-first candidate list, ranked so a domain with a real attached
    email always outranks a same-scored domain matched by name alone -- the
    email-finding waterfall (waterfall.run_find_with_domain_fallback) tries
    these in order, so putting the proven domain first also saves
    trykitt/icypeas credits downstream, not just Serper's.

    An email-derived domain must clear the SAME bar as a link-derived one:
    constants.SHARED_EMAIL_DOMAINS (~90 free providers, vs the 8 in
    enrich.validate_company_domain()'s inline set) plus validate_company_domain
    itself. Without this, a business whose only published contact was
    agapeseniorsolutions@gmail.com got gmail.com as its company domain --
    has_email=True sorts ahead of score, so a score-0 free provider beat a
    score-18 real match. The email itself is not discarded; it travels in
    extract_emails() output and lands as a public_email identity.
    """
    email_domains = {
        e["email"].split("@", 1)[1] for e in emails
        if "@" in e["email"] and not e.get("is_free_provider")
    }
    email_registrable = {company_registrable_domain(d) for d in email_domains}

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in scored_domains:
        domain = entry["domain"]
        has_email = domain in email_domains or company_registrable_domain(domain) in email_registrable
        out.append({
            "domain": domain,
            "score": entry["score"],
            "reason": entry.get("reason", ""),
            "has_email": has_email,
        })
        seen.add(domain)
    for domain in sorted(email_domains):
        if domain in seen:
            continue
        cleaned, _warning = enrich.validate_company_domain(domain, "")
        if not cleaned or cleaned in seen:
            continue
        # Score it like any other candidate. Hardcoding 0 here meant an
        # address merely *mentioned* on the page -- a partner clinic, a
        # billing vendor -- became the company's domain unchallenged, because
        # has_email sorts ahead of score and lifted a 0 to 0.85 confidence.
        # "Hightop Health" -> psychatlanta.com came in exactly this way.
        e_score, e_reason = score_domain_match(company_name, cleaned)
        out.append({
            "domain": cleaned,
            "score": e_score,
            "reason": f"email_derived_{e_reason}",
            "has_email": True,
        })
        seen.add(cleaned)

    # Score first, attached-email as the TIEBREAK -- which is what this
    # function's contract has always said ("outranks a *same-scored* domain").
    # Sorting on (has_email, score) instead made a proven mailbox beat any
    # score difference at all, so a score-0 address scraped off the page
    # displaced the company's own domain: psychatlanta.com (0) beat
    # hightophealth.com (34), email4pr.com (0) beat precioushospice.com (34).
    # Across 213 real observations it picked the wrong winner 48 times.
    out.sort(key=lambda d: (d["score"], d["has_email"]), reverse=True)
    return out


def summarize_source(emails: list[dict[str, Any]], ranked_domains: list[dict[str, Any]]) -> str:
    e_count = len(emails)
    u_count = len(ranked_domains)
    if e_count and u_count:
        if e_count == 1 and u_count == 1:
            return f"email+url_single ({e_count}e, {u_count}u)"
        return f"email+url ({e_count}e, {u_count}u)"
    if e_count:
        return f"email_only ({e_count}e)"
    if u_count:
        return f"url_only ({u_count}u)"
    return "no_signals"


def compute_confidence(ranked_domains: list[dict[str, Any]]) -> float:
    if not ranked_domains:
        return 0.0
    top = ranked_domains[0]
    if top["has_email"]:
        # An attached email proves the domain receives mail. It proves nothing
        # about WHOSE domain it is -- a partner clinic or billing vendor
        # mentioned on the page has a working address too. With no name
        # relation at all this stays below MIN_ATTACH_CONFIDENCE, so it is
        # recorded for review instead of written onto the company.
        if top["score"] >= 15:
            return 0.95
        if top["score"] >= 5:
            return 0.85
        if top["score"] > 0:
            return 0.60
        return 0.35
    if top["score"] >= 15:
        return 0.70
    if top["score"] >= 5:
        return 0.40
    return 0.20


def _recent_domain_lookup(
    conn: sqlite3.Connection, company_id: int, *, force: bool, window: str = "",
) -> Optional[dict[str, Any]]:
    """Org-wide cache: any workspace's prior search for this company_id
    counts, so the same company never gets re-searched just because a second
    campaign/workspace also wants its domain.

    Error observations are NOT a cache hit. An error is the absence of an
    answer -- "all 3 key(s) for SERPER_API_KEY failed" says nothing about the
    company -- but without this filter it satisfied the lookup and locked the
    company out of re-search for the whole freshness window. One credit-
    exhausted run on 2026-08-03 left 1,023 domainless companies unreachable
    that way, and the batch summary reported them as `not_found`, which sent
    the investigation after the name scorer instead of the dead API key.
    A genuine `not_found` still counts: the search ran and answered.
    """
    if force:
        return None
    window = window or f"-{FRESHNESS_DAYS} days"
    row = conn.execute(
        f"""SELECT o.observed_at, o.domain, o.source_detail, o.metadata_json
            FROM lead_provider_observations o
            JOIN leads l ON l.id = o.lead_id
            WHERE l.company_id = ? AND o.kind = ? AND o.provider = 'serper'
              AND o.status != 'error'
              AND o.observed_at >= datetime('now', ?)
            ORDER BY o.observed_at DESC LIMIT 1""",
        (company_id, KIND_DOMAIN_LOOKUP, window),
    ).fetchone()
    return dict(row) if row else None


def _attach_domain(
    conn: sqlite3.Connection, company_id: int, domain: str, *, role: Optional[str], source: str,
) -> dict[str, Any]:
    """Write a discovered domain into company_identities (INSERT OR IGNORE,
    same discipline as ensure_company()/merge_companies() elsewhere in
    pipeline.py) and backfill companies.domain only when it's still NULL --
    never overwrites an existing primary domain."""
    domain = normalize_company_domain(domain)
    if not domain:
        return {"attached": False, "reason": "invalid_domain"}

    # Sharing a domain is legal now: the UNIQUE constraint on
    # company_identities includes company_id, so this row lands regardless of
    # who else already records the same domain. That deletes the old
    # "domain_owned_by_other_company" branch, which used to queue a
    # domain_discovery_shared_domain merge candidate on every collision --
    # 106 of the 107 pending candidates, all brand portfolios (hilton.com
    # across 22 properties), all answered "no, keep separate", all regenerated
    # on the next find-domains run. Two companies sharing a mail domain is a
    # fact about hospitality and franchising, not evidence they are one
    # company. The signal that DOES mean "possibly the same company" is two
    # rows claiming the same *identifying* domain -- companies.domain -- and
    # that check is still below.
    conn.execute(
        """INSERT OR IGNORE INTO company_identities
               (org_id, company_id, identity_type, identity_value_normalized, role, source)
           VALUES (?, ?, 'domain', ?, ?, ?)""",
        (DEFAULT_ORG_ID, company_id, domain, role, source),
    )
    owner = conn.execute(
        """SELECT company_id, role FROM company_identities
           WHERE org_id = ? AND company_id = ? AND identity_type = 'domain'
             AND identity_value_normalized = ?""",
        (DEFAULT_ORG_ID, company_id, domain),
    ).fetchone()
    if owner is None:
        # The INSERT OR IGNORE didn't land and no row exists for this
        # (company, domain) -- nothing to report but the failure itself.
        return {"attached": False, "reason": "identity_insert_failed"}
    # Who else records this domain? Sharing is fine on its own -- but if one of
    # them is NAMED like this company, that is the duplicate-row signal the old
    # ownership guard used to catch, and dropping it wholesale would let "Great
    # Oaks Senior Living" and "Great Oaks Assisted Living" both quietly claim
    # greatoaks.net with nothing surfaced for review.
    others = conn.execute(
        """SELECT ci.company_id, c.name FROM company_identities ci
           JOIN companies c ON c.id = ci.company_id
           WHERE ci.org_id = ? AND ci.identity_type = 'domain'
             AND ci.identity_value_normalized = ? AND ci.company_id != ?""",
        (DEFAULT_ORG_ID, domain, company_id),
    ).fetchall()
    shared_with = len(others)
    this_name = conn.execute(
        "SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()
    duplicate_of = None
    for other in others:
        if names_look_like_same_company(this_name["name"] if this_name else "", other["name"]):
            duplicate_of = other["company_id"]
            _queue_merge_candidate(
                conn,
                existing_company_id=other["company_id"],
                candidate_company_id=company_id,
                reason="domain_discovery_shared_domain",
                payload={"domain": domain, "source": source,
                         "discovered_for_company_id": company_id},
            )
    if role == "email" and not owner["role"]:
        conn.execute(
            """UPDATE company_identities SET role = 'email'
               WHERE org_id = ? AND company_id = ? AND identity_type = 'domain'
                 AND identity_value_normalized = ?""",
            (DEFAULT_ORG_ID, company_id, domain),
        )
    if duplicate_of is not None:
        # We just told a human these two rows may be one company. Promoting the
        # domain to this row's IDENTIFYING domain in the meantime would presume
        # the answer, and it is the promotion -- not the identity row -- that
        # makes downstream code (public-email classification) file facts
        # against this row. Leave companies.domain alone until merge review
        # decides. The identity row above still stands, so the discovery is not
        # lost and email finding can already use the domain.
        return {"attached": True, "domain": domain, "primary_backfilled": False,
                "reason": "possible_duplicate_company", "other_company_id": duplicate_of,
                "merge_candidate_logged": True, "shared_with_companies": shared_with}
    company_row = conn.execute("SELECT domain FROM companies WHERE id = ?", (company_id,)).fetchone()
    if company_row is not None and not company_row["domain"]:
        # companies.domain is UNIQUE. The identity-ownership check above only
        # sees domains that have a company_identities row, and plenty of
        # companies carry a primary domain without one (imported/legacy rows),
        # so it does not cover this case -- the bare UPDATE used to raise
        # sqlite3.IntegrityError and kill the batch on the first duplicate.
        # Two company rows resolving to one domain is a merge candidate, which
        # is ensure_company()/company merge-review's job; surface it here
        # rather than guess. The identity row written above still stands, so
        # the discovery is not lost.
        clash = conn.execute(
            "SELECT id FROM companies WHERE domain = ? AND id != ?", (domain, company_id),
        ).fetchone()
        if clash is not None:
            # Two company rows resolving to one domain is the textbook signal
            # that they are the same company recorded twice (this dataset has
            # it constantly: "rockefeller capital management" and "rockco.com"
            # are one company as two rows). Queue it for human review rather
            # than guessing -- _log_company_merge_candidate is the existing
            # path for exactly this, feeding `pipeline.py company
            # merge-review`. Never auto-merged.
            _queue_merge_candidate(
                conn,
                existing_company_id=clash["id"],
                candidate_company_id=company_id,
                reason="domain_discovery_shared_domain",
                payload={"domain": domain, "source": source,
                         "discovered_for_company_id": company_id},
            )
            return {
                "attached": True,
                "domain": domain,
                "primary_backfilled": False,
                "reason": "domain_is_primary_for_other_company",
                "other_company_id": clash["id"],
                "merge_candidate_logged": True,
            }
        conn.execute(
            "UPDATE companies SET domain = ?, updated_at = datetime('now') WHERE id = ?",
            (domain, company_id),
        )
        return {"attached": True, "domain": domain, "primary_backfilled": True,
                "shared_with_companies": shared_with}
    return {"attached": True, "domain": domain, "shared_with_companies": shared_with}


# Above this share of the shorter name's words, two companies that resolve to
# the same domain are treated as one company recorded twice. Calibrated on the
# real pairs in this dataset rather than picked round:
#
#   duplicates (queue for review)          overlap
#     Great Oaks Senior / Assisted Living    0.75
#     Modern Storefront LLC / Group          1.00
#     Emory Conference Center Hotel / The …  1.00
#   brand portfolios (never a merge)
#     Grand Hyatt Tampa Bay / Hyatt Regency  0.25
#     Hilton Atlanta Downtown / Garden Inn   0.33
_SAME_COMPANY_NAME_OVERLAP = 0.6


def _name_words(name: str) -> set[str]:
    """Content words of a company name, entity suffix removed.

    strip_entity_suffix first so "Modern Storefront LLC" and "Modern Storefront
    Group" compare on what actually distinguishes them.
    """
    cleaned = re.sub(r"[^a-z0-9\s]", " ", strip_entity_suffix(name or "").lower())
    return {w for w in cleaned.split() if w}


def names_look_like_same_company(a: str, b: str) -> bool:
    """Do these two company names plausibly describe ONE company?

    The discriminator for what a shared domain means. Two rows on hilton.com
    are usually two different hotels; two rows on greatoaks.net are usually one
    nursing home entered twice. Sharing the domain says nothing either way --
    the NAMES do.

    Overlap coefficient (intersection over the SHORTER name) rather than
    Jaccard, so "Emory Conference Center Hotel" and "The Emory Conference
    Center Hotel" score 1.0 instead of being penalized for the extra word.
    """
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= _SAME_COMPANY_NAME_OVERLAP


def duplicate_name_key(name: str) -> str:
    """Key for 'these two company rows are the same company'.

    Collapsed raw name with only the legal-entity suffix removed -- crucially
    NOT normalize_company_name(), which also strips Partners/Group/Company/
    Solutions. Those words distinguish real, different companies from each
    other ("Sterling Group" vs "Sterling Partners"), and collapsing them
    together here would hand one company another's domain.
    """
    return re.sub(r"[^a-z0-9]", "", strip_entity_suffix(name or "").lower())


def build_company_name_index(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """duplicate_name_key -> (company_id, domain), for every company that
    already has a domain. Built once per run: doing this lookup per company
    instead would be a full table scan each time, i.e. O(n^2) across a
    2,500-company workspace."""
    index: dict[str, tuple[int, str]] = {}
    for row in conn.execute(
        "SELECT id, name, domain FROM companies WHERE domain IS NOT NULL AND TRIM(domain) != ''",
    ).fetchall():
        key = duplicate_name_key(row["name"])
        if key and key not in index:
            index[key] = (row["id"], row["domain"])
    return index


def build_company_domain_label_index(
    conn: sqlite3.Connection,
) -> dict[str, tuple[int, str]]:
    """registrable domain label -> (company_id, domain), for domains already known.

    Companion to build_company_name_index, keyed the other way round. The
    duplicate-name index only finds siblings whose names collapse identically
    ("Acme Widgets, Inc." vs "Acme Widgets"); a brand that appears under a
    longer descriptive name is invisible to it. "Amedisys Home Health &
    Hospice" and "Amedisys" do not share a name key, so a sibling row already
    holding amedisys.com was never consulted and the company went to a paid
    search anyway -- three of the fourteen companies in the 2026-08-03 report
    were exactly this.
    """
    index: dict[str, tuple[int, str, str]] = {}
    for row in conn.execute(
        "SELECT id, name, domain FROM companies WHERE domain IS NOT NULL AND TRIM(domain) != ''",
    ).fetchall():
        registrable = company_registrable_domain((row["domain"] or "").lower()) or row["domain"]
        label = re.sub(r"[^a-z0-9]", "", str(registrable).split(".", 1)[0].lower())
        if len(label) >= 5 and label not in index:
            # The sibling's own name travels with it: matching on the domain
            # label alone is not enough to prove the two rows are the same
            # brand (see the guard in domain_from_local_evidence).
            index[label] = (row["id"], row["domain"], row["name"] or "")
    return index


def _brand_prefix_candidates(company_name: str) -> list[str]:
    """Leading-token concatenations of a company name, longest first.

    "Amedisys Home Health Hospice" -> amedisyshomehealth, amedisyshome,
    amedisys. Longest first so a more specific sibling wins over a shorter,
    more collidable one. Bounded by token count, so this is a handful of dict
    lookups rather than a scan of every known domain.
    """
    tokens = _name_tokens(strip_entity_suffix(company_name))
    out: list[str] = []
    for count in range(len(tokens), 0, -1):
        candidate = "".join(tokens[:count])
        if len(candidate) >= 5:
            out.append(candidate)
    return out


def domain_from_local_evidence(
    conn: sqlite3.Connection,
    company_id: int,
    company_name: str,
    *,
    name_index: Optional[dict[str, tuple[int, str]]] = None,
    domain_label_index: Optional[dict[str, tuple[int, str]]] = None,
) -> Optional[dict[str, Any]]:
    """A domain the DB already knows, found without spending a Serper credit.

    Runs before any query. At thousands of companies the cheapest credit is
    the one never spent, and a surprising share of "undomained" companies are
    only undomained in the `companies.domain` column -- the answer is already
    sitting in an identity row, in the email addresses of their own leads, or
    on a duplicate company row.

    Ordered most authoritative first. Returns None when the DB knows nothing,
    which is the only case that justifies a search.
    """
    # 1. An identity row already records a domain for this exact company.
    row = conn.execute(
        """SELECT identity_value_normalized AS domain FROM company_identities
           WHERE company_id = ? AND identity_type = 'domain'
           ORDER BY (role = 'email') DESC, id ASC LIMIT 1""",
        (company_id,),
    ).fetchone()
    if row and row["domain"]:
        return {"domain": row["domain"], "evidence": "company_identities"}

    # NOT an evidence source: the email domains of this company's own leads.
    # It looks compelling and is not. On LinkedIn-sourced data a lead's email
    # domain says where they work NOW, not which company row they are attached
    # to. Measured against 50 real companies here it was wrong 50/50 --
    # "Dragon Con, Inc" -> trellahealth.com, "CARROLL" -> cc.edu. Requiring
    # the domain to also match the company name does not rescue it either,
    # because normalize_company_name() strips Partners/Group/Company and the
    # leftover single word collides with unrelated organizations: "Regent
    # Partners" -> regent.edu, "Hanover Company" -> hanover.edu, "Artisan
    # Partners" -> artisan.co all pass a strict name check and are all wrong.
    # A wrong domain costs far more than the Serper credit it saves.

    # 2. A duplicate company row already resolved this exact company.
    #
    #    Keyed on the collapsed raw name (entity suffix stripped), NOT
    #    normalize_company_name(): that strips generic words, so "Sterling
    #    Group" and "Sterling Partners" both reduce to "sterling" and would
    #    hand one company the other's domain. "Acme Widgets, Inc." and "Acme
    #    Widgets" still match, which is the case this is for.
    name_key = duplicate_name_key(company_name)
    if name_key:
        if name_index is None:
            name_index = build_company_name_index(conn)
        hit = name_index.get(name_key)
        if hit is not None and hit[0] != company_id:
            # Both signals must agree: the name key says these rows are the
            # same company, AND the domain must look like that company. The
            # twin row's domain can itself be wrong -- this DB already
            # contains a company literally named "Dragon Con" carrying
            # domain trellahealth.com -- and without this check the lookup
            # faithfully propagates that error to every namesake it finds.
            _score, reason = score_domain_match(company_name, hit[1])
            if reason in STRICT_MATCH_REASONS:
                return {
                    "domain": hit[1],
                    "evidence": f"duplicate_company({hit[0]},{reason})",
                    "duplicate_of_company_id": hit[0],
                }

    # 3. A SIBLING BRAND row already resolved this brand under a shorter name.
    #
    #    Step 2 only fires when two names collapse identically, so a brand
    #    filed once as "Amedisys" and again as "Amedisys Home Health & Hospice"
    #    was searched and paid for twice. Here the known domain's label is
    #    matched against the leading tokens of this company's name instead.
    #
    #    Guarded the same way step 2 is, and for the same reason: the match is
    #    only accepted when score_domain_match independently agrees at a STRICT
    #    tier. A shared leading word is not on its own evidence of a shared
    #    company -- "Sterling Group" and "Sterling Partners" are different
    #    businesses -- so the name-side check has to hold too.
    if looks_like_person_name(company_name):
        # A person-shaped company name matching a personal domain is the exact
        # failure mode find-domains already refuses to auto-attach for. Do not
        # let the free path do what the paid path is forbidden to.
        return None
    if domain_label_index is None:
        domain_label_index = build_company_domain_label_index(conn)
    collapsed_self = duplicate_name_key(company_name)
    for candidate in _brand_prefix_candidates(company_name):
        hit = domain_label_index.get(candidate)
        if hit is None or hit[0] == company_id:
            continue
        sibling_id, sibling_domain, sibling_name = hit
        # One name must be a leading prefix of the other: the two rows are the
        # same brand at different levels of specificity ("DaVita" /
        # "DaVita Kidney Care", "Amedisys" / "Amedisys Home Health & Hospice").
        # Direction does not matter -- either can be the row that happens to
        # hold the domain.
        #
        # Matching on the shared domain label alone is NOT sufficient:
        # "Sterling Group" and "Sterling Partners" would both find a sibling
        # holding sterling.com, and score_domain_match would call it
        # domain_is_name_prefix for both -- because it is, for the wrong
        # company. Neither name is a prefix of the other, so this rejects it.
        #
        # Exception: some company rows are named after their own domain
        # ("enhabit.com"). There is no independent name evidence to check in
        # that case, and demanding it would reject the clearest matches there
        # are -- the row's name IS the domain. The label match plus the STRICT
        # score below is the whole of the evidence for those.
        collapsed_sibling = duplicate_name_key(sibling_name)
        sibling_is_domain_named = bool(
            sibling_name and validate_domain(sibling_name.strip().lower()))
        if not sibling_is_domain_named and (
            not collapsed_sibling or not (
                collapsed_self.startswith(collapsed_sibling)
                or collapsed_sibling.startswith(collapsed_self)
            )
        ):
            continue
        _score, reason = score_domain_match(company_name, sibling_domain)
        if reason in STRICT_MATCH_REASONS:
            return {
                "domain": sibling_domain,
                "evidence": f"sibling_brand({sibling_id},{reason})",
                "duplicate_of_company_id": sibling_id,
            }
    return None


def _queue_merge_candidate(
    conn: sqlite3.Connection,
    *,
    existing_company_id: int,
    candidate_company_id: int,
    reason: str,
    payload: dict[str, Any],
) -> bool:
    """Log a company pair for human review, once.

    _log_company_merge_candidate() mints a new id per call with no dedup, and
    this pair can be reached twice in one company (the duplicate-name lookup
    and the shared-domain backfill guard both see it) and again on every
    subsequent run. Unchecked, a 3,000-company pass would bury the review
    queue in thousands of rows describing a few hundred real merges.
    """
    already = conn.execute(
        """SELECT 1 FROM company_merge_candidates
           WHERE status = 'pending' AND existing_company_id = ? AND candidate_company_id = ?""",
        (existing_company_id, candidate_company_id),
    ).fetchone()
    if already:
        return False
    from pipeline import _log_company_merge_candidate

    _log_company_merge_candidate(
        conn,
        existing_company_id=existing_company_id,
        candidate_company_id=candidate_company_id,
        reason=reason,
        payload=payload,
    )
    return True


def _attach_public_emails(
    conn: sqlite3.Connection, company_id: int, emails: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist discovered public emails as company_identities rows
    (identity_type='public_email').

    Not company_personalization: that table's PK is (company_id, field_name), so
    it holds exactly one value per field -- multiple emails would have to be
    comma-jammed into one string, losing per-email provenance and making
    per-email verification impossible. company_identities is one row per value
    and already carries role/label/verified_mx, all of which are in
    sync_contract.SYNCED_COLUMNS["company_identities"], so these round-trip the
    relay with no schema work on either side.

    These stay verifiable through the existing flow: bounces.verify_email()
    takes an `email_override`, so a public email is checked against the
    company's rep lead and lands as an ordinary kind='email_verification'
    observation. `verified_mx` here is only the local cache of that answer.
    """
    attached: list[dict[str, Any]] = []
    for entry in emails:
        email = (entry.get("email") or "").strip().lower()
        if "@" not in email:
            continue
        # Only addresses we can actually stand behind become identities.
        # 'placeholder' reaches nobody; 'off_domain' belongs to a different
        # company and would be a wrong contact filed under this one. Both stay
        # in the observation's public_emails for reference.
        role = entry.get("role")
        if role not in ("corporate", "free_provider"):
            continue
        conn.execute(
            """INSERT OR IGNORE INTO company_identities
                   (org_id, company_id, identity_type, identity_value_normalized, role, label, source)
               VALUES (?, ?, 'public_email', ?, ?, ?, ?)""",
            (DEFAULT_ORG_ID, company_id, email, role,
             (entry.get("source_url") or "")[:500] or None, "serper_domain_discovery"),
        )
        owner = conn.execute(
            """SELECT company_id FROM company_identities
               WHERE org_id = ? AND identity_type = 'public_email' AND identity_value_normalized = ?""",
            (DEFAULT_ORG_ID, email),
        ).fetchone()
        # Same discipline as _attach_domain(): a value already claimed by
        # another company is surfaced, never silently re-pointed.
        if owner is not None and owner["company_id"] == company_id:
            attached.append({"email": email, "role": role})
    return attached


DISCOVERY_SOURCES = ("serper_domain_discovery", "local_evidence")


def audit_attached_domains(conn: sqlite3.Connection) -> dict[str, Any]:
    """Re-score everything this feature has ever written, against current logic.

    Exists because four separate defects this feature shipped were caught by
    looking at real attached results, not by any test: a directory subdomain
    (health.usnews.com), a free provider (gmail.com), an unrelated address's
    domain (psychatlanta.com), and a heuristic that was wrong 50/50. Each was
    obvious the moment its output was scored; none was visible from the code.

    Read-only and free. Run it after every batch -- it also re-checks rows
    written by OLDER versions, which is the only way a scoring fix reaches
    data that is already on disk.
    """
    findings: list[dict[str, Any]] = []
    placeholders = ",".join("?" * len(DISCOVERY_SOURCES))

    domain_rows = conn.execute(
        f"""SELECT ci.company_id, c.name AS company_name, c.domain AS primary_domain,
                   ci.identity_value_normalized AS domain, ci.source
            FROM company_identities ci JOIN companies c ON c.id = ci.company_id
            WHERE ci.identity_type = 'domain' AND ci.source IN ({placeholders})
            ORDER BY c.name""",
        DISCOVERY_SOURCES,
    ).fetchall()

    for row in domain_rows:
        domain, name = row["domain"], row["company_name"] or ""
        issues: list[str] = []
        if normalize_company_domain(domain) != domain:
            issues.append("malformed")
        if domain in SHARED_EMAIL_DOMAINS:
            issues.append("free_provider")
        cleaned, warning = enrich.validate_company_domain(domain, name)
        if not cleaned:
            issues.append("aggregator" if "ggregator" in warning else "rejected")
        score, reason = score_domain_match(name, domain)
        if score <= 0:
            issues.append("no_name_match")
        if issues:
            findings.append({
                "kind": "domain",
                "company_id": row["company_id"],
                "company_name": name,
                "value": domain,
                "is_primary": row["primary_domain"] == domain,
                "source": row["source"],
                "score": score,
                "reason": reason,
                "issues": issues,
            })

    # A company legitimately owns several domains -- that is what
    # company_identities exists for -- and companies.domain is frequently NULL
    # even when we know one, because the clash guard declines to backfill a
    # domain another company row already claims. Judging an address against
    # companies.domain alone therefore flags info@kippatl.org as belonging to
    # somebody else. Compare against every domain we know for the company.
    known_domains: dict[int, set[str]] = {}
    for row in conn.execute(
        """SELECT company_id, identity_value_normalized AS domain FROM company_identities
           WHERE identity_type = 'domain'""",
    ).fetchall():
        reg = company_registrable_domain(row["domain"])
        if reg:
            known_domains.setdefault(row["company_id"], set()).add(reg)
    for row in conn.execute(
        "SELECT id, domain FROM companies WHERE domain IS NOT NULL AND TRIM(domain) != ''",
    ).fetchall():
        reg = company_registrable_domain(row["domain"])
        if reg:
            known_domains.setdefault(row["id"], set()).add(reg)

    email_rows = conn.execute(
        """SELECT ci.company_id, c.name AS company_name,
                  ci.identity_value_normalized AS email, ci.role
           FROM company_identities ci JOIN companies c ON c.id = ci.company_id
           WHERE ci.identity_type = 'public_email' AND ci.source = 'serper_domain_discovery'
           ORDER BY c.name""",
    ).fetchall()

    for row in email_rows:
        email = row["email"]
        owned = known_domains.get(row["company_id"], set())
        email_reg = company_registrable_domain(email.partition("@")[2])
        if is_placeholder_email(email):
            expected = "placeholder"
        elif email.partition("@")[2] in SHARED_EMAIL_DOMAINS:
            expected = "free_provider"
        elif email_reg and email_reg in owned:
            expected = "corporate"
        else:
            expected = "off_domain"
        # Only the two trustworthy classes should ever have been stored; an
        # older build's looser rules can leave the others behind.
        if expected not in ("corporate", "free_provider"):
            findings.append({
                "kind": "public_email",
                "company_id": row["company_id"],
                "company_name": row["company_name"] or "",
                "value": email,
                "stored_role": row["role"],
                "issues": [expected],
            })

    by_issue: dict[str, int] = {}
    for f in findings:
        for i in f["issues"]:
            by_issue[i] = by_issue.get(i, 0) + 1

    return {
        "status": "ok",
        "domains_checked": len(domain_rows),
        "public_emails_checked": len(email_rows),
        "clean": len(domain_rows) + len(email_rows) - len(findings),
        "suspect": len(findings),
        "by_issue": by_issue,
        "findings": findings,
    }


def export_scoring_corpus(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every stored domain_lookup as a (company_name, candidates) record, for
    measuring a scoring change against real data before shipping it.

    Deliberately NOT committed anywhere: these are live customer company
    names, and this repo is public. The repo's fixtures carry the same
    matching SHAPES under synthetic names; this exists so a rule can be scored
    against the real distribution first -- which is the step whose absence let
    every scoring bug in this module reach production. Costs nothing: it reads
    observations already paid for.
    """
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        """SELECT metadata_json FROM lead_provider_observations
           WHERE kind = ? AND metadata_json IS NOT NULL""",
        (KIND_DOMAIN_LOOKUP,),
    ).fetchall():
        try:
            meta = json.loads(row["metadata_json"])
        except (TypeError, ValueError):
            continue
        if not meta.get("ranked_domains"):
            continue
        out.append({
            "company_name": meta.get("company_name"),
            "found_domain": meta.get("found_domain"),
            "confidence": meta.get("confidence"),
            "candidates": [
                {"domain": d.get("domain"), "score": d.get("score"),
                 "reason": d.get("reason", ""), "has_email": d.get("has_email", False)}
                for d in meta["ranked_domains"]
            ],
        })
    return out


def run_company_domain_discovery(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    company_id: int,
    company_name: str,
    rep_lead_id: int,
    force: bool = False,
    retry_unresolved: bool = False,
    debug: bool = False,
    query_budget: Optional[int] = None,
    name_index: Optional[dict[str, tuple[int, str]]] = None,
    domain_label_index: Optional[dict[str, tuple[int, str]]] = None,
) -> dict[str, Any]:
    """Run the targeted waterfall for one company. Returns a status summary and
    never raises: a Serper/network failure is recorded as an 'error'
    observation and the company falls through to its no-result path, because
    this runs across thousands of companies in one batch and a single bad
    response must not cost the whole run (it previously did -- an uncaught
    ValueError from a free-tier `site:` rejection killed the batch mid-way).

    `query_budget` is the number of Serper calls this company may still spend,
    counted down by the caller across the batch; None means unmetered.
    """
    if is_non_company_name(company_name):
        return {"status": "skipped", "reason": "non_company_name"}

    # Free answers before paid ones: the DB frequently already knows this
    # domain (identity row, the company's own leads' email addresses, or a
    # duplicate company row), and a search would just re-derive it.
    if not force:
        local = domain_from_local_evidence(
            conn, company_id, company_name,
            name_index=name_index, domain_label_index=domain_label_index)
        if local is not None:
            # Two rows sharing a name AND a domain are one company; surface
            # the merge for review even though the domain itself is safe to
            # attach.
            twin = local.get("duplicate_of_company_id")
            if twin:
                _queue_merge_candidate(
                    conn,
                    existing_company_id=twin,
                    candidate_company_id=company_id,
                    reason="domain_discovery_duplicate_name",
                    payload={"domain": local["domain"], "company_name": company_name,
                             "evidence": local["evidence"]},
                )
            attach = _attach_domain(
                conn, company_id, local["domain"], role=None, source="local_evidence",
            )
            return {
                "status": "resolved_from_db",
                "domain": local["domain"],
                "evidence": local["evidence"],
                "confidence": 0.9,
                "queries_run": [],
                "attach": attach,
            }

    # retry_unresolved bypasses the freshness cache but NOT the free
    # pre-flight above: the point is to re-evaluate a company the scoring
    # could not resolve, under current logic, without re-targeting the ones
    # that already succeeded (which is what --force does).
    cached = _recent_domain_lookup(
        conn, company_id,
        force=force,
        window=f"-{RETRY_FRESHNESS_HOURS} hours" if retry_unresolved else "",
    )
    if cached is not None:
        return {"status": "cached", "domain": cached["domain"], "observed_at": cached["observed_at"]}

    # Budget is checked only AFTER the free paths above: an exhausted query
    # budget must never block a resolution that costs nothing.
    if query_budget is not None and query_budget < 1:
        return {"status": "budget_exhausted", "domain": None, "queries_run": []}

    search_cfg = dict(cfg)
    search_cfg["serper_num_results"] = max(int(cfg.get("serper_num_results", 10)), MIN_SEARCH_NUM_RESULTS)

    queries_run: list[int] = []

    def _run_query(query: str, query_num: int) -> dict[str, Any]:
        """One Serper call + its observation. Always returns a result dict --
        `error` is set instead of raising."""
        error: Optional[str] = None
        raw: dict[str, Any] = {}
        try:
            raw = enrich.serper_search(query, search_cfg) or {}
        except ValueError as exc:  # enrich wraps every HTTP/URL failure as ValueError
            error = str(exc)[:300]

        emails = extract_emails(raw)
        scored = extract_domains(raw, company_name)
        ranked = classify_domains(scored, emails, company_name)
        # The "q<n>" prefix is load-bearing, not decoration: compute_obs_uid()
        # hashes the content columns and metadata_json is NOT one of them, so
        # two queries for the same company that return the same domain in the
        # same wall-clock second (observed_at has second resolution) hash
        # identically and the second INSERT is a silent no-op. That would make
        # the stored history undercount credits actually spent -- the one thing
        # this log has to get right. source_detail IS hashed, so this keeps the
        # rows distinct.
        source_detail = f"q{query_num} " + ("error" if error else summarize_source(emails, ranked))
        confidence = compute_confidence(ranked)
        top_domain = ranked[0]["domain"] if ranked else None

        # Lean by default. metadata_json is a synced column
        # (sync_contract.SYNCED_COLUMNS["lead_provider_observations"]), so the
        # full ~5-8 KB Serper response used to cross the relay wire on every
        # lead_core push, for every company -- ~12-20 MB at workspace scale.
        # These summary fields are what debugging actually needs; the raw
        # response is opt-in via --debug.
        metadata: dict[str, Any] = {
            "company_name": company_name,
            "query": query,
            "query_num": query_num,
            "found_domain": top_domain,
            "confidence": confidence,
            "source": source_detail,
            "ranked_domains": ranked,
            "public_emails": emails,
            "organic_count": len(raw.get("organic") or []),
            "has_knowledge_graph": bool(raw.get("knowledgeGraph")),
            "top_links": [
                (r.get("link") or "") for r in (raw.get("organic") or [])[:3]
            ],
        }
        if error:
            metadata["error"] = error
        if debug:
            metadata["raw_serper"] = raw

        record_observation(
            conn, rep_lead_id,
            kind=KIND_DOMAIN_LOOKUP, origin=ORIGIN_ATTEMPT, provider="serper",
            status="error" if error else ("found" if top_domain else "not_found"),
            domain=top_domain,
            source_detail=source_detail,
            metadata_json=json.dumps(metadata),
        )
        queries_run.append(query_num)
        return {
            "ranked": ranked, "emails": emails, "confidence": confidence,
            "top_domain": top_domain, "error": error,
            "organic_count": len(raw.get("organic") or []),
        }

    def _budget_left() -> int:
        if query_budget is None:
            return 99
        return query_budget - len(queries_run)

    all_emails: dict[str, dict[str, Any]] = {}

    def _collect(result: dict[str, Any]) -> None:
        for e in result["emails"]:
            all_emails.setdefault(e["email"], e)

    def _finish(
        status: str, winner: Optional[dict[str, Any]], confidence: float, *,
        role: Optional[str] = None, error: Optional[str] = None,
    ) -> dict[str, Any]:
        """Single exit point. The domain is attached FIRST, then addresses are
        classified against whatever that attach actually established -- an
        address is only 'corporate' relative to a domain this company row
        genuinely owns.

        Order matters. Classifying against the merely top-ranked domain stored
        five greatoaks.net contacts on "Great Oaks Senior Living" while the
        domain itself went nowhere (the duplicate row "Great Oaks Assisted
        Living" already owned that identity), and stored
        intake@psychatlanta.com on a company whose domain was refused for low
        confidence. Free-provider addresses never depended on the domain and
        are kept regardless -- for many small businesses they are the only
        published contact there is.
        """
        out: dict[str, Any] = {
            "status": status,
            "domain": winner["domain"] if winner else None,
            "confidence": confidence,
            "queries_run": queries_run,
        }
        if error:
            out["error"] = error
        attached_domain: Optional[str] = None
        if winner is not None:
            # A company row that is really a person never auto-attaches. The
            # scorer will happily match "Rick Jensen" to drrickjensen.com --
            # correctly, as a string -- and the email finder then guesses
            # addresses at a personal domain for a dealership contact. Route it
            # to review instead; a human can tell in a second what no amount of
            # scoring can.
            if looks_like_person_name(company_name):
                out["status"] = "low_confidence"
                out["attach"] = {"attached": False, "reason": "person_shaped_company_name"}
                out["review_reason"] = (
                    f"{company_name!r} looks like a person, not a company; "
                    "a domain matching it is probably their personal site")
            elif confidence < MIN_ATTACH_CONFIDENCE:
                # Recorded and reviewable, but nothing is written to
                # companies.domain -- nothing downstream ever corrects a wrong one.
                out["status"] = "low_confidence"
                out["attach"] = {"attached": False, "reason": "below_confidence_floor"}
            else:
                attach = _attach_domain(
                    conn, company_id, winner["domain"], role=role,
                    source="serper_domain_discovery",
                )
                out["attach"] = attach
                # `attached` now means "recorded as a known domain for this
                # company", which a brand-portfolio sibling also gets. Filing
                # public emails needs the stronger claim -- that this row is
                # not currently under suspicion of being a duplicate of the
                # row that already has the domain. merge_candidate_logged is
                # exactly that suspicion, so it disqualifies: classifying
                # contacts against a row that may be about to be merged away
                # is how five greatoaks.net contacts ended up on a company
                # with no domain.
                if attach.get("attached") and not attach.get("merge_candidate_logged"):
                    attached_domain = attach.get("domain")

        emails = drop_truncated_duplicates(list(all_emails.values()))
        for e in emails:
            e["role"] = classify_public_email(e["email"], attached_domain)
        out["public_emails_attached"] = _attach_public_emails(conn, company_id, emails)
        return out

    q1 = _run_query(build_discovery_query(company_name, "email"), 1)
    _collect(q1)
    top = q1["ranked"][0] if q1["ranked"] else None

    if top is not None and top["has_email"]:
        return _finish("found", top, q1["confidence"], role="email")

    if top is not None:
        if _budget_left() < 1:
            return _finish("found_no_email", top, q1["confidence"])
        # Operator-free: `site:<domain> email OR contact` is rejected outright
        # by free Serper accounts ("Query pattern not allowed for free
        # accounts"), and a plain-text form asks the same question on any tier.
        q2 = _run_query(
            f'{strip_entity_suffix(company_name)} {top["domain"]} contact email', 2,
        )
        _collect(q2)
        promoted = next((d for d in q2["ranked"] if d["domain"] == top["domain"] and d["has_email"]), None)
        status = "found" if promoted else "found_no_email"
        return _finish(status, top, q1["confidence"], role="email" if promoted else None)

    # Query 3 only when query 1 came back with results that simply did not
    # match by name. Zero organic results (or an outright error) means the
    # company is not findable this way and a second generic query is a wasted
    # credit -- at thousands of companies that is the difference between one
    # and two credits on every dead end.
    #
    # These three exits used to collapse into one "not_found", which is the
    # single most expensive piece of dishonesty in this module: a batch that
    # died on exhausted Serper credits reported 878 companies as "no domain
    # found", and the resulting bug report went after the name scorer for a
    # day. They mean completely different things and now say so.
    if q1["error"]:
        return _finish("error", None, 0.0, error=q1["error"])
    if q1["organic_count"] == 0:
        return _finish("no_results", None, 0.0)
    if _budget_left() < 1:
        return _finish("budget_exhausted", None, 0.0)

    q3 = _run_query(build_discovery_query(company_name, "alt_domain"), 3)
    _collect(q3)
    best = q3["ranked"][0] if q3["ranked"] else None
    if best is not None:
        return _finish("found", best, q3["confidence"], role="email" if best["has_email"] else None)

    return _finish("not_found", None, 0.0)


# ── Claiming a public email as a lead's own address ──────────────────────────
# Serper already found these while resolving domains, so they cost nothing.
# Most are useless as a *person's* address -- role mailboxes reach the company,
# and the majority belong to a different employee -- but a minority follow a
# recognizable personal-address pattern built from this lead's own name, and
# those are free wins a domain-based provider will never surface (trykitt could
# not find Rebekah's address because it lives on Gmail).

# Local parts that address a function, never a person.
_ROLE_LOCALS = ROLE_PREFIXES | frozenset({
    "hr", "marketing", "service", "services", "enquiries", "enquiry", "inquiries",
    "reception", "frontdesk", "general", "mail", "email", "newsletter", "press",
    "media", "legal", "privacy", "compliance", "accounts", "accounting", "payroll",
    "reservations", "bookings", "orders", "shop", "store", "web", "webmaster",
    "postmaster", "noreply", "no-reply", "donotreply", "admissions", "referrals",
})

_NAME_NOISE = frozenset({
    "dba", "rn", "bsn", "msn", "mba", "msw", "dnp", "mn", "phn", "md", "do", "np",
    "pa", "lpn", "cna", "phd", "esq", "cpa", "jr", "sr", "ii", "iii", "iv",
    "cacts", "fhc", "lcsw", "ot", "pt", "rd", "chpn", "ccm", "clc",
    # A trailing credential steals the "last name" slot and silently breaks
    # every pattern: "Joanne Smith, CCSP" yielded last="ccsp", so jsmith@ did
    # not match its own owner.
    "ccsp", "cam", "cpm", "crx", "cfa", "cfp", "cpc", "chfp", "fache", "fache",
    "mha", "mph", "mpa", "ms", "ma", "bs", "ba", "aprn", "fnp", "pmp", "sphr",
    "phr", "cscp", "cpsm", "ccim", "sior", "leed", "ap", "reia", "gri", "abr",
})


def _person_tokens(full_name: str) -> tuple[str, str]:
    """(first, last) with credentials and honorifics removed. LinkedIn names
    routinely carry them -- "Xalicia Slater, MSW, FHC, CACTS"."""
    cleaned = re.sub(r"[^a-z ]+", " ", (full_name or "").lower())
    parts = [p for p in cleaned.split() if len(p) > 1 and p not in _NAME_NOISE]
    if not parts:
        return ("", "")
    return (parts[0], parts[-1] if len(parts) > 1 else "")


def match_public_email_to_lead(full_name: str, email: str) -> Optional[str]:
    """Name of the address pattern when `email` is plausibly THIS person's,
    else None.

    Deliberately pattern-exact rather than fuzzy: a substring test would
    accept asanders@ for "Alice Sanderson" and every other employee whose
    surname merely contains the lead's. Everything here is a full-local-part
    equality against a form built from the lead's own first/last name.
    """
    local = (email or "").partition("@")[0]
    key = re.sub(r"[^a-z0-9]", "", local.lower())
    if not key or key in _ROLE_LOCALS:
        return None
    first, last = _person_tokens(full_name)
    if not first:
        return None

    patterns: list[tuple[str, str]] = []
    if first and last:
        patterns += [
            (f"{first}{last}", "first_last"),
            (f"{last}{first}", "last_first"),
            (f"{first[0]}{last}", "finitial_last"),
            (f"{first[:2]}{last}", "f2_last"),
            (f"{first}{last[0]}", "first_linitial"),
            (f"{last}{first[0]}", "last_finitial"),
            (f"{first[0]}{last[0]}", "initials"),
        ]
    # A bare first or last name only when it is long enough to be
    # distinctive; "jo@" or "lee@" would match half a company.
    if len(first) >= 4:
        patterns.append((first, "first_only"))
    if len(last) >= 4:
        patterns.append((last, "last_only"))

    for candidate, label in patterns:
        if len(candidate) >= 3 and key == candidate:
            return label
    return None


def find_claimable_public_emails(
    conn: sqlite3.Connection,
    workspace_id: Optional[str] = None,
    *,
    tags: Optional[list] = None,
    include_free_providers: bool = False,
    report: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Leads with no email whose company has a public address matching their
    own name. Read-only.

    Pass a dict as `report` to have the drop-by-drop funnel written into it.
    Kept out of the return value so every existing caller is unaffected.

    Corporate-domain addresses only by default: a free-provider match
    (rebekah.<company>@gmail.com) is plausible but unverifiable, and a wrong
    email is worse than none -- it burns a send and can bounce.

    An address that matches MORE than one lead at the company is dropped
    rather than guessed at. "Jeremy Wise" and "Jessica Wise" both produce
    jewise@, and there is no way to tell from the string which one it is.
    """
    where = ["(l.email IS NULL OR TRIM(l.email) = '')"]
    params: list = []
    if workspace_id:
        where.append("wl.workspace_id = ?")
        params.append(workspace_id)
    if tags:
        where.append(
            f"""EXISTS (SELECT 1 FROM workspace_lead_tags t
                        WHERE t.workspace_id = wl.workspace_id AND t.lead_id = l.id
                          AND t.tag IN ({",".join("?" * len(tags))}))"""
        )
        params.extend(tags)

    rows = conn.execute(
        f"""SELECT DISTINCT l.id AS lead_id, l.name AS lead_name, l.company_id,
                   co.name AS company_name,
                   ci.identity_value_normalized AS email, ci.role, ci.label
            FROM workspace_leads wl
            JOIN leads l ON l.id = wl.lead_id
            JOIN companies co ON co.id = l.company_id
            JOIN company_identities ci
              ON ci.company_id = l.company_id AND ci.identity_type = 'public_email'
            WHERE {" AND ".join(where)}""",
        params,
    ).fetchall()

    # Every stage that drops a pair is counted. A bare "0 claimable" cannot
    # distinguish "no public emails have ever been scraped for these companies"
    # from "37 were scraped and every one is info@" -- opposite situations with
    # opposite next actions, and the command gave no way to tell them apart.
    funnel = {
        "leads_without_email_scanned": 0,
        "pairs_considered": len(rows),
        "companies_with_public_email": len({r["company_id"] for r in rows}),
        "rejected_free_provider": 0,
        "rejected_no_name_match": 0,
        "rejected_ambiguous": 0,
        "claimable": 0,
    }
    reject_samples: dict[str, list[dict]] = {}

    def _reject(reason: str, row) -> None:
        funnel[reason] += 1
        samples = reject_samples.setdefault(reason, [])
        if len(samples) < 5:
            samples.append({"lead_name": row["lead_name"], "email": row["email"],
                            "company_name": row["company_name"]})

    hits: list[dict[str, Any]] = []
    for row in rows:
        if row["role"] != "corporate" and not include_free_providers:
            _reject("rejected_free_provider", row)
            continue
        pattern = match_public_email_to_lead(row["lead_name"], row["email"])
        if not pattern:
            _reject("rejected_no_name_match", row)
            continue
        hits.append({
            "lead_id": row["lead_id"],
            "lead_name": row["lead_name"],
            "company_id": row["company_id"],
            "company_name": row["company_name"],
            "email": row["email"],
            "pattern": pattern,
            "role": row["role"],
            "source_url": row["label"],
        })

    # Drop any address claimed by more than one lead, and any lead matching
    # more than one address -- both are ambiguous, and a wrong address is
    # worse than none.
    from collections import Counter
    by_email = Counter(h["email"] for h in hits)
    by_lead = Counter(h["lead_id"] for h in hits)
    claimable = [h for h in hits if by_email[h["email"]] == 1 and by_lead[h["lead_id"]] == 1]
    funnel["rejected_ambiguous"] = len(hits) - len(claimable)
    funnel["claimable"] = len(claimable)
    if report is not None:
        report["funnel"] = funnel
        report["rejected_examples"] = reject_samples
    return claimable
