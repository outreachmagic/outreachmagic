#!/usr/bin/env python3
"""Domain registration dates via RDAP, for the deliverability picture.

Domain age is a first-order deliverability signal: mailbox providers weigh it
heavily, and a brand-new domain sending at volume lands in spam far more often
than an aged one. When a campaign gets zero replies, "how old is this sending
domain?" is one of the first questions -- and it used to mean a manual WHOIS
lookup per domain.

RDAP, not port-43 WHOIS: it is the modern replacement, returns JSON, needs no
key, and has no free-text parsing. `https://rdap.org/domain/<domain>` follows
the IANA bootstrap to the right registry.

A registration date never changes, so this is fetched once and cached forever.
`registration_checked_at` is stamped even when no date comes back, which is what
distinguishes "the registry redacts this" from "we never asked" -- both leave
`registration_date` null, and only one of them is worth retrying.

Nothing here may fail a scan. A registry being down, slow, or rate-limiting is
not a reason for a DNSBL check or a domain listing to error out, so every
failure path returns a status instead of raising.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

RDAP_BASE = "https://rdap.org/domain/"
RDAP_TIMEOUT_SEC = 5.0
USER_AGENT = "outreachmagic/1.x (+https://outreachmagic.io)"


def _parse_registration_event(document: dict) -> Optional[str]:
    """The `registration` event date from an RDAP response, as an ISO date."""
    for event in document.get("events") or []:
        if str(event.get("eventAction", "")).strip().lower() == "registration":
            raw = str(event.get("eventDate") or "").strip()
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return raw[:10] or None
            return parsed.date().isoformat()
    return None


def fetch_registration_date(domain: str, *, timeout: float = RDAP_TIMEOUT_SEC) -> dict:
    """{status, registration_date, source, error} for one domain. Never raises."""
    domain = (domain or "").strip().lower().strip(".")
    if not domain:
        return {"status": "error", "registration_date": None, "error": "domain is required"}
    request = urllib.request.Request(
        RDAP_BASE + urllib.parse.quote(domain),
        headers={"User-Agent": USER_AGENT, "Accept": "application/rdap+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # 404 = registry has no such object; 429 = slow down. Neither is a bug,
        # and neither should be retried on the next scan of the same domain.
        return {"status": "not_found" if exc.code == 404 else "error",
                "registration_date": None, "error": f"http_{exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "error", "registration_date": None, "error": str(exc)[:120]}
    except (ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "registration_date": None, "error": f"bad_json:{exc}"}

    date = _parse_registration_event(document)
    if not date:
        # Some registries (and privacy services) omit the registration event.
        return {"status": "redacted", "registration_date": None, "error": None}
    return {"status": "ok", "registration_date": date, "source": "rdap", "error": None}


def refresh_registration(
    domain=None,
    *,
    refresh: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Fill registration dates for the named sending domains.

    `domain` is one domain, a list of domains, or None for all of them. The list
    form exists so the dashboard can check a selection rather than forcing a
    choice between one domain and every domain.

    Skips anything already checked unless `refresh` -- the whole point of
    caching a value that cannot change is not paying for it twice.
    """
    from db_conn import get_conn

    if isinstance(domain, (list, tuple, set)):
        targets = sorted({str(d).strip().lower() for d in domain if str(d or "").strip()})
    else:
        targets = [str(domain).strip().lower()] if domain else []

    conn = get_conn()
    try:
        if targets:
            rows = conn.execute(
                "SELECT domain, registration_date, registration_checked_at"
                f" FROM sender_domains WHERE domain IN ({','.join('?' * len(targets))})",
                targets,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT domain, registration_date, registration_checked_at"
                " FROM sender_domains ORDER BY domain"
            ).fetchall()

        checked = skipped = found = 0
        results = []
        for row in rows:
            if row["registration_checked_at"] and not refresh:
                skipped += 1
                continue
            if limit is not None and checked >= limit:
                break
            outcome = fetch_registration_date(row["domain"])
            checked += 1
            if outcome.get("registration_date"):
                found += 1
            conn.execute(
                """UPDATE sender_domains
                      SET registration_date = COALESCE(?, registration_date),
                          registration_checked_at = ?,
                          registration_source = ?,
                          updated_at = datetime('now')
                    WHERE domain = ?""",
                (outcome.get("registration_date"),
                 datetime.now(timezone.utc).isoformat(),
                 outcome.get("source") or "rdap",
                 row["domain"]),
            )
            results.append({
                "domain": row["domain"],
                "status": outcome["status"],
                "registration_date": outcome.get("registration_date"),
                "error": outcome.get("error"),
            })
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "ok",
        "checked": checked,
        "skipped_cached": skipped,
        "dates_found": found,
        "results": results,
    }
