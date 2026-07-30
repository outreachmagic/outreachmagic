"""Fetch a page to markdown once, keep it forever, hand it back for free.

The cache is the record of truth for contact sourcing, not an optimisation.
Firecrawl bills per page and its credits do not roll over, so the difference
between "re-read what we already have" and "fetch it again" is money. Every
free re-run of the extractor -- `--reparse` after an ICP edit, a regex fix
replayed across the corpus -- reads from here.

Two consequences that shape the design:

  * **Nothing auto-expires.** A TTL is available for a caller that explicitly
    wants freshness, but it is off by default: a cache that silently
    invalidates itself is a cache that silently re-spends. Re-fetching is
    always an explicit `force=True`.
  * **The URL is normalised before it becomes a key.** Serper hands back
    result links carrying tracking parameters (`?srsltid=…` showed up on a
    real dealer URL during the Phase 0 run), and the same staff page arriving
    twice with different tracking would be cached twice and billed twice.

Network shape mirrors `enrich._serper_post`: stdlib `urllib`, no dependency,
and the key comes from `api_key_pool` so rotation and dead-slot tracking apply.
The error message deliberately contains `HTTP <code>` because that is the
literal `call_with_key_pool` matches on to decide whether to fail over to the
next key.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

FETCH_METHOD = "firecrawl"
PROVIDER = "firecrawl"
ENV_KEY = "FIRECRAWL_API_KEY"
DEFAULT_TIMEOUT = 90

# Flat rate on the self-serve tiers, and zero of the 18 pages measured so far
# needed stealth mode. If that stops being true the estimate has to change --
# Firecrawl does not publish the stealth surcharge.
CREDITS_PER_FETCH = 1

# Query parameters that identify a referrer, not a page. Stripping them is what
# keeps one staff page from occupying several cache rows.
_TRACKING_PARAMS = frozenset({
    "srsltid", "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid",
    "mc_cid", "mc_eid", "ref", "referrer", "source", "_ga", "_gl",
})
# utm_source, utm_medium, utm_campaign, utm_term, utm_content, utm_id, …
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking_param(name: str) -> bool:
    low = (name or "").lower()
    return low in _TRACKING_PARAMS or low.startswith(_TRACKING_PREFIXES)


@dataclass(frozen=True)
class PageFetch:
    """One page, however it was obtained."""

    url: str
    markdown: str = ""
    cache_hit: bool = False
    fetch_ms: int = 0
    http_status: Optional[int] = None
    content_hash: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.markdown.strip()) and not self.error

    @property
    def credits(self) -> int:
        """What this cost. A cache hit is free; so is a failed call that never
        returned a page -- Firecrawl does not bill a hard error."""
        if self.cache_hit or self.error:
            return 0
        return CREDITS_PER_FETCH


# ── keys ─────────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """The form a URL is keyed on: no fragment, no tracking parameters."""
    parsed = urllib.parse.urlsplit((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip()
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
         if not _is_tracking_param(k)]
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, "")
    )


def url_hash(url: str) -> str:
    return hashlib.blake2b(normalize_url(url).encode("utf-8"), digest_size=16).hexdigest()


def content_hash(markdown: str) -> str:
    """Identifies a page body, so `--reparse` can skip what did not change."""
    return hashlib.blake2b((markdown or "").encode("utf-8"), digest_size=16).hexdigest()


# ── storage ──────────────────────────────────────────────────────────────────

def cached_page(
    conn: sqlite3.Connection,
    url: str,
    *,
    max_age_days: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """The stored page for this URL, or None.

    `max_age_days` is opt-in. The default of None means a cached page never
    goes stale on its own, which is the property that makes re-extraction free.
    """
    sql = "SELECT * FROM company_page_cache WHERE url_hash = ?"
    params: list[Any] = [url_hash(url)]
    if max_age_days is not None:
        sql += " AND fetched_at >= datetime('now', ?)"
        params.append(f"-{int(max_age_days)} days")
    return conn.execute(sql, params).fetchone()


def store_page(
    conn: sqlite3.Connection,
    url: str,
    markdown: str,
    *,
    company_id: Optional[int] = None,
    http_status: Optional[int] = None,
    fetch_method: str = FETCH_METHOD,
) -> str:
    """Write (or replace) the cached body for a URL. Returns its content hash.

    Upsert on url_hash rather than insert: a `--force` re-fetch of a page we
    already hold should update it in place, not accumulate a second row that
    the next lookup might or might not pick.
    """
    digest = content_hash(markdown)
    conn.execute(
        """INSERT INTO company_page_cache (
               company_id, url, url_hash, fetched_at, fetch_method,
               http_status, content_hash, char_count, markdown
           ) VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
           ON CONFLICT (url_hash) DO UPDATE SET
               company_id   = COALESCE(excluded.company_id, company_page_cache.company_id),
               url          = excluded.url,
               fetched_at   = excluded.fetched_at,
               fetch_method = excluded.fetch_method,
               http_status  = excluded.http_status,
               content_hash = excluded.content_hash,
               char_count   = excluded.char_count,
               markdown     = excluded.markdown""",
        (company_id, url, url_hash(url), fetch_method, http_status,
         digest, len(markdown or ""), markdown),
    )
    return digest


def cache_misses(conn: sqlite3.Connection, urls: Iterable[str]) -> list[str]:
    """Which of these URLs would actually cost a credit.

    `--dry-run`'s estimate is built from this rather than from the target
    count. Counting targets over-reports on every run after the first, and a
    budget cap nobody believes is a budget cap nobody uses.
    """
    misses: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url:
            continue
        key = url_hash(url)
        if key in seen:
            continue
        seen.add(key)
        if cached_page(conn, url) is None:
            misses.append(url)
    return misses


# ── fetching ─────────────────────────────────────────────────────────────────

def _firecrawl_post(api_key: str, url: str, endpoint: str, *, main_only: bool) -> tuple[str, int]:
    payload = json.dumps({
        "url": url,
        "formats": ["markdown"],
        # 15-23% fewer tokens and zero real contacts lost across the bake-off;
        # what it drops is chrome like "Schedule Car Wash".
        "onlyMainContent": bool(main_only),
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return ((body.get("data") or {}).get("markdown") or ""), resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        # "HTTP <code>" is the literal call_with_key_pool matches to decide
        # whether this slot is dead and the next key should be tried.
        raise ValueError(f"Firecrawl HTTP {exc.code} for {url!r}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Firecrawl request failed for {url!r}: {exc}") from exc


def fetch_page(
    conn: sqlite3.Connection,
    url: str,
    *,
    company_id: Optional[int] = None,
    config: Optional[dict] = None,
    force: bool = False,
    main_only: bool = True,
    max_age_days: Optional[int] = None,
) -> PageFetch:
    """A page as markdown, from cache when possible. Never raises.

    A failed fetch is returned as a PageFetch carrying `error`, not thrown: the
    orchestrator above runs this across hundreds of companies and one bad
    response must cost one company, not the batch.
    """
    if not (url or "").strip():
        return PageFetch(url=url or "", error="no url")

    if not force:
        row = cached_page(conn, url, max_age_days=max_age_days)
        if row is not None:
            return PageFetch(
                url=row["url"],
                markdown=row["markdown"] or "",
                cache_hit=True,
                http_status=row["http_status"],
                content_hash=row["content_hash"],
            )

    import shared as cc

    if config is None:
        import enrich

        config = enrich.load_config()
    endpoint = config.get("firecrawl_endpoint") or "https://api.firecrawl.dev/v1/scrape"

    started = time.time()
    try:
        api_key_pool, call_with_key_pool, _ = cc.require_api_key_pool()
        if not api_key_pool(ENV_KEY):
            raise ValueError(
                f"{ENV_KEY} not set — add in Dashboard → API Keys, then sync-secrets"
            )
        markdown, status = call_with_key_pool(
            ENV_KEY,
            lambda key: _firecrawl_post(key, url, endpoint, main_only=main_only),
            provider=PROVIDER,
        )
    except (ValueError, RuntimeError) as exc:
        return PageFetch(
            url=url, cache_hit=False,
            fetch_ms=int((time.time() - started) * 1000),
            error=str(exc)[:300],
        )

    fetch_ms = int((time.time() - started) * 1000)
    digest = store_page(
        conn, url, markdown, company_id=company_id, http_status=status,
    )
    return PageFetch(
        url=url, markdown=markdown, cache_hit=False, fetch_ms=fetch_ms,
        http_status=status, content_hash=digest,
    )
