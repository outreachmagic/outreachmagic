"""GoHighLevel CRM driver.

API: https://highlevel.stoplight.io/docs/integrations/
"""

from __future__ import annotations

from typing import Optional

import json
import re
import time
import urllib.request
import urllib.error
import urllib.parse

BASE_URL = "https://services.leadconnectorhq.com"
SAFE_RATE = (80, 10)  # 80 requests per 10 seconds (GHL safety headroom)


class AuthError(Exception):
    """401 Unauthorized — API key invalid."""


class RateLimitError(Exception):
    """429 Too Many Requests."""


class GhlError(Exception):
    """Generic GHL API error."""


class NetworkError(GhlError):
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


class GhlDriver:
    """Real GHL API driver."""

    def __init__(self, config: dict):
        self.api_key = config["api_key"]
        self.location_id = config.get("location_id", "")
        self.bucket = TokenBucket(*SAFE_RATE)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Version": "2021-07-28",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OutreachMagic/1.0",
        }

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None) -> dict:
        """Rate-limited HTTP request with retry logic."""
        url = f"{BASE_URL}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        data = json.dumps(body).encode("utf-8") if body else None
        headers = self._headers()
        if method in ("GET", "DELETE"):
            headers.pop("Content-Type", None)
        last_exc = None

        for attempt in range(4):
            self.bucket.acquire()

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
                    last_exc = RateLimitError("GHL rate limited")
                    continue
                try:
                    body_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
                last_exc = GhlError(f"GHL HTTP {e.code}: {body_text[:500]}")
            except (urllib.error.URLError, OSError) as e:
                last_exc = NetworkError(f"GHL network error: {e}")
                time.sleep(2 ** attempt)
                continue

            if attempt < 3:
                time.sleep(2 ** attempt)

        raise last_exc or GhlError("GHL request failed")

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def lookup_contact(self, email: str) -> Optional[str]:
        """Look up a contact by email. Returns contactId or None."""
        try:
            resp = self._request("GET", "/contacts/lookup",
                                 params={"email": email, "locationId": self.location_id})
        except GhlError as e:
            if "404" in str(e) or "not found" in str(e).lower():
                return None
            raise

        contacts = resp.get("contacts", [])
        return contacts[0]["id"] if contacts else None

    def create_contact(
        self, lead_data: dict, field_mapping: dict | None = None,
        *, company_id: str = "",
    ) -> str:
        """Create a GHL contact. Returns contactId.

        If the location has duplicate prevention enabled and the contact
        already exists, returns the existing contactId instead of erroring.
        """
        body: dict = {
            "locationId": self.location_id,
            "name": lead_data.get("name", "Unknown"),
            "email": lead_data.get("email", ""),
        }

        if lead_data.get("company_name") or lead_data.get("company"):
            body["companyName"] = lead_data.get("company_name") or lead_data.get("company")

        # ── Standard contact fields ──
        # website (from company domain)
        website_val = lead_data.get("company_domain") or ""
        if website_val and not website_val.startswith("http"):
            website_val = "https://" + website_val
        if website_val:
            body["website"] = website_val

        # source (from original_source_detail)
        source_val = lead_data.get("original_source_detail") or ""
        if source_val:
            body["source"] = source_val

        # location (lead data preferred, fall back to company HQ)
        loc_city = lead_data.get("location_city") or lead_data.get("company_city") or ""
        if loc_city:
            body["city"] = loc_city
        loc_state = lead_data.get("location_state") or lead_data.get("company_state") or ""
        if loc_state:
            body["state"] = loc_state
        loc_country = lead_data.get("location_country") or lead_data.get("company_country") or ""
        if loc_country:
            body["country"] = loc_country

        # Custom fields via mapping
        custom_fields = []
        if field_mapping:
            for om_key, cf_id in field_mapping.items():
                val = lead_data.get(om_key)
                if val and cf_id:
                    custom_fields.append({"id": cf_id, "key": om_key, "field_value": str(val)})

        if custom_fields:
            body["customFields"] = custom_fields

        try:
            resp = self._request("POST", "/contacts/", body=body)
        except GhlError as e:
            # Handle duplicate-contact prevention
            err_msg = str(e)
            if "duplicated contact" in err_msg.lower() or "duplicate contact" in err_msg.lower():
                # Extract existing contactId from the error metadata
                import re
                match = re.search(r'"contactId"\s*:\s*"([^"]+)"', err_msg)
                if match:
                    return match.group(1)
            raise

        contact_id = resp.get("contact", {}).get("id", "")
        if not contact_id:
            raise GhlError("GHL create_contact returned no contact ID")
        return contact_id

    def update_contact(
        self, contact_id: str, lead_data: dict, field_mapping: dict | None = None,
        *, overwrite_existing: bool = False, company_id: str = "",
    ) -> None:
        """Update an existing GHL contact. Non-destructive by default.

        When overwrite_existing is False (default), only writes fields that
        don't already have a value. When True, overwrites all fields.
        """
        # Fetch existing contact to know what's already set
        existing_contact: dict = {}
        if not overwrite_existing:
            try:
                resp = self._request("GET", f"/contacts/{contact_id}")
                existing_contact = resp.get("contact", {})
            except GhlError:
                pass

        body: dict = {}

        # ── Standard fields ──
        # name
        new_name = lead_data.get("name", "")
        if new_name and (overwrite_existing or not existing_contact.get("name") and not existing_contact.get("contactName")):
            body["name"] = new_name

        # email
        new_email = lead_data.get("email", "")
        if new_email and (overwrite_existing or not existing_contact.get("email")):
            body["email"] = new_email

        # phone
        new_phone = lead_data.get("phone", "")
        if new_phone and (overwrite_existing or not existing_contact.get("phone")):
            body["phone"] = new_phone

        # companyName
        company_val = lead_data.get("company_name") or lead_data.get("company")
        if company_val:
            if overwrite_existing or not existing_contact.get("companyName"):
                body["companyName"] = company_val

        # website (from company domain)
        website_val = lead_data.get("company_domain") or ""
        if website_val and not website_val.startswith("http"):
            website_val = "https://" + website_val
        if website_val and (overwrite_existing or not existing_contact.get("website")):
            body["website"] = website_val

        # source (from original_source_detail)
        source_val = lead_data.get("original_source_detail") or ""
        if source_val:
            if overwrite_existing or not existing_contact.get("source"):
                body["source"] = source_val

        # location fields (lead data preferred, fall back to company HQ)
        loc_fields = [
            ("location_city", "company_city", "city"),
            ("location_state", "company_state", "state"),
            ("location_country", "company_country", "country"),
        ]
        for lead_key, company_key, ghl_key in loc_fields:
            val = lead_data.get(lead_key) or lead_data.get(company_key) or ""
            if val and (overwrite_existing or not existing_contact.get(ghl_key)):
                body[ghl_key] = val

        # ── Custom fields ──
        existing_cf: dict[str, str] = {}
        if existing_contact:
            for cf in (existing_contact.get("customFields") or []):
                cf_id = cf.get("id") or ""
                cf_key = cf.get("key") or ""
                cf_value = cf.get("value") or cf.get("field_value") or ""
                if cf_id and cf_value:
                    existing_cf[cf_id] = cf_value
                if cf_key and cf_value:
                    existing_cf[cf_key] = cf_value

        custom_fields = []
        if field_mapping:
            for om_key, cf_id in field_mapping.items():
                val = lead_data.get(om_key)
                if val and cf_id:
                    if not overwrite_existing and (cf_id in existing_cf or om_key in existing_cf):
                        continue
                    custom_fields.append({"id": cf_id, "key": om_key, "field_value": str(val)})

        if custom_fields:
            body["customFields"] = custom_fields

        # NOTE: GHL's public API does NOT support writing alternateEmails
        # (POST /contacts/, PUT /contacts/{id}, and upsert all reject it
        # with 422). See gohighlevel GitHub issue #262. If you need secondary
        # emails on GHL contacts, configure a custom field in the portal and
        # map it to the 'email' or another lead field.

        if body:
            self._request("PUT", f"/contacts/{contact_id}", body=body)

    def add_note(self, contact_id: str, body: str) -> bool:
        """Add a note to an existing GHL contact. Returns True on success."""
        if not body:
            return True
        try:
            self._request("POST", f"/contacts/{contact_id}/notes",
                          body={"body": body})
            return True
        except (GhlError, AuthError, NetworkError, RateLimitError):
            return False

    # ------------------------------------------------------------------
    # Company operations
    # ------------------------------------------------------------------

    def _search_business_by_name(self, name: str) -> Optional[str]:
        """Search for a GHL business by name. Returns businessId or None."""
        try:
            response = self._request(
                "GET", "/businesses/",
                params={"locationId": self.location_id, "limit": "50"},
            )
            businesses = response.get("businesses", [])
            for b in businesses:
                if b.get("name", "").lower() == name.lower():
                    return b["id"]
            return None
        except GhlError:
            return None

    def upsert_company(
        self, workspace_id: str, lead_data: dict, entity: dict | None = None,
    ) -> Optional[str]:
        """Create or find a GHL business (company). Returns businessId or None."""
        company_name = (
            lead_data.get("company_name")
            or lead_data.get("company")
            or ""
        )
        if not company_name:
            return None

        existing_id = self._search_business_by_name(company_name)
        if existing_id:
            return existing_id

        body = {
            "name": company_name,
            "locationId": self.location_id,
        }
        resp = self._request("POST", "/businesses/", body=body)
        return resp.get("business", {}).get("id")

    # ------------------------------------------------------------------
    # Deal operations
    # ------------------------------------------------------------------

    def upsert_deal(
        self, contact_id: str, lead_data: dict, stage_id: str, config: dict,
        *, company_id: str = "",
    ) -> str:
        """Create a GHL opportunity (deal).

        GHL automatically deduplicates by (contactId + pipelineId), so we
        always POST — GHL handles upsert semantics server-side.
        """
        pipeline_id = config.get("pipeline_id", "")
        deal_name = f"{lead_data.get('name', 'Lead')} - {lead_data.get('company_name', lead_data.get('company', ''))}".strip(" -")

        body: dict = {
            "pipelineId": pipeline_id,
            "locationId": self.location_id,
            "name": deal_name,
            "pipelineStageId": stage_id,
            "status": "open",
            "contactId": contact_id,
        }

        resp = self._request("POST", "/opportunities/", body=body)
        deal_id = resp.get("opportunity", {}).get("id", "")
        if not deal_id:
            raise GhlError("GHL upsert_deal returned no opportunity ID")
        return deal_id

    def update_deal_stage(self, deal_id: str, stage_id: str) -> None:
        """Move a deal to a different pipeline stage."""
        self._request("PUT", f"/opportunities/{deal_id}",
                      body={"pipelineStageId": stage_id})

    # ------------------------------------------------------------------
    # Pipeline discovery
    # ------------------------------------------------------------------

    def discover_pipelines(self, config: dict | None = None) -> list[dict]:
        """List GHL pipelines. Returns list of pipeline dicts with stages."""
        resp = self._request("GET", "/opportunities/pipelines?locationId=" + self.location_id)
        return resp.get("pipelines", [])

    def test_connection(self, config: dict | None = None) -> tuple[bool, str]:
        """Test GHL connection by listing pipelines."""
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
        """Push OM events to GHL. Returns (count_pushed, max_rowid_pushed).

        Email events (sent/reply) are posted as conversation messages so they
        appear in the contact's timeline with subject + body formatting.
        All other events go to contact notes.

        max_rowid_pushed tracks the highest event rowid that was successfully
        synced, so the cursor only advances past events that actually landed.
        Falls back to notes if Conversations API fails (missing scope, etc.).
        """
        count = 0
        max_rowid: int | None = None
        contact_email = ""  # lazily fetched, needed for inbound messages

        for event in events:
            event_type = event.get("event_type", "")
            event_rowid = event.get("rowid")
            try:
                if event_type in ("email_sent", "email_reply", "reply"):
                    if not contact_email:
                        contact_email = self._get_contact_email(contact_id)
                    if self._push_email_event(contact_id, contact_email, event):
                        count += 1
                        if event_rowid is not None and (max_rowid is None or event_rowid > max_rowid):
                            max_rowid = event_rowid
                        continue
            except AuthError:
                pass
            except GhlError:
                pass

            # Fallback: push as note
            try:
                note = _format_event_note(event)
                if not note:
                    continue
                context = _extract_event_context_lines(event)
                if context:
                    note += "\n" + context
                note += _note_footer(contact_id=contact_id, deal_id=deal_id)
                self._request(
                    "POST",
                    f"/contacts/{contact_id}/notes",
                    body={"body": note},
                )
                count += 1
                if event_rowid is not None and (max_rowid is None or event_rowid > max_rowid):
                    max_rowid = event_rowid
            except Exception:
                continue
        return count, max_rowid

    def _get_contact_email(self, contact_id: str) -> str:
        """Fetch a contact's email from GHL. Returns empty string on failure."""
        try:
            resp = self._request("GET", f"/contacts/{contact_id}")
            return resp.get("contact", {}).get("email", "")
        except (GhlError, AuthError, NetworkError, RateLimitError, KeyError):
            return ""

    def _push_email_event(self, contact_id: str, contact_email: str, event: dict) -> bool:
        """Push an email event to GHL conversation timeline without sending.

        Uses POST /conversations/messages/inbound for both outbound sends and
        inbound replies. This logs the message in the timeline without actually
        dispatching an email.
        """
        event_type = event.get("event_type", "")
        subject = event.get("subject", "")
        sender = event.get("sender", "")

        # Use full body from metadata if available, fall back to body_preview
        meta_raw = event.get("metadata_json") or "{}"
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (json.JSONDecodeError, TypeError):
            meta = {}
        body_text = (
            meta.get("body") 
            or event.get("body") 
            or event.get("body_preview") 
            or ""
        )

        if event_type == "email_sent":
            email_from = sender
            email_to = contact_email
            timeline_subject = f"[Sent] {subject}"
        elif event_type in ("email_reply", "reply"):
            email_from = contact_email
            email_to = sender
            timeline_subject = f"[Reply] {subject}"
        else:
            return False

        # Convert plain text to HTML for GHL timeline rendering.
        # If body is already HTML (starts with <), pass through as-is.
        body_html = body_text[:5000]
        if body_html:
            if not body_html.startswith("<"):
                # Split on double newlines for paragraph breaks, convert single
                # newlines within a paragraph to <br>
                paragraphs = re.split(r"\n{2,}", body_html)
                escaped = []
                for para in paragraphs:
                    para = para.strip()
                    if para:
                        para = para.replace("\n", "<br>")
                        escaped.append(f"<p>{para}</p>")
                body_html = "\n".join(escaped)

        self._request(
            "POST",
            "/conversations/messages/inbound",
            body={
                "contactId": contact_id,
                "type": "Email",
                "emailTo": email_to,
                "emailFrom": email_from,
                "subject": timeline_subject or "(no subject)",
                "html": body_html,
            },
        )
        return True


def _note_footer(contact_id: str = "", deal_id: str = "") -> str:
    """Build the standard note footer for GHL contact notes.

    Returns a string like:
    \n----------
    source=om_sync | ghl_deal_id=... | ghl_contact_id=...
    """
    parts = ["source=om_sync"]
    if deal_id:
        parts.append(f"ghl_deal_id={deal_id}")
    if contact_id:
        parts.append(f"ghl_contact_id={contact_id}")
    return "\n----------\n" + " | ".join(parts)


def _format_event_note(event: dict) -> str:
    """Format an OM event as a GHL note with prefix and detail."""
    event_type = event.get("event_type", "unknown")
    body = event.get("body_preview") or event.get("body") or ""
    subject = event.get("subject") or ""

    prefix_map = {
        "email_sent": f"[Sent] {subject}",
        "reply": f"[Replied] {body[:200] if body else subject}",
        "bounce": "[Bounced]",
        "stage_change": f"[Stage] {event.get('old_stage', '')} \u2192 {event.get('new_stage', '')}",
        "meeting_booked": f"[Meeting] {body[:200] if body else 'Scheduled'}",
        "interested": "[Interested]",
        "not_interested": "[Not Interested]",
    }

    if event_type in prefix_map:
        return prefix_map[event_type]

    title = event_type.replace("_", " ").title()
    detail = f" {body[:200]}" if body else ""
    return f"[{title}]{detail}"


def _extract_event_context_lines(event: dict) -> str:
    """Extract extra context lines (source platform, UTM params) from an event.

    Returns lines like:
      via Calendly
      UTM: utm_source=google | utm_campaign=spring

    or empty string if nothing extra to add.
    """
    result_parts = []

    # Parse metadata
    meta_raw = event.get("metadata_json") or "{}"
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
    except (json.JSONDecodeError, TypeError):
        meta = {}

    # Platform info
    event_type = event.get("event_type", "")
    platform = meta.get("platform", "")

    if event_type == "meeting_booked" and platform:
        result_parts.append(f"via {platform.title()}")

    # UTM params
    utm_fields = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]
    utm_parts = []
    for field in utm_fields:
        val = meta.get(field) or event.get(field) or ""
        if val:
            utm_parts.append(f"{field}={val}")

    if utm_parts:
        result_parts.append("UTM: " + " | ".join(utm_parts))
    elif event_type == "meeting_booked":
        result_parts.append("UTM: none")

    return "\n".join(result_parts) if result_parts else ""
