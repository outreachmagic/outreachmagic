"""MillionVerifier email verification (single + bulk)."""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from credits import verify_credits_used

MV_SINGLE_BASE = "https://api.millionverifier.com/api/v3"
MV_BULK_BASE = "https://bulkapi.millionverifier.com"
HTTP_TIMEOUT = 60


def mv_to_om_status(mv_status: str) -> str:
    mapping = {
        "ok": "valid",
        "catch_all": "catch_all",
        "unknown": "unknown",
        "risky": "catch_all",
        "disposable": "invalid",
        "invalid": "invalid",
        "error": "unknown",
    }
    return mapping.get((mv_status or "").strip().lower(), "unknown")


class MillionVerifierProvider:
    def __init__(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()

    def _single_params(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        params = {"api": self.api_key}
        if extra:
            params.update(extra)
        return params

    def _bulk_params(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        params = {"key": self.api_key}
        if extra:
            params.update(extra)
        return params

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
            return {"error": "MILLIONVERIFIER_API_KEY not set", "status": "no_key"}
        hdrs = {"User-Agent": "email-finder/2.1", **(headers or {})}
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

    def verify_single(self, email: str, timeout: int = 10) -> dict[str, Any]:
        qs = urllib.parse.urlencode(
            self._single_params({"email": email, "timeout": str(max(2, min(60, timeout)))})
        )
        payload = self._http_json("GET", f"{MV_SINGLE_BASE}/?{qs}", timeout=timeout + 5)
        if payload.get("status") in ("http_error", "error", "no_key"):
            return payload
        if str(payload.get("error") or "").strip():
            return {"status": "error", "error": payload.get("error"), "provider": "millionverifier"}
        return self._normalize(payload)

    def check_credits(self) -> tuple[float, Optional[str]]:
        qs = urllib.parse.urlencode(self._single_params())
        payload = self._http_json("GET", f"{MV_SINGLE_BASE}/credits?{qs}")
        if payload.get("status") in ("http_error", "error", "no_key"):
            return 0.0, str(payload.get("error") or payload.get("status"))
        if str(payload.get("error") or "").strip():
            return 0.0, str(payload.get("error"))
        return float(payload.get("credits") or 0), None

    def create_bulk(self, emails: list[str]) -> dict[str, Any]:
        if not emails:
            return {"error": "no emails", "status": "bad_input"}
        csv_content = "email\n" + "\n".join(emails)
        boundary = "----EmailFinderMV"
        body_parts: list[bytes] = []
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(b'Content-Disposition: form-data; name="file_contents"; filename="emails.csv"\r\n')
        body_parts.append(b"Content-Type: text/csv\r\n\r\n")
        body_parts.append(csv_content.encode("utf-8"))
        body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(body_parts)
        qs = urllib.parse.urlencode(self._bulk_params())
        return self._http_json(
            "POST",
            f"{MV_BULK_BASE}/bulkapi/v2/upload?{qs}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def check_status(self, file_id: str) -> dict[str, Any]:
        qs = urllib.parse.urlencode(self._bulk_params({"file_id": str(file_id)}))
        return self._http_json("GET", f"{MV_BULK_BASE}/bulkapi/v2/fileinfo?{qs}")

    def download_results(self, file_id: str) -> list[dict[str, str]]:
        if not self.api_key:
            return []
        qs = urllib.parse.urlencode(
            self._bulk_params({"file_id": str(file_id), "filter": "all"})
        )
        url = f"{MV_BULK_BASE}/bulkapi/v2/download?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "email-finder/2.1"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                text = resp.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return []
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]

    def list_files(self) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode(self._bulk_params({"limit": "25"}))
        payload = self._http_json("GET", f"{MV_BULK_BASE}/bulkapi/v2/filelist?{qs}")
        if isinstance(payload.get("files"), list):
            return payload["files"]
        return []

    def poll_until_complete(
        self,
        file_id: str,
        *,
        interval: float = 30.0,
        max_wait: float = 3600.0,
        poll_interval: Optional[float] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        wait_interval = poll_interval if poll_interval is not None else interval
        wait_max = timeout_seconds if timeout_seconds is not None else max_wait
        start = time.time()
        while time.time() - start < wait_max:
            status_payload = self.check_status(file_id)
            status = str(status_payload.get("status") or "").lower()
            if status in ("finished", "completed"):
                return status_payload
            if status in ("failed", "error", "canceled"):
                return status_payload
            time.sleep(max(5.0, wait_interval))
        return {"status": "timeout", "error": "bulk verification timed out", "file_id": file_id}

    def wait_for_completion(
        self,
        file_id: str,
        *,
        poll_interval: float = 10.0,
        timeout_seconds: float = 600.0,
    ) -> dict[str, Any]:
        """Alias for poll_until_complete (discoverable name for agents)."""
        return self.poll_until_complete(
            file_id,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
        )

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        mv_status = str(raw.get("result") or raw.get("status") or "")
        email = (raw.get("email") or "").strip()
        return {
            "email": email or raw.get("email"),
            "status": mv_to_om_status(mv_status),
            "mv_status": mv_status,
            "substatus": raw.get("subresult") or raw.get("substatus"),
            "credits_remaining": raw.get("credits"),
            "credits_used": verify_credits_used(count=1) if email else 0,
            "provider": "millionverifier",
        }

    def _parse_csv_report(self, csv_text: str) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(csv_text))
        out: list[dict[str, Any]] = []
        for row in reader:
            email = (row.get("email") or "").strip()
            if not email:
                continue
            out.append(
                self._normalize({"email": email, "result": row.get("result") or row.get("status")})
            )
        return out
