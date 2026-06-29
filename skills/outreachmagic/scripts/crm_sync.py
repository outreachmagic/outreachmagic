#!/usr/bin/env python3
"""CRM sync orchestrator for outreachmagic.

Syncs contacts, deals, and events from the local SQLite database
to GoHighLevel (GHL) and/or HubSpot, one workspace at a time.

This module is designed to run as a standalone fire-and-forget subprocess.
It does NOT import pipeline.py directly to avoid circular imports at module
load time. Cloud-push helpers import routing_cloud at runtime inside their
function bodies.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from crm_drivers.ghl import AuthError as GhlAuthError, GhlError, NetworkError as GhlNetworkError, RateLimitError as GhlRateLimitError
    from crm_drivers.hubspot import AuthError as HsAuthError, HubspotError, NetworkError as HsNetworkError, RateLimitError as HsRateLimitError
except ImportError:
    from skills.outreachmagic.scripts.crm_drivers.ghl import AuthError as GhlAuthError, GhlError, NetworkError as GhlNetworkError, RateLimitError as GhlRateLimitError
    from skills.outreachmagic.scripts.crm_drivers.hubspot import AuthError as HsAuthError, HubspotError, NetworkError as HsNetworkError, RateLimitError as HsRateLimitError

# sqlite3 is imported by the caller's context; we use conn passed to us.
# We import it at module level for type hints only.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RATE_LIMITS = {
    "ghl": (80, 10),     # 80 req / 10s
    "hubspot": (400, 10), # 400 req / 10s
}

SYNCABLE_STATUSES = {"interested", "replied", "scheduled", "won", "not_interested", "lost"}

# ---------------------------------------------------------------------------
# Test injection point (set to a MockDriver to intercept CLI calls)
# ---------------------------------------------------------------------------

_test_driver_override: Any = None


# ---------------------------------------------------------------------------
# Rate limiter (simple token bucket)
# ---------------------------------------------------------------------------

class TokenBucket:
    def __init__(self, rate: int = 80, per_seconds: float = 10.0):
        self.tokens = float(rate)
        self.max_tokens = float(rate)
        self.period = per_seconds
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


def rate_limiter_for(platform: str) -> TokenBucket:
    t, p = _RATE_LIMITS.get(platform, (100, 10))
    return TokenBucket(t, p)


# ---------------------------------------------------------------------------
# Config reading
# ---------------------------------------------------------------------------

def read_crm_config(conn, workspace_id: str) -> list[dict]:
    """Read CRM workspace config rows for a workspace. Returns parsed stage_mapping."""
    rows = conn.execute(
        """SELECT workspace_id, platform, api_key, location_id, pipeline_id,
                  stage_mapping, contact_field_mapping, overwrite_existing, enabled, updated_at
           FROM crm_workspace_config
           WHERE workspace_id = ? AND enabled = 1""",
        (workspace_id,),
    ).fetchall()
    configs = []
    for row in rows:
        cfg = dict(row)
        try:
            cfg["stage_mapping"] = json.loads(cfg.get("stage_mapping", "{}"))
        except (json.JSONDecodeError, TypeError):
            cfg["stage_mapping"] = {}
        raw_cfm = cfg.get("contact_field_mapping")
        if raw_cfm:
            try:
                cfg["contact_field_mapping"] = json.loads(raw_cfm)
            except (json.JSONDecodeError, TypeError):
                cfg["contact_field_mapping"] = None
        else:
            cfg["contact_field_mapping"] = None
        configs.append(cfg)
    return configs


# ---------------------------------------------------------------------------
# Driver loading
# ---------------------------------------------------------------------------

def load_driver(platform: str, config: dict):
    """Load the appropriate CRM driver for the given platform."""
    if platform == "ghl":
        from crm_drivers.ghl import GhlDriver
        return GhlDriver(config)
    elif platform == "hubspot":
        from crm_drivers.hubspot import HubspotDriver
        return HubspotDriver(config)
    else:
        raise ValueError(f"Unknown CRM platform: {platform}")


# ---------------------------------------------------------------------------
# Lead selection
# ---------------------------------------------------------------------------

def select_leads(conn, workspace_id: str, last_sync_at: str | None = None,
                 lead_id: int | None = None) -> list[dict]:
    """Select leads ready for CRM sync from a workspace.

    Returns only leads in SYNCABLE_STATUSES, ordered by updated_at ASC.
    When ``last_sync_at`` is provided, only leads updated after that time
    are returned (incremental sync). When ``lead_id`` is provided, only
    that specific lead is returned regardless of status.
    """
    params: list = [workspace_id]
    where = ["wl.workspace_id = ?"]

    if lead_id is not None:
        where.append("wl.lead_id = ?")
        params.append(lead_id)
    else:
        placeholders = ",".join("?" for _ in SYNCABLE_STATUSES)
        where.append(f"wl.status IN ({placeholders})")
        params.extend(sorted(SYNCABLE_STATUSES))
        if last_sync_at is not None:
            where.append("wl.updated_at > ?")
            params.append(last_sync_at)

    query = f"""
        SELECT wl.lead_id, wl.status, wl.updated_at, wl.current_status_sentiment,
               l.name, l.email, l.title, l.industry, l.headcount,
               l.linkedin_url, l.company,
               l.original_source, l.original_source_detail,
               l.latest_source_detail,
               c.name AS company_name, c.domain AS company_domain
          FROM workspace_leads wl
          JOIN leads l ON l.id = wl.lead_id
          LEFT JOIN companies c ON c.id = l.company_id
         WHERE {' AND '.join(where)}
         ORDER BY wl.updated_at ASC
         LIMIT 200
    """
    rows = conn.execute(query, params).fetchall()
    leads = [dict(r) for r in rows]
    # Enrich with additional (non-primary) emails
    if leads:
        lead_ids = [l["lead_id"] for l in leads]
        placeholders = ",".join("?" for _ in lead_ids)
        add_rows = conn.execute(
            f"""SELECT lead_id, email FROM lead_emails
                WHERE lead_id IN ({placeholders}) AND is_primary = 0
                ORDER BY lead_id, id""",
            lead_ids,
        ).fetchall()
        add_map: dict[int, list[str]] = {}
        for ar in add_rows:
            add_map.setdefault(ar["lead_id"], []).append(ar["email"])
        for lead in leads:
            lead["additional_emails"] = add_map.get(lead["lead_id"], [])
    return leads


# ---------------------------------------------------------------------------
# Event formatting
# ---------------------------------------------------------------------------

def format_event_for_crm(event: dict) -> dict:
    """Map OM event to CRM-compatible event structure.

    Returns a dict with crm_type, direction, and title keys suitable
    for pushing to CRM engagement/note APIs.
    """
    event_type = event.get("event_type", "unknown")

    if event_type in ("email_sent", "reply"):
        direction = event.get("direction", "inbound")
        title = event.get("subject", event.get("body_preview", ""))
        if event_type == "reply" and "Replied" not in title:
            title = "Replied: " + title
        return {
            "crm_type": "email",
            "direction": "OUTGOING" if direction == "outbound" else "INCOMING",
            "title": title,
        }
    elif event_type == "bounce":
        return {"crm_type": "note", "title": "Bounced"}
    elif event_type == "meeting_booked":
        return {"crm_type": "meeting", "title": "Meeting"}
    else:
        title = event_type.replace("_", " ").title()
        return {"crm_type": "note", "title": title}


# ---------------------------------------------------------------------------
# Event collection
# ---------------------------------------------------------------------------

def collect_pending_events(conn, workspace_id: str, lead_id: int,
                           last_event_id: int | None = None) -> list[dict]:
    """Collect unsynced events for a lead."""
    if last_event_id is not None:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT rowid, * FROM workspace_lead_events
                   WHERE workspace_id = ? AND lead_id = ?
                     AND rowid > ?
                   ORDER BY rowid ASC
                   LIMIT 500""",
                (workspace_id, lead_id, last_event_id),
            ).fetchall()
        ]
    else:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT rowid, * FROM workspace_lead_events
                   WHERE workspace_id = ? AND lead_id = ?
                   ORDER BY rowid ASC
                   LIMIT 500""",
                (workspace_id, lead_id),
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# Company sync
# ---------------------------------------------------------------------------

def sync_company(
    lead: dict, entity: dict | None, driver, *,
    conn, workspace_id: str, platform: str,
) -> str:
    """Sync company record to CRM. Returns company_id or empty string."""
    company_name = lead.get("company_name") or lead.get("company") or ""
    if not company_name:
        return ""

    entity_co_id: str | None = None
    if entity:
        try:
            entity_co_id = entity["crm_company_id"]
        except (KeyError, IndexError):
            pass
    if entity_co_id:
        return entity_co_id

    try:
        co_id = driver.upsert_company(workspace_id, lead, entity)
        return co_id or ""
    except (GhlAuthError, HsAuthError):
        raise  # Propagate — bad credentials should stop the sync
    except (GhlError, HubspotError, GhlNetworkError, HsNetworkError,
            GhlRateLimitError, HsRateLimitError) as exc:
        lead_id_val = lead.get("lead_id", lead.get("id", "?"))
        print(f"  Warning: company sync failed for lead {lead_id_val}: {exc}", file=sys.stderr)
        return ""
    except Exception as exc:
        lead_id_val = lead.get("lead_id", lead.get("id", "?"))
        print(f"  Warning: unexpected error in company sync for lead {lead_id_val}: {exc}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Single lead sync
# ---------------------------------------------------------------------------

def _build_sync_hash(lead: dict, contact_field_mapping: dict | None,
                     company_id: str = "") -> str:
    """Build a hash of lead fields relevant to CRM sync.

    Used to detect changes that would warrant a contact/deal re-sync.
    """
    import hashlib
    parts = [
        lead.get("name", ""),
        lead.get("email", ""),
        lead.get("title", ""),
        lead.get("industry", ""),
        lead.get("headcount", ""),
        lead.get("linkedin_url", ""),
        lead.get("company_name", ""),
        lead.get("company_domain", ""),
        lead.get("status", ""),
        lead.get("current_status_sentiment", ""),
        str(company_id),
    ]
    # Include additional emails so adding/removing secondary emails triggers re-sync
    add_emails = lead.get("additional_emails", [])
    if add_emails:
        parts.append(";".join(sorted(add_emails)))
    if contact_field_mapping:
        parts.append(json.dumps(contact_field_mapping, sort_keys=True))
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_single_lead(
    lead: dict, cfg: dict, driver, *,
    conn: "sqlite3.Connection | None" = None,
    workspace_id: str = "",
) -> tuple[str, str, str, str]:
    """Sync a single lead to the CRM. Returns (contact_id, deal_id, c_action, d_action)."""
    import sqlite3

    ws_id = workspace_id or cfg.get("workspace_id", "")
    platform = cfg["platform"]
    lead_id_val = lead.get("lead_id", lead.get("id", 0))
    email = lead.get("email", "")
    status = lead.get("status", "")
    stage_id = cfg["stage_mapping"].get(status, "")
    contact_field_mapping = cfg.get("contact_field_mapping")

    entity = None
    if conn:
        entity = conn.execute(
            """SELECT crm_contact_id, crm_deal_id, crm_company_id, sync_hash
               FROM crm_entity_map
               WHERE workspace_id = ? AND lead_id = ? AND platform = ?""",
            (ws_id, lead_id_val, platform),
        ).fetchone()

    # Include existing company_id in hash so company reassignment triggers re-sync
    existing_co_id = entity["crm_company_id"] if entity else ""
    new_hash = _build_sync_hash(lead, contact_field_mapping, company_id=existing_co_id or "")

    if entity and entity["sync_hash"] == new_hash:
        return (entity["crm_contact_id"], entity["crm_deal_id"], "skipped", "skipped")

    # -- Company --
    company_id = ""
    if conn:
        company_id = sync_company(
            lead, entity, driver, conn=conn, workspace_id=ws_id, platform=platform,
        )
        # Persist company_id immediately to avoid duplicates on partial failure
        if company_id and ws_id:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO crm_entity_map
                       (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                        crm_company_id, last_synced_at, last_sync_status, sync_hash,
                        updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'partial', ?, datetime('now'))""",
                    (ws_id, lead_id_val, platform,
                     entity["crm_contact_id"] if entity else "",
                     entity["crm_deal_id"] if entity else "",
                     company_id, new_hash),
                )
            except Exception:
                pass  # Non-fatal if entity_map write fails

    # -- Contact --
    contact_id = entity["crm_contact_id"] if entity else None
    c_action = "skipped"

    if contact_id:
        try:
            driver.update_contact(contact_id, lead, cfg.get("contact_field_mapping"),
                                  overwrite_existing=bool(cfg.get("overwrite_existing", False)),
                                  company_id=company_id)
            c_action = "updated_contact"
        except Exception as exc:
            print(f"  Error updating contact for lead {lead_id_val}: {exc}", file=sys.stderr)
            # Continue with existing contact_id
    else:
        try:
            existing_id = driver.lookup_contact(email)
            if existing_id:
                contact_id = existing_id
            else:
                contact_id = driver.create_contact(
                    lead, cfg.get("contact_field_mapping"),
                    company_id=company_id,
                )
            c_action = "created_contact"
        except Exception as exc:
            print(f"  Error creating/looking up contact for lead {lead_id_val}: {exc}", file=sys.stderr)
            return ("", "", "error", "error")

    if not contact_id:
        return ("", "", "error", "error")

    # -- Deal --
    deal_id = entity["crm_deal_id"] if entity else None
    d_action = "skipped"

    if not stage_id:
        d_action = "skipped"
    elif deal_id:
        try:
            driver.update_deal_stage(deal_id, stage_id)
            d_action = "updated_deal"
        except Exception as exc:
            print(f"  Error updating deal for lead {lead_id_val}: {exc}", file=sys.stderr)
            d_action = "error"
    else:
        try:
            deal_id = driver.upsert_deal(contact_id, lead, stage_id, cfg,
                                          company_id=company_id)
            d_action = "created_deal"
        except Exception as exc:
            print(f"  Error upserting deal for lead {lead_id_val}: {exc}", file=sys.stderr)
            d_action = "error"

    # -- Write entity map --
    if conn and ws_id and d_action != "error":
        conn.execute(
            """INSERT OR REPLACE INTO crm_entity_map
               (workspace_id, lead_id, platform, crm_contact_id, crm_deal_id,
                crm_company_id, last_synced_at, last_sync_status, sync_hash,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'synced', ?, datetime('now'))""",
            (ws_id, lead_id_val, platform, contact_id, deal_id,
             company_id or None, new_hash),
        )
        # Bump workspace_leads.updated_at so timestamp-based relay sync
        # re-pushes the snapshot (which now carries the entity mapping).
        conn.execute(
            "UPDATE workspace_leads SET updated_at = datetime('now') WHERE workspace_id = ? AND lead_id = ?",
            (ws_id, lead_id_val),
        )
        # Bump leads.updated_at for the same reason on lead_core snapshots.
        conn.execute(
            "UPDATE leads SET updated_at = datetime('now') WHERE id = ?",
            (lead_id_val,),
        )

    # -- Sentiment tag --
    # Pass the raw sentiment value; drivers handle "" as "clear all om_* tags".
    sentiment = lead.get("current_status_sentiment") or ""
    if contact_id:
        try:
            driver.sync_sentiment_tag(contact_id, sentiment)
        except Exception:
            pass  # Non-fatal

    return (contact_id, deal_id, c_action, d_action)


# ---------------------------------------------------------------------------
# Field coverage logging
# ---------------------------------------------------------------------------

def _log_field_coverage(lead: dict, cfg: dict, platform: str, ws_name: str):
    """Log which OM fields are mapped vs. skipped for this workspace."""
    cfm = cfg.get("contact_field_mapping") or {}
    om_fields = ["title", "industry", "headcount", "linkedin_url", "company_domain"]
    covered = [f for f in om_fields if cfm.get(f) and lead.get(f)]
    missing = [f for f in om_fields if not cfm.get(f)]
    if missing:
        print(f"[crm-sync] {platform} fields for {ws_name}: " +
              ", ".join(f"✓ {f}" if f in covered else f"✗ {f}" for f in om_fields))


# ---------------------------------------------------------------------------
# Workspace sync
# ---------------------------------------------------------------------------

def sync_workspace(
    conn,
    ws_id: str,
    ws_name: str,
    cfg: dict,
    *,
    dry_run: bool = False,
    skip_events: bool = False,
    single_lead_id: Optional[int] = None,
    driver: Optional[Any] = None,
) -> dict:
    """Sync a single workspace + platform config.

    Returns a results dict with counts. ``driver`` injectable for testing;
    when None, ``load_driver`` is called.
    """
    if driver is None:
        driver = load_driver(cfg["platform"], cfg)

    platform = cfg["platform"]
    started_at = datetime.now(timezone.utc).isoformat()

    if dry_run:
        print(f"[crm-sync] DRY RUN -- workspace={ws_name}, platform={platform}")
    else:
        print(f"[crm-sync] Syncing workspace={ws_name}, platform={platform}")

    # TODO: Use last completed_sync_at from crm_sync_log as a cursor for incremental
    # syncs. Currently disabled because event-only changes (new workspace_lead_events
    # rows) don't update workspace_leads.updated_at, so a cursor filter would
    # incorrectly skip leads that have new events but no lead-data changes.
    leads = select_leads(conn, ws_id, last_sync_at=None, lead_id=single_lead_id)

    results = {
        "leads_checked": len(leads),
        "contacts_created": 0,
        "contacts_updated": 0,
        "opportunities_created": 0,
        "opportunities_updated": 0,
        "events_pushed": 0,
        "skipped": 0,
        "errors": 0,
        "error_details": None,
    }

    rate_limiter = rate_limiter_for(platform)

    # Track which OM fields were covered vs. skipped (log once per sync)
    field_coverage_logged = False

    for lead in leads:
        status = lead.get("status", "")
        lead_name = lead.get("name", f"lead-{lead.get('lead_id', '?')}")
        lead_id_val = lead.get("lead_id", lead.get("id", "?"))

        if dry_run:
            stage_id = cfg["stage_mapping"].get(status, "unknown-stage")
            print(f"  Would create contact: {lead_name} ({lead.get('email', 'no-email')})")
            print(f"  Would upsert deal: {lead_id_val} -> stage {stage_id}")
            continue

        try:
            contact_id, deal_id, c_action, d_action = sync_single_lead(
                lead, cfg, driver, conn=conn, workspace_id=ws_id,
            )

            if c_action == "error":
                results["errors"] += 1
                continue

            if c_action == "created_contact":
                results["contacts_created"] += 1
            elif c_action == "updated_contact":
                results["contacts_updated"] += 1
            else:
                results["skipped"] += 1

            # Log field mapping coverage once per sync (on first non-skipped lead)
            if not field_coverage_logged and c_action != "skipped":
                field_coverage_logged = True
                _log_field_coverage(lead, cfg, platform, ws_name)

            if d_action == "created_deal":
                results["opportunities_created"] += 1
            elif d_action == "updated_deal":
                results["opportunities_updated"] += 1
            elif d_action in ("skipped", "error"):
                pass  # skipped already counted above; errors counted below
            else:
                results["opportunities_created"] += 1  # unexpected action, assume created

            # ---- Event push (Phase 5) ----
            if not skip_events and contact_id:
                entity_row = conn.execute(
                    """SELECT last_event_id_synced FROM crm_entity_map
                       WHERE workspace_id = ? AND lead_id = ? AND platform = ?""",
                    (ws_id, lead_id_val, platform),
                ).fetchone()
                last_event_id = entity_row["last_event_id_synced"] if entity_row else None
                events = collect_pending_events(
                    conn, ws_id, lead_id_val, last_event_id,
                )
                if events:
                    # Inject lead's linkedin URL into events for note formatting
                    receiver_li = lead.get("linkedin_url", "")
                    for ev in events:
                        ev["receiver_linkedin_url"] = receiver_li
                    try:
                        count, max_pushed = driver.push_events(contact_id, deal_id, events)
                        results["events_pushed"] += count
                        if max_pushed is not None:
                            conn.execute(
                                """UPDATE crm_entity_map
                                   SET last_event_id_synced = ?, updated_at = datetime('now')
                                   WHERE workspace_id = ? AND lead_id = ? AND platform = ?""",
                                (max_pushed, ws_id, lead_id_val, platform),
                            )
                    except Exception as ev_exc:
                        print(
                            f"  Error pushing events for lead {lead_id_val}: "
                            f"{ev_exc}",
                            file=sys.stderr,
                        )
                        results["errors"] += 1

            # Commit per-lead so entity map and event cursor survive crashes
            if not dry_run:
                conn.commit()

        except Exception as exc:
            print(f"  Error syncing lead {lead_id_val}: {exc}", file=sys.stderr)
            results["errors"] += 1
            if results["error_details"] is None:
                results["error_details"] = ""
            results["error_details"] += f"lead {lead_id_val}: {exc}\n"

    if not dry_run:
        write_sync_log(conn, ws_id, platform, started_at, results)

    return results


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------

def write_sync_log(conn, ws_id: str, platform: str, started_at: str, results: dict):
    """Write a sync log entry and commit."""
    conn.execute(
        """INSERT INTO crm_sync_log
           (workspace_id, platform, started_at, completed_at,
            leads_checked, contacts_created, contacts_updated,
            opportunities_created, opportunities_updated,
            events_pushed, skipped, errors, error_details, status)
           VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ws_id, platform, started_at,
            results["leads_checked"],
            results["contacts_created"],
            results["contacts_updated"],
            results["opportunities_created"],
            results["opportunities_updated"],
            results["events_pushed"],
            results["skipped"],
            results["errors"],
            results.get("error_details"),
            "completed",
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Cloud push
# ---------------------------------------------------------------------------

def maybe_push_crm_sync_status(conn, *, workspace_id: str = "") -> dict:
    """Push latest per-platform CRM sync results for a workspace to cloud. Non-fatal."""
    try:
        import os
        from om_paths import get_config_path
        from agent_secrets_cloud import get_api_base
        from routing_cloud import push_crm_sync_status

        cfg_path = get_config_path()
        if not cfg_path.exists():
            return {"crm_sync_status_reported": "skipped_no_config"}
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

        agent_key = cfg.get("agent_key") or os.environ.get("OUTREACHMAGIC_API_KEY") or ""
        if not agent_key:
            return {"crm_sync_status_reported": "skipped_no_key"}

        client_id = cfg.get("client_id", "")

        query = """SELECT platform, completed_at AS last_sync_at,
                          leads_checked, contacts_created, contacts_updated,
                          opportunities_created, opportunities_updated,
                          events_pushed, skipped, errors, status
                   FROM crm_sync_log
                   WHERE completed_at IS NOT NULL"""
        params: list = []
        if workspace_id:
            query += " AND workspace_id = ?"
            params.append(workspace_id)
        query += " AND workspace_id IN (SELECT id FROM workspaces) ORDER BY started_at DESC LIMIT 20"

        rows = conn.execute(query, tuple(params)).fetchall()

        sync_results: dict = {}
        FIELD_MAP = {
            "last_sync_at": "lastSyncAt",
            "leads_checked": "leadsChecked",
            "contacts_created": "contactsCreated",
            "contacts_updated": "contactsUpdated",
            "opportunities_created": "opportunitiesCreated",
            "opportunities_updated": "opportunitiesUpdated",
            "events_pushed": "eventsPushed",
            "skipped": "skipped",
            "errors": "errors",
            "status": "status",
        }
        for row in rows:
            d = dict(row)
            plat = d.pop("platform")
            if plat not in sync_results:
                mapped = {}
                for db_key, camel_key in FIELD_MAP.items():
                    v = d.get(db_key)
                    if v is not None:
                        mapped[camel_key] = v
                sync_results[plat] = mapped

        if not sync_results:
            return {"crm_sync_status_reported": "skipped_no_data"}

        payload = {
            "clientId": client_id,
            "syncResults": sync_results,
        }

        api_base = get_api_base(lambda: cfg)

        result = push_crm_sync_status(api_base, agent_key, payload)

        platforms = list(sync_results.keys())
        return {
            "crm_sync_status_reported": "reported",
            "platforms": platforms,
            "response_id": result.get("id"),
        }
    except Exception:
        return {"crm_sync_status_reported": "skipped_error"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Outreach Magic CRM Sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # sync
    sync_p = sub.add_parser("sync", help="Sync leads to CRM")
    sync_p.add_argument("--workspace", help="Workspace slug")
    sync_p.add_argument("--all", action="store_true", help="Sync all enabled workspaces")
    sync_p.add_argument("--dry-run", action="store_true", help="Preview without API calls")
    sync_p.add_argument("--lead-id", type=int, help="Sync a single lead by ID")
    sync_p.add_argument("--skip-events", action="store_true", help="Skip event history push")
    sync_p.add_argument("--platform", choices=["ghl", "hubspot"], help="Filter by platform")

    # discover
    disc_p = sub.add_parser("discover", help="Discover CRM pipelines")
    disc_p.add_argument("--workspace", required=True, help="Workspace slug")
    disc_p.add_argument("--platform", choices=["ghl", "hubspot"], required=True,
                        help="CRM platform")

    # status
    status_p = sub.add_parser("status", help="Show CRM sync status")

    return parser.parse_args(argv)


def get_pipeline_db_path() -> Path:
    """Returns the path to the outreachmagic SQLite database."""
    from om_paths import get_data_root
    return get_data_root() / "skills" / "outreachmagic" / "databases" / "outreachmagic.db"


def _get_db_connection() -> "sqlite3.Connection":
    """Open the outreachmagic SQLite database with standard settings."""
    import sqlite3
    conn = sqlite3.connect(str(get_pipeline_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def main(argv: list[str] | None = None):
    args = _parse_args(argv)

    # Set up DB path override if not already set
    try:
        from om_paths import get_data_root_override
        if not get_data_root_override():
            import tempfile
            import os as _os
            from om_paths import set_data_root_override
            om_dir = Path.home() / ".outreachmagic"
            om_dir.mkdir(parents=True, exist_ok=True)
            set_data_root_override(om_dir)
    except Exception:
        pass

    conn = _get_db_connection()

    try:
        if args.command == "sync":
            _cmd_sync(args, conn)
        elif args.command == "discover":
            _cmd_discover(args, conn)
        elif args.command == "status":
            _cmd_status(args, conn)
    finally:
        conn.close()


def cmd_sync(args):
    """Handle 'sync' subcommand with automatic DB connection."""
    conn = _get_db_connection()
    try:
        _cmd_sync(args, conn)
    finally:
        conn.close()


def _cmd_sync(args, conn):
    """Handle 'sync' subcommand."""
    dry_run = getattr(args, "dry_run", False)
    skip_events = getattr(args, "skip_events", False)
    single_lead_id = getattr(args, "lead_id", None)
    platform_filter = getattr(args, "platform", None)

    # Determine workspaces
    if getattr(args, "workspace", None):
        ws_slug = args.workspace
        ws_rows = conn.execute(
            "SELECT id, name, slug FROM workspaces WHERE LOWER(slug) = LOWER(?)",
            (ws_slug,),
        ).fetchall()
        if not ws_rows:
            print(f"[crm-sync] Unknown workspace '{ws_slug}'", file=sys.stderr)
            sys.exit(1)
    elif getattr(args, "all", False):
        ws_rows = conn.execute(
            "SELECT id, name, slug FROM workspaces WHERE id IN (SELECT DISTINCT workspace_id FROM crm_workspace_config WHERE enabled = 1)"
        ).fetchall()
    else:
        print("[crm-sync] Use --workspace=<slug> or --all", file=sys.stderr)
        sys.exit(1)

    if not ws_rows:
        print("[crm-sync] No workspaces configured for CRM sync")
        return

    # Sync each workspace
    for ws in ws_rows:
        ws_id, ws_name, ws_slug = ws["id"], ws["name"], ws["slug"]
        configs = read_crm_config(conn, ws_id)
        if platform_filter:
            configs = [c for c in configs if c["platform"] == platform_filter]
        if not configs:
            print(f"[crm-sync] No enabled CRM configs for workspace '{ws_name}'")
            continue

        try:
            for cfg in configs:
                results = sync_workspace(
                    conn,
                    ws_id,
                    ws_name,
                    cfg,
                    dry_run=dry_run,
                    skip_events=skip_events,
                    single_lead_id=single_lead_id,
                )
                if not dry_run:
                    print(
                        f"[crm-sync] Done: {results['contacts_created']} contacts, "
                        f"{results['opportunities_created']} deals, "
                        f"{results['errors']} errors"
                    )

            if not dry_run and configs:
                push_result = maybe_push_crm_sync_status(conn, workspace_id=ws_id)
                if push_result.get("crm_sync_status_reported") == "reported":
                    print(
                        f"[crm-sync] Sync status reported to dashboard "
                        f"({', '.join(push_result.get('platforms', []))})"
                    )
        except Exception as exc:
            print(f"[crm-sync] Workspace sync failed for {ws_name}: {exc}", file=sys.stderr)


def _cmd_discover(args, conn):
    """Handle 'discover' subcommand."""
    ws_slug = args.workspace
    platform = getattr(args, "platform", None)

    ws_row = conn.execute(
        "SELECT id, name FROM workspaces WHERE LOWER(slug) = LOWER(?)",
        (ws_slug,),
    ).fetchone()

    if not ws_row:
        print(f"[crm-sync] Unknown workspace '{ws_slug}'", file=sys.stderr)
        sys.exit(1)

    configs = read_crm_config(conn, ws_row["id"])
    if platform:
        configs = [c for c in configs if c["platform"] == platform]
    if not configs:
        print(f"[crm-sync] No config for workspace '{ws_slug}'", file=sys.stderr)
        sys.exit(1)

    for cfg in configs:
        plat = cfg["platform"]
        if _test_driver_override is not None:
            driver = _test_driver_override
        else:
            driver = load_driver(plat, cfg)

        try:
            pipelines = driver.discover_pipelines(cfg)
            print(json.dumps({
                "workspace": ws_slug,
                "platform": plat,
                "pipelines": pipelines,
            }, indent=2))
        except Exception as exc:
            print(f"[crm-sync] Pipeline discovery failed for {plat}: {exc}", file=sys.stderr)
            sys.exit(1)


def _cmd_status(args, conn):
    """Handle 'status' subcommand."""
    # Show configured CRM integrations
    ws_rows = conn.execute("SELECT id, name, slug FROM workspaces").fetchall()
    configs_shown = False
    for ws in ws_rows:
        configs = read_crm_config(conn, ws["id"])
        for cfg in configs:
            configs_shown = True
            print(f"\nWorkspace: {ws['name']} ({ws['slug']})")
            print(f"  Platform: {cfg['platform']}")
            print(f"  Enabled: Yes")
            if cfg.get("pipeline_id"):
                print(f"  Pipeline: {cfg['pipeline_id']}")
            stage_mapping = cfg.get("stage_mapping")
            if stage_mapping:
                print(f"  Stage mapping: {json.dumps(stage_mapping)}")

    if not configs_shown:
        print("No CRM configs found")

    # Show sync history
    rows = conn.execute(
        """SELECT workspace_id, platform,
                  MAX(completed_at) AS last_sync_at,
                  SUM(contacts_created) AS contacts_created,
                  SUM(contacts_updated) AS contacts_updated,
                  SUM(opportunities_created) AS opportunities_created,
                  SUM(opportunities_updated) AS opportunities_updated,
                  SUM(events_pushed) AS events_pushed,
                  SUM(errors) AS errors
           FROM crm_sync_log
           GROUP BY workspace_id, platform
           ORDER BY last_sync_at DESC"""
    ).fetchall()

    for r in rows:
        d = dict(r)
        ws_name_row = conn.execute(
            "SELECT name FROM workspaces WHERE id = ?",
            (d["workspace_id"],),
        ).fetchone()
        ws_name = ws_name_row["name"] if ws_name_row else d["workspace_id"]

        print(f"\nSync history for {ws_name} ({d['platform']})")
        print(f"  Last sync: {d['last_sync_at']}")
        print(f"  Contacts: {d['contacts_created']} created, {d['contacts_updated']} updated")
        print(f"  Deals: {d['opportunities_created']} created, {d['opportunities_updated']} updated")
        print(f"  Events: {d['events_pushed']} pushed")
        print(f"  Errors: {d['errors']}")


def cmd_discover(args):
    """Handle 'discover' subcommand with automatic DB connection."""
    conn = _get_db_connection()
    try:
        _cmd_discover(args, conn)
    finally:
        conn.close()


def cmd_status(args):
    """Handle 'status' subcommand with automatic DB connection."""
    conn = _get_db_connection()
    try:
        _cmd_status(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
