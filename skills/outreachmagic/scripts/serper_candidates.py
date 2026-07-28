"""Turn Serper result blocks into a list of candidates a human can choose from.

The research run deliberately does not map results onto lead fields: deciding
which of nine "Sam Rivera"s is *the* Sam Rivera is a judgement, and a
confidence threshold dressed up as automation gets it wrong silently. But the
run previously offered no way to record the judgement either -- the research
was saved as prose and the contact's title and linkedin_url stayed empty.

This module is the missing half. It extracts structured *candidates* and scores
them, and that is all. Scores order the list; they never select from it. The
picker (dashboard or `serper-apply`) is where a value gets chosen, and
"none of these" is a recordable answer there, not the absence of one.

Pure functions, no I/O -- so the extraction rules are testable against a fixture
without a database, a network, or a Serper key.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from constants import SHARED_EMAIL_DOMAINS, is_non_company_name

# Local parts that mean "the company", not "a person at the company". Used to
# decide whether a scraped address becomes a public_email record or a candidate
# address for a real contact -- the two must not be confused, because one is
# safe to share between several people and the other is not.
GENERIC_LOCAL_PARTS = frozenset({
    "admin", "administration", "careers", "contact", "contactus", "enquiries",
    "enquiry", "general", "help", "hello", "hi", "hr", "info", "information",
    "jobs", "mail", "media", "office", "press", "recruitment", "sales",
    "support", "team", "welcome",
})

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LINKEDIN_IN_RE = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+", re.I)
_WORD_RE = re.compile(r"[a-z0-9]+")

# Hosts that are never a company's own site, so a company_discovery hit on one
# is noise rather than an answer.
_NON_COMPANY_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "wikipedia.org", "crunchbase.com", "bloomberg.com",
    "zoominfo.com", "rocketreach.co", "apollo.io", "glassdoor.com", "indeed.com",
    "yelp.com", "mapquest.com", "yellowpages.com",
})


def _tokens(text: Optional[str]) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _host(url: Optional[str]) -> str:
    try:
        host = urlparse(url or "").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _registrable(host: str) -> str:
    """Good-enough eTLD+1 for scoring. Not a public-suffix implementation --
    it only has to group `careers.acme.com` with `acme.com`, and a wrong answer
    costs a place in an ordering, not a wrong write."""
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _sections_by_label(sections: Iterable[dict], prefix: str) -> list[dict]:
    return [s for s in sections or [] if str(s.get("label", "")).startswith(prefix)]


def _organic(section: dict) -> list[dict]:
    data = section.get("data") or {}
    return [r for r in (data.get("organic") or []) if isinstance(r, dict)]


# ── LinkedIn profile candidates ──────────────────────────────────────────────

def extract_linkedin_candidates(
    sections: list[dict], *, name: str = "", company: str = "",
) -> list[dict[str, Any]]:
    """Profile URLs from the linkedin_profile block, best-guess first.

    Scoring is deliberately dull and explainable: every point comes from an
    overlap you could verify by eye. A candidate that scores 0 is still
    returned -- the operator may recognise it when the heuristic doesn't.
    """
    name_tokens = _tokens(name)
    company_tokens = _tokens(company) - {"the", "inc", "llc", "ltd", "co", "company"}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for section in _sections_by_label(sections, "linkedin_profile"):
        for rank, row in enumerate(_organic(section)):
            url = (row.get("link") or "").strip()
            if not _LINKEDIN_IN_RE.match(url) or url in seen:
                continue
            seen.add(url)
            title = (row.get("title") or "").strip()
            snippet = (row.get("snippet") or "").strip()
            haystack = _tokens(f"{title} {snippet}")

            score = 0
            if name_tokens and name_tokens <= haystack:
                score += 3          # every part of the name appears
            elif name_tokens & haystack:
                score += 1
            if company_tokens and company_tokens & haystack:
                score += 4          # the company is the strongest signal we have
            score += max(0, 2 - rank)   # Google's own ordering, weakly

            out.append({
                "url": url, "title": title, "snippet": snippet,
                "score": score, "result_rank": rank,
            })

    out.sort(key=lambda c: (-c["score"], c["result_rank"]))
    return out


def title_from_linkedin_candidate(candidate: dict, *, company: str = "") -> str:
    """The role text out of a LinkedIn result title ("Jane Doe - VP Sales").

    Returned as a *suggestion* for the title picker. Splitting on the dash is
    right often enough to be worth offering and wrong often enough that nothing
    should apply it without being asked.

    LinkedIn puts either the role or the employer after the dash
    ("Sam Rivera - Northfield College"), so a tail that is just the company
    name is dropped rather than offered: a title picker whose top suggestion is
    the company teaches the operator to stop reading it.
    """
    title = (candidate.get("title") or "").strip()
    company_tokens = _tokens(company) - {"the", "inc", "llc", "ltd", "co", "company"}
    for sep in (" - ", " – ", " — ", " | "):
        if sep not in title:
            continue
        tail = title.split(sep, 1)[1].strip()
        if company_tokens and _tokens(tail) <= company_tokens:
            return ""
        return tail
    return ""


# ── Company website candidates ───────────────────────────────────────────────

def extract_company_candidates(
    sections: list[dict], *, company: str = "",
) -> list[dict[str, Any]]:
    """Candidate company domains from the company_discovery blocks."""
    company_tokens = _tokens(company) - {"the", "inc", "llc", "ltd", "co", "company"}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for section in _sections_by_label(sections, "company_discovery"):
        for rank, row in enumerate(_organic(section)):
            host = _host(row.get("link"))
            domain = _registrable(host)
            if not domain or domain in _NON_COMPANY_HOSTS or domain in seen:
                continue
            seen.add(domain)
            title = (row.get("title") or "").strip()
            snippet = (row.get("snippet") or "").strip()

            score = max(0, 3 - rank)
            if company_tokens:
                if company_tokens & _tokens(domain.split(".")[0]):
                    score += 5      # the domain is named after the company
                if company_tokens & _tokens(title):
                    score += 2

            out.append({
                "domain": domain, "url": (row.get("link") or "").strip(),
                "name": title, "snippet": snippet,
                "score": score, "result_rank": rank,
            })

    out.sort(key=lambda c: (-c["score"], c["result_rank"]))
    return out


# ── Email addresses ──────────────────────────────────────────────────────────

def extract_email_candidates(
    sections: list[dict], *, company_domains: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Addresses appearing anywhere in the result text, classified.

    `kind` is "public" for a generic company mailbox (info@, hello@ ...) and
    "personal" for anything else. The distinction decides what happens next:
    a public mailbox can become a shared, sendable record of its own, while a
    personal address belongs to one human and must never be shared between
    contacts. Getting this backwards is how two people end up as one lead.
    """
    known = {d.strip().lower() for d in company_domains if d and d.strip()}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for section in sections or []:
        label = section.get("label", "")
        for row in _organic(section):
            blob = " ".join(str(row.get(k) or "") for k in ("title", "snippet", "link"))
            for raw in _EMAIL_RE.findall(blob):
                email = raw.strip().lower().rstrip(".")
                if email in seen:
                    continue
                seen.add(email)
                local, _, domain = email.partition("@")
                if domain in SHARED_EMAIL_DOMAINS:
                    # A gmail.com address is not a company mailbox no matter how
                    # generic the local part looks.
                    continue
                out.append({
                    "email": email,
                    "domain": domain,
                    "kind": "public" if local in GENERIC_LOCAL_PARTS else "personal",
                    "matches_company_domain": domain in known,
                    "context": (row.get("title") or "").strip(),
                    "source_label": label,
                })
    return out


# ── One call for all three ───────────────────────────────────────────────────

def extract_candidates(
    sections: list[dict], *, name: str = "", company: str = "",
    company_domains: Iterable[str] = (),
) -> dict[str, Any]:
    """Everything the picker needs, from one pass over the result blocks."""
    company_name = "" if is_non_company_name(company) else (company or "")
    linkedin = extract_linkedin_candidates(sections, name=name, company=company_name)
    for c in linkedin:
        c["suggested_title"] = title_from_linkedin_candidate(c, company=company_name)
    return {
        "linkedin": linkedin,
        "company": extract_company_candidates(sections, company=company_name),
        "emails": extract_email_candidates(sections, company_domains=company_domains),
    }
