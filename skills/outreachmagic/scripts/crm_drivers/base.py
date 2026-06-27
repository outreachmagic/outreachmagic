"""Driver interface contract for CRM integrations.

Each driver implements these methods. The orchestrator calls them through
the interface, never directly inspecting driver internals.
"""

from __future__ import annotations


class MockDriver:
    """Fake driver for testing. Records calls and returns canned responses."""

    def __init__(self, platform: str = ""):
        self.platform = platform
        self.calls: list[str] = []
        self.lookups: list[str] = []
        self.creates: list[dict] = []
        self.updates: list[tuple] = []
        self.updated_contacts: list[dict] = []
        self.deals: list[tuple] = []
        self.companies: list[tuple] = []
        self.events: list[tuple] = []
        self.pushed_events_count = 0
        self._contact_counter = 0
        self._deal_counter = 0
        self._company_counter = 0

    def _record(self, method: str, *details: str):
        self.calls.append(f"{method} {' '.join(details)}")

    def lookup_contact(self, email: str) -> str | None:
        self.lookups.append(email)
        self._record("lookup_contact", email)
        return None

    def create_contact(
        self, lead_data: dict, field_mapping: dict | None = None,
        *, company_id: str = "",
    ) -> str:
        self._contact_counter += 1
        self.creates.append({"lead": lead_data, "field_mapping": field_mapping, "company_id": company_id})
        cid = f"mock-contact-{self._contact_counter:03d}"
        self._record("create_contact", cid)
        return cid

    def update_contact(
        self, contact_id: str, lead_data: dict,
        field_mapping: dict | None = None,
        *, overwrite_existing: bool = False, company_id: str = "",
    ) -> None:
        self.updates.append(
            (contact_id, lead_data, field_mapping, overwrite_existing, company_id),
        )
        self.updated_contacts.append(lead_data)
        self._record("update_contact", contact_id)

    def update_deal_stage(self, deal_id: str, stage_id: str) -> None:
        self._record("update_deal_stage", deal_id, stage_id)

    def upsert_deal(
        self, contact_id: str, lead_data: dict, stage_id: str, config: dict,
        *, company_id: str = "",
    ) -> str:
        self._deal_counter += 1
        self.deals.append(
            (contact_id, lead_data, stage_id, config, company_id),
        )
        did = f"mock-deal-{self._deal_counter:03d}"
        self._record("upsert_deal", did, stage_id)
        return did

    def upsert_company(
        self, workspace_id: str, lead_data: dict, entity: dict | None = None,
    ) -> str | None:
        self._company_counter += 1
        self.companies.append((workspace_id, lead_data, entity))
        company_name = (
            lead_data.get("company_name")
            or lead_data.get("company")
            or ""
        )
        if not company_name:
            return None
        cid = f"mock-company-{self._company_counter:03d}"
        self._record("upsert_company", cid)
        return cid

    def push_events(self, contact_id: str, deal_id: str, events: list[dict]) -> int:
        count = len(events)
        self.events.append((contact_id, deal_id, events))
        self.pushed_events_count += count
        self._record("push_events", str(count))
        max_rowid = None
        for e in events:
            r = e.get("rowid")
            if r is not None and (max_rowid is None or r > max_rowid):
                max_rowid = r
        return count, max_rowid

    def discover_pipelines(self, config: dict | None = None) -> list[dict]:
        self._record("discover_pipelines", "")
        return [
            {
                "pipeline_id": (config or {}).get("pipeline_id", "pipe-1"),
                "pipeline_name": "Mock Pipeline",
                "stages": [
                    {"id": "stage-1", "name": "New"},
                    {"id": "stage-2", "name": "Won"},
                ],
            },
        ]

    def sync_sentiment_tag(self, contact_id: str, sentiment: str) -> None:
        self._record("sync_sentiment_tag", contact_id, sentiment)

    def test_connection(self, config: dict | None = None) -> tuple[bool, str]:
        self._record("test_connection", "")
        return True, "connected"


def _format_event_note(event: dict) -> str:
    """Format an event as a CRM note string."""
    event_type = event.get("event_type", "unknown")
    direction = event.get("direction", "")
    subject = event.get("subject", "")
    event_at = event.get("event_at", "")
    body = event.get("body", "")

    prefix = ""
    if event_type == "stage_change":
        prefix = "[Stage]"
    elif event_type == "bounce":
        prefix = "[Bounced]"

    lines = []
    if prefix:
        lines.append(prefix)
    if subject:
        lines.append(f"Subject: {subject}")
    if direction:
        lines.append(f"Direction: {direction}")
    if event_at:
        lines.append(f"Date: {event_at}")
    if body:
        lines.append("---")
        lines.append(body)
    return "\n".join(lines) if lines else f"Event: {event_type}"
