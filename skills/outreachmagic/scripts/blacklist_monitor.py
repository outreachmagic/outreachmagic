#!/usr/bin/env python3
"""DNSBL (DNS blacklist) monitoring for sender domains.

Domain-based lists (DBL/SURBL/URIBL) always run and need no config. IP-based
lists (ZEN/BRBL/SpamCop/Invaluement/PSBL) only run for a domain that has a
user-registered static sending_ip -- cold email routes through shared,
rotating provider relays, so a domain's own A/MX records don't reveal the IP
that actually sends, and checking the provider's whole shared range tells you
nothing domain-specific.

Uses stdlib socket.gethostbyname_ex() (no dnspython dependency), wrapped in a
ThreadPoolExecutor to bound per-lookup wait time since the stdlib call takes no
timeout argument.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Optional

CLEAN = "clean"
LISTED = "listed"
CHECK_FAILED = "check_failed"

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
    # Spamhaus returns 127.255.255.0/24 to signal a blocked/rate-limited query
    # (open-resolver refusal) -- a real answer, not NXDOMAIN, so it must NOT be
    # read as "listed".
    return any(str(a).startswith("127.255.255.") for a in addresses)


def _lookup(query: str, timeout: float) -> tuple[str, list[str], Optional[str]]:
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
    if _in_blocked_range(addrs):
        return (CHECK_FAILED, addrs, "blocked_range")
    if addrs:
        return (LISTED, addrs, None)
    return (CLEAN, [], None)


def check_ip(ip: str, dnsbl_host: str, timeout: float = 5.0) -> dict:
    reversed_ip = ".".join(reversed(str(ip).split(".")))
    query = f"{reversed_ip}.{dnsbl_host}"
    status, addrs, error = _lookup(query, timeout)
    return {"query": query, "status": status, "addresses": addrs, "error": error}


def check_domain(domain: str, dnsbl_host: str, timeout: float = 5.0) -> dict:
    query = f"{domain}.{dnsbl_host}"
    status, addrs, error = _lookup(query, timeout)
    return {"query": query, "status": status, "addresses": addrs, "error": error}


def scan_domain(domain: str, sending_ip: Optional[str], tiers: list[dict]) -> dict:
    """Run the given DNSBL checks for a domain; ip-based checks only if sending_ip is set."""
    results = []
    for entry in tiers:
        if entry["type"] == "ip":
            if not sending_ip:
                continue
            check = check_ip(sending_ip, entry["host"])
            target = sending_ip
        else:
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
        "results": results,
    }
