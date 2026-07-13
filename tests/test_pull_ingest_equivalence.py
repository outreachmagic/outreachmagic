"""Golden test: ingesting a page of agent relay events produces an exact DB state.

The lead_core pull path is heavily optimized (writes are coalesced, identity
upserts batched, redundant re-applies skipped). Every one of those optimizations
is only valid if it leaves the resulting database untouched, which is easy to
break in a way no narrower test would notice -- a field silently no longer
written, a source column changing provenance, an updated_at bump appearing where
it must not (leads.updated_at drives the relay push, so a spurious bump sends the
lead straight back where it came from).

So: ingest a fixture that walks every branch of the path and assert on the whole
database, normalized for the values that are legitimately non-deterministic.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pipeline as om
from om_paths import get_db_path, set_data_root_override
from pipeline_sync import _ingest_relay_page

LI = "https://www.linkedin.com/in/synthetic-one"
LI2 = "https://www.linkedin.com/in/synthetic-two"
LI3 = "https://www.linkedin.com/in/synthetic-three"


def _core(relay_id, entity_key, data, ts="2026-06-01T10:00:00Z"):
    return {
        "platform": "agent",
        "relay_id": relay_id,
        "entity_key": entity_key,
        "event_type": "lead_core_update",
        "received_at": ts,
        "payload": {
            "action": "lead_core_update",
            "client_id": "upstream-client",
            "timestamp": ts,
            "data": data,
        },
    }


PAGE_1 = [
    # Rich created path: every optional branch at once.
    _core(1, "ada@northwind-example.com", {
        "email": "ada@northwind-example.com",
        "name": "Ada Synth", "company": "Northwind Example", "title": "VP Eng",
        "industry": "Software", "headcount": "51-200",
        "company_domain": "northwind-example.com",
        "location_city": "Austin", "location_state": "TX", "location_country": "US",
        "linkedin": LI, "linkedin_headline": "VP Eng", "linkedin_bio": "bio text",
        "linkedin_sales_nav_id": "ACwAAsyntheticOne1234567",
        "notes": "first note",
        "secondary_emails": ["ada.alt@northwind-example.com", "ada2@northwind-example.com"],
        "external_id": "EXT-1",
        "original_source": "apollo", "original_source_detail": "list A",
        "original_source_platform": "apollo", "original_source_at": "2026-01-05T00:00:00Z",
        "latest_source": "apollo", "latest_source_detail": "list A",
        "latest_source_platform": "apollo", "latest_source_at": "2026-02-05T00:00:00Z",
        "personalization": {"first_name": "Ada"},
        "email_verification_status": "valid",
        "latest_email_verification_source": "millionverifier",
        "email_verified_at": "2026-03-01T00:00:00Z",
        "stage": "contacted",
    }),
    # Minimal created path: no LinkedIn at all, so the gated promote pass must no-op.
    _core(2, "bob@northwind-example.com", {
        "email": "bob@northwind-example.com", "name": "Bob Synth",
    }),
    # Shares a company with #1 (exercises the company cache).
    _core(3, "cleo@northwind-example.com", {
        "email": "cleo@northwind-example.com", "name": "Cleo Synth",
        "company": "Northwind Example", "industry": "Software",
    }),
    # company_domain overrides the email domain -> the company relink branch.
    _core(4, "dan@gmail.com", {
        "email": "dan@gmail.com", "name": "Dan Synth",
        "company": "Contoso Example", "company_domain": "contoso-example.com",
    }),
    # LinkedIn only, no email.
    _core(5, LI2, {"linkedin": LI2, "name": "Eve Synth", "company": "Initech Example"}),
    # Name-only company, no domain anywhere.
    _core(6, "EXT-9", {
        "external_id": "EXT-9", "name": "Fay Synth", "company": "Nodomain Example",
    }),
    # "Self-Employed" must not bucket everyone into one shared company row.
    _core(7, "gus@soleprop-example.com", {
        "email": "gus@soleprop-example.com", "name": "Gus Synth",
        "company": "Self-Employed",
    }),
]

PAGE_2 = [
    # Matched by entity_key, so resolve_lead never runs and apply carries the
    # whole update -- including the location change, which must NOT bump
    # updated_at.
    _core(8, "ada@northwind-example.com", {
        "email": "ada@northwind-example.com",
        "title": "SVP Eng", "notes": "second note", "location_city": "Denver",
        "latest_source": "clay", "latest_source_platform": "clay",
        "latest_source_at": "2026-04-01T00:00:00Z",
    }, ts="2026-06-02T10:00:00Z"),
    # Created path carrying its own LinkedIn.
    _core(9, "hal@initech-example.com", {
        "email": "hal@initech-example.com", "name": "Hal Synth", "linkedin": LI3,
    }, ts="2026-06-02T11:00:00Z"),
    # New lead on a company first seen on page 1 (cross-page company cache).
    _core(10, "iris@northwind-example.com", {
        "email": "iris@northwind-example.com", "name": "Iris Synth",
        "company": "Northwind Example", "headcount": "51-200",
    }, ts="2026-06-02T12:00:00Z"),
    # Keyed by LinkedIn URL rather than email: resolves to Ada through the
    # identity table, again apply-only. A payload with no source fields must
    # leave her attribution alone.
    _core(11, LI, {"linkedin": LI, "title": "Chief Eng"}, ts="2026-06-02T13:00:00Z"),
]

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _normalize(value):
    """Collapse the two things that legitimately differ between runs.

    Identity ids are random blobs. Timestamps written as datetime('now') land on
    the current time -- payload-supplied timestamps are months old, so a tight
    window around now separates the two cleanly and keeps the payload ones
    asserted verbatim.
    """
    if not isinstance(value, str):
        return value
    if _HEX32.match(value):
        return "<RANDOM_ID>"
    if _TS.match(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if abs(parsed - datetime.now(timezone.utc)) < timedelta(days=2):
            return "<NOW>"
    return value


def _dump() -> dict[str, list[tuple]]:
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out = {}
        for table in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
            rows = [
                tuple(f"{c}={_normalize(r[c])!r}" for c in cols)
                for r in conn.execute(f"SELECT * FROM {table}")
            ]
            if rows:
                out[table] = sorted(rows)
        return out
    finally:
        conn.close()


def _ingest_all(tmp_path, company_cache=None):
    # Fresh data root per run: these assertions are about a *first* pull into an
    # empty database, and re-running into a populated one is a different path.
    set_data_root_override(tmp_path)
    om.init_db()
    om.set_workspace_routing("single")
    for page in (PAGE_1, PAGE_2):
        _ingest_relay_page(page, quiet=True, company_cache=company_cache)
    return _dump()


def _fields(dump, table, match):
    row = next(r for r in dump[table] if match in r)
    return dict(f.split("=", 1) for f in row)


def test_lead_core_ingest_matches_golden(tmp_path):
    dump = _ingest_all(tmp_path)
    assert len(dump["leads"]) == 9  # 7 from page 1, + Hal and Iris from page 2

    ada = _fields(dump, "leads", "email='ada@northwind-example.com'")
    # Event 8 (keyed by email) then event 11 (keyed by LinkedIn) both resolved to
    # her and were applied.
    assert ada["title"] == "'Chief Eng'"
    assert ada["notes"] == "'second note'"
    assert ada["location_city"] == "'Denver'"
    # latest_source came from event 8's payload, not from "now" -- and event 11,
    # which carried no source fields, left it alone.
    assert ada["latest_source"] == "'clay'"
    assert ada["latest_source_at"] == "'2026-04-01T00:00:00Z'"
    # original_* attribution is never downgraded by a later snapshot.
    assert ada["original_source"] == "'apollo'"
    # linkedin_url was promoted from the identity row.
    assert ada["linkedin_url"] == "'linkedin.com/in/synthetic-one'"

    # The extras (secondary emails, external_id, sales-nav) are still registered
    # by the apply half under "agent_sync" -- resolve_lead must not adopt them and
    # relabel them with the payload's source platform.
    idents = [dict(f.split("=", 1) for f in row) for row in dump["lead_identities"]]
    extras = [
        i for i in idents
        if i["identity_value_normalized"] in (
            "'ada.alt@northwind-example.com'", "'ada2@northwind-example.com'", "'EXT-1'",
        )
    ]
    assert len(extras) == 3
    assert {i["source"] for i in extras} == {"'agent_sync'"}
    # ...while the profile-derived ones keep the payload's platform.
    email_ident = next(
        i for i in idents if i["identity_value_normalized"] == "'ada@northwind-example.com'"
    )
    assert email_ident["source"] == "'apollo'"

    # Bob's payload has no LinkedIn at all, so the (now gated) promote pass must
    # leave both LinkedIn columns null rather than inventing one.
    bob = _fields(dump, "leads", "email='bob@northwind-example.com'")
    assert bob["linkedin_url"] == "None"
    assert bob["linkedin_sales_nav_id"] == "None"

    companies = {dict(f.split("=", 1) for f in row)["name"] for row in dump["companies"]}
    # "Self-Employed" describes employment status; it must never become a company.
    assert "'Self-Employed'" not in companies
    # company_domain overrode dan@gmail.com's (shared, non-company) email domain.
    assert "'Contoso Example'" in companies
    # Northwind was created once and reused across both pages.
    northwind = [
        r for r in dump["companies"] if "name='Northwind Example'" in r
    ]
    assert len(northwind) == 1


def test_pull_scoped_company_cache_does_not_change_the_result(tmp_path):
    """The cache is held across pages for speed; it must not alter what's written."""
    page_scoped = _ingest_all(tmp_path / "a", company_cache=None)
    pull_scoped = _ingest_all(tmp_path / "b", company_cache={})
    assert page_scoped == pull_scoped
