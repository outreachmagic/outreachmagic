"""Cross-platform lead snapshot build/apply for relay sync."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from activity_sync import (
    apply_activity_sync_payload,
    attach_activity_to_sync_payload,
    compute_lead_activity_from_events,
    _read_workspace_activity_row,
)
from constants import COMPANY_DOMAIN_SQL
from db_conn import get_conn
from workspace_routing import (
    DEFAULT_ORG_ID,
    ensure_organization,
    import_extra_from_entity_key,
    lead_external_id_value,
    normalize_linkedin,
    normalize_linkedin_sales_nav_id,
    parse_entity_key,
    parse_linkedin_value,
    upsert_all_identities,
    upsert_workspace_lead,
)

# `stage` is deliberately absent. Pipeline stage is a per-workspace fact
# (workspace_leads.status) -- a lead can sit at different stages in different
# workspaces, so an org-wide stage is ill-defined by construction. leads.stage
# survives only as a derived cache for org-wide reporting, maintained from
# workspace_leads by trigger; it is never transmitted and never authoritative.
SYNC_PROFILE_FIELDS = (
    "name", "title", "notes",
    "location_city", "location_state", "location_country",
    "email_verification_status",
    "linkedin_headline", "linkedin_bio", "linkedin_sales_nav_id",
)

WORKSPACE_ACTIVITY_SELECT = """
    status, current_status_label, current_status_sentiment, current_sentiment_since,
    contact_priority,
    COALESCE(last_contacted_at, last_activity_at) AS last_contacted_at,
    last_activity_at, email_sent_count, linkedin_sent_count, total_replies_count
"""


def _personalization_sync_payload(rows: dict) -> tuple[dict, dict, Optional[str]]:
    values = {k: v["field_value"] for k, v in rows.items()}
    dates = {k: v["field_date"] for k, v in rows.items() if v.get("field_date")}
    at = max((v["processed_at"] for v in rows.values()), default=None)
    return values, dates, at


def _resolve_workspace_identity(conn, workspace_slug: str):
    from pipeline import resolve_workspace_identity

    return resolve_workspace_identity(conn, workspace_slug)


def _prefetch_membership(
    prefetch: dict,
    lead_id: int,
    *,
    workspace_id: Optional[str] = None,
    workspace_slug: Optional[str] = None,
) -> Optional[dict]:
    for mem in prefetch.get("memberships", {}).get(lead_id, []):
        if workspace_id and mem["workspace_id"] == workspace_id:
            return mem
        if workspace_slug and mem["slug"] == workspace_slug:
            return mem
    return None


def _resolve_sync_workspace(
    conn: sqlite3.Connection,
    lead_id: int,
    workspace_slug: Optional[str],
    prefetch: Optional[dict],
) -> tuple[Optional[str], Optional[sqlite3.Row]]:
    ws_id = None
    if workspace_slug:
        ws_row = _resolve_workspace_identity(conn, workspace_slug)
        ws_id = ws_row["id"] if ws_row else None
    if prefetch:
        mem = _prefetch_membership(
            prefetch,
            lead_id,
            workspace_id=ws_id,
            workspace_slug=workspace_slug if not ws_id else None,
        )
        if mem:
            return mem["workspace_id"], mem["wl_row"]
        if ws_id is None:
            mems = prefetch.get("memberships", {}).get(lead_id, [])
            if len(mems) == 1:
                return mems[0]["workspace_id"], mems[0]["wl_row"]
    if ws_id is None:
        wl = conn.execute(
            "SELECT workspace_id FROM workspace_leads WHERE lead_id = ? LIMIT 1",
            (lead_id,),
        ).fetchone()
        if wl:
            ws_id = wl["workspace_id"]
    if ws_id and prefetch:
        mem = _prefetch_membership(prefetch, lead_id, workspace_id=ws_id)
        if mem:
            return ws_id, mem["wl_row"]
    if ws_id:
        wl_row = conn.execute(
            f"""SELECT {WORKSPACE_ACTIVITY_SELECT}
                FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchone()
        return ws_id, wl_row
    return None, None


def _assemble_lead_core_sync_payload(
    row: sqlite3.Row,
    *,
    identity_rows: list,
    external_id: Optional[str],
    personalization_rows: list,
    provider_observation_rows: Optional[list] = None,
) -> dict:
    """Org-wide lead profile for relay core snapshot."""
    payload: dict = {}
    for field in SYNC_PROFILE_FIELDS:
        val = row[field]
        if val is not None and str(val).strip():
            payload[field] = val
    if row["email"]:
        payload["email"] = row["email"]
    if row["linkedin_url"]:
        payload["linkedin"] = row["linkedin_url"]
    if row.get("company_domain"):
        payload["company_domain"] = row["company_domain"]
    primary_email_norm = (row["email"] or "").strip().lower()
    secondary_emails = []
    for id_row in identity_rows:
        if id_row["identity_type"] == "email":
            val = id_row["identity_value_normalized"]
            if val and val != primary_email_norm:
                secondary_emails.append(val)
            continue
        if id_row["identity_type"] == "linkedin_sales_nav_id":
            # Normally already set above from SYNC_PROFILE_FIELDS/
            # row["linkedin_sales_nav_id"], which holds the canonical mixed
            # case -- lead_identities stores this lowercase on purpose (the
            # match key, see _sales_nav_match_key), so blindly assigning it
            # here would clobber the mixed-case value with the lowercase one,
            # same bug the aliases loop below already guards against for the
            # same reason. Only fall back to the lowercase identity value in
            # the rare case promote_linkedin_sales_nav_id_from_identities
            # never ran (a field conflict with another lead blocked it), so
            # the payload isn't missing the field entirely.
            if not payload.get("linkedin_sales_nav_id"):
                payload["linkedin_sales_nav_id"] = id_row["identity_value_normalized"]
            continue
        payload[id_row["identity_type"]] = id_row["identity_value_normalized"]
    if secondary_emails:
        payload["secondary_emails"] = secondary_emails

    # Aliases: every natural identifier this lead can also be known by. The relay
    # keys snapshots by the immutable uid, but inbound webhooks arrive keyed by
    # whatever the vendor has (prosp sends a LinkedIn URL, plusvibe an email), so
    # the relay needs this map to resolve a webhook back to the right uid.
    aliases: list[str] = []
    if row["email"]:
        aliases.append(str(row["email"]).strip().lower())
    if row["linkedin_url"]:
        aliases.append(str(row["linkedin_url"]).strip())
    if row["linkedin_sales_nav_id"]:
        aliases.append(f"linkedin_sales_nav_id:{str(row['linkedin_sales_nav_id']).strip()}")
    for id_row in identity_rows:
        itype, val = id_row["identity_type"], id_row["identity_value_normalized"]
        if not val:
            continue
        # Sales-nav is already emitted above from the leads column (which holds
        # the canonical mixed case); the identity row is lowercase, so appending
        # it here would ship two aliases for the same identity in different cases.
        if itype == "linkedin_sales_nav_id":
            continue
        aliases.append(val if itype == "email" else f"{itype}:{val}")
    seen: set[str] = set()
    aliases = [a for a in aliases if a and not (a in seen or seen.add(a))]
    if aliases:
        payload["aliases"] = aliases

    if row["latest_sender"]:
        payload["latest_sender"] = row["latest_sender"]
    if row["latest_sender_platform"]:
        payload["latest_sender_platform"] = row["latest_sender_platform"]
    if row["email_verified_at"]:
        payload["email_verified_at"] = row["email_verified_at"]
    if "email_verification_status" in row.keys() and row["email_verification_status"]:
        payload["email_verification_status"] = row["email_verification_status"]
    if "original_email_verification_source" in row.keys() and row["original_email_verification_source"]:
        payload["original_email_verification_source"] = row["original_email_verification_source"]
    if "latest_email_verification_source" in row.keys() and row["latest_email_verification_source"]:
        payload["latest_email_verification_source"] = row["latest_email_verification_source"]
    if external_id:
        payload["external_id"] = external_id
    # `list_source` and `import_name` were verbatim copies of latest_source_detail
    # and original_source_detail, and `email_verification_source` a copy of
    # latest_email_verification_source -- three strings sent twice on every one of
    # ~150k lead payloads. The originals below are the single source of truth.
    #
    # *_source_platform is dropped from the wire: 85% of its values are the
    # transport ("relay"), not a provenance fact. The local columns and their
    # backfill are Stage 8; this stops the lie propagating now.
    for field in (
        "original_source",
        "original_source_detail",
        "original_source_at",
        "latest_source",
        "latest_source_detail",
        "latest_source_at",
    ):
        val = row[field]
        if val is not None and str(val).strip():
            payload[field] = val
    if personalization_rows:
        pers = {
            r["field_name"]: {
                "field_value": r["field_value"],
                "field_date": r["field_date"],
                "processed_at": r["processed_at"],
            }
            for r in personalization_rows
        }
        values, dates, at = _personalization_sync_payload(pers)
        payload["personalization"] = values
        if dates:
            payload["personalization_dates"] = dates
        if at:
            payload["personalization_at"] = at
    if provider_observation_rows:
        # Stage 7: the full observation history, not just "latest per provider"
        # (that's what the legacy provider_attempts key used to carry). Apply
        # keeps *accepting* the old key for the ~150k D1 snapshots that already
        # carry it (see apply_agent_lead_core_payload) -- only emission moved.
        # Drop null fields from each observation: the apply side defaults every
        # column to None (see apply_provider_observations_payload), so an omitted
        # key round-trips identically while cutting the snapshot size — many
        # observations carry only a handful of the 17 columns. Falsy-but-present
        # values (False / 0 / "") are meaningful and kept.
        payload["provider_observations"] = [
            {k: v for k, v in {
                "kind": r["kind"],
                "origin": r["origin"],
                "provider": r["provider"],
                "email": r["email"],
                "status": r["status"],
                "sub_status": r["sub_status"],
                "domain": r["domain"],
                "source_detail": r["source_detail"],
                "bounce_message": r["bounce_message"],
                "free_email": r["free_email"],
                "mx_found": r["mx_found"],
                "smtp_provider": r["smtp_provider"],
                "result_email": r["result_email"],
                "result_validity": r["result_validity"],
                "observed_at": r["observed_at"],
                "completed_at": r["completed_at"],
                "metadata_json": r["metadata_json"],
            }.items() if v is not None}
            for r in provider_observation_rows
        ]
    return payload


def _assemble_lead_workspace_sync_payload(
    conn: sqlite3.Connection,
    lead_id: int,
    row: sqlite3.Row,
    *,
    ws_id: str,
    wl_row: sqlite3.Row,
    tags: list[str],
    linkedin_status: list,
) -> dict:
    """Per-workspace pipeline state for relay workspace snapshot."""
    payload: dict = {}
    if wl_row["current_status_label"]:
        payload["lead_status"] = wl_row["current_status_label"]
    if wl_row["current_status_sentiment"]:
        payload["lead_sentiment"] = wl_row["current_status_sentiment"]
        # Ship the run-start alongside the sentiment so a rebuild from the relay
        # restores the same anchor the campaigns view groups on.
        if wl_row["current_sentiment_since"]:
            payload["sentiment_since"] = wl_row["current_sentiment_since"]
    if wl_row["contact_priority"] is not None:
        payload["contact_order"] = wl_row["contact_priority"]
    if wl_row["status"]:
        # Unconditionally. This used to be emitted only when it differed from
        # leads.stage, which made the workspace snapshot's stage depend on the
        # *core* snapshot's stage -- rebuild workspace state from the relay and a
        # lead whose workspace status happened to equal the org-wide stage came
        # back with no stage at all. Emitted as `stage` now (this *is* the stage;
        # there is no other). Apply still accepts the legacy `workspace_stage` key,
        # which ~140k snapshots already in D1 carry.
        payload["stage"] = wl_row["status"]
    # Sorted, not insertion order: tags added in the same batch import can share
    # one created_at second, so the ORDER BY that ranks them ties arbitrarily --
    # and the prefetch batch path's tag list isn't guaranteed to agree with the
    # single-lookup path's either. An unsorted list here makes content_hash
    # unstable across two otherwise-identical builds of the same payload, which
    # looks like a real change and triggers a needless re-push forever.
    payload["tags"] = sorted(tags)
    if linkedin_status:
        payload["linkedin_status"] = [
            {
                "sender_profile": r["sender_profile"],
                "is_connected": bool(r["is_connected"]),
                "is_request_pending": bool(r["is_request_pending"]),
            }
            for r in linkedin_status
        ]
    attach_activity_to_sync_payload(
        payload, conn, lead_id, workspace_id=ws_id, wl_row=wl_row,
    )
    # Include CRM entity map so fresh-install pulls restore GHL/HubSpot linkage
    crm_rows = conn.execute(
        """SELECT platform, crm_contact_id, crm_deal_id, crm_company_id,
                  crm_owner_id, last_synced_at, last_event_id_synced,
                  last_sync_status, sync_hash, crm_note_id
           FROM crm_entity_map
           WHERE workspace_id = ? AND lead_id = ?""",
        (ws_id, lead_id),
    ).fetchall()
    if crm_rows:
        payload["crm_entity_map"] = [dict(r) for r in crm_rows]
    return payload


def _load_lead_sync_prefetch(
    conn: sqlite3.Connection,
    org_id: str,
    lead_ids: list[int],
) -> dict:
    """Bulk-load rows used by build_lead_sync_payload for many leads at once."""
    if not lead_ids:
        return {
            "leads": {},
            "identities": {},
            "external_ids": {},
            "memberships": {},
            "personalization": {},
        }

    placeholders = ",".join("?" for _ in lead_ids)
    leads = {
        r["id"]: r
        for r in conn.execute(
            f"""SELECT l.*,
                       {COMPANY_DOMAIN_SQL},
                       co.hq_city AS hq_city,
                       co.hq_state AS hq_state,
                       co.hq_country AS hq_country,
                       COALESCE(co.name, l.company) AS company_display
                FROM leads l
                LEFT JOIN companies co ON l.company_id = co.id
                WHERE l.id IN ({placeholders})""",
            lead_ids,
        ).fetchall()
    }

    identities: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT lead_id, identity_type, identity_value_normalized
            FROM lead_identities
            WHERE org_id = ? AND lead_id IN ({placeholders})
              AND identity_type IN ('linkedin_sales_nav_id', 'linkedin_member_id', 'email')""",
        [org_id, *lead_ids],
    ).fetchall():
        identities[r["lead_id"]].append(r)

    # A merge can leave a lead with more than one external_id row (each merged
    # lead brings its own). ORDER BY ascending + unconditional overwrite means
    # the last row seen per lead_id -- the most recently recorded one -- wins,
    # matching lead_external_id_value()'s single-lookup ordering exactly. If
    # these two disagree, a lead's payload flips depending on which code path
    # built it (bulk push vs sync-preview/sync-diff), which is exactly the bug
    # this comment is here to prevent regressing.
    external_ids: dict[int, str] = {}
    for r in conn.execute(
        f"""SELECT lead_id, identity_value_normalized
            FROM lead_identities
            WHERE org_id = ? AND lead_id IN ({placeholders}) AND identity_type = 'external_id'
            ORDER BY created_at ASC, id ASC""",
        [org_id, *lead_ids],
    ).fetchall():
        external_ids[r["lead_id"]] = r["identity_value_normalized"]

    workspace_slugs: dict[int, str] = {}
    memberships: dict[int, list[dict]] = {lid: [] for lid in lead_ids}
    membership_index: dict[tuple[int, str], dict] = {}
    for r in conn.execute(
        f"""SELECT wl.lead_id, wl.workspace_id, w.slug, wl.status, wl.current_status_label,
                   wl.current_status_sentiment, wl.current_sentiment_since, wl.contact_priority,
                   COALESCE(wl.last_contacted_at, wl.last_activity_at) AS last_contacted_at,
                   wl.last_activity_at, wl.email_sent_count, wl.linkedin_sent_count,
                   wl.total_replies_count
            FROM workspace_leads wl
            JOIN workspaces w ON wl.workspace_id = w.id
            WHERE wl.lead_id IN ({placeholders})
            ORDER BY w.slug, wl.workspace_id""",
        lead_ids,
    ).fetchall():
        workspace_slugs.setdefault(r["lead_id"], r["slug"])
        mem = {
            "workspace_id": r["workspace_id"],
            "slug": r["slug"],
            "wl_row": r,
            "tags": [],
            "linkedin_status": [],
        }
        memberships[r["lead_id"]].append(mem)
        membership_index[(r["lead_id"], r["workspace_id"])] = mem

    for r in conn.execute(
        f"""SELECT wl.lead_id, wl.workspace_id, wlt.tag
            FROM workspace_lead_tags wlt
            JOIN workspace_leads wl ON wl.workspace_id = wlt.workspace_id AND wl.lead_id = wlt.lead_id
            WHERE wl.lead_id IN ({placeholders})
            ORDER BY wlt.created_at""",
        lead_ids,
    ).fetchall():
        mem = membership_index.get((r["lead_id"], r["workspace_id"]))
        if mem:
            mem["tags"].append(r["tag"])

    for r in conn.execute(
        f"""SELECT wl.lead_id, wl.workspace_id, lis.sender_profile, lis.is_connected,
                   lis.is_request_pending
            FROM workspace_lead_linkedin_status lis
            JOIN workspace_leads wl ON wl.workspace_id = lis.workspace_id AND wl.lead_id = lis.lead_id
            WHERE wl.lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        mem = membership_index.get((r["lead_id"], r["workspace_id"]))
        if mem:
            mem["linkedin_status"].append(r)

    personalization: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT lead_id, field_name, field_value, field_date, processed_at
            FROM lead_personalization
            WHERE lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        personalization[r["lead_id"]].append(r)

    # Base table, not the lead_provider_attempts/lead_email_verification compat
    # VIEWs: those each project only the *latest* row per provider (Stage 7),
    # but the wire payload carries the full append-only history.
    provider_observations: dict[int, list] = {lid: [] for lid in lead_ids}
    for r in conn.execute(
        f"""SELECT lead_id, kind, origin, provider, email, status, sub_status, domain,
                   source_detail, bounce_message, free_email, mx_found, smtp_provider,
                   result_email, result_validity, observed_at, completed_at, metadata_json
            FROM lead_provider_observations
            WHERE lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall():
        provider_observations[r["lead_id"]].append(r)

    return {
        "leads": leads,
        "identities": identities,
        "external_ids": external_ids,
        "workspace_slugs": workspace_slugs,
        "memberships": memberships,
        "personalization": personalization,
        "provider_observations": provider_observations,
    }


def entity_key_from_prefetch(prefetch: dict, lead_id: int) -> str:
    """The lead's immutable relay key: uid:<uid>. Mirrors lead_entity_key().

    Previously this derived the key from email > linkedin_url > first identity,
    and returned "" when a lead had none of those -- which the push loop treats as
    "skip", so 2,830 real leads never reached the relay. It also disagreed with
    lead_entity_key(), which had a name+company fallback this one lacked.

    Both now return the uid, so there is nothing left to diverge on and no lead is
    unpushable.
    """
    row = prefetch["leads"].get(lead_id)
    if not row:
        return ""
    uid = row["uid"] if "uid" in row.keys() else None
    return f"uid:{uid}" if uid else ""


def _lead_row_for_sync(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    prefetch: Optional[dict] = None,
) -> Optional[sqlite3.Row]:
    if prefetch is not None:
        return prefetch["leads"].get(lead_id)
    return conn.execute(
        f"""SELECT l.*,
                  {COMPANY_DOMAIN_SQL},
                  co.hq_city AS hq_city,
                  co.hq_state AS hq_state,
                  co.hq_country AS hq_country,
                  COALESCE(co.name, l.company) AS company_display
           FROM leads l
           LEFT JOIN companies co ON l.company_id = co.id
           WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()


def build_lead_core_sync_payload(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    prefetch: Optional[dict] = None,
) -> dict:
    """Org-wide lead profile for relay lead_core_update."""
    row = _lead_row_for_sync(conn, org_id, lead_id, prefetch=prefetch)
    if not row:
        return {}
    if prefetch is not None:
        identity_rows = prefetch["identities"].get(lead_id) or []
        external_id = prefetch["external_ids"].get(lead_id)
        personalization_rows = prefetch["personalization"].get(lead_id) or []
        provider_observation_rows = prefetch.get("provider_observations", {}).get(lead_id) or []
    else:
        identity_rows = conn.execute(
            """SELECT identity_type, identity_value_normalized FROM lead_identities
               WHERE org_id = ? AND lead_id = ?
                 AND identity_type IN ('linkedin_sales_nav_id', 'linkedin_member_id', 'email')""",
            (org_id, lead_id),
        ).fetchall()
        external_id = lead_external_id_value(conn, org_id, lead_id)
        personalization_rows = conn.execute(
            "SELECT field_name, field_value, field_date, processed_at FROM lead_personalization WHERE lead_id = ?",
            (lead_id,),
        ).fetchall()
        # Base table, not the compat VIEWs: the wire payload carries the full
        # append-only history, not just the latest row per provider.
        provider_observation_rows = conn.execute(
            """SELECT kind, origin, provider, email, status, sub_status, domain,
                      source_detail, bounce_message, free_email, mx_found, smtp_provider,
                      result_email, result_validity, observed_at, completed_at, metadata_json
               FROM lead_provider_observations WHERE lead_id = ?""",
            (lead_id,),
        ).fetchall()
    row_dict = dict(row)
    original_lev, latest_lev = _lev_sources_for_lead(conn, lead_id)
    if original_lev:
        row_dict["original_email_verification_source"] = original_lev
    if latest_lev:
        row_dict["latest_email_verification_source"] = latest_lev
    return _assemble_lead_core_sync_payload(
        row_dict,
        provider_observation_rows=provider_observation_rows,
        identity_rows=identity_rows,
        external_id=external_id,
        personalization_rows=personalization_rows,
    )


def build_lead_workspace_sync_payload(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    workspace_slug: str,
    prefetch: Optional[dict] = None,
) -> dict:
    """Per-workspace pipeline state for relay lead_workspace_update."""
    row = _lead_row_for_sync(conn, org_id, lead_id, prefetch=prefetch)
    if not row:
        return {}
    ws_id, wl_row = _resolve_sync_workspace(conn, lead_id, workspace_slug, prefetch)
    if not ws_id or not wl_row:
        return {}
    if prefetch is not None:
        mem = _prefetch_membership(prefetch, lead_id, workspace_id=ws_id)
        tags = mem["tags"] if mem else []
        linkedin_status = mem["linkedin_status"] if mem else []
    else:
        tags = [
            r["tag"]
            for r in conn.execute(
                "SELECT tag FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ? ORDER BY created_at",
                (ws_id, lead_id),
            ).fetchall()
        ]
        linkedin_status = conn.execute(
            """SELECT sender_profile, is_connected, is_request_pending
               FROM workspace_lead_linkedin_status
               WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchall()
    return _assemble_lead_workspace_sync_payload(
        conn, lead_id, row,
        ws_id=ws_id,
        wl_row=wl_row,
        tags=tags,
        linkedin_status=linkedin_status,
    )


def build_lead_sync_payload(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    workspace_slug: Optional[str] = None,
    prefetch: Optional[dict] = None,
) -> dict:
    """Merged core + workspace payload for inspect/export only; relay push uses split snapshots."""
    core = build_lead_core_sync_payload(conn, org_id, lead_id, prefetch=prefetch)
    if not workspace_slug:
        ws_id, _ = _resolve_sync_workspace(conn, lead_id, None, prefetch)
        if prefetch and ws_id:
            mems = prefetch.get("memberships", {}).get(lead_id) or []
            workspace_slug = mems[0]["slug"] if len(mems) == 1 else None
        elif ws_id:
            wl = conn.execute(
                "SELECT w.slug FROM workspaces w JOIN workspace_leads wl ON wl.workspace_id = w.id WHERE wl.lead_id = ? LIMIT 1",
                (lead_id,),
            ).fetchone()
            workspace_slug = wl["slug"] if wl else None
    ws_payload = (
        build_lead_workspace_sync_payload(
            conn, org_id, lead_id, workspace_slug=workspace_slug, prefetch=prefetch,
        )
        if workspace_slug
        else {}
    )
    merged = dict(core)
    merged.update(ws_payload)
    return merged


def _attribution_from_sync_payload(payload: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Map relay lead_core snapshot fields to resolve_lead source attribution.

    Returns None (not a transport string like "agent_sync"/"relay") when the
    payload carries no real provenance -- unknown-source is the honest state
    and the abort trigger blocks anything from re-filling the column with
    transport garbage.
    """
    source = (
        (payload.get("original_source") or "").strip()
        or (payload.get("latest_source") or "").strip()
        or None
    )
    source_detail = (
        (payload.get("original_source_detail") or "").strip()
        or (payload.get("latest_source_detail") or "").strip()
        or None
    )
    source_platform = (
        (payload.get("original_source_platform") or "").strip()
        or (payload.get("latest_source_platform") or "").strip()
        or None
    )
    return source, source_detail, source_platform


_WEAK_VERIFICATION_SOURCES = frozenset({"agent_sync", "relay_sync", "platform_bounce", ""})


def _is_weak_verification_source(source: Optional[str]) -> bool:
    return (source or "").strip() in _WEAK_VERIFICATION_SOURCES


def _lev_sources_for_lead(conn: sqlite3.Connection, lead_id: int) -> tuple[Optional[str], Optional[str]]:
    """Return (original_lev_source, latest_lev_source) from tool verification rows."""
    rows = conn.execute(
        """SELECT source, verified_at FROM lead_email_verification
           WHERE lead_id = ? AND source != 'platform_bounce'
           ORDER BY verified_at ASC""",
        (lead_id,),
    ).fetchall()
    tool_rows = [r for r in rows if not _is_weak_verification_source(r["source"])]
    if not tool_rows:
        return None, None
    original = (tool_rows[0]["source"] or "").strip() or None
    latest = (tool_rows[-1]["source"] or "").strip() or None
    return original, latest


def _attribution_sets(payload: dict) -> tuple[list[str], list]:
    """SET clauses restoring source attribution from a relay lead_core snapshot.

    Split out from apply_attribution_from_sync_payload so the caller can fold
    these into a larger single UPDATE on `leads` instead of issuing another one.

    original_* is COALESCE-preserved (first-touch attribution never gets
    overwritten by a later snapshot); latest_* always takes the payload's
    value. The DB-level abort trigger (pipeline_migration.py's
    _install_provenance_transport_guard) is the sole enforcement against a
    transport string ("agent_sync"/"relay_sync"/"relay") landing in a
    provenance column -- there is no live writer left that could still send
    one for this to scrub.
    """
    sets: list[str] = []
    params: list = []
    for col in (
        "original_source",
        "original_source_detail",
        "original_source_platform",
        "original_source_at",
    ):
        val = payload.get(col)
        if val is not None and str(val).strip():
            sets.append(f"{col} = COALESCE({col}, ?)")
            params.append(val)
    for col in (
        "latest_source",
        "latest_source_detail",
        "latest_source_platform",
        "latest_source_at",
    ):
        val = payload.get(col)
        if val is not None and str(val).strip():
            sets.append(f"{col} = ?")
            params.append(val)
    return sets, params


def apply_attribution_from_sync_payload(
    conn: sqlite3.Connection,
    lead_id: int,
    payload: dict,
) -> None:
    """Restore source attribution from relay lead_core snapshot."""
    sets, params = _attribution_sets(payload)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(lead_id)
    conn.execute(
        f"UPDATE leads SET {', '.join(sets)} WHERE id = ?",
        params,
    )


def agent_sync_extra_identities(
    entity_key: Optional[str],
    payload: dict,
) -> list[tuple[str, str]]:
    """Identities a lead_core payload carries beyond what its profile implies.

    build_import_identities() (which resolve_lead runs on the profile) already
    derives email / linkedin_url / external_id. These are the extras only the
    relay snapshot knows: an explicit sales-nav id, secondary emails, and the
    entity key itself. Shared by the resolve and apply halves of the lead_core
    path so both agree on one identity set and only one of them has to write it.
    """
    out: list[tuple[str, str]] = []

    def _add(itype: str, val: Optional[str]) -> None:
        if val and not any(t == itype and v == val for t, v in out):
            out.append((itype, val))

    if payload.get("external_id"):
        _add("external_id", str(payload["external_id"]))
    if payload.get("linkedin"):
        for itype, val in parse_linkedin_value(str(payload["linkedin"])):
            _add(itype, val)
    if payload.get("linkedin_sales_nav_id"):
        _add(
            "linkedin_sales_nav_id",
            normalize_linkedin_sales_nav_id(str(payload["linkedin_sales_nav_id"])),
        )
    for addr in payload.get("secondary_emails") or []:
        _add("email", str(addr).strip().lower())
    itype, val = parse_entity_key(entity_key or "")
    if itype and val and itype != "email":
        _add(itype, val)
    return out


def _payload_can_create_lead(payload: dict, entity_key: str) -> bool:
    """Is there enough here to be a lead at all?

    A uid is NOT enough. It identifies a lead that already exists somewhere; if
    we cannot find that lead locally, minting an empty row under the same uid
    produces something that can never be matched, enriched or sent to -- there
    is literally nothing to search on. Any real identifier or profile text
    qualifies, including one carried by a typed entity_key (email:…,
    linkedin:…, external_id:…), which is a legitimate first sighting.
    """
    from workspace_routing import parse_entity_key

    payload = payload or {}
    for key in ("email", "linkedin", "linkedin_url", "linkedin_sales_nav_id",
                "external_id", "company", "title", "company_domain"):
        if str(payload.get(key) or "").strip():
            return True
    name = str(payload.get("name") or "").strip()
    if name and name.lower() != "unknown":
        return True

    key = str(entity_key or "").strip()
    if not key:
        return False
    if key.startswith("uid:"):
        return False
    if "@" in key:                       # bare-email key
        return True
    itype, val = parse_entity_key(key)
    return bool(itype and val and itype != "uid")


def resolve_lead_from_agent_sync(
    entity_key: str,
    payload: dict,
    *,
    stage: str = "prospecting",
    conn: Optional[sqlite3.Connection] = None,
    company_cache: Optional[dict] = None,
) -> dict:
    """Create or match a lead from a relay agent entry (uses entity_key + full payload).

    Refuses to CREATE from a payload that carries no identity and no profile.
    Updating an existing lead is unaffected -- by the time this is called the
    caller has already failed to find one locally.

    This is the junk-lead factory named in find_lead_by_identifier's comment:
    the lead_workspace_update path calls in with an EMPTY payload ({}), so any
    entity_key that fails to resolve locally minted a name="Unknown" lead with
    no email, no linkedin, no company and no events. A single `pull --full` on
    2026-07-13 produced 10,457 of them in 17 minutes -- every uid-keyed relay
    snapshot whose payload had nothing in it, mostly stale rows left over from
    the pre-uid rekey. They are not recoverable data and cannot be enriched:
    there is nothing to search on.
    """
    from pipeline import resolve_lead

    extra = dict(import_extra_from_entity_key(entity_key))
    if not _payload_can_create_lead(payload, entity_key):
        return {
            "status": "error",
            "error": "empty snapshot: no identity or profile to create a lead from",
            "reason": "empty_snapshot",
            "entity_key": entity_key,
            # resolve_lead would also have refused this, one step later, as a
            # weak identity. Keep that flag set so callers (and the identity
            # guard tests) still see the same contract -- this only refuses
            # earlier, and says why more precisely.
            "weak_identity": True,
        }
    if payload.get("external_id"):
        extra["external_id"] = str(payload["external_id"])
    if payload.get("list_source"):
        extra["list_source"] = str(payload["list_source"])
    if payload.get("import_name"):
        extra["import_name"] = str(payload["import_name"])
    if payload.get("company_domain"):
        extra["company_domain"] = str(payload["company_domain"])
    source, source_detail, source_platform = _attribution_from_sync_payload(payload)
    return resolve_lead(
        email=payload.get("email"),
        linkedin_url=payload.get("linkedin"),
        name=payload.get("name", "Unknown"),
        company=payload.get("company"),
        title=payload.get("title"),
        industry=payload.get("industry"),
        headcount=payload.get("headcount"),
        stage=payload.get("stage") or stage,
        notes=payload.get("notes"),
        company_domain=payload.get("company_domain"),
        location_city=payload.get("location_city"),
        location_state=payload.get("location_state"),
        location_country=payload.get("location_country"),
        hq_city=payload.get("hq_city"),
        hq_state=payload.get("hq_state"),
        hq_country=payload.get("hq_country"),
        import_extra=extra,
        import_batch=payload.get("import_batch_id"),
        source=source,
        source_detail=source_detail,
        source_platform=source_platform,
        overwrite=True,
        conn=conn,
        company_cache=company_cache,
    )


def apply_agent_lead_core_payload(
    lead_id: int,
    payload: dict,
    *,
    org_id: str = DEFAULT_ORG_ID,
    entity_key: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    company_cache: Optional[dict] = None,
    resolved: Optional[dict] = None,
) -> None:
    """Apply org-wide lead profile from relay lead_core_update.

    resolved: the resolve_lead() result, when this same payload just created or
    matched the lead. Its identity set was already registered there, so we only
    upsert whatever it didn't cover (normally nothing).

    Every column this writes to `leads` goes out as a single UPDATE. It used to
    be up to five -- enrich_lead, locations, company link, notes, attribution --
    each rewriting the same 8-index row and bumping updated_at.
    """
    from bounces import verify_email
    from pipeline import ensure_company, _apply_personalization_payload
    from pipeline_utils import email_domain

    own_conn = conn is None
    if own_conn:
        conn = get_conn()

    # One read of the two columns the writes below actually depend on, in place
    # of the separate SELECTs enrich_lead / link_lead_company / attribution each
    # used to issue.
    row = conn.execute(
        "SELECT original_source, email_domain FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()

    sets: list[str] = []
    params: list = []
    # Only some of these columns used to travel with an updated_at bump, and the
    # distinction is load-bearing: leads.updated_at drives the timestamp-based
    # relay push, so a bump here sends the lead straight back to the relay it
    # just came from. Locations and the company link were deliberately "quiet"
    # writes; the profile, notes and attribution ones were not.
    bump_updated_at = False

    def _set(col: str, val) -> None:
        sets.append(f"{col} = ?")
        params.append(val)

    # Profile fields; was enrich_lead(overwrite=True), whose writes are
    # unconditional for every truthy value.
    for col in ("name", "title", "industry", "company", "headcount"):
        if payload.get(col):
            _set(col, payload[col])
            bump_updated_at = True

    for col in (
        "location_city", "location_state", "location_country",
        "linkedin_headline", "linkedin_bio",
    ):
        if payload.get(col):
            _set(col, payload[col])

    if payload.get("notes"):
        _set("notes", payload["notes"])
        bump_updated_at = True

    # Company link; was link_lead_company(company=None), i.e. company_id only,
    # resolved from the email domain.
    email = payload.get("email")
    if email:
        domain = email_domain(email)
    else:
        domain = ((row["email_domain"] or "").strip().lower() or None) if row else None
    company_id = ensure_company(conn, domain=domain, company_cache=company_cache)
    if company_id:
        _set("company_id", company_id)

    attr_sets, attr_params = _attribution_sets(payload)
    if attr_sets:
        sets.extend(attr_sets)
        params.extend(attr_params)
        bump_updated_at = True

    if sets:
        if bump_updated_at:
            sets.append("updated_at = datetime('now')")
        params.append(lead_id)
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", params)

    # resolve_lead already registered the identities it derived from the profile
    # (email, linkedin_url, ...) under the payload's own source platform. Anything
    # it covered would only come back as an ignored INSERT here, so drop it: what
    # remains is the genuinely new extras, still written under "agent_sync" exactly
    # as before. Skipping the overlap also skips the LinkedIn promote pass, which
    # resolve_lead has already run.
    identities = agent_sync_extra_identities(entity_key, payload)
    if resolved is not None:
        already = {(t, v) for t, v in (resolved.get("identities") or [])}
        identities = [pair for pair in identities if pair not in already]
    if identities:
        upsert_all_identities(conn, org_id, lead_id, identities, source="agent_sync")

    secondary = [
        (lead_id, str(addr).strip().lower())
        for addr in payload.get("secondary_emails") or []
        if str(addr).strip()
    ]
    if secondary:
        conn.executemany(
            "INSERT OR IGNORE INTO lead_emails (lead_id, email, is_primary) VALUES (?, ?, 0)",
            secondary,
        )

    personalization = payload.get("personalization")
    if personalization:
        _apply_personalization_payload(
            lead_id, payload, table="lead_personalization", id_col="lead_id", entity_id=lead_id,
            conn=conn,
        )

    # Stage 7 emits provider_observations; keep accepting the legacy
    # provider_attempts key too -- ~150k D1 snapshots already carry it and
    # must still replay on a `pull --full`.
    provider_observations = payload.get("provider_observations")
    if provider_observations:
        from provider_observations import apply_provider_observations_payload

        apply_provider_observations_payload(conn, lead_id, provider_observations)

    provider_attempts = payload.get("provider_attempts")
    if provider_attempts:
        from pipeline_provider_attempts import apply_provider_attempts_payload

        apply_provider_attempts_payload(conn, lead_id, provider_attempts)

    if own_conn:
        conn.commit()
        conn.close()

    # This whole block only exists for the ~150k pre-Stage-7 D1 snapshots that
    # carry email_verification_status/latest_email_verification_source but no
    # provider_observations array -- for those, it's the only way to
    # reconstruct a verification event. Once provider_observations is present
    # (true for every payload built after Stage 7 shipped), the real events
    # were already replayed above via apply_provider_observations_payload(),
    # and synthesizing another one here from the lead's *rolled-up* status is
    # not just redundant, it's wrong: email_verification_status can flip to
    # "bounced" from a platform bounce (see bounces._compute_verification_status),
    # which has nothing to do with lev_source (the last *tool* provider that
    # ran a check) -- calling verify_email(status="bounced", source=lev_source)
    # then fabricates a "millionverifier said bounced" observation that
    # MillionVerifier's API can never actually produce, duplicating the real
    # platform_bounce row under the wrong provider/kind every time this lead
    # gets pulled again.
    if not provider_observations:
        lev_source = (
            (payload.get("latest_email_verification_source")
             or payload.get("latest_lev_source")
             or payload.get("email_verification_source")
             or payload.get("lev_source") or "").strip()
        )
        if payload.get("email_verification_status") and lev_source and not _is_weak_verification_source(lev_source):
            verify_email(
                lead_id,
                str(payload["email_verification_status"]),
                lev_source,
                verified_at=payload.get("email_verified_at"),
                conn=None if own_conn else conn,
            )


def apply_agent_lead_workspace_payload(
    lead_id: int,
    payload: dict,
    *,
    org_id: str = DEFAULT_ORG_ID,
    workspace_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Apply per-workspace pipeline state from relay lead_workspace_update."""
    from pipeline import parse_tags_value

    status_label = (payload.get("lead_status") or "").strip().lower().replace("_", " ") or None
    status_sentiment = (payload.get("lead_sentiment") or "").strip().lower() or None
    sentiment_since = (payload.get("sentiment_since") or "").strip() or None
    contact_pri = None
    if payload.get("contact_order") is not None:
        try:
            contact_pri = int(payload["contact_order"])
        except (ValueError, TypeError):
            pass
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    ensure_organization(conn)
    upsert_workspace_lead(
        conn, org_id, workspace_id, lead_id,
        status=payload.get("stage", "prospecting"),
        current_status_label=status_label,
        current_status_sentiment=status_sentiment,
        current_sentiment_since=sentiment_since,
        contact_priority=contact_pri,
    )
    if "tags" in payload:
        conn.execute(
            "DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id = ?",
            (workspace_id, lead_id),
        )
        for tag in parse_tags_value(payload.get("tags")):
            tag_id = f"wlt_{workspace_id}_{lead_id}_{hashlib.md5(tag.encode()).hexdigest()[:8]}"
            conn.execute(
                """INSERT OR IGNORE INTO workspace_lead_tags (id, workspace_id, lead_id, tag)
                   VALUES (?, ?, ?, ?)""",
                (tag_id, workspace_id, lead_id, tag),
            )
    for li in payload.get("linkedin_status") or []:
        sender = normalize_linkedin(li.get("sender_profile"))
        if not sender:
            continue
        is_connected = bool(li.get("is_connected"))
        is_pending = bool(li.get("is_request_pending"))
        if not is_connected and not is_pending:
            continue
        now_ts = datetime.now(timezone.utc).isoformat()
        li_id = f"lis_{workspace_id}_{lead_id}_{sender[:20]}"
        conn.execute(
            """INSERT INTO workspace_lead_linkedin_status
               (id, workspace_id, lead_id, sender_profile, is_connected,
                is_request_pending, connected_at, request_sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (workspace_id, lead_id, sender_profile) DO UPDATE SET
                   is_connected = excluded.is_connected,
                   is_request_pending = excluded.is_request_pending,
                   updated_at = datetime('now')""",
            (li_id, workspace_id, lead_id, sender,
             int(is_connected), int(is_pending),
             now_ts if is_connected else None,
             now_ts if is_pending else None),
        )
    activity = payload.get("activity")
    if activity:
        apply_activity_sync_payload(
            conn, lead_id, workspace_id, activity, merge=True,
        )
    # Restore CRM entity map from relay snapshot so IDs survive refresh.
    # ON CONFLICT ... WHERE only fires the UPDATE (and therefore the
    # crm_entity_map bump triggers on leads/workspace_leads) when something
    # actually changed — re-applying an identical snapshot is a no-op.
    crm_map = payload.get("crm_entity_map")
    if crm_map:
        for entry in crm_map:
            platform = entry.get("platform", "")
            if not platform:
                continue
            conn.execute(
                """INSERT INTO crm_entity_map
                   (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                    crm_company_id, crm_owner_id, last_synced_at, last_event_id_synced,
                    last_sync_status, sync_hash, crm_note_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT (workspace_id, lead_id, platform) DO UPDATE SET
                       crm_contact_id = excluded.crm_contact_id,
                       crm_deal_id = excluded.crm_deal_id,
                       crm_company_id = excluded.crm_company_id,
                       crm_owner_id = excluded.crm_owner_id,
                       last_synced_at = excluded.last_synced_at,
                       last_event_id_synced = excluded.last_event_id_synced,
                       last_sync_status = excluded.last_sync_status,
                       sync_hash = excluded.sync_hash,
                       crm_note_id = excluded.crm_note_id,
                       updated_at = datetime('now')
                   WHERE crm_entity_map.crm_contact_id IS NOT excluded.crm_contact_id
                      OR crm_entity_map.crm_deal_id IS NOT excluded.crm_deal_id
                      OR crm_entity_map.crm_company_id IS NOT excluded.crm_company_id
                      OR crm_entity_map.crm_owner_id IS NOT excluded.crm_owner_id
                      OR crm_entity_map.last_synced_at IS NOT excluded.last_synced_at
                      OR crm_entity_map.last_event_id_synced IS NOT excluded.last_event_id_synced
                      OR crm_entity_map.last_sync_status IS NOT excluded.last_sync_status
                      OR crm_entity_map.sync_hash IS NOT excluded.sync_hash
                      OR crm_entity_map.crm_note_id IS NOT excluded.crm_note_id""",
                (
                    workspace_id, lead_id, platform,
                    entry.get("crm_contact_id"),
                    entry.get("crm_deal_id"),
                    entry.get("crm_company_id"),
                    entry.get("crm_owner_id"),
                    entry.get("last_synced_at"),
                    entry.get("last_event_id_synced"),
                    entry.get("last_sync_status", "synced"),
                    entry.get("sync_hash"),
                    entry.get("crm_note_id"),
                ),
            )
    if own_conn:
        conn.commit()
        conn.close()


def inspect_sync_lead(
    conn: sqlite3.Connection,
    org_id: str,
    lead_id: int,
    *,
    workspace_slug: Optional[str] = None,
) -> dict:
    """Compare stored, event-derived, and sync-payload activity for one lead."""
    ws_id = None
    if workspace_slug:
        ws_row = _resolve_workspace_identity(conn, workspace_slug)
        ws_id = ws_row["id"] if ws_row else None
    if ws_id is None:
        wl = conn.execute(
            "SELECT workspace_id FROM workspace_leads WHERE lead_id = ? LIMIT 1",
            (lead_id,),
        ).fetchone()
        ws_id = wl["workspace_id"] if wl else None

    stored = _read_workspace_activity_row(conn, ws_id, lead_id) if ws_id else {}
    computed = compute_lead_activity_from_events(conn, lead_id)
    payload = build_lead_sync_payload(conn, org_id, lead_id, workspace_slug=workspace_slug)
    lead_row = conn.execute(
        "SELECT email, name, last_contact_at FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    wl_row = None
    if ws_id:
        wl_row = conn.execute(
            """SELECT current_status_label, current_status_sentiment,
                      current_sentiment_since, status
               FROM workspace_leads WHERE workspace_id = ? AND lead_id = ?""",
            (ws_id, lead_id),
        ).fetchone()
    return {
        "lead_id": lead_id,
        "email": lead_row["email"] if lead_row else None,
        "name": lead_row["name"] if lead_row else None,
        "workspace_slug": workspace_slug,
        "workspace_id": ws_id,
        "lead_status": wl_row["current_status_label"] if wl_row else None,
        "lead_sentiment": wl_row["current_status_sentiment"] if wl_row else None,
        "sentiment_since": wl_row["current_sentiment_since"] if wl_row else None,
        "workspace_stage": wl_row["status"] if wl_row else None,
        "activity_stored": stored,
        "activity_computed_from_events": computed,
        "activity_sync_payload": payload.get("activity", {}),
        "full_sync_payload": payload,
    }

