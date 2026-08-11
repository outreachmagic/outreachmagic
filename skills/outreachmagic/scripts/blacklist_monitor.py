#!/usr/bin/env python3
"""DNSBL (DNS blacklist) monitoring for sender domains.

Domain-based lists (DBL/SURBL/URIBL) always run and need no config. IP-based
lists (ZEN/BRBL/SpamCop/Invaluement/PSBL) only run for a domain that has a
user-registered static sending_ip -- cold email routes through shared,
rotating provider relays, so a domain's own A/MX records don't reveal the IP
that actually sends, and checking the provider's whole shared range tells you
nothing domain-specific.

**A DNS answer is not automatically a listing.** The big lists deliberately
answer *refused* to queries they will not serve -- Spamhaus returns
127.255.255.0/24 to open/public resolvers, and URIBL returns 127.0.0.1 because
bit 0x1 is not used for domain classification. Reading either as "listed" flags
every domain you own the moment your resolver gets rate-limited. RESPONSE_CODES
below is the per-list truth table; anything not in it keeps the old
"any answer means listed" behaviour.

Uses stdlib socket.gethostbyname_ex() (no dnspython dependency), wrapped in a
ThreadPoolExecutor to bound per-lookup wait time since the stdlib call takes no
timeout argument. An operator can point checks at a specific resolver with
`dnsbl_resolver` in outreachmagic_config.json, which routes through `dig` --
that must be a LOCAL recursive resolver (unbound/dnsmasq), never a public one
like 8.8.8.8: the public resolvers are exactly what these lists refuse.
"""

from __future__ import annotations

import json
import random
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Optional

CLEAN = "clean"
LISTED = "listed"
CHECK_FAILED = "check_failed"

#: Per-list response semantics. `refused_prefix` / `refused_exact` are answers
#: the list returns to say "I will not serve this query" -- rate limit, open
#: resolver policy, or an unsupported query. They are NOT listings.
RESPONSE_CODES: dict[str, dict] = {
    # Spamhaus signals a blocked/rate-limited query in 127.255.255.0/24.
    "zen.spamhaus.org": {"refused_prefix": "127.255.255."},
    "dbl.spamhaus.org": {"refused_prefix": "127.255.255."},
    # URIBL: "All queries that we refuse, we return a 127.0.0.1 response to, as
    # bit 0x1 is not used for domain classification." A real listing is
    # 127.0.0.2 (black) / .4 (grey) / .8 (red) / .16 (abuse), or a bitmask combo.
    "multi.uribl.com": {"refused_exact": {"127.0.0.1"}},
    # SURBL follows the same convention for its unused low bit.
    "multi.surbl.org": {"refused_exact": {"127.0.0.1"}},
}

#: Retry budget for a refused/transient answer. Refusals are usually a
#: momentary rate limit; one retry turns most of them back into a real answer,
#: and pacing the scan stops us causing the limit in the first place.
RETRY_DELAYS = (0.5, 1.5)
#: Delay between individual DNSBL queries in one scan.
QUERY_SPACING_SEC = 0.05

DNSBL_TIERS = {
    "tier1": [
        {"name": "Spamhaus_ZEN", "type": "ip", "host": "zen.spamhaus.org"},
        {"name": "Spamhaus_DBL", "type": "domain", "host": "dbl.spamhaus.org"},
        {"name": "Barracuda_BRBL", "type": "ip", "host": "b.barracudacentral.org"},
        {"name": "SpamCop", "type": "ip", "host": "bl.spamcop.net"},
    ],
    "tier2": [
        {"name": "SURBL", "type": "domain", "host": "multi.surbl.org"},
        {"name": "URIBL", "type": "domain", "host": "multi.uribl.com"},
        {"name": "Invaluement", "type": "ip", "host": "sip.invaluement.com"},
        {"name": "PSBL", "type": "ip", "host": "psbl.surriel.com"},
    ],
}


def select_tiers(tier: str = "all") -> list[dict]:
    if tier == "all":
        return DNSBL_TIERS["tier1"] + DNSBL_TIERS["tier2"]
    return list(DNSBL_TIERS.get(tier, []))


def _in_blocked_range(addresses: list[str]) -> bool:
    """Legacy helper kept for callers/tests: Spamhaus's blocked-query range."""
    return any(str(a).startswith("127.255.255.") for a in addresses)


def classify_addresses(dnsbl_host: str, addresses: list[str]) -> tuple[str, Optional[str]]:
    """(status, error) for a non-empty DNS answer from `dnsbl_host`.

    The default for an unknown list stays "any answer is a listing" -- that is
    the DNSBL convention -- but every list we ship a truth table for gets its
    refusal codes honoured first.
    """
    addrs = [str(a) for a in addresses]
    if not addrs:
        return (CLEAN, None)
    codes = RESPONSE_CODES.get(dnsbl_host, {})
    prefix = codes.get("refused_prefix")
    exact = codes.get("refused_exact") or set()
    if prefix and all(a.startswith(prefix) for a in addrs):
        return (CHECK_FAILED, "refused")
    if exact and all(a in exact for a in addrs):
        return (CHECK_FAILED, "refused")
    # Spamhaus's blocked range is a refusal on any list that returns it.
    if _in_blocked_range(addrs):
        return (CHECK_FAILED, "blocked_range")
    return (LISTED, None)


def _resolver() -> Optional[str]:
    """Operator-configured resolver for DNSBL queries, or None for the system one."""
    try:
        from om_paths import get_config_path

        path = get_config_path()
        if not path.exists():
            return None
        value = json.loads(path.read_text()).get("dnsbl_resolver")
    except Exception:  # noqa: BLE001 - config problems must not break a scan
        return None
    value = (value or "").strip()
    return value or None


def _dig_lookup(query: str, resolver: str, timeout: float) -> tuple[bool, list[str]]:
    """(answered, addresses) via dig. `answered` is False when dig is unusable,
    so the caller can fall back to the stdlib resolver rather than reading a
    missing binary as NXDOMAIN."""
    if not shutil.which("dig"):
        return (False, [])
    try:
        out = subprocess.run(
            ["dig", "+short", f"+time={max(1, int(timeout))}", "+tries=1",
             f"@{resolver}", query, "A"],
            capture_output=True, text=True, timeout=timeout + 2,
        )
    except (subprocess.SubprocessError, OSError):
        return (False, [])
    if out.returncode != 0:
        return (False, [])
    addrs = [ln.strip() for ln in out.stdout.splitlines()
             if ln.strip() and not ln.strip().endswith(".")]
    return (True, addrs)


def _lookup_once(query: str, dnsbl_host: str, timeout: float) -> tuple[str, list[str], Optional[str]]:
    resolver = _resolver()
    if resolver:
        answered, addrs = _dig_lookup(query, resolver, timeout)
        if answered:
            status, error = classify_addresses(dnsbl_host, addrs)
            return (status, addrs, error)

    def do() -> tuple:
        return socket.gethostbyname_ex(query)

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(do)
        try:
            _, _, addrs = fut.result(timeout=timeout)
        except FuturesTimeout:
            return (CHECK_FAILED, [], "timeout")
        except socket.gaierror as exc:
            # EAI_NONAME is the genuine "not listed" (NXDOMAIN) signal; every
            # other errno (EAI_AGAIN etc.) is a transient failure, not "clean".
            if exc.errno == socket.EAI_NONAME:
                return (CLEAN, [], None)
            return (CHECK_FAILED, [], f"gaierror:{exc.errno}")
        except Exception as exc:  # noqa: BLE001 - any resolver failure is a check error, not clean
            return (CHECK_FAILED, [], str(exc))
    addrs = list(addrs or [])
    status, error = classify_addresses(dnsbl_host, addrs)
    return (status, addrs, error)


def _lookup(query: str, timeout: float, dnsbl_host: str = "") -> tuple[str, list[str], Optional[str]]:
    """One DNSBL query, retried through transient failures.

    A refusal or timeout is retried with jittered backoff before it is recorded:
    the whole failure mode this guards against is a momentary rate limit turning
    into a stored verdict. A clean or listed answer returns immediately.
    """
    result = _lookup_once(query, dnsbl_host, timeout)
    for delay in RETRY_DELAYS:
        if result[0] != CHECK_FAILED:
            return result
        time.sleep(delay + random.uniform(0, delay / 2))
        result = _lookup_once(query, dnsbl_host, timeout)
    return result


def check_ip(ip: str, dnsbl_host: str, timeout: float = 5.0) -> dict:
    reversed_ip = ".".join(reversed(str(ip).split(".")))
    query = f"{reversed_ip}.{dnsbl_host}"
    status, addrs, error = _lookup(query, timeout, dnsbl_host)
    return {"query": query, "status": status, "addresses": addrs, "error": error}


def check_domain(domain: str, dnsbl_host: str, timeout: float = 5.0) -> dict:
    query = f"{domain}.{dnsbl_host}"
    status, addrs, error = _lookup(query, timeout, dnsbl_host)
    return {"query": query, "status": status, "addresses": addrs, "error": error}


def scan_domain(
    domain: str,
    sending_ip: Optional[str],
    tiers: list[dict],
    *,
    tier_label: str = "all",
) -> dict:
    """Run the given DNSBL checks for a domain; ip-based checks only if sending_ip is set.

    Queries are spaced rather than fired together: a burst of eight lookups per
    domain across a batch of domains is what earns the rate limit that produced
    the false listings in the first place.

    `tiers_scanned` / `partial` travel with the block so a caller merging this
    into a stored status knows which lists this run actually covered.
    """
    results = []
    for index, entry in enumerate(tiers):
        if entry["type"] == "ip":
            if not sending_ip:
                continue
            if index:
                time.sleep(QUERY_SPACING_SEC)
            check = check_ip(sending_ip, entry["host"])
            target = sending_ip
        else:
            if index:
                time.sleep(QUERY_SPACING_SEC)
            check = check_domain(domain, entry["host"])
            target = domain
        results.append(
            {
                "name": entry["name"],
                "host": entry["host"],
                "type": entry["type"],
                "target": target,
                "status": check["status"],
                "addresses": check["addresses"],
                "error": check["error"],
            }
        )
    clean = sum(1 for r in results if r["status"] == CLEAN)
    listed = sum(1 for r in results if r["status"] == LISTED)
    errors = sum(1 for r in results if r["status"] == CHECK_FAILED)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "all_clean": listed == 0,
        "summary": {"clean": clean, "listed": listed, "errors": errors},
        "tiers_scanned": [tier_label] if tier_label != "all" else ["tier1", "tier2"],
        "partial": tier_label != "all",
        "results": results,
    }


def merge_scan_blocks(prior: Optional[dict], current: dict) -> dict:
    """Fold a scan into a stored status, keeping lists the new scan did not run.

    A `--tier tier1` run used to replace the whole record, silently erasing
    yesterday's tier-2 answers. Results are keyed by list name; each list keeps
    its own `checked_at` so the UI can say how old each half is.
    """
    stamp = current.get("checked_at")
    by_name: dict = {}
    for row in (prior or {}).get("results", []) or []:
        name = row.get("name")
        if name:
            by_name[name] = dict(row)
    for row in current.get("results", []) or []:
        name = row.get("name")
        if not name:
            continue
        merged = dict(row)
        merged["checked_at"] = stamp
        by_name[name] = merged

    results = list(by_name.values())
    clean = sum(1 for r in results if r.get("status") == CLEAN)
    listed = sum(1 for r in results if r.get("status") == LISTED)
    errors = sum(1 for r in results if r.get("status") == CHECK_FAILED)
    scanned = sorted({
        *((prior or {}).get("tiers_scanned") or []),
        *(current.get("tiers_scanned") or []),
    })
    return {
        "checked_at": stamp,
        "all_clean": listed == 0,
        "summary": {"clean": clean, "listed": listed, "errors": errors},
        "tiers_scanned": scanned,
        "partial": sorted(scanned) != ["tier1", "tier2"],
        "results": results,
    }
