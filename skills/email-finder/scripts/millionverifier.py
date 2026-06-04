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

MV_BASE = "https://api.millionverifier.com/api/v3"
HTTP_TIMEOUT = 60


def mv_to_om_status(mv_status: str) -> str:
    mapping = {
        "ok": "valid",
        "catch_all": "catch_all",
        "unknown": "unknown",
        "risky": "catch_all",
        "disposable": "invalid",
        "invalid": "invalid",
    }
    return mapping.get((mv_status or "").strip().lower(), "unknown")


class MillionVerifierProvider:
    def __init__(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()

    def _params(self) -> dict[str, str]:
        return {"apikey": self.api_key}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        files: Optional[dict] = None,
        timeout: int = HTTP_TIMEOUT,
    ) -> dict[str, Any]:
        if not self.api_key:
            return {"error": "MILLIONVERIFIER_API_KEY not set", "status": "no_key"}
        url = f"{MV_BASE}{path}"
        if method == "GET" and not files:
            qs = urllib.parse.urlencode(self._params())
            url = f"{url}?{qs}"
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "email-finder/2.1"})
        elif files:
            boundary = "----EmailFinderMV"
            body_parts: list[bytes] = []
            for field_name, (fname, content, mime) in files.items():
                body_parts.append(f"--{boundary}\r\n".encode())
                body_parts.append(
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{fname}"\r\n'.encode()
                )
                body_parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
                body_parts.append(content if isinstance(content, bytes) else content.encode("utf-8"))
                body_parts.append(b"\r\n")
            body_parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(body_parts)
            req = urllib.request.Request(
                f"{url}?{urllib.parse.urlencode(self._params())}",
                data=body,
                method="POST",
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "email-finder/2.1",
                },
            )
        else:
            data = json.dumps(json_body or {}).encode("utf-8")
            req = urllib.request.Request(
                f"{url}?{urllib.parse.urlencode(self._params())}",
                data=data,
                method=method,
                headers={"Content-Type": "application/json", "User-Agent": "email-finder/2.1"},
            )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            return {"status": "http_error", "http_status": e.code, "error": err[:500]}
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            return {"status": "error", "error": str(e)}

    def verify_single(self, email: str, timeout: int = 10) -> dict[str, Any]:
        payload = self._request("POST", "/verify", json_body={"email": email, "timeout": timeout})
        if payload.get("error") or payload.get("status") in ("error", "http_error", "no_key"):
            return payload
        return self._normalize(payload)

    def check_credits(self) -> tuple[float, Optional[str]]:
        payload = self._request("GET", "/credits")
        if payload.get("error"):
            return 0.0, str(payload.get("error"))
        return float(payload.get("credits") or 0), None

    def create_bulk(self, emails: list[str]) -> dict[str, Any]:
        if not emails:
            return {"error": "no emails", "status": "bad_input"}
        csv_content = "email\n" + "\n".join(emails)
        return self._request(
            "POST",
            "/bulk",
            files={"file": ("emails.csv", csv_content, "text/csv")},
        )

    def check_status(self, file_id: str) -> dict[str, Any]:
        return self._request("GET", f"/bulk/{file_id}")

    def download_results(self, file_id: str) -> list[dict[str, str]]:
        if not self.api_key:
            return []
        url = f"{MV_BASE}/bulk/{file_id}/report?{urllib.parse.urlencode(self._params())}"
        req = urllib.request.Request(url, headers={"User-Agent": "email-finder/2.1"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                text = resp.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return []
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]

    def list_files(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/bulk/list")
        if isinstance(payload.get("files"), list):
            return payload["files"]
        return []

    def poll_until_complete(
        self,
        file_id: str,
        *,
        interval: float = 30.0,
        max_wait: float = 3600.0,
    ) -> dict[str, Any]:
        start = time.time()
        while time.time() - start < max_wait:
            status_payload = self.check_status(file_id)
            status = str(status_payload.get("status") or "").lower()
            if status == "completed":
                return status_payload
            if status in ("failed", "error"):
                return status_payload
            time.sleep(max(5.0, interval))
        return {"status": "timeout", "error": "bulk verification timed out", "file_id": file_id}

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        mv_status = str(raw.get("status") or "")
        return {
            "email": raw.get("email"),
            "status": mv_to_om_status(mv_status),
            "mv_status": mv_status,
            "substatus": raw.get("substatus"),
            "provider": "millionverifier",
        }

    def _parse_csv_report(self, csv_text: str) -> list[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(csv_text))
        out: list[dict[str, Any]] = []
        for row in reader:
            email = (row.get("email") or "").strip()
            if not email:
                continue
            out.append(self._normalize({"email": email, "status": row.get("status") or row.get("result")}))
        return out
