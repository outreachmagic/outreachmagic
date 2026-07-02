"""Scrubby Deep Verification — 72-hour email verification via real SMTP delivery observation.

POST /validate_bulk_emails/deep submits a batch.
POST /fetch_bulk_results/deep polls results with 3 tiers: results_24, results_48, results_72.
3 credits per email charged up-front. Results complete in ~72 hours.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from credits import verify_credits_used

SCRUBBY_BASE = "https://api.scrubby.io"
HTTP_TIMEOUT = 60

# Default poll interval: check every 4 hours (deep results take 24-72h).
# First poll sooner (12h) to catch early 24h results.
DEFAULT_POLL_INTERVAL = 4 * 3600
DEFAULT_MAX_WAIT = 3600 * 72 + 3600  # 73h (72h + buffer)


def scrubby_deep_to_om_status(result: str) -> str:
    """Map Scrubby deep verification result to OM email_verification_status."""
    mapping = {
        "Valid": "valid",
        "Invalid": "invalid",
        "Risky": "catch_all",
        "Catch All": "catch_all",
        "Unknown": "unknown",
    }
    return mapping.get((result or "").strip(), "unknown")


class ScrubbyProvider:
    """Client for the Scrubby deep email verification API."""

    def __init__(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()

    def _http_json(
        self,
        method: str,
        url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: int = HTTP_TIMEOUT,
    ) -> dict[str, Any]:
        if not self.api_key:
            return {"error": "SCRUBBY_API_KEY not set", "status": "no_key"}
        hdrs = {
            "User-Agent": "email-finder/2.1",
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            **(headers or {}),
        }
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"raw": raw}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            return {"status": "http_error", "http_status": e.code, "error": err[:500]}
        except (urllib.error.URLError, TimeoutError) as e:
            return {"status": "error", "error": str(e)}

    def submit_deep(self, emails: list[str]) -> dict[str, Any]:
        """POST /validate_bulk_emails/deep — submit batch for deep verification.

        Returns: status, identifier, total, credits_used, remaining_credits, retry_after_seconds
        Charges 3 credits per email up-front.
        """
        if not emails:
            return {"error": "no emails", "status": "bad_input"}
        filtered = [e.strip() for e in emails if e.strip()]
        if not filtered:
            return {"error": "no valid emails", "status": "bad_input"}
        body = json.dumps({"email": filtered}).encode("utf-8")
        payload = self._http_json("POST", f"{SCRUBBY_BASE}/validate_bulk_emails/deep", data=body)
        if payload.get("status") in ("http_error", "error", "no_key"):
            return payload
        if str(payload.get("error") or "").strip():
            return {"status": "error", "error": payload["error"], "provider": "scrubby_deep"}
        return {
            "status": payload.get("status", "processing"),
            "identifier": payload.get("identifier", ""),
            "fetch_result_endpoint": payload.get("fetch_result_endpoint", "/fetch_bulk_results/deep"),
            "total": int(payload.get("total") or 0),
            "credits_used": int(payload.get("credits_used") or 0),
            "remaining_credits": int(payload.get("remaining_credits") or 0),
            "retry_after_seconds": int(payload.get("retry_after_seconds") or 259200),
            "provider": "scrubby_deep",
        }

    def fetch_results(self, identifier: str) -> dict[str, Any]:
        """POST /fetch_bulk_results/deep — poll for deep verification results.

        Returns: status (processing/completed), identifier, results_24, results_48, results_72.
        """
        body = json.dumps({"identifier": identifier}).encode("utf-8")
        payload = self._http_json("POST", f"{SCRUBBY_BASE}/fetch_bulk_results/deep", data=body)
        if payload.get("status") in ("http_error", "error", "no_key"):
            return payload
        return payload

    def aggregate_results(self, raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Merge results_24/48/72 into a flat email-keyed dict.

        Prefers results_72 > results_48 > results_24. Skips "pending" entries.
        Returns: {email: {result, status, quick_status}, ...}
        """
        aggregated: dict[str, dict[str, Any]] = {}
        # Process tiers in ascending priority order (24 → 48 → 72), last write wins
        for tier_key in ("results_24", "results_48", "results_72"):
            tier = raw.get(tier_key)
            if not isinstance(tier, dict):
                continue
            for email, entry in tier.items():
                if not isinstance(entry, dict):
                    continue
                result = str(entry.get("result") or "").strip().lower()
                if result == "pending":
                    continue
                aggregated[email] = dict(entry)
        return aggregated

    def poll_until_complete(
        self,
        identifier: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout_seconds: float = DEFAULT_MAX_WAIT,
    ) -> dict[str, Any]:
        """Poll fetch_results until status is 'completed' or timeout."""
        start = time.time()
        first_poll = True
        while time.time() - start < timeout_seconds:
            status_payload = self.fetch_results(identifier)
            status = str(status_payload.get("status") or "").lower()
            if status == "completed":
                return status_payload
            if status in ("failed", "error", "canceled"):
                return status_payload
            # Shorter wait for first poll (check sooner — ~12h)
            wait = min(3600 * 12, poll_interval / 2) if first_poll else poll_interval
            first_poll = False
            time.sleep(max(60.0, wait))
        return {"status": "timeout", "error": "deep verification timed out", "identifier": identifier}

    def check_credits(self) -> tuple[int, Optional[str]]:
        """Check remaining Scrubby credits via a cheap single validation.

        There is no dedicated credits endpoint — we read remaining_credits from
        any validation response. Uses a single quick validation to read credits.
        """
        test_email = "credits-check@scrubby.io"
        body = json.dumps({"email": test_email}).encode("utf-8")
        payload = self._http_json("POST", f"{SCRUBBY_BASE}/validate_email", data=body)
        if payload.get("status") in ("http_error", "error", "no_key"):
            return 0, str(payload.get("error") or payload.get("status"))
        if str(payload.get("error") or "").strip():
            return 0, str(payload.get("error"))
        return int(payload.get("remaining_credits") or 0), None
