"""HubSpot CRM driver.

API: https://developers.hubspot.com/docs/api/crm/
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
import urllib.parse

SAFE_RATE = (400, 10)  # 400 requests per 10 seconds (HubSpot allows more)


class AuthError(Exception):
    """401 Unauthorized — API key invalid."""


class RateLimitError(Exception):
    """429 Too Many Requests."""


class HubspotError(Exception):
    """Generic HubSpot API error."""


class NetworkError(HubspotError):
    """Network-level error (DNS, timeout, connection refused)."""


class TokenBucket:
    """Simple token-bucket rate limiter."""

    def __init__(self, tokens: int, period: float):
        self.tokens = float(tokens)
        self.max_tokens = float(tokens)
        self.period = period
        self.last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + (elapsed / self.period) * self.max_tokens)
        self.last_refill = now

    def acquire(self):
        """Acquire a token, waiting if necessary. Returns seconds waited (may be 0)."""
        self._refill()
        wait = 0.0
        if self.tokens < 1.0:
            wait = (1.0 - self.tokens) / self.max_tokens * self.period
            time.sleep(wait)
            self._refill()
        self.tokens -= 1.0
        return wait


class HubspotDriver:
    """Real HubSpot API driver."""

    def __init__(self, config: dict):
        self.access_token = config["api_key"]
        self.bucket = TokenBucket(*SAFE_RATE)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Rate-limited HTTP request with retry logic.

        Path can be a full URL (for HubSpot search) or a relative path
        (prepended with https://api.hubapi.com).
        """
        if path.startswith("http"):
            url = path
        else:
            url = f"https://api.hubapi.com{path}"

        data = json.dumps(body).encode("utf-8") if body else None
        headers = self._headers()
        last_exc = None

        for attempt in range(4):
            self.bucket.acquire()
            if method in ("GET", "DELETE") and "Content-Type" in headers:
                headers.pop("Content-Type", None)

            req = urllib.request.Request(url, data=data, headers=headers, method=method)

            try:
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise AuthError("API key rejected")
                if e.code == 429:
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                    last_exc = RateLimitError("HubSpot rate limited")
                    continue
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
                last_exc = HubspotError(f"HubSpot HTTP {e.code}: {body_text[:500]}")
            except (urllib.error.URLError, OSError) as e:
                last_exc = HubspotError(f"HubSpot network error: {e}")
                time.sleep(2 ** attempt)
                continue

            if attempt < 3:
                time.sleep(2 ** attempt)

        raise last_exc or HubspotError("HubSpot request failed")

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def search_contact(self, email: str) -> str | None:
        """Search HubSpot contacts by email. Returns contactId or None."""
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        },
                    ],
                },
            ],
            "properties": [
                "email", "firstname", "lastname", "company", "jobtitle",
                "hs_additional_emails",
            ],
        }
        try:
            resp = self._request(
                "POST", "/crm/v3/objects/contacts/search",
                body=body,
            )
            results = resp.get("results", [])
            return results[0].get("id") if results else None
        except HubspotError:
            return None

    def lookup_contact(self, email: str) -> str | None:
        """Look up a contact by email. Returns contactId or None."""
        return self.search_contact(email)

    def create_contact(
        self, lead_data: dict, field_mapping: dict | None = None,
        *, company_id: str = "",
    ) -> str:
        """Create a HubSpot contact. Returns contactId."""
        name = lead_data.get("name") or "Unknown"
        parts = name.split(" ", 1)
        properties: dict = {
            "email": lead_data.get("email", ""),
            "firstname": parts[0],
            "lastname": parts[1] if len(parts) > 1 else "",
        }

        if lead_data.get("company_name") or lead_data.get("company"):
            properties["company"] = lead_data.get("company_name") or lead_data.get("company")

        if lead_data.get("title"):
            properties["jobtitle"] = str(lead_data["title"])

        if lead_data.get("linkedin_url"):
            properties["linkedinbio"] = str(lead_data["linkedin_url"])

        if lead_data.get("industry"):
            properties["industry"] = str(lead_data["industry"])

        if lead_data.get("headcount_numeric"):
            properties["numemployees"] = str(lead_data["headcount_numeric"])

        if lead_data.get("company_domain"):
            properties["website"] = str(lead_data["company_domain"])

        if lead_data.get("phone"):
            properties["phone"] = str(lead_data["phone"])

        # Additional emails (HubSpot hs_additional_emails)
        add_emails = lead_data.get("additional_emails", [])
        if add_emails:
            properties["hs_additional_emails"] = ";".join(add_emails)

        body = {"properties": properties}

        resp = self._request("POST", "/crm/v3/objects/contacts", body=body)
        contact_id = resp.get("id", "")
        if not contact_id:
            raise HubspotError("HubSpot create_contact returned no contact ID")
        return contact_id

    def update_contact(
        self, contact_id: str, lead_data: dict, field_mapping: dict | None = None,
        *, overwrite_existing: bool = False, company_id: str = "",
    ) -> None:
        """Update an existing HubSpot contact. Non-destructive by default."""
        field_map = {
            "title": "jobtitle",
            "linkedin_url": "linkedinbio",
            "industry": "industry",
            "headcount_numeric": "numemployees",
            "company_domain": "website",
        }

        properties: dict = {}

        # Fetch existing contact if non-destructive mode
        existing_props: dict[str, str] = {}
        if not overwrite_existing:
            try:
                resp = self._request("GET", f"/crm/v3/objects/contacts/{contact_id}?properties=jobtitle,linkedinbio,industry,numemployees,website,company,email,firstname,lastname")
                existing_props = resp.get("properties", {})
            except HubspotError:
                pass

        for om_key, hs_key in field_map.items():
            val = lead_data.get(om_key)
            if not val:
                continue
            if not overwrite_existing and om_key == "title":
                if existing_props.get(hs_key):
                    continue
            elif not overwrite_existing and existing_props.get(hs_key):
                continue
            properties[hs_key] = str(val)

        # company_name → company
        company_val = lead_data.get("company_name") or lead_data.get("company")
        if company_val:
            if overwrite_existing or not existing_props.get("company"):
                properties["company"] = company_val

        # Additional emails (HubSpot hs_additional_emails)
        add_emails = lead_data.get("additional_emails", [])
        if add_emails:
            if overwrite_existing or not existing_props.get("hs_additional_emails"):
                properties["hs_additional_emails"] = ";".join(add_emails)

        if properties:
            self._request(
                "PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                body={"properties": properties},
            )

    # ------------------------------------------------------------------
    # Company operations
    # ------------------------------------------------------------------

    def _search_company_by_name(self, name: str) -> dict | None:
        """Search HubSpot companies by name."""
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "name", "operator": "EQ", "value": name},
                    ],
                },
            ],
        }
        try:
            resp = self._request(
                "POST", "/crm/v3/objects/companies/search",
                body=body,
            )
            results = resp.get("results", [])
            return results[0] if results else None
        except HubspotError:
            return None

    def upsert_company(
        self, workspace_id: str, lead_data: dict, entity: dict | None = None,
    ) -> str | None:
        """Create or find a HubSpot company. Returns companyId or None."""
        company_name = (
            lead_data.get("company_name")
            or lead_data.get("company")
            or ""
        )
        if not company_name:
            return None

        existing = self._search_company_by_name(company_name)
        if existing:
            return existing["id"]

        domain = lead_data.get("company_domain") or ""
        properties = {"name": company_name}
        if domain:
            properties["domain"] = domain

        resp = self._request(
            "POST", "/crm/v3/objects/companies",
            body={"properties": properties},
        )
        return resp.get("id")

    # ------------------------------------------------------------------
    # Deal operations
    # ------------------------------------------------------------------

    def _search_deal_by_contact(self, contact_id: str, pipeline_id: str) -> str | None:
        """Search for an existing deal associated with this contact+pipeline."""
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id},
                        {
                            "propertyName": "associations.contact",
                            "operator": "EQ",
                            "value": contact_id,
                        },
                    ],
                },
            ],
            "properties": ["dealname", "pipeline", "dealstage"],
            "limit": 10,
        }
        try:
            resp = self._request(
                "POST", "/crm/v3/objects/deals/search",
                body=body,
            )
            results = resp.get("results", [])
            if results:
                return results[0]["id"]
        except HubspotError:
            pass
        return None

    def upsert_deal(
        self, contact_id: str, lead_data: dict, stage_id: str, config: dict,
        *, company_id: str = "",
    ) -> str:
        """Create or update a HubSpot deal. Returns dealId."""
        pipeline_id = config.get("pipeline_id", "")
        deal_name = f"{lead_data.get('name', 'Lead')} - {lead_data.get('company_name', lead_data.get('company', ''))}".strip(" -")

        existing_deal_id = self._search_deal_by_contact(contact_id, pipeline_id)

        properties = {
            "dealname": deal_name,
            "pipeline": pipeline_id,
            "dealstage": stage_id,
        }

        if existing_deal_id:
            self._request(
                "PATCH", f"/crm/v3/objects/deals/{existing_deal_id}",
                body={"properties": properties},
            )
            return existing_deal_id

        body = {
            "properties": properties,
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}],
                },
            ],
        }

        # Add company association if provided
        if company_id:
            body["associations"].append({
                "to": {"id": company_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}],
            })

        resp = self._request("POST", "/crm/v3/objects/deals", body=body)
        deal_id = resp.get("id", "")
        if not deal_id:
            raise HubspotError("HubSpot upsert_deal returned no deal ID")

        # Create explicit contact-deal and company-deal associations
        # (belt-and-suspenders: inline handles it, but explicit PUT is defense)
        try:
            self._request(
                "PUT",
                f"/crm/v3/objects/contacts/{contact_id}/associations/deals/{deal_id}/3",
                body={},
            )
        except HubspotError:
            pass

        if company_id:
            try:
                self._request(
                    "PUT",
                    f"/crm/v3/objects/deals/{deal_id}/associations/companies/{company_id}/5",
                    body={},
                )
            except HubspotError:
                pass  # Non-fatal; inline association likely already handled it

        return deal_id

    def update_deal_stage(self, deal_id: str, stage_id: str) -> dict:
        """Move a deal to a different pipeline stage."""
        return self._request(
            "PATCH", f"/crm/v3/objects/deals/{deal_id}",
            body={"properties": {"dealstage": stage_id}},
        )

    # ------------------------------------------------------------------
    # Pipeline discovery
    # ------------------------------------------------------------------

    def discover_pipelines(self, config: dict | None = None) -> list[dict]:
        """List HubSpot pipelines. Returns list of pipeline dicts with stages."""
        resp = self._request("GET", "/crm/v3/pipelines/deals")
        results = resp.get("results", [])
        # Normalize HubSpot "label" → "name" for consistency
        normalized = []
        for p in results:
            pipeline = dict(p)
            if "label" in pipeline and "name" not in pipeline:
                pipeline["name"] = pipeline.pop("label")
            if "stages" in pipeline:
                pipeline["stages"] = [
                    {**s, "name": s["label"]} if "label" in s and "name" not in s else dict(s)
                    for s in pipeline["stages"]
                ]
            normalized.append(pipeline)
        return normalized

    def test_connection(self, config: dict | None = None) -> tuple[bool, str]:
        """Test HubSpot connection by listing pipelines."""
        try:
            self.discover_pipelines(config)
            return True, ""
        except AuthError as e:
            return False, f"API key rejected: {e}"
        except Exception as e:
            return False, f"connection_error: {e}"

    # ------------------------------------------------------------------
    # Event push
    # ------------------------------------------------------------------

    def push_events(self, contact_id: str, deal_id: str, events: list[dict]) -> tuple[int, int | None]:
        """Push OM events as HubSpot notes/emails. Returns (count_pushed, max_rowid_pushed)."""
        count = 0
        max_rowid: int | None = None
        for event in events:
            event_type = event.get("event_type", "")
            event_rowid = event.get("rowid")
            note = _format_event_note(event)
            if not note:
                continue

            pushed_ok = False
            try:
                # Email events get both a Note AND an Email object
                if event_type == "email_sent":
                    self._create_note(contact_id, note)
                    self._create_email(contact_id, event)
                elif event_type == "reply":
                    self._create_note(contact_id, note)
                    self._create_email(contact_id, event, direction="INCOMING_EMAIL")
                else:
                    self._create_note(contact_id, note)
                pushed_ok = True
            except Exception:
                continue

            if pushed_ok:
                count += 1
                if event_rowid is not None and (max_rowid is None or event_rowid > max_rowid):
                    max_rowid = event_rowid
        return count, max_rowid

    def _create_note(self, contact_id: str, note_body: str) -> None:
        """Create a HubSpot note engagement."""
        body = {
            "properties": {
                "hs_timestamp": str(int(time.time() * 1000)),
                "hs_note_body": note_body,
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                },
            ],
        }
        self._request("POST", "/crm/v3/objects/notes", body=body)

    def _create_email(self, contact_id: str, event: dict, direction: str = "EMAIL") -> None:
        """Create a HubSpot email engagement (v3)."""
        # HubSpot v3 email objects use association type 198 (email_to_contact)
        body = {
            "properties": {
                "hs_timestamp": str(int(time.time() * 1000)),
                "hs_email_direction": direction,
                "hs_email_status": "SENT",
                "hs_email_subject": event.get("subject", ""),
                "hs_email_text": event.get("body_preview", event.get("body", "")),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 198}],
                },
            ],
        }
        self._request("POST", "/crm/v3/objects/emails", body=body)


def _format_event_note(event: dict) -> str:
    """Format an OM event as a HubSpot note with prefix and detail."""
    event_type = event.get("event_type", "unknown")
    body = event.get("body_preview") or event.get("body") or ""
    subject = event.get("subject") or ""

    prefix_map = {
        "email_sent": f"[Sent] {subject}",
        "reply": f"[Replied] {body[:200] if body else subject}",
        "bounce": "[Bounced]",
        "stage_change": f"[Stage] {event.get('old_stage', '')} → {event.get('new_stage', '')}",
        "meeting_booked": f"[Meeting] {body[:200] if body else 'Scheduled'}",
        "interested": "[Interested]",
        "not_interested": "[Not Interested]",
    }

    if event_type in prefix_map:
        return prefix_map[event_type]

    title = event_type.replace("_", " ").title()
    detail = f": {body[:200]}" if body else ""
    return f"[{title}]{detail}"
