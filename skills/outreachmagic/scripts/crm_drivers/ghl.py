"""GoHighLevel CRM driver.

API: https://highlevel.stoplight.io/docs/integrations/
"""

from __future__ import annotations

import datetime
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

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

        body_snapshot = body

        for attempt in range(4):
            self.bucket.acquire()

            req = urllib.request.Request(url, data=data, headers=headers, method=method)

            try:
                with urllib.request.urlopen(req) as resp:
                    raw = resp.read()
                    result = json.loads(raw) if raw else {}
                    print(
                        f"[ghl-api] {method} {path} -> 200 "
                        f"body_keys={list(body_snapshot.keys()) if body_snapshot else 'none'} "
                        f"resp_keys={list(result.keys()) if result else 'empty'}",
                        file=sys.stderr,
                    )
                    return result
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                print(
                    f"[ghl-api] {method} {path} -> {e.code} "
                    f"body_keys={list(body_snapshot.keys()) if body_snapshot else 'none'} "
                    f"error={err_body[:300]}",
                    file=sys.stderr,
                )
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

    def lookup_contact(self, email: str) -> str | None:
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

        # Custom fields via mapping
        custom_fields = []
        if field_mapping:
            for om_key, cf_id in field_mapping.items():
                val = lead_data.get(om_key)
                if val and cf_id:
                    # Ensure LinkedIn URLs carry a protocol so they render
                    # as clickable links in GHL.
                    if om_key in ("linkedin_url", "linkedin"):
                        val = _ensure_url_protocol(str(val))
                    custom_fields.append({"id": cf_id, "key": om_key, "field_value": str(val)})

        if custom_fields:
            body["customFields"] = custom_fields

        # Additional emails (GHL alternateEmails)
        add_emails = lead_data.get("additional_emails", [])
        if add_emails:
            body["alternateEmails"] = add_emails

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
                    # Ensure LinkedIn URLs carry a protocol so they render
                    # as clickable links in GHL.
                    if om_key in ("linkedin_url", "linkedin"):
                        val = _ensure_url_protocol(str(val))
                    custom_fields.append({"id": cf_id, "key": om_key, "field_value": str(val)})

        if custom_fields:
            body["customFields"] = custom_fields

        # Additional emails (GHL alternateEmails)
        add_emails = lead_data.get("additional_emails", [])
        if add_emails:
            body["alternateEmails"] = add_emails

        if body:
            self._request("PUT", f"/contacts/{contact_id}", body=body)

    # ------------------------------------------------------------------
    # Company operations
    # ------------------------------------------------------------------

    def _search_business_by_name(self, name: str) -> str | None:
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
    ) -> str | None:
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
        LinkedIn and other non-email events are posted as internal comments
        (type=InternalComment) so they show up in the timeline with an
        "internal" badge.
        """
        count = 0
        max_rowid: int | None = None
        contact_email = ""  # lazily fetched, needed for inbound messages

        for event in events:
            event_type = event.get("event_type", "")
            event_rowid = event.get("rowid")

            payload_raw = event.get("payload_json") or "{}"
            if isinstance(payload_raw, str):
                try:
                    payload = json.loads(payload_raw)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            else:
                payload = payload_raw
            for k, v in payload.items():
                if k not in event:
                    event[k] = v

            try:
                if event_type in ("email_sent", "reply"):
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

            # Non-email events: push as internal comment
            try:
                if self._push_internal_comment(contact_id, event):
                    count += 1
                    if event_rowid is not None and (max_rowid is None or event_rowid > max_rowid):
                        max_rowid = event_rowid
                    continue
            except (AuthError, GhlError, NetworkError, RateLimitError):
                pass
        return count, max_rowid

    def _push_internal_comment(self, contact_id: str, event: dict) -> bool:
        """Push a non-email event as an internal comment on the conversation timeline.

        The original event time is embedded in the comment body as a human-readable
        date header rather than the API ``date`` field, since GHL does not support
        backdating InternalComment messages.
        """
        body = _format_event_note(event)
        if not body:
            return False

        self._request(
            "POST",
            "/conversations/messages",
            body={
                "contactId": contact_id,
                "type": "InternalComment",
                "message": body,
                "mentions": [],
                "status": "delivered",
            },
        )
        return True

    def _get_contact_email(self, contact_id: str) -> str:
        """Fetch a contact's email from GHL. Returns empty string on failure."""
        try:
            resp = self._request("GET", f"/contacts/{contact_id}")
            return resp.get("contact", {}).get("email", "")
        except (GhlError, AuthError, NetworkError, RateLimitError, KeyError):
            return ""

    def _push_email_event(self, contact_id: str, contact_email: str, event: dict) -> bool:
        """Push an email event to GHL as a conversation message. Returns True on success."""
        event_type = event.get("event_type", "")
        subject = event.get("subject", "") or ""
        body_text = event.get("body_preview") or event.get("body") or ""
        sender = event.get("sender", "")

        if event_type == "email_sent":
            # Outbound: OM sent to contact
            email_from = sender
        elif event_type == "reply":
            # Inbound: contact replied — sender is the contact who replied
            email_from = sender
        else:
            return False

        # Build body HTML
        body_html = body_text
        if body_text:
            body_html = f"<p>{body_text}</p>"

        payload: dict = {
            "contactId": contact_id,
            "type": "Email",
            "emailTo": contact_email,
        }
        if email_from:
            payload["emailFrom"] = email_from
        payload["subject"] = subject or "(no subject)"

        # Include original timestamp so GHL orders by actual event time.
        event_at = event.get("event_at") or event.get("timestamp") or ""
        if event_at:
            payload["date"] = event_at

        if body_html:
            payload["html"] = body_html

        self._request(
            "POST",
            "/conversations/messages/inbound",
            body=payload,
        )
        return True

    # ------------------------------------------------------------------
    # Sentiment tag management
    # ------------------------------------------------------------------

    SENTIMENT_TAG_PREFIX = "om_"
    SENTIMENT_VALUES = {"positive", "negative", "autoreply", "invalid"}
    _sentiment_tags_loaded: bool = False
    _existing_tag_names: set[str] = set()

    def _ensure_sentiment_tags(self) -> None:
        """Idempotent: load existing tags once per driver instance."""
        if self._sentiment_tags_loaded:
            return
        try:
            resp = self._request("GET",
                                 f"/locations/{self.location_id}/tags")
            tags = resp.get("tags", [])
            self._existing_tag_names = {t.get("name", "") for t in tags if isinstance(t, dict)}
        except (GhlError, AuthError):
            pass
        self._sentiment_tags_loaded = True

    def _create_tag(self, name: str) -> None:
        """Create a new tag in GHL."""
        self._request(
            "POST",
            f"/locations/{self.location_id}/tags",
            body={"name": name},
        )
        self._existing_tag_names.add(name)

    def list_location_tags(self) -> list[dict]:
        """List all tags for the configured location. Returns list of tag dicts."""
        resp = self._request("GET", f"/locations/{self.location_id}/tags")
        return resp.get("tags", [])

    def create_location_tag(self, name: str) -> str:
        """Create a new tag. Returns the tag ID."""
        resp = self._request(
            "POST",
            f"/locations/{self.location_id}/tags",
            body={"name": name},
        )
        self._existing_tag_names.add(name)
        return resp.get("id", "")

    def sync_sentiment_tag(self, contact_id: str, sentiment: str) -> None:
        """Sync a sentiment tag to the GHL contact.

        Creates the tag if it doesn't exist, then applies it to the contact.
        Empty sentiment is sent as ``(empty)`` so the timeline shows the
        tag was cleared.
        """
        tag_name = f"{self.SENTIMENT_TAG_PREFIX}{sentiment or '(empty)'}"

        self._ensure_sentiment_tags()
        if tag_name not in self._existing_tag_names:
            self._create_tag(tag_name)

        self._request(
            "PUT",
            f"/contacts/{contact_id}/tags",
            body={"tags": [tag_name]},
        )


def _ensure_url_protocol(value: str) -> str:
    """Prepend ``https://`` to URL values that lack a protocol."""
    if value and not value.startswith("http://") and not value.startswith("https://"):
        return f"https://{value}"
    return value


def _format_event_date(iso_str: str) -> str:
    """Convert ISO 8601 timestamp to a human-readable ET date header.

    Returns something like ``Monday, June 2nd, 2026 9:23AM ET``,
    or an empty string if the input cannot be parsed.
    """
    if not iso_str:
        return ""
    # Python 3.9 fromisoformat doesn't handle trailing Z
    s = iso_str.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return ""

    # Convert to US Eastern time
    try:
        et = ZoneInfo("America/New_York")
        dt_et = dt.astimezone(et)
    except Exception:
        dt_et = dt

    day_name = dt_et.strftime("%A")
    month_name = dt_et.strftime("%B")
    day = dt_et.day
    year = dt_et.year
    minute = dt_et.strftime("%M")

    hour = dt_et.hour
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12

    # Day ordinal suffix
    if 4 <= day <= 20 or 24 <= day <= 30:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    return f"{day_name}, {month_name} {day}{suffix}, {year} {hour12}:{minute}{ampm} ET"


def _format_event_note(event: dict) -> str:
    """Format an OM event as a GHL internal-comment body."""
    event_type = event.get("event_type", "unknown")
    body = event.get("body_preview") or event.get("body") or ""
    subject = event.get("subject") or ""
    direction = event.get("direction", "")

    # -- LinkedIn events: structured multi-line format --
    if event_type.startswith("linkedin_"):
        # linkedin_connect → LinkedIn Connect
        label = event_type.replace("_", " ").title().replace("Linkedin", "LinkedIn")
        payload_event = event.get("event", {})
        sender = payload_event.get("sender", "") if isinstance(payload_event, dict) else ""
        receiver = event.get("receiver_linkedin_url", "")

        # Prepend date header
        event_at = event.get("event_at") or event.get("timestamp") or ""
        date_str = _format_event_date(event_at)

        lines = []
        if date_str:
            lines.append(date_str)
            lines.append("")
        lines.append(label)
        lines.append(f"Direction: {direction}")
        if sender:
            lines.append(f"Sender: {sender}")
        if receiver:
            lines.append(f"Receiver: {receiver}")
        if subject:
            lines.append(f"Subject: {subject}")
        if body:
            lines.append(f"Body:\n{body[:2000]}")
        lines.append("---")

        return "\n".join(lines)

    # -- Standard events (email, stage_change, etc.) --
    date_str = _format_event_date(event.get("event_at") or event.get("timestamp") or "")
    prefix = f"{date_str}\n" if date_str else ""

    prefix_map = {
        "email_sent": f"{prefix}{subject or '(no subject)'}",
        "reply": f"{prefix}{body[:200] if body else subject or '(no subject)'}",
        "bounce": f"{prefix}[Bounced]",
        "stage_change": f"{prefix}[Stage] {event.get('old_stage', '')} → {event.get('new_stage', '')}",
        "meeting_booked": f"{prefix}[Meeting] {body[:200] if body else 'Scheduled'}",
        "interested": f"{prefix}[Interested]",
        "not_interested": f"{prefix}[Not Interested]",
    }

    if event_type in prefix_map:
        return prefix_map[event_type]

    title = event_type.replace("_", " ").title()
    detail = f": {body[:200]}" if body else ""
    return f"{prefix}[{title}]{detail}"
