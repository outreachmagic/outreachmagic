"""Write actions and sync orchestration for the local dashboard.

All writes go through the same functions the CLI uses (lead_actions,
pipeline.enrich_lead) so outbox triggers queue them for relay push. Sync runs
on a background thread — HTTP handlers must never block on the relay — with a
non-blocking lock so at most one dashboard-initiated sync runs at a time.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

import lead_actions
from constants import PIPELINE_STAGES
from db_conn import get_conn
from om_paths import get_db_path
from pipeline_update import utc_now_for_storage

ENRICH_FIELDS = ("name", "title", "industry", "company", "headcount")


def _require_lead(lead_id: int) -> None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"lead not found: {lead_id}")


def change_stage(
    lead_id: int,
    stage: str,
    *,
    workspace_slug: Optional[str] = None,
    label: Optional[str] = None,
    sentiment: Optional[str] = None,
) -> dict:
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"Invalid stage: {stage}. Valid: {PIPELINE_STAGES}")
    _require_lead(lead_id)
    return lead_actions.change_stage_scoped(
        lead_id, stage, workspace_slug=workspace_slug,
        label=label, sentiment=sentiment)


def enrich(lead_id: int, fields: dict, overwrite: bool = False) -> dict:
    unknown = sorted(set(fields) - set(ENRICH_FIELDS))
    if unknown:
        raise ValueError(
            f"Unknown enrich fields: {', '.join(unknown)}. Valid: {', '.join(ENRICH_FIELDS)}"
        )
    if not fields:
        raise ValueError(f"No fields to update. Valid: {', '.join(ENRICH_FIELDS)}")
    _require_lead(lead_id)
    import pipeline as _pipeline

    filled = _pipeline.enrich_lead(
        lead_id,
        overwrite=overwrite,
        **{k: v for k, v in fields.items() if v is not None},
    )
    return {"status": "updated", "id": lead_id, "filled": filled}


def log_event(
    lead_id: int,
    event_type: str,
    *,
    direction: str = "outbound",
    channel: str = "email",
    subject: Optional[str] = None,
    body: Optional[str] = None,
    metadata: Optional[dict] = None,
    workspace_slug: Optional[str] = None,
) -> dict:
    if not (event_type or "").strip():
        raise ValueError("event_type is required")
    _require_lead(lead_id)
    return lead_actions.log_event_scoped(
        lead_id, event_type.strip(), direction=direction, channel=channel,
        subject=subject, body=body, metadata=metadata,
        workspace_slug=workspace_slug, idempotency_prefix="dashboard")


def update_lead_identity(
    lead_id: int, *, name: Optional[str] = None, title: Optional[str] = None,
    linkedin: Optional[str] = None,
) -> dict:
    """Authoritatively set a lead's name/title, and store a LinkedIn value into
    the correct column (public URL -> linkedin_url, Sales Navigator token ->
    linkedin_sales_nav_id) via the identity path so dedup stays consistent.

    Unlike enrich() this overwrites — it's a direct edit, not a fill-if-empty.
    """
    _require_lead(lead_id)
    from workspace_routing import (
        DEFAULT_ORG_ID, parse_linkedin_value, upsert_identity_alias,
    )

    conn = get_conn()
    try:
        sets: list[str] = []
        params: list = []
        if name is not None and name.strip():
            sets.append("name = ?")
            params.append(name.strip())
        if title is not None:
            sets.append("title = ?")
            params.append(title.strip() or None)
        if sets:
            sets.append("updated_at = datetime('now')")
            conn.execute(
                f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", params + [lead_id])
        linkedin_written: list[str] = []
        if linkedin is not None and linkedin.strip():
            pairs = parse_linkedin_value(linkedin)
            if not pairs:
                raise ValueError(
                    f"could not read a LinkedIn URL or Sales Navigator id from: {linkedin!r}")
            # upsert_identity_alias writes the identity row and promotes the value
            # onto leads.linkedin_url / leads.linkedin_sales_nav_id; it raises on a
            # cross-lead identity conflict, which surfaces as a 400.
            for itype, value in pairs:
                upsert_identity_alias(
                    conn, DEFAULT_ORG_ID, lead_id, itype, value, source="dashboard")
                linkedin_written.append(itype)
        conn.commit()
    finally:
        conn.close()
    return {"status": "updated", "id": lead_id, "linkedin": linkedin_written}


def set_lead_custom_field(
    lead_id: int, scope: str, field: str, value: str,
) -> dict:
    """Write one personalization/custom field for a lead (scope='lead') or its
    linked company (scope='company'). Reuses pipeline_personalize so the write
    flows through the normal outbox path."""
    import pipeline_personalize as pp

    field = (field or "").strip()
    if not field:
        raise ValueError("field is required")
    if scope == "lead":
        result = pp.personalize_set(lead_id, field, value or "")
    elif scope == "company":
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        finally:
            conn.close()
        if not row or not row["company_id"]:
            raise ValueError("lead has no linked company — cannot set a company field")
        result = pp.company_personalize_set(
            field, value or "", company_id=row["company_id"])
    else:
        raise ValueError("scope must be 'lead' or 'company'")
    if result.get("status") == "error":
        raise ValueError(result["error"])
    return result


def lead_email_action(lead_id: int, op: str, email: str) -> dict:
    """Add a secondary email or promote one to primary (op in {add, promote})."""
    import lead_emails

    if op == "add":
        return lead_emails.add_lead_email(lead_id, email)
    if op == "promote":
        return lead_emails.promote_lead_email(lead_id, email)
    raise ValueError("op must be 'add' or 'promote'")


COMPANY_EDITABLE_FIELDS = (
    "name", "industry", "headcount", "hq_city", "hq_state", "hq_country",
)


def update_company(company_id: int, fields: dict) -> dict:
    """Authoritatively set company fields (overwrites, unlike the fill-only
    import path). Wraps pipeline._update_company_fields(authoritative=True)."""
    unknown = sorted(set(fields) - set(COMPANY_EDITABLE_FIELDS))
    if unknown:
        raise ValueError(
            f"Unknown company fields: {', '.join(unknown)}. "
            f"Valid: {', '.join(COMPANY_EDITABLE_FIELDS)}")
    clean = {k: v for k, v in fields.items() if v is not None}
    if not clean:
        raise ValueError("No fields to update")
    import pipeline as _pipeline

    conn = get_conn()
    try:
        rec = conn.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if rec is None:
            raise ValueError(f"company not found: {company_id}")
        rec = dict(rec)
        _pipeline._update_company_fields(
            conn, rec,
            clean.get("name"), clean.get("industry"), clean.get("headcount"),
            clean.get("hq_city"), clean.get("hq_state"), clean.get("hq_country"),
            authoritative=True)
        conn.commit()
    finally:
        conn.close()
    return {"status": "updated", "id": company_id, "fields": sorted(clean)}


def link_company(
    lead_id: int,
    company_id: Optional[int] = None,
    company_name: Optional[str] = None,
) -> dict:
    """Link a lead to a company: an existing company by id, or by name (which
    finds/creates the company via ensure_company)."""
    _require_lead(lead_id)
    import pipeline as _pipeline

    conn = get_conn()
    try:
        if company_id is not None:
            row = conn.execute(
                "SELECT id, name FROM companies WHERE id = ?", (company_id,)).fetchone()
            if row is None:
                raise ValueError(f"company not found: {company_id}")
            conn.execute(
                "UPDATE leads SET company_id = ?, updated_at = datetime('now') WHERE id = ?",
                (company_id, lead_id))
            conn.commit()
            return {"status": "linked", "lead_id": lead_id,
                    "company_id": company_id, "company": row["name"]}
        if company_name and company_name.strip():
            cid = _pipeline.link_lead_company(conn, lead_id, company=company_name.strip())
            conn.commit()
            return {"status": "linked", "lead_id": lead_id,
                    "company_id": cid, "company": company_name.strip()}
        raise ValueError("provide company_id or company_name")
    finally:
        conn.close()


def edit_sender_account(email: str, fields: dict) -> dict:
    """Edit a sender account's manual fields (reseller/provider/name/etc.).
    Delegates field-whitelisting to pipeline.update_sender_account."""
    import pipeline as _pipeline

    if not email:
        raise ValueError("email is required")
    result = _pipeline.update_sender_account(email, **fields)
    if result.get("status") == "error":
        raise ValueError(result["error"])
    return result


def edit_sender_domain(domain: str, fields: dict) -> dict:
    """Set a sending domain's reseller / cost / currency / notes / sending_ip."""
    import pipeline as _pipeline

    if not domain:
        raise ValueError("domain is required")
    allowed = {"reseller", "domain_cost", "currency", "notes", "sending_ip", "is_active"}
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(f"Unknown domain fields: {', '.join(unknown)}. "
                         f"Valid: {', '.join(sorted(allowed))}")
    result = _pipeline.set_sender_domain_cost(domain, **fields)
    if result.get("status") == "error":
        raise ValueError(result["error"])
    return result


BULK_OPS = ("stage", "lead_status", "sentiment", "tag_add", "tag_remove", "email_finder")


def bulk_edit_contacts(
    lead_ids: list, op: str, value: Optional[str] = None,
    *, workspace_slug: Optional[str] = None, force: bool = False,
) -> dict:
    """Apply one edit to many contacts at once. Ops:
    stage/lead_status (set stage), sentiment (set sentiment on current stage),
    tag_add/tag_remove (workspace tag), email_finder (background job).

    Every write reuses the same scoped path the single-lead edits use, so the
    outbox picks them up.
    """
    ids = [int(x) for x in (lead_ids or [])]
    if not ids:
        raise ValueError("no lead_ids provided")
    if op not in BULK_OPS:
        raise ValueError(f"op must be one of {', '.join(BULK_OPS)}")

    if op == "email_finder":
        status = sync_manager.start_email_finder(
            workspace_slug or "", ids, force=force)
        if status is None:
            raise ValueError("a sync is already running")
        return {"status": "started", "op": op, "job": status}

    if op in ("tag_add", "tag_remove"):
        import pipeline_tags
        conn = get_conn()
        try:
            ws = _resolve_ws_id(conn, workspace_slug)
        finally:
            conn.close()
        if not (value or "").strip():
            raise ValueError("a tag value is required")
        changed = 0
        for lid in ids:
            r = (pipeline_tags.tag_add(ws, lid, value) if op == "tag_add"
                 else pipeline_tags.tag_remove(ws, lid, value))
            if r.get("status") in ("added", "removed"):
                changed += 1
        return {"status": "ok", "op": op, "changed": changed, "requested": len(ids)}

    # stage / lead_status / sentiment all go through change_stage_scoped.
    if not (value or "").strip():
        raise ValueError(f"a value is required for {op}")
    conn = get_conn()
    try:
        for lid in ids:
            if op == "sentiment":
                row = conn.execute(
                    "SELECT wl.status FROM workspace_leads wl"
                    " JOIN workspaces w ON w.id = wl.workspace_id"
                    " WHERE wl.lead_id = ? AND w.slug = ?", (lid, workspace_slug)).fetchone()
                stage = (row["status"] if row else None) or "prospecting"
                lead_actions.change_stage_scoped(
                    lid, stage, workspace_slug=workspace_slug, sentiment=value)
            else:  # stage / lead_status
                lead_actions.change_stage_scoped(lid, value, workspace_slug=workspace_slug)
    finally:
        conn.close()
    return {"status": "ok", "op": op, "updated": len(ids)}


def _resolve_ws_id(conn: sqlite3.Connection, workspace_slug: Optional[str]) -> str:
    import pipeline as _pipeline

    if not workspace_slug:
        raise ValueError("workspace is required")
    ws = _pipeline.resolve_workspace_identity(conn, workspace_slug)
    if not ws:
        raise ValueError(f"workspace not found: {workspace_slug}")
    return ws["id"]


def set_company_domain(
    company_id: int, domain: str, purpose: Optional[str] = None,
) -> dict:
    """Attach a sending domain to a company (or update its purpose). Wraps
    pipeline_sender_accounts.set_sender_domain_cost with the company link."""
    import pipeline_sender_accounts as psa

    if not (domain or "").strip():
        raise ValueError("domain is required")
    kwargs: dict = {"company_id": company_id}
    if purpose:
        kwargs["purpose"] = purpose
    result = psa.set_sender_domain_cost(domain, **kwargs)
    if result.get("status") == "error":
        raise ValueError(result["error"])
    return result


def bulk_link_companies(lead_ids: list) -> dict:
    """One-click company link for the 'linkable' set (Section D): link each lead
    to a company derived from its own `company` text via link_lead_company
    (which finds/creates the company and matches on the lead's email domain)."""
    import pipeline as _pipeline

    ids = [int(x) for x in (lead_ids or [])]
    if not ids:
        raise ValueError("no lead_ids provided")
    linked, skipped, results = 0, 0, []
    conn = get_conn()
    try:
        cache: dict = {}
        for lead_id in ids:
            row = conn.execute(
                "SELECT company, company_id FROM leads WHERE id = ?", (lead_id,)
            ).fetchone()
            if row is None:
                results.append({"lead_id": lead_id, "status": "not_found"})
                skipped += 1
                continue
            if row["company_id"]:
                results.append({"lead_id": lead_id, "status": "already_linked"})
                skipped += 1
                continue
            text = (row["company"] or "").strip()
            if not text:
                results.append({"lead_id": lead_id, "status": "no_company_text"})
                skipped += 1
                continue
            cid = _pipeline.link_lead_company(conn, lead_id, company=text, company_cache=cache)
            if cid:
                linked += 1
                results.append({"lead_id": lead_id, "status": "linked", "company_id": cid})
            else:
                skipped += 1
                results.append({"lead_id": lead_id, "status": "unresolved"})
        conn.commit()
    finally:
        conn.close()
    return {"linked": linked, "skipped": skipped, "results": results}


def cleanup_preview() -> dict:
    """Dry-run the truly-empty (event-less) junk-lead cleanup: counts +
    distribution, nothing written. Org-wide by nature."""
    import junk_cleanup

    return junk_cleanup.cleanup_junk_leads(dry_run=True)


def cleanup_run() -> dict:
    """Execute the junk-lead cleanup (quarantine → delete → drop tombstones).
    Confirmed; the UI gates this behind the dry-run preview."""
    import junk_cleanup

    return junk_cleanup.cleanup_junk_leads(dry_run=False, confirm=True)


def resolve_merge_candidate(candidate_id: str, approve: bool, note: Optional[str] = None) -> dict:
    """Approve (execute the merge) or reject a queued company merge candidate."""
    import pipeline as _pipeline

    if approve:
        result = _pipeline.approve_company_merge_candidate(candidate_id)
    else:
        result = _pipeline.reject_company_merge_candidate(candidate_id, note=note)
    if result.get("status") == "error":
        raise ValueError(result["error"])
    return result


class SyncManager:
    """At most one dashboard-initiated pull/push at a time, on a worker thread.

    A CLI-driven sync can still run concurrently in another process; WAL and
    busy_timeout make that safe but not instant — this lock only serializes
    what the dashboard itself starts.
    """

    def __init__(self):
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = {
            "state": "idle", "kind": None, "started_at": None,
            "finished_at": None, "summary": None, "error": None,
            "log_offset": 0,
        }
        # The relay progress machinery (pipeline_sync/_relay_log) mirrors its
        # stderr lines to OM_SYNC_LOG when set. Point it at a stable per-install
        # file so the console drawer can tail it; each run records the file's
        # size at start so the drawer shows only that run's lines.
        try:
            existing = os.environ.get("OM_SYNC_LOG", "").strip()
            if existing:
                self._log_path: Optional[Path] = Path(existing).expanduser()
            else:
                self._log_path = get_db_path().parent / "dashboard_sync.log"
                os.environ["OM_SYNC_LOG"] = str(self._log_path)
        except (OSError, RuntimeError):
            self._log_path = None

    def _log_size(self) -> int:
        try:
            return self._log_path.stat().st_size if self._log_path else 0
        except OSError:
            return 0

    def read_log(self, after: int = 0) -> dict:
        """New log bytes since `after`, for the console drawer to append."""
        if not self._log_path or not self._log_path.exists():
            return {"text": "", "offset": after}
        try:
            with self._log_path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(max(0, after))
                text = fh.read()
            return {"text": text, "offset": after + len(text.encode("utf-8", "replace"))}
        except OSError:
            return {"text": "", "offset": after}

    def status(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def start_pull(self) -> Optional[dict]:
        return self._start("pull", self._run_pull)

    def start_push(self, workspace_slug: Optional[str] = None) -> Optional[dict]:
        return self._start("push", self._run_push, workspace_slug=workspace_slug)

    def start_crm_sync(
        self,
        workspace_slug: str,
        lead_id: Optional[int] = None,
        max_age: Optional[str] = None,
    ) -> Optional[dict]:
        return self._start(
            "crm", self._run_crm,
            workspace_slug=workspace_slug, lead_id=lead_id, max_age=max_age)

    def start_email_finder(
        self, workspace_slug: str, lead_ids: list,
        domains: Optional[dict] = None, force: bool = False,
    ) -> Optional[dict]:
        return self._start(
            "email-finder", self._run_email_finder,
            workspace_slug=workspace_slug, lead_ids=lead_ids,
            domains=domains, force=force)

    def start_serper(
        self, workspace_slug: str, lead_ids: list, force: bool = False,
    ) -> Optional[dict]:
        return self._start(
            "serper", self._run_serper,
            workspace_slug=workspace_slug, lead_ids=lead_ids, force=force)

    def _start(self, kind: str, fn, **kwargs) -> Optional[dict]:
        """Returns the running status, or None when a sync is already running."""
        if not self._run_lock.acquire(blocking=False):
            return None
        with self._state_lock:
            self._state = {
                "state": "running", "kind": kind,
                "started_at": utc_now_for_storage(),
                "finished_at": None, "summary": None, "error": None,
                "log_offset": self._log_size(),
            }
        thread = threading.Thread(
            target=self._run, args=(fn,), kwargs=kwargs,
            name=f"dashboard-{kind}", daemon=True)
        thread.start()
        return self.status()

    def _run(self, fn, **kwargs) -> None:
        try:
            summary = fn(**kwargs)
            with self._state_lock:
                self._state.update(
                    state="done", finished_at=utc_now_for_storage(), summary=summary)
        except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
            with self._state_lock:
                self._state.update(
                    state="error", finished_at=utc_now_for_storage(), error=str(exc))
        finally:
            self._run_lock.release()

    @staticmethod
    def _run_pull() -> dict:
        import pipeline as _pipeline

        agent_key = _pipeline.get_agent_key()
        if not agent_key:
            raise RuntimeError(
                "No agent key configured. Run: python3 scripts/pipeline.py setup --key om_agent_..."
            )
        # First-ever pull (no cursor) backfills everything; mirrors the CLI's
        # effective_full auto-detect. The dashboard never offers --full replays.
        last_max_id = _pipeline.get_last_max_id()
        effective_full = not last_max_id
        stats: dict = {}
        imported, skipped = _pipeline.sync_from_relay_org(
            agent_key,
            after_id=None if effective_full else last_max_id,
            full=effective_full,
            quiet=True,
            stats=stats,
        )
        return {"imported": imported, "skipped": skipped}

    @staticmethod
    def _run_push(workspace_slug: Optional[str] = None) -> dict:
        import pipeline as _pipeline

        return _pipeline.sync_all(workspace=workspace_slug or None)

    @staticmethod
    def _run_crm(
        workspace_slug: Optional[str] = None,
        lead_id: Optional[int] = None,
        max_age: Optional[str] = None,
    ) -> dict:
        """Mirror crm_sync._cmd_sync for one workspace: sync each enabled
        config, then report status upstream. sync_workspace commits itself."""
        import crm_sync
        import pipeline as _pipeline

        if not workspace_slug:
            raise ValueError("workspace is required for CRM sync")
        conn = get_conn()
        try:
            ws = _pipeline.resolve_workspace_identity(conn, workspace_slug)
            if not ws:
                raise ValueError(f"workspace not found: {workspace_slug}")
            configs = crm_sync.read_crm_config(conn, ws["id"])
            if not configs:
                raise RuntimeError(
                    f"No enabled CRM config for workspace {workspace_slug}. "
                    "Configure one with: pipeline.py crm-sync setup"
                )
            summary: dict = {"workspace": workspace_slug, "platforms": [], "results": []}
            for cfg in configs:
                results = crm_sync.sync_workspace(
                    conn, ws["id"], ws["name"], cfg,
                    single_lead_id=lead_id, max_age=max_age)
                summary["platforms"].append(cfg["platform"])
                summary["results"].append(dict(results))
            crm_sync.maybe_push_crm_sync_status(conn, workspace_id=ws["id"])
            return summary
        finally:
            conn.close()

    # Cap per-run fan-out: each lead is one or more paid provider / Serper
    # calls, so a runaway selection shouldn't be able to drain credits in one
    # background job. The UI selects explicitly; this is a backstop.
    MAX_ENRICH_LEADS = 200

    @staticmethod
    def _run_email_finder(
        workspace_slug: str, lead_ids: list, domains: Optional[dict] = None,
        force: bool = False,
    ) -> dict:
        """Company/multi-domain-aware email finder over a chosen lead set.

        Resolves each lead's ranked candidate domains (company_identities, then
        the lead's own email_domain) — or an explicit per-lead override — and
        runs the provider waterfall against them, stopping the whole batch once
        every provider is out of credits. Reuses email_finder.save_find_result
        so a hit flows through import-profiles and the outbox like any write.

        Leads that already have an email-finding attempt on record are skipped
        (status "already_ran") unless `force` is set — the re-run guard that
        keeps a re-selected batch from re-spending finder credits.
        """
        import email_finder
        import dashboard_queries
        from pipeline_provider_attempts import has_attempted
        from waterfall import run_find_with_domain_fallback

        ids = [int(x) for x in (lead_ids or [])][: SyncManager.MAX_ENRICH_LEADS]
        if not ids:
            raise ValueError("no lead_ids provided")
        overrides = {int(k): v for k, v in (domains or {}).items()}
        cfg = email_finder.load_config()
        om_dir = email_finder.find_outreachmagic(cfg)
        if not om_dir:
            raise RuntimeError(
                "Outreach Magic data dir not found — cannot save finder results.")

        summary = {
            "workspace": workspace_slug, "requested": len(ids),
            "found": 0, "not_found": 0, "skipped_no_domain": 0,
            "skipped_already_ran": 0,
            "credits_exhausted": False, "results": [],
        }
        conn = get_conn()
        try:
            for lead_id in ids:
                lead = conn.execute(
                    "SELECT name, company, email FROM leads WHERE id = ?", (lead_id,)
                ).fetchone()
                if lead is None:
                    continue
                if (lead["email"] or "").strip():
                    summary["results"].append({"lead_id": lead_id, "status": "has_email"})
                    continue
                if not force and (
                    has_attempted(conn, lead_id, "trykitt")
                    or has_attempted(conn, lead_id, "icypeas")
                ):
                    summary["skipped_already_ran"] += 1
                    summary["results"].append(
                        {"lead_id": lead_id, "status": "already_ran"})
                    continue
                auto_domains, company_text = dashboard_queries._lead_domains(conn, lead_id)
                override = overrides.get(lead_id)
                cand = [override] if override else auto_domains
                cand = [d for d in cand if d]
                if not cand:
                    summary["skipped_no_domain"] += 1
                    summary["results"].append({"lead_id": lead_id, "status": "no_domain"})
                    continue
                result = run_find_with_domain_fallback(
                    cfg, full_name=lead["name"] or "", domains=cand)
                if result.get("email"):
                    email_finder.save_find_result(
                        om_dir,
                        full_name=lead["name"] or "",
                        company=company_text or (cand[0] if cand else ""),
                        domain=result.get("winning_domain") or cand[0],
                        linkedin="",
                        find_result=result,
                        workspace=workspace_slug,
                        lead_id=lead_id,
                    )
                    summary["found"] += 1
                    summary["results"].append({
                        "lead_id": lead_id, "status": "found",
                        "email": result["email"],
                        "domain": result.get("winning_domain") or cand[0],
                    })
                else:
                    summary["not_found"] += 1
                    summary["results"].append({"lead_id": lead_id, "status": "not_found"})
                if result.get("status") == "credits_exhausted":
                    summary["credits_exhausted"] = True
                    break
        finally:
            conn.close()
        return summary

    @staticmethod
    def _run_serper(workspace_slug: str, lead_ids: list, force: bool = False) -> dict:
        """Web-research surface for leads the email finder can't place: runs the
        Serper query pack per lead and returns the formatted result blocks for
        the agent to read and act on. This is the automatable slice — the final
        map-to-fields step stays agent-in-the-loop (needs a model to judge
        which result is the right person/company).

        Leads with a Serper research attempt already on record are skipped
        (status "already_ran") unless `force` is set."""
        import enrich
        from pipeline_provider_attempts import has_attempted

        ids = [int(x) for x in (lead_ids or [])][: SyncManager.MAX_ENRICH_LEADS]
        if not ids:
            raise ValueError("no lead_ids provided")
        cfg = enrich.load_config()
        summary = {
            "workspace": workspace_slug, "requested": len(ids),
            "searched": 0, "errors": 0, "skipped_already_ran": 0, "results": [],
        }
        conn = get_conn()
        try:
            for lead_id in ids:
                lead = conn.execute(
                    "SELECT name, company, title FROM leads WHERE id = ?", (lead_id,)
                ).fetchone()
                if lead is None or not (lead["name"] or "").strip():
                    continue
                if not force and has_attempted(conn, lead_id, "serper"):
                    summary["skipped_already_ran"] += 1
                    summary["results"].append(
                        {"lead_id": lead_id, "status": "already_ran"})
                    continue
                person = {
                    "full_name": lead["name"],
                    "company_name": lead["company"] or "",
                    "stated_role": lead["title"] or "",
                }
                sections = []
                try:
                    for q in enrich.build_serper_queries(person):
                        if not q.get("always"):
                            continue
                        data = enrich.serper_search(q["query"], cfg)
                        sections.append(
                            {"label": q["label"], "query": q["query"], "data": data})
                    summary["searched"] += 1
                    summary["results"].append({
                        "lead_id": lead_id, "name": lead["name"],
                        "research": enrich.format_serper_for_model(sections),
                    })
                except (ValueError, RuntimeError, OSError) as exc:
                    summary["errors"] += 1
                    summary["results"].append({"lead_id": lead_id, "error": str(exc)})
        finally:
            conn.close()
        return summary


sync_manager = SyncManager()
