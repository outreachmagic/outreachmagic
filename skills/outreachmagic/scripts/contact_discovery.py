"""Contact discovery for one company (find-contacts).

The sibling of `run_company_domain_discovery`, one layer down: that resolves a
company to a domain, this resolves a company to the people at it. Same
discipline, deliberately -- free answers before paid ones, a hard budget
checked only after the free paths, a single `_finish()` that records an
observation on **every** path including failure, and never raising, because
this runs across hundreds of companies and one bad response must cost one
company rather than the batch.

The chain per company:

    staff-page URL  ->  page_cache.fetch_page  ->  regex_pass
                                                ->  score_against_icp
                                                ->  attach, or leave for the agent

URL discovery is Serper's job, as it is for domains -- but only after two free
sources are exhausted: a page already in the cache for this company, and the
`top_links` that a previous `find-domains` run already stored on its
observations. On the Phase 0 tail those free paths did not exist yet and Serper
resolved 10 of 10; once a workspace has been through `find-domains`, most
companies should never need the query.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional
from urllib.parse import urlparse

import contact_extract as ce
import contact_review
import page_cache

# Path shapes that mean "this is the staff page", across the dealer CMS
# platforms observed so far: /staff/, /about-us/staff/, /dealership/staff.htm,
# /staff.aspx, /meet-our-team/.
STAFF_PATH_RE = re.compile(
    r"(staff|our-?team|meet-?our-?team|meet-?the-?team|our-?people|employees|"
    r"leadership|management)", re.I,
)

# Serper query for a staff page. No operators: free Serper accounts reject
# `site:` outright with an HTTP 400, and a plain phrase asks the same question
# on every tier (the lesson build_discovery_query already encodes).
def build_staff_query(company_name: str) -> str:
    from domain_discovery import strip_entity_suffix

    return f"{strip_entity_suffix(company_name)} staff"


def _same_site(link: str, domain: Optional[str]) -> bool:
    if not domain:
        return False
    host = (urlparse(link).hostname or "").lower()
    base = domain.lower().removeprefix("www.")
    return bool(host) and (host == base or host.endswith("." + base) or host == "www." + base)


def _rank_links(links: list[str], domain: Optional[str]) -> Optional[str]:
    """The most staff-page-shaped link on the company's own site.

    Off-domain links are never returned. A directory profile is not a staff
    page, and paying to fetch dealerrater is how a contact list fills up with
    other people's data.
    """
    on_site = [l for l in links if l and _same_site(l, domain)]
    for link in on_site:
        if STAFF_PATH_RE.search(urlparse(link).path or ""):
            return link
    return None


def staff_url_from_local_evidence(
    conn: sqlite3.Connection,
    company_id: int,
    domain: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """A staff URL we already know, and where it came from. Costs nothing."""
    row = conn.execute(
        """SELECT url FROM company_page_cache
            WHERE company_id = ? AND IFNULL(TRIM(url), '') != ''
            ORDER BY fetched_at DESC LIMIT 1""",
        (company_id,),
    ).fetchone()
    if row:
        return row["url"], "page_cache"

    # find-domains stores the top organic links on each observation. If it has
    # already run over this workspace, the staff page is frequently sitting in
    # there and the Serper credit has effectively been paid once already.
    seen: list[str] = []
    for obs in conn.execute(
        """SELECT o.metadata_json FROM lead_provider_observations o
             JOIN leads l ON l.id = o.lead_id
            WHERE l.company_id = ? AND o.kind = 'domain_lookup'
              AND o.metadata_json IS NOT NULL
            ORDER BY o.observed_at DESC LIMIT 8""",
        (company_id,),
    ).fetchall():
        try:
            meta = json.loads(obs["metadata_json"])
        except (TypeError, ValueError):
            continue
        seen.extend(meta.get("top_links") or [])
    hit = _rank_links(seen, domain)
    return (hit, "domain_observation") if hit else (None, None)


def discover_staff_url(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    company_id: int,
    company_name: str,
    domain: Optional[str],
    allow_search: bool = True,
) -> dict[str, Any]:
    """Resolve the staff page. Reports whether a Serper credit was spent."""
    url, source = staff_url_from_local_evidence(conn, company_id, domain)
    if url:
        return {"url": url, "source": source, "serper_used": 0, "error": None}
    if not allow_search:
        return {"url": None, "source": None, "serper_used": 0, "error": "no local staff url"}

    import enrich
    import shared as cc

    # Pre-flight the key pool rather than discovering it inside the call. A
    # missing key is a misconfiguration, not a fact about this company: counting
    # it as a spent query reported 10 credits for a run that never reached the
    # network, and recording it as "no staff url" would have written 10 rows
    # claiming we looked and found nothing.
    try:
        api_key_pool, _call, _results = cc.require_api_key_pool()
        has_key = bool(api_key_pool("SERPER_API_KEY"))
    except (RuntimeError, ImportError) as exc:
        return {"url": None, "source": None, "serper_used": 0,
                "error": f"api key pool unavailable: {exc}"[:300], "config_error": True}
    if not has_key:
        return {"url": None, "source": None, "serper_used": 0, "config_error": True,
                "error": "SERPER_API_KEY not set — add in Dashboard → API Keys, then sync-secrets"}

    try:
        raw = enrich.serper_search(build_staff_query(company_name), cfg) or {}
    except (ValueError, RuntimeError) as exc:
        return {"url": None, "source": "serper", "serper_used": 1, "error": str(exc)[:300]}

    links = [(r.get("link") or "") for r in (raw.get("organic") or [])]
    hit = _rank_links(links, domain)
    if hit:
        return {"url": hit, "source": "serper", "serper_used": 1, "error": None}
    # A result on the right site but with no staff-shaped path is still worth
    # one fetch: several dealer CMSs put the roster on /about.
    on_site = next((l for l in links if _same_site(l, domain)), None)
    if on_site:
        return {"url": on_site, "source": "serper_fallback", "serper_used": 1, "error": None}
    return {"url": None, "source": "serper", "serper_used": 1,
            "error": "no result on the company's own domain"}


def run_company_contact_discovery(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    company_id: int,
    company_name: str,
    domain: Optional[str],
    workspace_id: Optional[str] = None,
    icp: Optional[dict] = None,
    icp_hash: Optional[str] = None,
    force: bool = False,
    reparse: bool = False,
    fetch_budget: Optional[int] = None,
    extractor: str = "regex",
    allow_search: bool = True,
) -> dict[str, Any]:
    """One company, end to end. Returns a status summary and never raises."""
    spent = {"fetches": 0, "serper": 0}

    def _finish(
        status: str, *,
        url: Optional[str] = None,
        fetch: Optional[page_cache.PageFetch] = None,
        regex_found: int = 0,
        attached: int = 0,
        queued: int = 0,
        error: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Single exit point, so no path can forget the observation.

        A company that returned nothing is the fact that stops the next run
        paying to learn the same thing -- it is worth more than a success.
        """
        contact_review.record_observation(
            conn, company_id,
            outcome=status,
            workspace_id=workspace_id,
            url=url,
            cache_hit=bool(fetch.cache_hit) if fetch else False,
            fetch_ms=fetch.fetch_ms if fetch else None,
            http_status=fetch.http_status if fetch else None,
            regex_found=regex_found,
            extractor=extractor,
            contacts_attached=attached,
            contacts_queued=queued,
            icp_config_hash=icp_hash,
            cost_estimate_usd=None,
            error=error,
        )
        out = {
            "status": status, "company_id": company_id, "company": company_name,
            "url": url, "regex_found": regex_found, "attached": attached,
            "queued": queued, "error": error,
            "cache_hit": bool(fetch.cache_hit) if fetch else False,
            "fetches_spent": spent["fetches"], "serper_spent": spent["serper"],
        }
        if detail:
            out.update(detail)
        return out

    # --reparse re-reads the cache and never touches the network, so it is
    # exempt from the fetch budget entirely -- that is the whole point of it.
    if reparse:
        row = conn.execute(
            """SELECT url, markdown, http_status, content_hash FROM company_page_cache
                WHERE company_id = ? ORDER BY fetched_at DESC LIMIT 1""",
            (company_id,),
        ).fetchone()
        if row is None:
            return _finish("no_cached_page", error="nothing cached to reparse")
        fetch = page_cache.PageFetch(
            url=row["url"], markdown=row["markdown"] or "", cache_hit=True,
            http_status=row["http_status"], content_hash=row["content_hash"],
        )
        return _extract_and_attach(
            conn, fetch, _finish, company_id=company_id, company_name=company_name,
            workspace_id=workspace_id, icp=icp, icp_hash=icp_hash, extractor=extractor,
        )

    discovery = discover_staff_url(
        conn, cfg, company_id=company_id, company_name=company_name,
        domain=domain, allow_search=allow_search,
    )
    spent["serper"] += discovery["serper_used"]
    url = discovery["url"]
    if not url:
        # A misconfiguration is not an answer about this company. Filing it as
        # `no_staff_url` would put a row in the log saying we looked and found
        # nothing, which a later precision-per-campaign join would believe.
        return _finish(
            "config_error" if discovery.get("config_error") else "no_staff_url",
            error=discovery["error"],
        )

    # The budget is checked only here -- after every free path. An exhausted
    # fetch budget must never block a result that costs nothing.
    already_cached = page_cache.cached_page(conn, url) is not None
    if not already_cached or force:
        if fetch_budget is not None and fetch_budget < 1:
            return _finish("budget_exhausted", url=url)

    fetch = page_cache.fetch_page(
        conn, url, company_id=company_id, config=cfg, force=force,
    )
    spent["fetches"] += fetch.credits
    if fetch.error:
        return _finish("fetch_error", url=url, fetch=fetch, error=fetch.error)
    if not fetch.ok:
        return _finish("empty_page", url=url, fetch=fetch)

    return _extract_and_attach(
        conn, fetch, _finish, company_id=company_id, company_name=company_name,
        workspace_id=workspace_id, icp=icp, icp_hash=icp_hash, extractor=extractor,
    )


def _extract_and_attach(
    conn: sqlite3.Connection,
    fetch: page_cache.PageFetch,
    finish,
    *,
    company_id: int,
    company_name: str,
    workspace_id: Optional[str],
    icp: Optional[dict],
    icp_hash: Optional[str],
    extractor: str,
) -> dict[str, Any]:
    """Score a fetched page and write what it earns.

    `--extractor regex` attaches what the pass is confident about and leaves
    the rest; the page reappears in `contact-extract-pending` when the pass did
    not clear `min_contacts`, and the agent picks it up there. Neither backend
    is privileged in the database -- the cache and the observations do not care
    which one ran.
    """
    candidates = ce.regex_pass(fetch.markdown)
    regex_found = sum(1 for c in candidates if c.title)
    scored = ce.score_against_icp(candidates, icp, company_name=company_name)
    keep = ce.kept_contacts(scored)

    if not keep:
        status = "no_contacts" if not ce.needs_agent(candidates, icp) else "needs_agent"
        return finish(status, url=fetch.url, fetch=fetch, regex_found=regex_found,
                      queued=1 if status == "needs_agent" else 0)

    applied = contact_review.apply_company_contacts(
        conn, company_id,
        [{"name": c.name, "title": c.title, "email": c.email, "phone": c.phone}
         for c in keep],
        workspace_id=workspace_id, icp=icp, icp_hash=icp_hash,
        url=fetch.url, extractor=extractor,
        # _finish() below writes the observation for this company; letting the
        # apply write one too would double every count in the run summary.
        record=False,
    )
    return finish(
        "applied", url=fetch.url, fetch=fetch, regex_found=regex_found,
        attached=applied["attached"], queued=applied["rejected"],
        detail={"contacts": applied["contacts"]},
    )
