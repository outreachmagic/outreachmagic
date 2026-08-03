"""
Normalization helpers and utility functions extracted from pipeline.py.

Dependency-free leaf module — uses only stdlib, typed leaf imports,
and no pipeline.py internals.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Optional

from constants import PIPELINE_STAGES
from platform_registry import LINKEDIN_PLATFORMS
from workspace_routing import normalize_linkedin


def email_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].strip().lower()


def normalize_company_domain(raw: Optional[str]) -> Optional[str]:
    """Normalize a company domain to canonical form: 'acme.com'.

    See also: company_registrable_domain(), a few lines below -- that one is
    for COMPARING two already-normalized domains, never for storage. This
    function is the single source of truth for what gets stored/matched on
    (company_identities.identity_value_normalized, ensure_company()'s domain
    lookups, the wire aliases array); do not fold registrable-domain
    collapsing into it.
    """
    if not raw:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    # www2./www3. are host prefixes exactly as www. is; leaving them on splits
    # one real domain into two identities.
    text = re.sub(r"^www\d*\.", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    # Trailing dots: the FQDN root label ("acme.com.") is valid DNS but never
    # what gets stored, and scraped sources (a domain at the end of a snippet
    # sentence, an email regex that ran one char long) produce the same shape.
    # Stripping here rather than at each call site keeps "acme.com" and
    # "acme.com." from becoming two distinct company_identities rows.
    text = text.strip(".")
    if not text or "." not in text or " " in text or len(text) > 253:
        return None
    return text


# Curated, stdlib-only approximation of the Public Suffix List's "two-label
# effective TLD" cases (co.uk, com.au, etc.) -- NOT a full PSL mirror (this
# codebase has zero third-party dependencies, confirmed, and a full PSL
# library isn't warranted for a dataset that's overwhelmingly US .edu/.com/
# .org/.net/.io). Exists specifically so company_registrable_domain() never
# collapses e.g. "foo.co.uk" and "bar.co.uk" to the same "co.uk" value, which
# a naive last-two-labels split would do.
_MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "sch.uk", "ltd.uk", "plc.uk", "me.uk",
    "co.nz", "org.nz", "govt.nz", "ac.nz", "net.nz", "school.nz",
    "com.au", "org.au", "edu.au", "gov.au", "net.au", "asn.au", "id.au",
    "co.za", "org.za", "gov.za", "net.za", "ac.za",
    "co.in", "org.in", "gov.in", "net.in", "ac.in",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.sg", "org.sg", "gov.sg", "edu.sg",
    "com.br", "org.br", "gov.br", "net.br",
    "com.mx", "org.mx", "gov.mx",
    "co.il", "co.kr", "co.id", "com.cn",
})


def company_registrable_domain(domain: Optional[str]) -> Optional[str]:
    """eTLD+1 (registrable domain) for COMPARISON purposes only.

    Used to decide whether two domains plausibly belong to the same
    organization (e.g. "mail.wvu.edu" and "wvu.edu" both reduce to
    "wvu.edu") -- never for storage. A future maintainer must not "simplify"
    by folding this into normalize_company_domain(): doing so would collapse
    distinct domain identities that company_identities/ensure_company()/
    rank_company_domains() deliberately track separately (multi-domain
    tracking is a shipped feature, not an oversight).

    Expects an already-normalize_company_domain()-normalized input (lowercase,
    no scheme/www/path). Returns None for falsy/malformed input.
    """
    if not domain or "." not in domain:
        return None
    labels = domain.split(".")
    if len(labels) <= 2:
        return domain
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


_COMPANY_GENERIC_WORDS_RE = re.compile(
    r"\b(?:university|college|corp|corporation|inc|llc|ltd|co|company|technologies|"
    r"technology|systems|services|solutions|group|associates|partners|school|of|the|and|at|"
    r"incorporated|holdings|international|intl)\b",
    re.I,
)
_COMPANY_PUNCT_RE = re.compile(r"[,.\-()&]")


def normalize_company_name(name: Optional[str]) -> str:
    """Canonical company-name normalizer: strip punctuation and generic
    business/legal words, collapse whitespace, lowercase.

    Single source of truth for "is this the same company name" -- the
    codebase used to have three separate, uncoordinated implementations of
    this (pipeline_dedup.normalize_company(), enrich.normalize_company_name(),
    and constants.squash_company_name()) none of which were used by
    ensure_company()'s actual company-matching fallback. This is their
    replacement; pipeline_dedup and enrich now delegate here.

    Deliberately NOT a replacement for workspace_routing.normalize_company_name_key():
    that one feeds build_import_key_fingerprint(), a hash relied on to
    re-match already-imported weak-identity leads on repeat import, and
    changing its stripping rules would change already-persisted fingerprints.
    """
    if not name:
        return ""
    text = _COMPANY_PUNCT_RE.sub(" ", str(name))
    text = _COMPANY_GENERIC_WORDS_RE.sub(" ", text)
    return " ".join(text.split()).lower()


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Canonical address, or None. One implementation, in normalize.py --
    there used to be two identical copies of `.strip().lower()` here and in
    workspace_routing, and neither repaired the malformed addresses that were
    reaching the verifier and coming back falsely invalid."""
    from normalize import canonicalize_email

    address, _repairs = canonicalize_email(email)
    return address


def normalize_event_sender(platform: str, sender: str) -> Optional[str]:
    """Normalize relay sender for storage; None if missing or unknown."""
    raw = (sender or "").strip()
    if not raw or raw.lower() == "unknown":
        return None
    plat = (platform or "").lower()
    if plat in LINKEDIN_PLATFORMS:
        return normalize_linkedin(raw)
    return raw.lower()


def normalize_tag(tag: str) -> str:
    """Lowercase, strip whitespace, collapse internal whitespace."""
    return " ".join(tag.strip().lower().split())


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        norm = normalize_tag(tag)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def parse_tags_value(val) -> list[str]:
    """Parse tags from CSV/JSON/CLI/sync payloads into normalized tag strings."""
    if val is None:
        return []
    if isinstance(val, list):
        out: list[str] = []
        for item in val:
            out.extend(parse_tags_value(item))
        return _dedupe_tags(out)
    if isinstance(val, (int, float)):
        val = str(val)
    if not isinstance(val, str):
        val = str(val)
    raw = val.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    out.extend(parse_tags_value(item))
                return _dedupe_tags(out)
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    out.extend(parse_tags_value(item))
                return _dedupe_tags(out)
        except (ValueError, SyntaxError):
            pass
        inner = raw[1:-1].strip().strip("'\"")
        if inner and ";" not in inner and "," not in inner:
            norm = normalize_tag(inner)
            return [norm] if norm else []
    return _parse_tags(raw)


def _parse_tags(raw_tags: str) -> list[str]:
    """Parse semicolon or comma-separated tags into a deduplicated list."""
    tags: list[str] = []
    seen: set[str] = set()
    for sep in (";", ","):
        if sep in raw_tags:
            for t in raw_tags.split(sep):
                norm = normalize_tag(t)
                if norm and norm not in seen:
                    tags.append(norm)
                    seen.add(norm)
            return tags
    norm = normalize_tag(raw_tags)
    if norm:
        return [norm]
    return []


def parse_headcount_numeric(raw: Optional[str]) -> Optional[int]:
    """Extract a numeric midpoint from headcount strings like '11-50' or '500+'."""
    if not raw:
        return None
    text = re.sub(r'[^\d\-+]', '', str(raw).strip())
    if not text:
        return None
    range_match = re.match(r'(\d+)-(\d+)', text)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        return (lo + hi) // 2
    plus_match = re.match(r'(\d+)\+?$', text)
    if plus_match:
        return int(plus_match.group(1))
    return None


def furthest_stage(stage_a: str, stage_b: str) -> str:
    def rank(s: str) -> int:
        try:
            return PIPELINE_STAGES.index(s)
        except ValueError:
            return 0
    return stage_a if rank(stage_a) >= rank(stage_b) else stage_b
