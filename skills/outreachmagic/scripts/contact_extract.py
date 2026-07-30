"""Turn a fetched staff page into name+title pairs, and score them against an ICP.

Pure functions over text. **No database, no network, no LLM** -- the same split
`serper_candidates.py` (extract) keeps from `serper_review.py` (persist), and for
the same reason: the extraction rules have to be testable against a fixture
without a key, a connection, or a credit.

Three functions, in the order the pipeline calls them:

  * `regex_pass(markdown)` -- the structural pass. Deliberately knows nothing
    about any particular ICP: it finds *people on a page*, and whether a person
    is interesting is a separate question with a separate, versioned answer.
  * `score_against_icp(candidates, icp)` -- applies that answer.
  * `needs_agent(candidates, icp)` -- did extraction work? This is what routes a
    page into the queue the agent drains.

### Why a regex pass at all

Firecrawl emits clean structure for the pages this targets -- `### Name` then a
title on the next line, or a bare name line then a title line. A validated regex
pass over the bake-off corpus recovered the great majority of pairs, so sending
every page to a model would be paying for judgement on pages that need none. The
agent handles the tail, which is where judgement is actually worth something.

### The trap this module exists to not fall into again

Firecrawl italicizes with underscores -- `_New Car Sales Manager_` -- and `_` is
a regex word character, so `\\b(sales manager)\\b` does not match it. One page in
the bake-off scored *zero* titles for exactly this reason and looked like a
fetch failure. Every line is stripped of `*_#>` emphasis before anything looks at
it, and `_norm()` is what all matching goes through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ── line shapes ──────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_RULE_RE = re.compile(r"^\s*([*\-_=]\s*){3,}$")
_LIST_MARKER_RE = re.compile(r"^\s{0,8}(?:[-*+]|\d+[.)])\s+")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_EMPHASIS_CHARS = str.maketrans("", "", "*_`~")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\(?\d{3}\)?[ .\-]?\d{3}[ .\-]\d{4}")

# A person-shaped line: two to four capitalised words, allowing initials
# ("Ana R Delgado"), particles ("Mira Van-Hoorn", "O'Brien"), and a trailing
# credential ("Jane Doe, CPA" -- the comma tail is dropped before matching).
_NAME_TOKEN = r"(?:[A-Z][a-z'’\-]+|[A-Z]{1,3}\.?|Mc[A-Z][a-z'’\-]+|O'[A-Z][a-z'’\-]+)"
_NAME_RE = re.compile(rf"^{_NAME_TOKEN}(?:[ \-]{_NAME_TOKEN}){{1,3}}$")

# Words that make a line a *role*, not a person. Checked before name shape,
# because "Dealer Principal" and "Sales Manager" are both two capitalised words
# and would otherwise read as people.
_ROLE_WORDS = (
    "manager", "director", "president", "principal", "owner", "consultant",
    "advisor", "adviser", "specialist", "coordinator", "associate", "assistant",
    "representative", "supervisor", "superintendent", "administrator",
    "technician", "controller", "receptionist", "cashier", "porter", "detailer",
    "estimator", "writer", "greeter", "concierge", "valet", "buyer", "broker",
    "agent", "clerk", "partner", "founder", "officer", "chief", "head",
    "executive", "vp", "svp", "evp", "ceo", "cfo", "coo", "cto", "cmo", "cio",
    "sales", "service", "parts", "finance", "financial", "operations", "team",
    "staff", "department", "management", "leadership", "advisors", "managers",
    "consultants", "directors", "specialists", "technicians", "lead", "leader",
    "creator", "trainer", "instructor", "engineer", "designer", "developer",
    "analyst", "accountant", "bookkeeper", "controller", "recruiter",
    "photographer", "marketing", "internet", "digital", "fleet", "foreman",
    "foremen", "apprentice", "intern", "trainee", "liaison", "ambassador",
)
_ROLE_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in _ROLE_WORDS) + r")(?![a-z0-9])"
)

# Tokens that appear in navigation and calls-to-action but never in a person's
# name. "Meet Our Team" and "Read More" are both three capitalised words.
_NON_NAME_TOKENS = frozenset({
    "our", "us", "we", "your", "you", "the", "a", "an", "and", "or", "of", "for",
    "all", "more", "here", "now", "today", "new", "used", "pre", "owned",
    "certified", "view", "get", "shop", "learn", "read", "call", "email",
    "text", "chat", "apply", "find", "search", "schedule", "save", "browse",
    "explore", "contact", "meet", "about", "home", "hours", "directions",
    "inventory", "specials", "dealership", "dealer", "trades", "trade",
    "vehicle", "vehicles", "car", "cars", "truck", "trucks", "wash", "menu",
    "click", "close", "open", "next", "previous", "back", "skip", "toggle",
    "photo", "image", "map", "form", "page", "site", "privacy", "policy",
    "terms", "cookie", "cookies", "accessibility", "sitemap", "careers",
    "reviews", "review", "blog", "news", "español", "espanol",
})

# How far past a name line to look for its title. Three content lines covers
# "name / blank / image / title" without letting the next person's title drift
# onto the previous person.
_TITLE_LOOKAHEAD = 3
# How far past a name line to harvest contact details before assuming they
# belong to somebody else.
_DETAIL_LOOKAHEAD = 6

_MAX_TITLE_CHARS = 80


@dataclass(frozen=True)
class ContactCandidate:
    """One person the structural pass believes is on the page."""

    name: str
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    section: Optional[str] = None
    line: int = 0

    def as_dict(self) -> dict:
        return {
            "name": self.name, "title": self.title, "email": self.email,
            "phone": self.phone, "section": self.section, "line": self.line,
        }


@dataclass(frozen=True)
class ScoredContact:
    """A candidate with the ICP's verdict on it, and why."""

    candidate: ContactCandidate
    kept: bool
    reason: str
    matched: Optional[str] = None

    def as_dict(self) -> dict:
        return {**self.candidate.as_dict(), "kept": self.kept,
                "reason": self.reason, "matched": self.matched}


# ── normalization ────────────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    """One line with its markdown removed, ready to be matched against.

    Links collapse to their label (`[Email Me](mailto:...)` -> `Email Me`), then
    emphasis characters go. Dropping `_` is not cosmetic: it is a word character
    to `re`, so a title left as `_Sales Manager_` fails every `\\b`-anchored
    pattern and the page reads as empty.
    """
    line = _LINK_RE.sub(r"\1", text or "")
    line = _LIST_MARKER_RE.sub("", line)
    line = _HEADING_RE.sub(r"\2", line)
    return line.translate(_EMPHASIS_CHARS).strip()


def _norm(text: str) -> str:
    """Lowercased, whitespace-collapsed, emphasis-free -- the matching form."""
    return re.sub(r"\s+", " ", strip_markdown(text)).strip().lower()


def looks_like_role(text: str) -> bool:
    return bool(_ROLE_RE.search(_norm(text)))


def is_link_only(raw: str) -> bool:
    """True when a line is nothing but links -- i.e. it is navigation.

    The single highest-value discriminator on these pages, and one that
    survives every CMS: a staff member's name is plain text or a heading, while
    "Special Offers", "Guest Amenities" and "More Info / Schedule Service" are
    entirely link labels. Without this, a dealer's main nav reads as a dozen
    two-capitalised-word "people" and its inline CTAs read as their job titles.

    Deliberately structural rather than a stoplist: no vocabulary of nav phrases
    is ever finished, and every site invents new ones.
    """
    body = _LIST_MARKER_RE.sub("", raw or "")
    if "](" not in body:
        return False
    return not _LINK_RE.sub("", body).translate(_EMPHASIS_CHARS).strip(" \t|-–—·•>")


def looks_like_name(text: str) -> bool:
    """True for a line that is *only* a person's name.

    Rejects role lines first -- "Dealer Principal" is name-shaped and is not a
    name -- then navigation phrases, then anything that isn't two-to-four
    capitalised words.
    """
    cleaned = strip_markdown(text)
    if not cleaned or len(cleaned) > 60:
        return False
    # "Jane Doe, CPA" / "Jane Doe - Sales" -> match on the part before the comma.
    cleaned = cleaned.split(",")[0].strip()
    if not cleaned or any(ch.isdigit() for ch in cleaned):
        return False
    if looks_like_role(cleaned):
        return False
    tokens = cleaned.split()
    if not (2 <= len(tokens) <= 4):
        return False
    if any(t.strip(".'-").lower() in _NON_NAME_TOKENS for t in tokens):
        return False
    # An all-caps line is a section banner ("MANAGEMENT TEAM"), not a name --
    # but "AJ Fenwick" is a name, so this rejects only the fully-shouted line.
    if all(t.isupper() for t in tokens):
        return False
    return bool(_NAME_RE.match(cleaned))


def looks_like_title(text: str) -> bool:
    """A short line naming a role. Length matters: a paragraph about the sales
    team contains "sales team" and is not anybody's job title."""
    cleaned = strip_markdown(text)
    if not cleaned or len(cleaned) > _MAX_TITLE_CHARS:
        return False
    if cleaned.endswith((".", "!", "?")) and len(cleaned.split()) > 4:
        return False
    # Digits mean a phone number, an hours block or an address that happens to
    # sit next to a role word ("Sales: 304-746-0500", "Sales: 9am-8pm"); a job
    # title is words. Breadcrumbs and table pipes are the same kind of noise.
    if any(ch.isdigit() for ch in cleaned) or ">" in cleaned or "|" in cleaned:
        return False
    return looks_like_role(cleaned)


# ── the structural pass ──────────────────────────────────────────────────────

@dataclass
class _Line:
    raw: str
    text: str
    index: int
    heading: int = 0          # heading level, 0 for body text
    skip: bool = False        # image, rule, or blank -- carries no content


def _classify(markdown: str) -> list[_Line]:
    out: list[_Line] = []
    for i, raw in enumerate((markdown or "").splitlines()):
        heading = 0
        match = _HEADING_RE.match(raw)
        if match:
            heading = len(match.group(1))
        text = strip_markdown(raw)
        skip = (
            not text
            or bool(_IMAGE_LINE_RE.match(raw))
            or bool(_RULE_RE.match(raw.strip()))
            or is_link_only(raw)
        )
        out.append(_Line(raw=raw, text=text, index=i, heading=heading, skip=skip))
    return out


def regex_pass(markdown: str) -> list[ContactCandidate]:
    """Every person the page structurally presents, in page order.

    Name-line then title-within-three-content-lines adjacency, which is what
    both observed layouts reduce to: a `### Name` heading followed by a title
    (heading or plain), and a bare name line followed by a title line.

    ICP-agnostic by design. A page's people do not change when the whitelist
    does, and keeping the two apart is what lets `--reparse` re-score a cached
    page for free.
    """
    lines = _classify(markdown)
    consumed: set[int] = set()
    section: Optional[str] = None
    seen: set[tuple[str, str]] = set()
    out: list[ContactCandidate] = []

    for pos, line in enumerate(lines):
        if line.skip or line.index in consumed:
            continue

        if not looks_like_name(line.text):
            # A heading that isn't a person names the block that follows.
            if line.heading:
                section = line.text
            continue

        title, title_index = _find_title(lines, pos)
        if title_index is not None:
            consumed.add(title_index)
        email, phone = _find_details(lines, pos)

        name = strip_markdown(line.text).split(",")[0].strip()
        key = (name.lower(), (title or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(ContactCandidate(
            name=name, title=title, email=email, phone=phone,
            section=section, line=line.index,
        ))
    return out


def _find_title(lines: list[_Line], pos: int) -> tuple[Optional[str], Optional[int]]:
    """The first role line within the lookahead, unless another name gets there
    first -- a name means the previous person's block has ended."""
    looked = 0
    for line in lines[pos + 1:]:
        if line.skip:
            continue
        if looked >= _TITLE_LOOKAHEAD or looks_like_name(line.text):
            return (None, None)
        if looks_like_title(line.text):
            return (re.sub(r"\s+", " ", strip_markdown(line.text)).strip(), line.index)
        looked += 1
    return (None, None)


def _find_details(lines: list[_Line], pos: int) -> tuple[Optional[str], Optional[str]]:
    """Email and phone from this person's block, stopping at the next person.

    Unlike the title scan, this one reads link-only lines: `[Email Me](mailto:…)`
    and `[Call Me](tel:…)` are pure navigation as far as names and titles are
    concerned, and are exactly where the contact details live.
    """
    email = phone = None
    looked = 0
    for line in lines[pos + 1:]:
        if not line.raw.strip():
            continue
        if looked >= _DETAIL_LOOKAHEAD or (not line.skip and looks_like_name(line.text)):
            break
        # Read the raw line, not the stripped one: the address lives in the
        # link target (`[Email Me](mailto:jdoe@…)`), which strip_markdown drops.
        if email is None:
            found = _EMAIL_RE.search(line.raw)
            if found:
                email = found.group(0).rstrip(".")
        if phone is None:
            found = _PHONE_RE.search(line.raw)
            if found:
                phone = found.group(0).strip()
        looked += 1
    return (email, phone)


# ── ICP scoring ──────────────────────────────────────────────────────────────

def _term_matches(term: str, text: str) -> bool:
    """Whole-phrase match with non-alphanumeric boundaries.

    Boundaries are `[a-z0-9]`, not `\\b`: `\\b` treats `_` as a word character,
    so a title that arrived as `_sales manager_` would fail to match the very
    term written to catch it.
    """
    if not term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    return bool(re.search(pattern, text))


def _longest_match(terms: Iterable[str], text: str) -> Optional[str]:
    """The longest matching term, so a specific rule beats a general one."""
    hits = [t for t in terms or () if _term_matches(t, text)]
    return max(hits, key=len) if hits else None


def score_against_icp(
    candidates: Iterable[ContactCandidate],
    icp: Optional[dict],
    *,
    company_name: Optional[str] = None,
) -> list[ScoredContact]:
    """Apply an ICP profile to a page's candidates. Never drops anything.

    Rejections are returned alongside keeps, with the reason, because "which
    people were on the page and refused" is the only way to find out whether the
    whitelist is any good. Callers that want the survivors use `kept_contacts`.

    Order is deliberate:

      1. **Blocklist first.** "assistant general manager" contains "general
         manager"; whitelist-first would keep exactly the people the blocklist
         was written to exclude.
      2. Section, when the profile names sections worth reading.
      3. Whitelist, if there is one. An empty whitelist keeps everything the
         blocklist didn't take -- a profile with no positive rule is not a
         profile that rejects everybody.
    """
    icp = icp or {}
    blocklist = icp.get("blocklist") or []
    whitelist = icp.get("whitelist") or []
    sections = icp.get("section_headers") or []
    brand = _brand_tokens(company_name)

    out: list[ScoredContact] = []
    for candidate in candidates:
        title_norm = _norm(candidate.title or "")

        if brand and _is_brand_name(candidate.name, brand):
            out.append(ScoredContact(candidate, False, "company_name"))
            continue

        blocked = _longest_match(blocklist, title_norm)
        if blocked:
            out.append(ScoredContact(candidate, False, "blocklist", blocked))
            continue

        if sections:
            section_norm = _norm(candidate.section or "")
            hit = _longest_match(sections, section_norm) if section_norm else None
            if not hit:
                out.append(ScoredContact(candidate, False, "section"))
                continue

        if not whitelist:
            out.append(ScoredContact(candidate, True, "no_whitelist"))
            continue

        if not title_norm:
            out.append(ScoredContact(candidate, False, "no_title"))
            continue

        allowed = _longest_match(whitelist, title_norm)
        if allowed:
            out.append(ScoredContact(candidate, True, "whitelist", allowed))
        else:
            out.append(ScoredContact(candidate, False, "not_in_whitelist"))
    return out


def kept_contacts(scored: Iterable[ScoredContact]) -> list[ContactCandidate]:
    return [s.candidate for s in scored if s.kept]


def _brand_tokens(company_name: Optional[str]) -> frozenset:
    """The company's own name, as tokens, so its brand line isn't read as a person.

    Reuses `domain_discovery.strip_entity_suffix` rather than re-deriving the
    legal-suffix list -- two implementations of "is `Inc.` part of the name"
    would eventually disagree.
    """
    if not company_name:
        return frozenset()
    from domain_discovery import strip_entity_suffix

    base = _norm(strip_entity_suffix(company_name))
    return frozenset(t for t in base.split() if len(t) > 2)


def _is_brand_name(name: str, brand: frozenset) -> bool:
    """True when every word of this "person" is a word of the company name.

    Catches the dealership's own name sitting where a person's would be
    ("Carlton Motorcars"), without touching a real person who happens to share
    one word with the brand.
    """
    tokens = {t for t in _norm(name).split() if len(t) > 2}
    return bool(tokens) and tokens.issubset(brand)


def needs_agent(candidates: Iterable[ContactCandidate], icp: Optional[dict]) -> bool:
    """Should this page go to the queue the agent drains?

    Counts what the *structural pass* found, not what the ICP kept. The question
    is "did extraction work", and a page full of people none of whom fit the ICP
    is a page extraction handled perfectly -- routing it to the agent would send
    every off-profile page to a model forever, on every run.
    """
    minimum = (icp or {}).get("min_contacts")
    if minimum in (None, ""):
        minimum = 1
    return sum(1 for c in candidates if c.title) < int(minimum)


# Kept as an alias: the plan and the CLI both talk about a "fall-through to the
# agent", and `needs_llm` is the name that reads right at the call site even
# though no LLM API is involved -- the agent is the model.
needs_llm = needs_agent
