#!/usr/bin/env python3
"""CLI dispatcher: argument parsing and command dispatch for pipeline.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import agent_secrets_cloud
import db_health
import pipeline_dedup
import pipeline_lead_review
import query_cli
import review_cloud
import routing_cloud
import workspace_archive


def _remap_to_lead_review_export(args) -> None:
    """Route sheets export (or export --format sheets) to review export lead-review."""
    args.command = "review"
    args.review_command = "export"
    args.template = "lead-review"
    if not getattr(args, "title", None):
        ws = getattr(args, "workspace", None) or "leads"
        args.title = f"{ws} leads"
    if not getattr(args, "detail", None):
        args.detail = "standard"
    for name, default in (
        ("limit", 5000),
        ("never_contacted", False),
        ("no_email", False),
        ("require_domain", False),
        ("share_email", None),
        ("public", False),
        ("anyone_with_link", False),
        ("sheet_id", None),
        ("parent_sheet_id", None),
        ("tab_name", None),
        ("fields", None),
        ("tag", None),
        ("stage", None),
        ("since", None),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)


LEAD_REVIEW_MAX_ROWS_PER_TAB = 1000


def _export_lead_review_chunked(
    api_base: str,
    tok: str,
    *,
    template: str,
    title: str,
    share_email: Any,
    public_link: bool,
    sheet_id: Any,
    parent_sheet_id: Any,
    tab_title: Any,
    detail: Any,
    headers: Any,
    rows: list,
    workspace: str,
    columns: Any,
    freeze_header: Any,
) -> dict:
    """Auto-chunk lead-review exports over LEAD_REVIEW_MAX_ROWS_PER_TAB rows across
    multiple tabs of one spreadsheet, reusing the existing parent_sheet_id/tab_title
    plumbing. Exports at or under the threshold, or exports already targeting an
    explicit sheet_id/parent_sheet_id, are sent exactly as before (single call)."""
    if len(rows) <= LEAD_REVIEW_MAX_ROWS_PER_TAB or sheet_id or parent_sheet_id:
        return review_cloud.export_review(
            api_base, tok,
            template=template, title=title, share_email=share_email, public_link=public_link,
            sheet_id=sheet_id, parent_sheet_id=parent_sheet_id, tab_title=tab_title,
            detail=detail, headers=headers, rows=rows,
            workspace=workspace, columns=columns, freeze_header=freeze_header,
        )
    chunks = [
        rows[i:i + LEAD_REVIEW_MAX_ROWS_PER_TAB]
        for i in range(0, len(rows), LEAD_REVIEW_MAX_ROWS_PER_TAB)
    ]
    first_title = tab_title or "Page 1"
    result = review_cloud.export_review(
        api_base, tok,
        template=template, title=title, share_email=share_email, public_link=public_link,
        sheet_id=None, parent_sheet_id=None, tab_title=first_title,
        detail=detail, headers=headers, rows=chunks[0],
        workspace=workspace, columns=columns, freeze_header=freeze_header,
    )
    base_sheet_id = result.get("sheet_id")
    tabs = [{"tab_title": first_title, "rows": len(chunks[0])}]
    for n, chunk in enumerate(chunks[1:], start=2):
        page_title = f"Page {n}"
        review_cloud.export_review(
            api_base, tok,
            template=template, title=title, share_email=None, public_link=False,
            sheet_id=None, parent_sheet_id=base_sheet_id, tab_title=page_title,
            detail=detail, headers=headers, rows=chunk,
            workspace=workspace, columns=columns, freeze_header=freeze_header,
        )
        tabs.append({"tab_title": page_title, "rows": len(chunk)})
    result = dict(result)
    result["rows"] = len(rows)
    result["tabs"] = tabs
    result["chunked"] = True
    return result


def _cmd_sheets_campaign_stats(args) -> None:
    """Handler for `sheets campaign-stats` — build payload and POST to backend."""
    import pipeline as _pipeline

    ws = getattr(args, "workspace", None)
    if not ws:
        print(json.dumps({"error": "--workspace required for sheets campaign-stats"}))
        sys.exit(1)

    from campaign_stats import build_campaign_stats_payload
    from db_conn import get_conn

    conn = get_conn()
    try:
        payload = build_campaign_stats_payload(
            conn,
            workspace=ws,
            since=getattr(args, "since", None),
        )
    finally:
        conn.close()

    # Dry-run or JSON preview: print payload and exit
    if getattr(args, "dry_run", False) or getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return

    tok = _pipeline.get_agent_key()
    if not tok:
        print(json.dumps({"error": "login required — ask Outreach Magic to log in"}))
        sys.exit(1)

    api_base = review_cloud.get_api_base(_pipeline.load_config)

    share_email, public_link = _pipeline.resolve_sheets_export_access(args)
    sheet_id = getattr(args, "sheet_id", None)
    update_mode = getattr(args, "update", False)

    # --update: look up the last sheet_id for this workspace
    if update_mode and not sheet_id:
        from om_paths import get_sheets_dir
        safe_ws = "".join(c if c.isalnum() or c in "-_" else "-" for c in ws)[:40]
        latest_path = get_sheets_dir() / f"campaign-stats-{safe_ws}.latest.json"
        if latest_path.exists():
            try:
                saved = json.loads(latest_path.read_text(encoding="utf-8"))
                sid = saved.get("sheet_id")
                if sid:
                    sheet_id = str(sid).strip()
                    print(f"Reusing saved sheet {sheet_id}", file=sys.stderr)
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        if not sheet_id:
            print("No saved sheet found for this workspace. Creating a new one.", file=sys.stderr)

    result = review_cloud.export_review(
        api_base,
        tok,
        template=payload["template"],
        title=payload["title"],
        sheets=payload["sheets"],
        workspace=ws,
        share_email=share_email,
        public_link=public_link,
        sheet_id=str(sheet_id).strip() if sheet_id else None,
        freeze_header=True,
    )

    if isinstance(result, dict) and result.get("sheet_id"):
        try:
            meta_path = _pipeline.save_sheets_export_record(
                workspace=ws,
                title=payload["title"],
                sheet_id=str(result["sheet_id"]),
                url=str(result.get("url") or result.get("spreadsheet_url") or ""),
                detail="campaign-stats",
            )
            result = dict(result)
            result["metadata_path"] = str(meta_path)

            # Save or update the .latest file so --update can find it later
            from om_paths import get_sheets_dir
            safe_ws = "".join(c if c.isalnum() or c in "-_" else "-" for c in ws)[:40]
            latest_path = get_sheets_dir() / f"campaign-stats-{safe_ws}.latest.json"
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            latest_path.write_text(json.dumps({
                "workspace": ws,
                "sheet_id": str(result["sheet_id"]),
                "url": str(result.get("url") or result.get("spreadsheet_url") or ""),
                "title": payload["title"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# CRM sync subprocess hook
# ---------------------------------------------------------------------------

CRM_SYNCABLE_STATUSES = frozenset({"interested", "replied", "scheduled", "won", "not_interested", "lost"})


def _maybe_trigger_crm_sync(
    *,
    lead_id: int,
    stage: str | None = None,
    workspace_slug: str | None = None,
) -> None:
    """If --crm-sync was passed and conditions are met, fire crm_sync.py as a subprocess."""
    if stage is not None and stage not in CRM_SYNCABLE_STATUSES:
        return
    if not workspace_slug:
        return
    crm_sync_path = Path(__file__).parent / "crm_sync.py"
    if not crm_sync_path.exists():
        return
    args = [sys.executable, str(crm_sync_path), "sync", "--lead-id", str(lead_id), "--workspace", workspace_slug]
    subprocess.Popen(args)
    print(f"CRM sync triggered for lead {lead_id} in workspace {workspace_slug}", file=sys.stderr)


def _cmd_crm_sync(args) -> None:
    """Dispatch pipeline.py crm-sync to the standalone crm_sync.py subprocess."""
    crm_sync_path = Path(__file__).parent / "crm_sync.py"
    if not crm_sync_path.exists():
        print(json.dumps({"error": "crm_sync.py not found"}))
        sys.exit(1)

    action = getattr(args, "action", None) or "status"
    cmd = [sys.executable, str(crm_sync_path), action]

    if action == "sync":
        if getattr(args, "workspace", None):
            cmd.extend(["--workspace", args.workspace])
        elif getattr(args, "all", False):
            cmd.append("--all")
        else:
            print(json.dumps({"error": "Use --workspace=<slug> or --all for sync"}))
            sys.exit(1)
        if getattr(args, "dry_run", False):
            cmd.append("--dry-run")
        if getattr(args, "lead_id", None):
            cmd.extend(["--lead-id", str(args.lead_id)])
        if getattr(args, "skip_events", False):
            cmd.append("--skip-events")
        if getattr(args, "platform", None):
            cmd.extend(["--platform", args.platform])
        if getattr(args, "max_age", None):
            cmd.extend(["--max-age", args.max_age])
    elif action == "discover":
        ws = getattr(args, "workspace", None)
        platform = getattr(args, "platform", None)
        if not ws:
            print(json.dumps({"error": "--workspace is required for discover"}))
            sys.exit(1)
        cmd.extend(["--workspace", ws])
        if platform:
            cmd.extend(["--platform", platform])

    # Fire and forget — crm_sync.py prints its own output
    result = subprocess.run(cmd, capture_output=False, text=True)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    import pipeline as _pipeline

    parser = argparse.ArgumentParser(description="Outreach Magic — Pipeline visibility for Hermes")
    sub = parser.add_subparsers(dest="command", help="Commands")

    init_p = sub.add_parser("init", help="Initialize the database")
    init_p.add_argument(
        "--from-tag",
        dest="from_tag",
        help=argparse.SUPPRESS,
    )
    init_p.add_argument(
        "--agent",
        choices=_pipeline.AGENT_DIR_NAMES,
        help="Which agent tool you use (cursor, agents, claude, hermes). "
        "Sets data_root so all skill copies share one database.",
    )
    sub.add_parser("version", help="Print installed outreachmagic version")
    sub.add_parser(
        "paths",
        help="Print resolved install, config, and database paths (JSON)",
    )

    update_p = sub.add_parser(
        "update",
        help="Install skill scripts from the latest GitHub release (user-triggered)",
    )
    update_p.add_argument("--check", action="store_true", help="Only check for updates, do not install")
    update_p.add_argument("--tag", help="Install a specific release tag (e.g. v1.4.5)")
    update_p.add_argument(
        "--channel",
        choices=("release", "main"),
        default="release",
        help="release (default): latest GitHub tag; main: moving main branch (power users)",
    )

    rollback_p = sub.add_parser(
        "rollback",
        help="Restore skill scripts from backup taken before the last pipeline.py update",
    )

    show_p = sub.add_parser("show", help="Show pipeline")
    show_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    show_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    show_p.add_argument("--stage")
    show_p.add_argument("--sentiment", choices=("positive", "negative", "autoreply", "invalid", "neutral"),
                        help="Filter by current lead status sentiment (latest status event)")
    show_p.add_argument("--auto-reply", dest="auto_reply", choices=("true", "false"),
                        help="Filter by current auto-reply flag (OOO, etc.)")
    show_p.add_argument("--lead-status", dest="lead_status",
                        help="Filter by current lead status label (e.g. interested, not_interested)")
    show_p.add_argument("--sort", choices=("updated_at", "sentiment", "auto_reply", "status_at"),
                        default="updated_at")
    show_p.add_argument("--order", choices=("asc", "desc"), default="desc")
    show_p.add_argument("--limit", type=int, default=50)
    show_p.add_argument("--workspace", help="Filter by workspace name or slug")
    show_p.add_argument("--since", help="Show leads created or updated on/after this date (YYYY-MM-DD or 'today')")
    show_p.add_argument("--email", help="Filter by exact email")
    show_p.add_argument("--name", help="Filter by name (partial match)")
    show_p.add_argument("--json", action="store_true")

    lead_table_p = sub.add_parser("lead-table", help="Show canonical lead information table")
    lead_table_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    lead_table_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    lead_table_p.add_argument("--stage")
    lead_table_p.add_argument("--sentiment", choices=("positive", "negative", "autoreply", "invalid", "neutral"),
                              help="Filter by current lead status sentiment (latest status event)")
    lead_table_p.add_argument("--auto-reply", dest="auto_reply", choices=("true", "false"),
                              help="Filter by current auto-reply flag (OOO, etc.)")
    lead_table_p.add_argument("--lead-status", dest="lead_status",
                              help="Filter by current lead status label (e.g. interested, not_interested)")
    lead_table_p.add_argument("--sort", choices=("updated_at", "sentiment", "auto_reply", "status_at"),
                              default="updated_at")
    lead_table_p.add_argument("--order", choices=("asc", "desc"), default="desc")
    lead_table_p.add_argument("--limit", type=int, default=50)
    lead_table_p.add_argument("--workspace", help="Filter by workspace name or slug")
    lead_table_p.add_argument("--since", help="Show leads created or updated on/after this date (YYYY-MM-DD or 'today')")
    lead_table_p.add_argument("--email", help="Filter by exact email")
    lead_table_p.add_argument("--name", help="Filter by name (partial match)")
    lead_table_p.add_argument("--markdown", action="store_true", help="Render as markdown table")
    lead_table_p.add_argument("--json", action="store_true")

    # --- relay round-trip troubleshooting -----------------------------------
    for _name, _help in (
        ("sync-preview", "Show the exact payload that WOULD be pushed for a lead (sends nothing)"),
        ("sync-diff", "Compare a lead's local payload against what the relay actually stores"),
        ("sync-audit", "Timeline of every payload pushed/pulled for a lead, with errors"),
    ):
        _p = sub.add_parser(_name, help=_help)
        _g = _p.add_mutually_exclusive_group(required=True)
        _g.add_argument("--lead-id", type=int, help="Lead id")
        _g.add_argument("--email", help="Look the lead up by email instead")
        _p.add_argument("--json", action="store_true")
        if _name == "sync-audit":
            _p.add_argument("--last", type=int, default=20, help="Rows to show (default 20)")
            _p.add_argument("--errors", action="store_true", help="Only rows that errored")

    stats_p = sub.add_parser("stats", help="Pipeline statistics")
    stats_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    stats_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    stats_p.add_argument("--json", action="store_true")

    camp_p = sub.add_parser("campaigns", help="Event and lead counts by campaign name")
    camp_p.add_argument("--pull", action="store_true", help="Pull latest events before showing")
    camp_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    camp_p.add_argument("--json", action="store_true")

    summary_p = sub.add_parser(
        "summary",
        help="Lightweight daily digest (counts and reply highlights)",
    )
    summary_p.add_argument("--since", help="Date or window: today, YYYY-MM-DD, 48h, 7d")
    summary_p.add_argument("--workspace", help="Workspace slug (campaign prefix filter)")
    summary_p.add_argument("--campaign-prefix", help="Override campaign name LIKE prefix")
    summary_p.add_argument("--pull", action="store_true", help="Pull before summarizing")
    summary_p.add_argument(
        "--force-pull",
        action="store_true",
        help="With --pull, always contact relay (ignore 5m freshness cache)",
    )
    summary_p.add_argument("--json", action="store_true")

    pmap_p = sub.add_parser(
        "platform-map",
        help="Show platform/event type mappings (use --json for agents)",
    )
    pmap_p.add_argument("--platform", help="Filter to one platform id (e.g. prosp)")
    pmap_p.add_argument("--json", action="store_true")

    add_p = sub.add_parser("add-lead", help="Add a lead")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--company"); add_p.add_argument("--title")
    add_p.add_argument("--industry"); add_p.add_argument("--headcount")
    add_p.add_argument("--email"); add_p.add_argument("--linkedin")
    add_p.add_argument("--channel", default="email"); add_p.add_argument("--stage", default="prospecting")
    add_p.add_argument("--notes")
    add_p.add_argument("--workspace", help="Optional: associate lead with a workspace")

    imp_p = sub.add_parser(
        "import-profiles",
        help="Bulk import/enrich leads from CSV or JSON (tiered identity match)",
    )
    imp_p.add_argument("--file", help="Path to .csv, .json, or .jsonl file")
    imp_p.add_argument(
        "--json",
        dest="json_data",
        help='JSON array string, or "-" to read JSON array from stdin',
    )
    imp_p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    imp_p.add_argument("--yes", action="store_true", help="Skip confirmation prompt (no-op: import-profiles is non-destructive)")
    imp_p.add_argument("--overwrite", action="store_true", help="Overwrite non-empty profile fields")
    imp_p.add_argument("--channel", default="email")
    imp_p.add_argument("--stage", default="prospecting")
    imp_p.add_argument("--notes")
    imp_p.add_argument("--workspace", help="Workspace slug/ID to associate imported leads with")
    imp_p.add_argument("--sender-profile", dest="sender_profile", help="LinkedIn sender profile URL for connection status tracking")
    imp_p.add_argument(
        "--source",
        dest="source",
        help="Attribution source (e.g. sales_navigator, csv_import, trykitt, icypeas, lead_enrich)",
    )
    imp_p.add_argument("--source-detail", dest="source_detail", help="Attribution source detail (e.g. list name)")
    imp_p.add_argument(
        "--import-batch-id",
        dest="import_batch_id",
        help="Stable batch id for name-only rows (import_key dedupe within batch)",
    )

    imp_p.add_argument(
        "--no-sync",
        action="store_true",
        help="Deprecated, no-op — import-profiles never auto-syncs; run pipeline.py sync explicitly",
    )
    imp_p.add_argument(
        "--import-format",
        dest="import_format",
        choices=("auto", "generic", "sales_navigator"),
        default="auto",
        help="CSV format preset (auto-detect Sales Nav / Vayne exports by default)",
    )

    aef_p = sub.add_parser(
        "apply-email-find-results",
        help="Fast batch save when every row has lead id (outreachmagic email-finding)",
    )
    aef_p.add_argument("--file", help="Path to .json file")
    aef_p.add_argument("--json", dest="json_data", help='JSON array string, or "-" for stdin')
    aef_p.add_argument("--workspace", required=True, help="Workspace slug/ID (required for tags)")
    aef_p.add_argument("--dry-run", action="store_true")
    aef_p.add_argument("--overwrite", action="store_true")
    aef_p.add_argument("--source", dest="source", help="Attribution source (trykitt, icypeas, …)")
    aef_p.add_argument("--source-detail", dest="source_detail")

    tag_p = sub.add_parser("tag", help="Manage workspace-scoped lead tags")
    tag_sub = tag_p.add_subparsers(dest="tag_action")
    tag_add_p = tag_sub.add_parser("add", help="Add a tag to a lead")
    tag_add_p.add_argument("--workspace", required=True)
    tag_add_p.add_argument("--lead-id", type=int, required=True)
    tag_add_p.add_argument("--tag", required=True)
    tag_rm_p = tag_sub.add_parser("remove", help="Remove a tag from a lead")
    tag_rm_p.add_argument("--workspace", required=True)
    tag_rm_p.add_argument("--lead-id", type=int, required=True)
    tag_rm_p.add_argument("--tag", required=True)
    tag_set_p = tag_sub.add_parser("set", help="Replace all tags for a lead")
    tag_set_p.add_argument("--workspace", required=True)
    tag_set_p.add_argument("--lead-id", type=int, required=True)
    tag_set_p.add_argument("--tags", required=True, help="Comma-separated tags")
    tag_list_p = tag_sub.add_parser("list", help="List tags in a workspace")
    tag_list_p.add_argument("--workspace", required=True)
    tag_list_p.add_argument("--lead-id", type=int, help="Optional: filter to one lead")
    tag_bulk_p = tag_sub.add_parser("bulk", help="Add/remove tags across multiple leads")
    tag_bulk_p.add_argument("--workspace", required=True)
    tag_bulk_p.add_argument("--lead-ids", help="Comma-separated lead IDs")
    tag_bulk_p.add_argument(
        "--identity-type",
        choices=["linkedin_sales_nav_id", "email", "linkedin_url", "external_id"],
        help="Resolve --identity-values to lead ids instead of using --lead-ids "
             "(e.g. tag a fresh Sales Nav import by linkedin_sales_nav_id, its only stable identity)",
    )
    tag_bulk_p.add_argument("--identity-values", help="Comma-separated identity values, paired with --identity-type")
    tag_bulk_p.add_argument("--tags", required=True, help="Comma-separated tags")
    tag_bulk_p.add_argument("--remove", action="store_true", help="Remove instead of add")
    tag_repair_p = tag_sub.add_parser(
        "repair",
        help="Fix malformed workspace tags (e.g. \"['nace']\" -> nace)",
    )
    tag_repair_p.add_argument("--dry-run", action="store_true", help="Preview fixes without writing")

    bj_p = sub.add_parser(
        "batch-job",
        help="Generic async provider batch-job tracking (MillionVerifier, Scrubby, future providers)",
    )
    bj_sub = bj_p.add_subparsers(dest="batch_job_action")
    bj_record_p = bj_sub.add_parser("record", help="Record a newly-submitted batch job")
    bj_record_p.add_argument("--provider", required=True)
    bj_record_p.add_argument("--kind", required=True, help="e.g. email_verification, email_finding")
    bj_record_p.add_argument("--job-id", required=True)
    bj_record_p.add_argument("--item-count", type=int, required=True)
    bj_record_p.add_argument("--item-hash", required=True)
    bj_record_p.add_argument("--workspace")
    bj_record_p.add_argument("--metadata", help="JSON object")
    bj_find_p = bj_sub.add_parser("find-pending", help="Look up a not-yet-downloaded job for an item set")
    bj_find_p.add_argument("--provider", required=True)
    bj_find_p.add_argument("--item-hash", required=True)
    bj_status_p = bj_sub.add_parser("mark-status", help="Update a batch job's status")
    bj_status_p.add_argument("--provider", required=True)
    bj_status_p.add_argument("--job-id", required=True)
    bj_status_p.add_argument("--status", required=True)
    bj_list_p = bj_sub.add_parser("list", help="List batch jobs")
    bj_list_p.add_argument("--provider")
    bj_list_p.add_argument("--workspace")

    pa_p = sub.add_parser(
        "provider-attempt",
        help="Org-wide per-lead provider attempt tracking (trykitt, icypeas, serper, "
             "millionverifier, scrubby) -- no --workspace, follows the lead everywhere",
    )
    pa_sub = pa_p.add_subparsers(dest="provider_attempt_action")
    pa_bulk_p = pa_sub.add_parser("bulk", help="Stamp the same provider+status across multiple leads")
    pa_bulk_p.add_argument("--lead-ids", required=True, help="Comma-separated lead IDs")
    pa_bulk_p.add_argument("--provider", required=True)
    pa_bulk_p.add_argument("--status", default="unknown")
    pa_list_p = pa_sub.add_parser("list", help="List provider attempts for one lead")
    pa_list_p.add_argument("--lead-id", type=int, required=True)

    ver_p = sub.add_parser("verify-email", help="Record email verification result")
    ver_p.add_argument("--lead-id", type=int, help="Lead ID (single mode)")
    ver_p.add_argument("--status", help="Verification status (valid, invalid, catch-all, unknown, risky, etc.)")
    ver_p.add_argument("--source", help="Verification source (zerobounce, neverbounce, etc.)")
    ver_p.add_argument("--sub-status", dest="sub_status", help="Sub-status detail")
    ver_p.add_argument("--source-detail", dest="source_detail")
    ver_p.add_argument("--smtp-provider", dest="smtp_provider")
    ver_p.add_argument("--batch", action="store_true", help="Read JSON array from --json or --file")
    ver_p.add_argument("--json", dest="json_input", help="JSON array for batch mode")
    ver_p.add_argument("--file", help="JSON file path for batch mode")

    vers_p = sub.add_parser("verify-status", help="Check verification status for a lead")
    vers_p.add_argument("--lead-id", type=int)
    vers_p.add_argument("--email")

    verp_p = sub.add_parser("verify-pending", help="List leads needing email verification")
    verp_p.add_argument("--limit", type=int, default=50)
    verp_p.add_argument("--json", action="store_true")

    vc_p = sub.add_parser(
        "verification-candidates",
        help="List workspace leads due for MillionVerifier (or similar) re-check",
    )
    vc_p.add_argument("--workspace", required=True)
    vc_p.add_argument("--max-age", type=int, default=30, dest="max_age")
    vc_p.add_argument("--skip-mv-days", type=int, default=7, dest="skip_mv_days")
    vc_p.add_argument("--limit", type=int, default=5000)
    vc_p.add_argument(
        "--never-contacted",
        action="store_true",
        help="Exclude leads with any outreach activity",
    )
    vc_p.add_argument(
        "--include-mv-attempted",
        action="store_true",
        help="Include leads already tagged mv_attempted",
    )

    bounce_p = sub.add_parser("bounce-list", help="List deduplicated platform bounce records")
    bounce_p.add_argument("--platform", help="Filter by platform (plusvibe, smartlead, etc.)")
    bounce_p.add_argument("--bounce-type", dest="bounce_type", choices=("hard", "soft", "unknown"))
    bounce_p.add_argument("--sender", help="Filter by sending mailbox")
    bounce_p.add_argument("--since", help="Last seen on/after date (YYYY-MM-DD or today)")
    bounce_p.add_argument("--limit", type=int, default=100)
    bounce_p.add_argument("--json", action="store_true")

    bounce_stats_p = sub.add_parser("bounce-stats", help="Deliverability bounce analytics summary")
    bounce_stats_p.add_argument("--since", help="Last seen on/after date (YYYY-MM-DD or today)")
    bounce_stats_p.add_argument("--json", action="store_true")

    export_p = sub.add_parser(
        "export",
        help="Export leads to local CSV or JSON (use sheets export for Google Sheets)",
    )
    export_p.add_argument("--workspace", required=True, help="Workspace slug")
    export_p.add_argument("--tag", help="Filter by workspace tag")
    export_p.add_argument("--stage", help="Filter by workspace stage")
    export_p.add_argument("--since", help="Created/updated on or after date (YYYY-MM-DD or today)")
    export_p.add_argument("--limit", type=int, default=5000)
    export_p.add_argument(
        "--format",
        choices=("csv", "json", "sheets"),
        default="csv",
        help="csv/json write local files; sheets opens a hosted Google Sheet (see: pipeline.py sheets export)",
    )
    export_p.add_argument("--file", help="Output path under outreachmagic/exports/ (default auto-named)")
    export_p.add_argument(
        "--never-contacted",
        action="store_true",
        help="Only leads with no email/LinkedIn sends, replies, or events",
    )
    export_p.add_argument("--no-email", action="store_true", help="Only leads missing email")
    export_p.add_argument(
        "--require-domain",
        action="store_true",
        help="Only leads with companies.domain set (never fall back to company name)",
    )

    efc_p = sub.add_parser(
        "email-finding-candidates",
        help="List workspace leads shaped for email-finding (real domains only)",
    )
    efc_p.add_argument("--workspace", required=True)
    efc_p.add_argument("--tag")
    efc_p.add_argument("--stage")
    efc_p.add_argument("--since")
    efc_p.add_argument("--limit", type=int, default=5000)
    efc_p.add_argument("--never-contacted", action="store_true")
    efc_p.add_argument("--no-email", action="store_true", default=True)
    efc_p.add_argument(
        "--require-domain",
        action="store_true",
        help="Pre-filter scope to leads with a professional company domain "
             "(the domain-based candidate builder already drops domain-less leads "
             "regardless; this narrows the scope earlier and affects the reported counts)",
    )
    efc_p.add_argument(
        "--linkedin-only",
        action="store_true",
        help="Instead of domain-based candidates, list leads with no email, no usable "
             "company domain, but a linkedin_url -- shaped for TryKitt's optional "
             "linkedinStandardProfileURL signal. Run separately from the default "
             "domain-based mode, not combined with it.",
    )
    efc_p.add_argument(
        "--lead-ids",
        help="Comma-separated lead ids to scope candidates (e.g. from a CSV batch)",
    )
    efc_p.add_argument(
        "--file",
        help="JSON batch file; lead_id from each row scopes candidates",
    )

    agent_export_p = sub.add_parser("agent-changes", help="Show agent-created leads and events not yet synced")
    agent_export_p.add_argument("--json", action="store_true", help="Output as JSON (default)")
    agent_export_p.add_argument("--file", help="Write CSV to file (import-profiles compatible)")
    agent_export_p.add_argument("--all", action="store_true", help="Include all leads, not just locally-created")
    agent_export_p.add_argument("--workspace", help="Filter export to a specific workspace")
    agent_export_p.add_argument("--limit", type=int, help="Cap lead rows fetched/built (quick spot check on large accounts)")

    up_p = sub.add_parser("update-stage", help="Update lead stage")
    up_p.add_argument("--id", type=int, required=True); up_p.add_argument("--stage", required=True)
    up_p.add_argument("--next-action")
    up_p.add_argument("--sentiment", choices=["positive", "negative", "autoreply", "invalid", "neutral"],
                      help="Qualitative sentiment for this stage change")
    up_p.add_argument("--label", help="Human-readable status label (e.g. 'not interested', 'meeting booked')")
    up_p.add_argument("--workspace", help="Workspace for this stage change (required in multi-workspace mode)")
    up_p.add_argument("--crm-sync", action="store_true", help="Trigger CRM sync after stage update")

    log_p = sub.add_parser("log-event", help="Log an outreach event")
    log_p.add_argument("--lead-id", type=int, required=True)
    log_p.add_argument("--type", dest="event_type", required=True)
    log_p.add_argument("--direction", default="outbound"); log_p.add_argument("--channel", default="email")
    log_p.add_argument("--subject"); log_p.add_argument("--body")
    log_p.add_argument("--metadata", help='JSON metadata string e.g. \'{"lead_status_raw":"not_interested","lead_status_sentiment":"negative"}\'')
    log_p.add_argument("--workspace", help="Workspace for this event (required in multi-workspace mode)")
    log_p.add_argument("--crm-sync", action="store_true", help="Trigger CRM sync after logging event")

    fd_p = sub.add_parser(
        "find-domains",
        help="Discover company domains + public emails via Serper for undomained companies in a workspace",
    )
    fd_p.add_argument("--workspace", required=True, help="Workspace whose undomained companies to search")
    fd_p.add_argument("--limit", type=int, help="Cap number of companies searched this run")
    fd_p.add_argument("--force", action="store_true", help="Re-search companies that already have a domain or a cached lookup")
    fd_p.add_argument("--dry-run", action="store_true", help="List target companies and worst-case query count without spending Serper credits")
    fd_p.add_argument("--max-queries", type=int, help="Hard cap on Serper queries this run; stops cleanly when reached")
    fd_p.add_argument("--debug", action="store_true", help="Store the full raw Serper response in the observation (large; off by default)")

    # ── Setup & relay commands ──
    login_p = sub.add_parser("login", help="Connect this machine via browser (device authorization)")
    login_p.add_argument(
        "--platform",
        choices=["hermes", "cursor", "claude-code"],
        help="Host app (auto-detected from skill install path if omitted)",
    )
    login_p.add_argument("--generate-url", action="store_true", help="Generate device URL/code and exit")
    login_p.add_argument("--claim-token", action="store_true", help="Claim token for an existing device code")
    login_p.add_argument("--device-code", help="Device code returned from --generate-url")
    login_p.add_argument(
        "--wait",
        type=int,
        default=30,
        help="Seconds to wait while polling in --claim-token mode (0 = single attempt)",
    )
    login_p.add_argument(
        "--force",
        action="store_true",
        help="Run browser device login even when a valid agent key is already configured",
    )
    sub.add_parser("logout", help="Clear local agent credentials")

    sync_secrets_p = sub.add_parser(
        "sync-secrets",
        help="Sync org API keys from dashboard vault to local agent_secrets.env",
    )
    sync_secrets_p.add_argument("--check", action="store_true", help="Report local key status only (no network)")
    sync_secrets_p.add_argument("--json", action="store_true", help="Emit JSON")
    sync_secrets_p.add_argument("--cron", action="store_true", help=argparse.SUPPRESS)

    api_keys_p = sub.add_parser(
        "api-keys",
        help="Show runtime API key status (last use, errors, failover)",
    )
    api_keys_p.add_argument("--json", action="store_true", help="Emit JSON")
    api_keys_p.add_argument(
        "--push",
        action="store_true",
        help="Report runtime status to dashboard (no secret values)",
    )

    pull_p = sub.add_parser("pull", help="Pull events from relay to local database")
    pull_p.add_argument("--cron", action="store_true", help="Silent mode for cron")
    pull_p.add_argument("--full", action="store_true", help="Re-import all webhook events (after DB reset)")
    pull_p.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt for pull --full",
    )
    pull_p.add_argument(
        "--diagnose",
        action="store_true",
        help="Print pull cursor and dedupe diagnostics",
    )
    pull_p.add_argument(
        "--debug-sentiment",
        action="store_true",
        help="Print a label -> sentiment mapping summary (and any unmapped labels) after the pull",
    )
    pull_p.add_argument(
        "--skip-routing-sync",
        action="store_true",
        help="Skip cloud routing config sync (events pull only; use if routing API times out)",
    )
    pull_p.add_argument(
        "--probe",
        action="store_true",
        help="Read-only backlog check (limit=1/relay stream, no ingest)",
    )
    pull_p.add_argument(
        "--kind",
        metavar="KINDS",
        help="Comma-separated streams: events,core,workspace,company,sender_account,sender_domain (default: all)",
    )
    pull_p.add_argument(
        "--skip-snapshots",
        action="store_true",
        help="Only pull webhook events (skip lead/workspace snapshots)",
    )
    pull_p.add_argument(
        "--reset-snapshot-cursors",
        action="store_true",
        help="Zero core/workspace snapshot cursors before pull (fix desync after hung partial pulls)",
    )
    pull_p.add_argument(
        "--if-stale",
        metavar="DURATION",
        help="Skip pull when last_pull is within DURATION (e.g. 5m, 1h)",
    )
    pull_p.add_argument(
        "--force",
        action="store_true",
        help="Always pull from relay (ignore --if-stale)",
    )

    refresh_p = sub.add_parser(
        "refresh",
        help="DANGER: sync, backup, staging pull --full, then swap DB (rare; keeps old DB until pull succeeds)",
    )
    refresh_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive refresh after reading the warning",
    )
    refresh_p.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip pre-refresh sync (not recommended — may lose unsynced local data)",
    )
    refresh_p.add_argument(
        "--backup",
        help="Backup path for the current database (default: outreachmagic.db.backup-<timestamp>.db)",
    )

    restore_p = sub.add_parser(
        "restore",
        help="Restore local database from a refresh backup",
    )
    restore_p.add_argument(
        "--list",
        action="store_true",
        help="List available backup files (newest first)",
    )
    restore_p.add_argument(
        "--latest",
        action="store_true",
        help="Restore the newest backup in the databases folder",
    )
    restore_p.add_argument(
        "--from",
        dest="from_path",
        help="Path to a specific backup .db file",
    )
    restore_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore (replaces the live database)",
    )

    status_p = sub.add_parser("status", help="Dashboard-style status: plan, connections, usage, routing")
    status_p.add_argument("--json", action="store_true", help="JSON output for agents")

    whoami_p = sub.add_parser("whoami", help="Show connected account email, org, and plan")
    whoami_p.add_argument("--json", action="store_true", help="JSON output for agents")
    sync_p = sub.add_parser("sync", help="Push pending workspaces and routing rules to the webapp")
    sync_p.add_argument("--status", action="store_true", help="Show what needs syncing without pushing")
    sync_p.add_argument("--json", action="store_true", help="JSON-only output (use with --status)")
    sync_p.add_argument(
        "--inspect",
        metavar="VALUE",
        help=(
            "Show the exact sync payload for one entity, keyed by --type "
            "(default lead: email or lead id -- id is required for a weak-identity "
            "lead that has no email at all; requires --workspace)"
        ),
    )
    sync_p.add_argument(
        "--type",
        choices=[
            "lead", "company", "sender_account", "sender_domain", "event",
            "merge_delete", "company_merge_delete", "quarantine_resolution",
        ],
        default="lead",
        help=(
            "Entity type for --inspect (default: lead). --inspect VALUE is: email for "
            "lead/sender_account, domain for company/sender_domain, event id for event, "
            "lead_merges.id or merge_entity_key for merge_delete, company_merges.id or "
            "merge_entity_key for company_merge_delete, queue id or "
            "external_event_id for quarantine_resolution"
        ),
    )
    sync_p.add_argument(
        "--workspace",
        help="Scope push/status to a single workspace slug (default: all workspaces)",
    )
    sync_p.add_argument(
        "--no-health-report",
        action="store_true",
        help="Skip aggregate local DB health POST to portal (lead sync still runs)",
    )
    sync_p.add_argument(
        "--full-snapshot",
        action="store_true",
        help=(
            "Mark every lead, workspace membership, company, sender account, and "
            "sender domain pending for a full resync to relay. Expensive: on a large "
            "account this can take a very long time to drain. Requires --yes unless "
            "scoped with --workspace"
        ),
    )
    sync_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm --full-snapshot across the whole account (required unless --workspace is set)",
    )
    sync_p.add_argument(
        "--bulk",
        action="store_true",
        help="Force large snapshot batch sizes regardless of pending count",
    )
    sync_p.add_argument(
        "--no-bulk",
        action="store_true",
        help="Force routine (smaller) snapshot batch sizes",
    )
    sync_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what sync would push without sending anything (sampled per stream)",
    )
    sync_p.add_argument(
        "--sample-size",
        type=int,
        default=3,
        help="Entries to preview per stream with --dry-run (default: 3)",
    )
    sync_p.add_argument(
        "--file",
        help="With --dry-run or --inspect, write the output JSON to a file instead of stdout",
    )

    activity_p = sub.add_parser("activity", help="Lead activity summary (last contacted, counts)")
    activity_sub = activity_p.add_subparsers(dest="activity_command", required=True)
    activity_show_p = activity_sub.add_parser("show", help="Show stored/computed/sync activity for a lead")
    activity_show_p.add_argument("--lead-id", type=int)
    activity_show_p.add_argument("--email")
    activity_show_p.add_argument("--workspace", help="Workspace slug (recommended)")
    activity_show_p.add_argument("--json", action="store_true", default=True)
    activity_recompute_p = activity_sub.add_parser(
        "recompute", help="Recompute activity from events for a lead"
    )
    activity_recompute_p.add_argument("--lead-id", type=int, required=True)
    activity_recompute_p.add_argument("--workspace", help="Limit to one workspace")
    activity_recompute_p.add_argument("--json", action="store_true", default=True)
    db_health_p = sub.add_parser("db-health", help="Local SQLite health (aggregates only)")
    db_health_p.add_argument("--json", action="store_true", help="Print JSON")
    db_health_p.add_argument("--full", action="store_true", help="Run full integrity_check (slower on large DBs)")
    db_health_p.add_argument("--push", action="store_true", help="POST health to portal (debug)")
    db_health_p.add_argument("--verbose", action="store_true", help="Include internal diagnostics (page counts, table breakdown)")
    archive_p = sub.add_parser("archive", help="Export workspace data to a separate SQLite file")
    archive_p.add_argument("--workspace", required=True, help="Workspace slug")
    archive_p.add_argument("--output", help="Output .db path (required unless --dry-run)")
    archive_p.add_argument("--dry-run", action="store_true", help="Show counts only")
    archive_p.add_argument("--purge", action="store_true", help="Remove exported data from main DB (requires --output)")
    archive_p.add_argument("--vacuum", action="store_true", help="Run VACUUM after --purge")

    crm_sync_p = sub.add_parser(
        "crm-sync",
        help="Push leads to CRM (GHL / HubSpot). Delegates to crm_sync.py.",
    )
    crm_sync_p.add_argument(
        "action",
        nargs="?",
        choices=["sync", "status", "discover"],
        default="status",
        help="Action: sync leads, show status, or discover pipelines (default: status)",
    )
    crm_sync_p.add_argument("--workspace", help="Workspace slug (required for sync/discover)")
    crm_sync_p.add_argument("--all", action="store_true", help="Sync all enabled CRM workspaces")
    crm_sync_p.add_argument("--dry-run", action="store_true", help="Preview without API calls")
    crm_sync_p.add_argument("--lead-id", type=int, help="Sync a single lead by ID")
    crm_sync_p.add_argument("--skip-events", action="store_true", help="Skip event history push")
    crm_sync_p.add_argument("--platform", choices=["ghl", "hubspot"], help="Filter by platform")
    crm_sync_p.add_argument("--max-age", help="Only sync leads active in last N days (e.g. 7d, 30d, 90d)")

    conn_p = sub.add_parser("connections", help="List connected platforms with webhook URLs and stats")
    conn_p.add_argument("--json", action="store_true")

    cp_p = sub.add_parser("connect-platform", help="Generate a webhook URL for a platform")
    cp_p.add_argument("--platform", required=True,
                       help="Platform id (smartlead, instantly, heyreach, plusvibe, emailbison, etc.)")

    dp_p = sub.add_parser("disconnect-platform", help="Delete a platform webhook token (URL stops working)")
    dp_p.add_argument("--platform", required=True,
                       help="Platform id to disconnect")
    dp_p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    query_cli.register_query_parser(sub)
    query_cli.register_sql_parser(sub)
    query_cli.register_schema_parser(sub)
    query_cli.register_tag_summary_parser(sub)

    bulk_lookup_p = sub.add_parser(
        "batch-lead-lookup",
        help="Lookup many leads in one DB pass (companion dedup)",
    )
    bulk_lookup_p.add_argument("--json", dest="json_input", help="JSON array of lookup keys")
    bulk_lookup_p.add_argument("--file", help="JSON file path (array of lookup keys)")
    bulk_lookup_p.add_argument("--workspace", help="Workspace slug or name")

    hist_p = sub.add_parser("history", help="Show event history for a lead")
    hist_p.add_argument("--id", type=int, help="Lead ID")
    hist_p.add_argument("--email", help="Find lead by email")
    hist_p.add_argument("--linkedin", help="Find lead by LinkedIn URL or profile slug")
    hist_p.add_argument("--name", help="Find lead by name (partial match)")
    hist_p.add_argument("--workspace", help="Filter lead lookup by workspace name or slug")

    dedup_p = sub.add_parser("dedup", help="Find and batch-merge duplicate leads")
    dedup_sub = dedup_p.add_subparsers(dest="dedup_command", required=True)

    dedup_find_p = dedup_sub.add_parser("find", help="Scan workspace for duplicate leads")
    dedup_find_p.add_argument("--workspace", required=True, help="Workspace slug")
    dedup_find_p.add_argument("--tag", help="Tag filter (supports %% wildcards)")
    dedup_find_p.add_argument("--output", help="Write candidates JSON (default stdout)")
    dedup_find_p.add_argument(
        "--min-confidence",
        choices=pipeline_dedup.CONFIDENCE_ORDER,
        default=pipeline_dedup.MIN_CONFIDENCE_DEFAULT_FIND,
    )

    dedup_merge_p = dedup_sub.add_parser("merge", help="Batch-merge from candidates JSON")
    dedup_merge_p.add_argument("--candidates", required=True, help="JSON from dedup find")
    dedup_merge_p.add_argument("--commit", action="store_true", help="Perform merges (default dry-run)")
    dedup_merge_p.add_argument(
        "--min-confidence",
        choices=pipeline_dedup.CONFIDENCE_ORDER,
        default=pipeline_dedup.MIN_CONFIDENCE_DEFAULT_MERGE,
    )
    dedup_merge_p.add_argument("--reason", default="dedup", help="Reason stored in lead_merges")

    dedup_events_p = sub.add_parser(
        "dedup-events",
        help="Merge historical duplicate email_reply events sharing a message_id",
    )
    dedup_events_p.add_argument("--commit", action="store_true", help="Perform the merge (default dry-run)")

    company_p = sub.add_parser("company", help="Company entity-resolution audit, review, and merge")
    company_sub = company_p.add_subparsers(dest="company_command", required=True)

    company_audit_p = company_sub.add_parser(
        "dedup-audit",
        help="Read-only report: existing companies sharing a name, flagged by conflicting lead email domains",
    )
    company_audit_p.add_argument("--limit", type=int, help="Cap the number of groups returned")

    company_backfill_p = company_sub.add_parser(
        "backfill-candidates",
        help="Queue likely pre-existing bad merges from dedup-audit into the merge-review queue",
    )

    company_domain_stats_p = company_sub.add_parser(
        "domain-stats",
        help="Per-domain found/attempted email counts and rank for one company",
    )
    company_domain_stats_p.add_argument("--id", type=int, required=True, help="Company id")

    company_domain_label_p = company_sub.add_parser(
        "domain-label",
        help="Set a human-curated label (e.g. branch/department name) on a company's known domain",
    )
    company_domain_label_p.add_argument("--company-id", type=int, required=True)
    company_domain_label_p.add_argument("--domain", required=True)
    company_domain_label_p.add_argument("--label", required=True)

    company_mr_p = company_sub.add_parser(
        "merge-review",
        help="Review proposed company merges (name-only domain-attach conflicts, backfill audit) before executing",
    )
    company_mr_sub = company_mr_p.add_subparsers(dest="company_merge_review_action")
    company_mr_list_p = company_mr_sub.add_parser("list", help="List merge candidates")
    company_mr_list_p.add_argument("--status", default="pending", help="Filter by status (default: pending)")
    company_mr_list_p.add_argument("--reason", help="Filter by reason (e.g. name_only_domain_attach, backfill_audit)")
    company_mr_list_p.add_argument("--limit", type=int, default=50)
    company_mr_list_p.add_argument(
        "--min-confidence",
        choices=pipeline_dedup.CONFIDENCE_ORDER,
        default="ALL",
        help="Filter to candidates at or above this confidence tier (HIGH = mechanically explainable, e.g. same registrable domain)",
    )
    company_mr_approve_p = company_mr_sub.add_parser("approve", help="Execute a proposed company merge")
    company_mr_approve_p.add_argument("--id", required=True, help="Candidate id from 'company merge-review list'")
    company_mr_reject_p = company_mr_sub.add_parser("reject", help="Dismiss a proposed company merge without merging")
    company_mr_reject_p.add_argument("--id", required=True)
    company_mr_reject_p.add_argument("--note", help="Optional reason for the rejection")

    company_merge_p = company_sub.add_parser("merge", help="Merge two company records into one")
    company_merge_p.add_argument("--keep", type=int, required=True, help="Company ID to keep")
    company_merge_p.add_argument("--merge", type=int, required=True, help="Company ID to merge into --keep and delete")

    review_p = sub.add_parser(
        "review",
        help="Dedup review and two-way Google Sheet sync workflows (not the same as sheets export)",
    )
    review_sub = review_p.add_subparsers(dest="review_command", required=True)

    review_templates_p = review_sub.add_parser("templates", help="List review templates")
    review_templates_sub = review_templates_p.add_subparsers(dest="templates_command", required=True)
    review_templates_sub.add_parser("list", help="List available templates")

    review_export_p = review_sub.add_parser(
        "export",
        help="Export leads to a Google Sheet via OM API (dedup-review or lead-review)",
    )
    review_export_p.add_argument("--template", default="dedup-review")
    review_export_p.add_argument("--input", help="candidates.json from dedup find (dedup-review)")
    review_export_p.add_argument("--title", default="Outreach Dedup Review")
    review_export_p.add_argument("--share-email", help="Email to share sheet with (default: org owner)")
    review_export_p.add_argument(
        "--anyone-with-link",
        action="store_true",
        help="Unlisted URL — anyone with the link can edit (no email share)",
    )
    review_export_p.add_argument(
        "--public",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    review_export_p.add_argument(
        "--sheet-id",
        help="Refresh an existing Google Sheet instead of creating a new one",
    )
    review_export_p.add_argument(
        "--parent-sheet-id",
        help="Add as a new tab in an existing spreadsheet (requires --tab-name)",
    )
    review_export_p.add_argument(
        "--tab-name",
        help="Tab name when using --parent-sheet-id (default: stage or title)",
    )
    review_export_p.add_argument("--workspace", help="Workspace slug (lead-review)")
    review_export_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        default="standard",
        help="Column set for lead-review template",
    )
    review_export_p.add_argument("--fields", help="Comma-separated columns for --detail custom")
    review_export_p.add_argument("--tag", help="Filter workspace tag (lead-review)")
    review_export_p.add_argument("--stage", help="Filter workspace stage (lead-review)")
    review_export_p.add_argument("--since", help="Created/updated since (lead-review)")
    review_export_p.add_argument("--limit", type=int, default=5000)
    review_export_p.add_argument("--never-contacted", action="store_true")
    review_export_p.add_argument("--no-email", action="store_true")
    review_export_p.add_argument("--require-domain", action="store_true")
    review_export_p.add_argument("--original-source", dest="original_source")
    review_export_p.add_argument("--original-source-detail", dest="original_source_detail")
    review_export_p.add_argument("--latest-source", dest="latest_source")
    review_export_p.add_argument("--latest-source-detail", dest="latest_source_detail")
    review_export_p.add_argument("--industry")
    review_export_p.add_argument("--headcount-min", dest="headcount_min", type=int)
    review_export_p.add_argument("--headcount-max", dest="headcount_max", type=int)
    review_export_p.add_argument("--location-city", dest="location_city")
    review_export_p.add_argument("--location-state", dest="location_state")
    review_export_p.add_argument("--email-domain", dest="email_domain")
    review_export_p.add_argument("--email-verified", dest="email_verification_status")

    review_sync_p = review_sub.add_parser("sync", help="Read sheet approvals and merge locally")
    review_sync_p.add_argument("--sheet-id", required=True)
    review_sync_p.add_argument("--template", default="dedup-review")
    review_sync_p.add_argument("--workspace", help="Workspace slug (required for lead-review sync)")
    review_sync_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        help="Rebuild field_keys for lead-review sync (default: standard)",
    )
    review_sync_p.add_argument("--fields", help="Comma-separated columns used at export (--detail custom)")
    review_sync_p.add_argument("--tag", help="Filter workspace tag (override stored export metadata)")
    review_sync_p.add_argument("--stage", help="Filter workspace stage (override stored export metadata)")
    review_sync_p.add_argument("--since", help="Created/updated since (override stored export metadata)")
    review_sync_p.add_argument("--limit", type=int, help="Max leads for baseline (override stored export metadata)")
    review_sync_p.add_argument("--never-contacted", action="store_true", dest="sync_never_contacted",
                               help="Override stored export: never-contacted filter")
    review_sync_p.add_argument("--no-email", action="store_true", dest="sync_no_email",
                               help="Override stored export: no-email filter")
    review_sync_p.add_argument("--require-domain", action="store_true", dest="sync_require_domain",
                               help="Override stored export: require-domain filter")
    review_sync_p.add_argument("--dry-run", action="store_true", help="Report approved rows only")
    review_sync_p.add_argument("--commit", action="store_true", help="Merge approved rows and write results")

    review_payload_p = review_sub.add_parser(
        "export-payload",
        help="Build lead-review export payload locally (no API call)",
    )
    review_payload_p.add_argument("--workspace", required=True)
    review_payload_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        default="standard",
    )
    review_payload_p.add_argument("--fields", help="Comma-separated columns for --detail custom")
    review_payload_p.add_argument("--tag")
    review_payload_p.add_argument("--stage")
    review_payload_p.add_argument("--since")
    review_payload_p.add_argument("--limit", type=int, default=5000)
    review_payload_p.add_argument("--never-contacted", action="store_true")
    review_payload_p.add_argument("--no-email", action="store_true")
    review_payload_p.add_argument("--require-domain", action="store_true")
    review_payload_p.add_argument("--original-source", dest="original_source")
    review_payload_p.add_argument("--original-source-detail", dest="original_source_detail")
    review_payload_p.add_argument("--latest-source", dest="latest_source")
    review_payload_p.add_argument("--latest-source-detail", dest="latest_source_detail")
    review_payload_p.add_argument("--industry")
    review_payload_p.add_argument("--headcount-min", dest="headcount_min", type=int)
    review_payload_p.add_argument("--headcount-max", dest="headcount_max", type=int)
    review_payload_p.add_argument("--location-city", dest="location_city")
    review_payload_p.add_argument("--location-state", dest="location_state")
    review_payload_p.add_argument("--email-domain", dest="email_domain")
    review_payload_p.add_argument("--email-verified", dest="email_verification_status")
    review_payload_p.add_argument("--title", default="Lead Review")

    review_apply_p = review_sub.add_parser(
        "apply-sync",
        help="Apply lead-review sync from local JSON sheet rows (no API call)",
    )
    review_apply_p.add_argument("--workspace", required=True)
    review_apply_p.add_argument("--input", required=True, help="JSON file: array of sheet row dicts")
    review_apply_p.add_argument("--dry-run", action="store_true")
    review_apply_p.add_argument("--commit", action="store_true")

    review_presets_p = review_sub.add_parser("presets", help="List lead-review field presets and groups")
    review_presets_p.add_argument("--template", default="lead-review")

    sheets_p = sub.add_parser(
        "sheets",
        help="Export workspace leads to a hosted Google Sheet (no local Google credentials)",
        description="Export workspace leads to a hosted Google Sheet via Outreach Magic (no local Google credentials).",
    )
    sheets_sub = sheets_p.add_subparsers(dest="sheets_command", required=True)
    sheets_export_p = sheets_sub.add_parser(
        "export",
        help="Create a Google Sheet from workspace leads (no local Google credentials)",
    )
    sheets_export_p.add_argument("--workspace", required=True, help="Workspace slug")
    sheets_export_p.add_argument("--title", help="Sheet title (default: <workspace> leads)")
    sheets_export_p.add_argument("--share-email", help="Email to share sheet with (default: org owner)")
    sheets_export_p.add_argument(
        "--anyone-with-link",
        action="store_true",
        help="Unlisted URL — anyone with the link can edit (no email share)",
    )
    sheets_export_p.add_argument(
        "--public",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    sheets_export_p.add_argument(
        "--sheet-id",
        help="Refresh an existing Google Sheet instead of creating a new one",
    )
    sheets_export_p.add_argument(
        "--parent-sheet-id",
        help="Add as a new tab in an existing spreadsheet (requires --tab-name)",
    )
    sheets_export_p.add_argument(
        "--tab-name",
        help="Tab name when using --parent-sheet-id (default: stage or title)",
    )
    sheets_export_p.add_argument(
        "--detail",
        choices=pipeline_lead_review.DETAIL_LEVELS,
        default="standard",
    )
    sheets_export_p.add_argument("--fields", help="Comma-separated columns for --detail custom")
    sheets_export_p.add_argument("--tag")
    sheets_export_p.add_argument("--stage")
    sheets_export_p.add_argument("--since")
    sheets_export_p.add_argument("--limit", type=int, default=5000)
    sheets_export_p.add_argument("--never-contacted", action="store_true")
    sheets_export_p.add_argument("--no-email", action="store_true")
    sheets_export_p.add_argument("--require-domain", action="store_true")

    sheets_cs_p = sheets_sub.add_parser(
        "campaign-stats",
        help="Export campaign performance data to a multi-sheet Google Workbook",
    )
    sheets_cs_p.add_argument("--workspace", required=True, help="Workspace slug")
    sheets_cs_p.add_argument(
        "--since", default="14d",
        help="Time window: 14d, 30d, 7d, all, or YYYY-MM-DD",
    )
    sheets_cs_p.add_argument("--share-email", help="Email to share sheet with (default: org owner)")
    sheets_cs_p.add_argument(
        "--anyone-with-link",
        action="store_true",
        help="Unlisted URL — anyone with the link can edit (no email share)",
    )
    sheets_cs_p.add_argument(
        "--public",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    sheets_cs_p.add_argument(
        "--sheet-id",
        help="Refresh an existing Google Sheet instead of creating a new one",
    )
    sheets_cs_p.add_argument("--dry-run", action="store_true", help="Print payload without uploading")
    sheets_cs_p.add_argument("--json", action="store_true", help="Print payload as JSON (implies --dry-run)")
    sheets_cs_p.add_argument(
        "--update",
        action="store_true",
        help="Reuse the last sheet for this workspace instead of creating a new one. Stores the sheet ID at sheets/<workspace>-campaign-stats.latest.json for cron.",
    )

    merge_p = sub.add_parser("merge-leads", help="Merge two lead records into one")
    merge_p.add_argument("--keep", type=int, help="Lead ID to keep")
    merge_p.add_argument("--merge", type=int, help="Lead ID to merge into --keep and delete")
    merge_p.add_argument("--email", help="Keep lead matched by email (with --linkedin)")
    merge_p.add_argument("--linkedin", help="Merge lead matched by LinkedIn into email lead")

    mr_p = sub.add_parser(
        "merge-review",
        help="Review proposed lead merges (email-find conflicts, identity conflicts) before executing",
    )
    mr_sub = mr_p.add_subparsers(dest="merge_review_action")
    mr_list_p = mr_sub.add_parser("list", help="List merge proposals")
    mr_list_p.add_argument("--status", default="pending", help="Filter by status (default: pending)")
    mr_list_p.add_argument("--reason", help="Filter by reason (e.g. email_find_conflict, identity_conflict)")
    mr_list_p.add_argument("--limit", type=int, default=50)
    mr_approve_p = mr_sub.add_parser("approve", help="Execute a proposed merge")
    mr_approve_p.add_argument("--id", required=True, help="Merge job id from 'merge-review list'")
    mr_reject_p = mr_sub.add_parser("reject", help="Dismiss a proposed merge without merging")
    mr_reject_p.add_argument("--id", required=True)
    mr_reject_p.add_argument("--note", help="Optional reason for the rejection")

    ilc_p = sub.add_parser(
        "import-linkedin-connections",
        help="Import a LinkedIn connections CSV export and track connection status per sender",
    )
    ilc_p.add_argument("--file", required=True, help="Path to LinkedIn's Connections.csv export")
    ilc_p.add_argument("--workspace", required=True, help="Workspace slug/ID to associate imported leads with")
    ilc_p.add_argument(
        "--sender", required=True,
        help="LinkedIn sender profile URL this connections list belongs to (e.g. linkedin.com/in/janedoe)",
    )
    ilc_p.add_argument("--tag", help="Optional tag applied to every matched/imported lead (requires --workspace)")
    ilc_p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    ilc_p.add_argument("--overwrite", action="store_true", help="Overwrite non-empty profile fields")

    isa_p = sub.add_parser(
        "import-sender-accounts",
        help="Import a PlusVibe sender-account CSV export (warmup/health/deliverability data)",
    )
    isa_p.add_argument("--file", required=True, help="Path to the PlusVibe CSV export")
    isa_p.add_argument("--workspace", help="Associate every row with this workspace slug "
                                           "(default: leave unlinked; link explicitly afterward)")

    sa_p = sub.add_parser("sender-accounts", help="List/link/edit imported sender accounts")
    sa_sub = sa_p.add_subparsers(dest="sender_accounts_action")
    sa_list_p = sa_sub.add_parser("list", help="List sender accounts")
    sa_list_p.add_argument("--workspace", help="Filter to sender accounts linked to this workspace")
    sa_list_p.add_argument("--json", action="store_true")
    sa_update_p = sa_sub.add_parser("update", help="Edit a sender account's own fields (not sync-owned metrics)")
    sa_update_p.add_argument("--email", required=True, help="Sender account email")
    sa_update_p.add_argument("--provider")
    sa_update_p.add_argument("--first-name", dest="first_name")
    sa_update_p.add_argument("--last-name", dest="last_name")
    sa_update_p.add_argument("--daily-limit", dest="daily_limit", type=int)
    sa_update_p.add_argument("--status")
    sa_update_p.add_argument("--warmup-status", dest="warmup_status")
    sa_update_p.add_argument("--channel", choices=("email", "linkedin"))
    sa_link_p = sa_sub.add_parser("link", help="Link a sender account to a workspace")
    sa_link_p.add_argument("--email", required=True, help="Sender account email")
    sa_link_p.add_argument("--workspace", required=True, help="Workspace slug")
    sa_unlink_p = sa_sub.add_parser("unlink", help="Unlink a sender account from a workspace")
    sa_unlink_p.add_argument("--email", required=True, help="Sender account email")
    sa_unlink_p.add_argument("--workspace", required=True, help="Workspace slug")

    sd_p = sub.add_parser("sender-domains", help="Per-domain sender counts, cost, and reseller tracking")
    sd_sub = sd_p.add_subparsers(dest="sender_domains_action")
    sd_list_p = sd_sub.add_parser("list", help="List domains with live sender counts and cost")
    sd_list_p.add_argument("--json", action="store_true")
    sd_set_p = sd_sub.add_parser(
        "set",
        help="Set/update a domain's flat cost, reseller, and/or notes -- also how you "
             "register a domain you own before any sender accounts exist on it",
    )
    sd_set_p.add_argument("--domain", required=True, help="e.g. acmemail.com")
    sd_set_p.add_argument("--reseller", help="Vendor who resells/manages mailboxes on this domain")
    sd_set_p.add_argument("--cost", type=float, help="Flat total cost for every mailbox on this domain")
    sd_set_p.add_argument("--currency", help="Default: USD")
    sd_set_p.add_argument("--notes", help='Freeform note, e.g. "blacklisted in Azure" -- overwrites any previous note')
    sd_set_p.add_argument("--ip", help="User-registered static sending IP (enables IP-based DNSBL checks)")
    sd_blcheck_p = sd_sub.add_parser(
        "blacklist-check",
        help="Scan sender domains against DNSBLs; exits 1 if any domain is listed",
    )
    sd_blcheck_p.add_argument("--domain", help="Check one domain (default: all registered)")
    sd_blcheck_p.add_argument("--tier", choices=("tier1", "tier2", "all"), default="all")
    sd_blcheck_p.add_argument("--json", action="store_true")
    sd_blstatus_p = sd_sub.add_parser(
        "blacklist-status",
        help="Show stored DNSBL status without re-scanning",
    )
    sd_blstatus_p.add_argument("--domain", help="One domain (default: all)")
    sd_blstatus_p.add_argument("--stale-hours", type=int, help="Flag statuses older than N hours as stale")
    sd_blstatus_p.add_argument("--json", action="store_true")
    sd_cost_p = sd_sub.add_parser(
        "cost", help="Total sender-account cost and cost-per-positive-reply for a workspace or reseller",
    )
    sd_cost_p.add_argument("--workspace", help="Workspace slug")
    sd_cost_p.add_argument("--reseller", help="Reseller name")
    sd_cost_p.add_argument(
        "--months", type=int,
        help="Window size for the 'windowed' figure, e.g. 3 = cost x 3 months vs. positive "
             "leads from the last 3 months (default: 1, i.e. per-month). 'all_time' is always included too.",
    )
    sd_cost_p.add_argument("--json", action="store_true")

    si_p = sub.add_parser(
        "sender-insights",
        help="Sender accounts with computed reply/bounce rates alongside PlusVibe warmup/health data",
    )
    si_p.add_argument("--workspace", help="Filter to sender accounts linked to this workspace")
    si_p.add_argument("--since", help="Only count events on/after this date (YYYY-MM-DD)")
    si_p.add_argument("--json", action="store_true")

    hist_p.add_argument("--limit", type=int, default=50, help="Max events to show")
    hist_p.add_argument("--json", action="store_true")

    copy_p = sub.add_parser(
        "copy-insights",
        help="Show full copy for positive leads and rank best-performing templates",
    )
    copy_p.add_argument(
        "--lead-status",
        default="interested",
        help="Current lead status to treat as positive (default: interested)",
    )
    copy_p.add_argument("--limit", type=int, default=200, help="Max positive leads to include")
    copy_p.add_argument("--workspace", help="Filter by workspace name or slug")
    copy_p.add_argument("--json", action="store_true")

    segment_p = sub.add_parser(
        "segment-insights",
        help="Rank best converting title/industry/headcount segments from positive leads",
    )
    segment_p.add_argument(
        "--positive-lead-status",
        default="interested",
        help="Current lead status to treat as positive (default: interested)",
    )
    segment_p.add_argument(
        "--positive-sentiment",
        choices=("positive", "negative", "autoreply", "invalid", "neutral"),
        help="Optional sentiment to combine with --positive-lead-status",
    )
    segment_p.add_argument(
        "--fields",
        default="title,industry,headcount",
        help="Comma-separated segment fields (title,industry,headcount)",
    )
    segment_p.add_argument("--min-sent", type=int, default=2, help="Minimum sent leads per value")
    segment_p.add_argument("--top", type=int, default=12, help="Top values per field")
    segment_p.add_argument("--workspace", help="Filter by workspace name or slug")
    segment_p.add_argument("--json", action="store_true")

    ws_p = sub.add_parser("workspace", help="List or create workspaces")
    ws_sub = ws_p.add_subparsers(dest="workspace_cmd")
    ws_list = ws_sub.add_parser("list", help="List workspaces")
    ws_list.add_argument("--json", action="store_true", help="JSON output for agents")
    ws_create = ws_sub.add_parser("create", help="Create a workspace")
    ws_create.add_argument("--name", required=True)
    ws_create.add_argument("--slug")
    ws_create.add_argument("--sync", action="store_true", help="Sync to webapp immediately")
    ws_sub.add_parser("sync", help="Sync all local workspaces to the webapp")
    ws_routing = ws_sub.add_parser("routing", help="Single vs multi-workspace routing mode")
    ws_routing_sub = ws_routing.add_subparsers(dest="workspace_routing_cmd")
    ws_routing_sub.add_parser("show", help="Show current routing mode")
    ws_routing_set = ws_routing_sub.add_parser("set", help="Set routing mode")
    ws_routing_set.add_argument(
        "--mode",
        required=True,
        choices=_pipeline.VALID_WORKSPACE_ROUTING_MODES,
        help="single: all events to one workspace; multi: require campaign maps",
    )
    ws_routing_set.add_argument(
        "--workspace",
        help="Workspace slug (required for single mode)",
    )
    ws_summary = ws_sub.add_parser(
        "summary",
        help="Workspace inventory: lead count, tags, LinkedIn connection counts by sender",
    )
    ws_summary.add_argument("--workspace", required=True, help="Workspace slug or name")
    ws_summary.add_argument("--json", action="store_true", help="JSON output (recommended for agents)")
    ws_summary.add_argument(
        "--tags-only",
        action="store_true",
        help="Skip LinkedIn sender aggregates (faster on large workspaces)",
    )

    cmap_p = sub.add_parser("campaign-map", help="Campaign to workspace routing")
    cmap_sub = cmap_p.add_subparsers(dest="campaign_map_cmd")
    cmap_sub.add_parser("list", help="List campaign maps")
    cmap_add = cmap_sub.add_parser("add", help="Add campaign map")
    cmap_add.add_argument("--platform", default="*")
    cmap_add.add_argument("--workspace", required=True, help="Workspace slug")
    cmap_add.add_argument("--campaign-platform-id", help="Platform campaign ID (e.g. Smartlead numeric ID, Prosp UUID)")
    cmap_add.add_argument("--campaign-name")
    cmap_add.add_argument(
        "--match-strategy",
        choices=("id_exact", "name_exact", "rule_contains", "rule_prefix", "rule_regex"),
    )
    cmap_add.add_argument("--priority", type=int, default=100)
    cmap_conflicts = cmap_sub.add_parser(
        "conflicts",
        help="List active name_exact rows shadowed by a broader rule pointing at a different workspace",
    )
    cmap_conflicts.add_argument("--platform", help="Filter to a source platform (best-effort)")
    cmap_conflicts.add_argument("--json", action="store_true")
    cmap_deact = cmap_sub.add_parser(
        "deactivate", help="Soft-deactivate one campaign map row by id (explicit, auditable)",
    )
    cmap_deact.add_argument("--id", required=True, help="campaign_workspace_map id")
    cmap_recon = cmap_sub.add_parser(
        "reconcile",
        help="Re-apply current routing rules to already-ingested leads/events. "
             "Fix an affected DB in three steps: campaign-map conflicts -> "
             "campaign-map deactivate --id X -> campaign-map reconcile.",
    )
    cmap_recon.add_argument("--dry-run", action="store_true", help="Preview moves without mutating")
    cmap_recon.add_argument("--platform", help="Best-effort filter via leads.latest_source_platform")
    cmap_recon.add_argument("--workspace", help="Only move rows currently in this workspace slug")
    cmap_recon.add_argument("--limit", type=int, default=0, help="Max mismatched campaign groups to act on (0 = all)")
    cmap_recon.add_argument("--json", action="store_true")

    q_p = sub.add_parser("quarantine", help="Unmapped campaign queue")
    q_sub = q_p.add_subparsers(dest="quarantine_cmd")
    q_list = q_sub.add_parser("list", help="List quarantined events")
    q_list.add_argument(
        "--status",
        default="pending",
        choices=("pending", "skipped", "assigned", "replayed", "all"),
        help="Filter by queue status (default: pending)",
    )
    q_list.add_argument("--limit", type=int, default=0, help="Limit rows in JSON mode (0 = all)")
    q_list.add_argument("--json", action="store_true", help="Output raw queue rows as JSON")
    q_skip = q_sub.add_parser("skip", help="Skip quarantined event(s) (syncs to relay on next sync)")
    q_skip.add_argument("--id", help="Single queue item id")
    q_skip.add_argument("--campaign-platform-id", help="Skip all pending rows for this campaign platform id")
    q_skip.add_argument("--platform", help="With --campaign-platform-id, filter by source platform")
    q_skip.add_argument(
        "--all",
        action="store_true",
        help="Skip all pending quarantine rows",
    )
    q_skip.add_argument(
        "--reason",
        metavar="REASON",
        help="Skip all pending rows with this reason (e.g. no_campaign_id); run sync after",
    )
    q_backfill = q_sub.add_parser(
        "backfill-no-campaign",
        help="Move legacy events with no campaign into quarantine (skipped by default)",
    )
    q_backfill.add_argument(
        "--keep-pending",
        action="store_true",
        help="Add to quarantine as pending instead of auto-skipping",
    )
    q_assign = q_sub.add_parser("assign", help="Assign workspace (syncs to relay; ingested on next pull)")
    q_assign.add_argument("--id", required=True, help="Queue item id")
    q_assign.add_argument("--workspace", required=True, help="Workspace slug")
    q_replay = q_sub.add_parser("replay", help="Replay pending items locally after campaign-map rules")
    q_replay.add_argument("--workspace")
    q_replay.add_argument("--limit", type=int, default=100)

    reprocess_p = sub.add_parser("reprocess", help="Re-apply current extractors to ingested data")
    reprocess_p.add_argument(
        "--kind", nargs="*",
        choices=("events", "core", "workspace", "company", "all"),
        default=["events"],
        help="Data kinds to reprocess (default: events; use 'all' for everything)",
    )
    reprocess_p.add_argument("--from", dest="from_id", type=int, default=None,
                             help="Start relay/snapshot ID (default: 0)")
    reprocess_p.add_argument("--to", dest="to_id", type=int, default=None,
                             help="End relay/snapshot ID (default: cursor value for kind)")
    reprocess_p.add_argument("--platform", default=None,
                             help="Platform filter (single value, e.g. prosp)")
    reprocess_p.add_argument("--dry-run", action="store_true",
                             help="Print plan and affected row count, don't execute")
    reprocess_p.add_argument("--verbose", action="store_true",
                             help="Print per-row diff of changed fields")
    reprocess_p.add_argument("--force", action="store_true",
                             help="Skip confirmation prompt")
    reprocess_p.add_argument("--reingest", action="store_true",
                             help="Delete and re-ingest events from relay replay (refreshes all data in ID range)")

    pset = sub.add_parser("personalize-set", help="Write lead personalization (first_name, etc.)")
    pset.add_argument("--lead-id", type=int, help="Lead ID (single mode)")
    pset.add_argument("--field", help="Field name (single mode)")
    pset.add_argument("--value", help="Field value (single mode)")
    pset.add_argument("--date", help="Optional ISO date for date-aware fields")
    pset.add_argument("--batch", action="store_true", help="Read JSON array from --json")
    pset.add_argument("--json", dest="json_input", help="JSON array: [{lead_id, field, value, date?}, ...]")

    pget = sub.add_parser("personalize-get", help="Read merged personalization for a lead")
    pget.add_argument("--lead-id", type=int, required=True)
    pget.add_argument("--layer", choices=("merged", "lead", "company"), default="merged")
    pget.add_argument("--json", action="store_true")

    ppend = sub.add_parser("personalize-pending", help="List leads missing lead-scoped fields")
    ppend.add_argument("--fields", default="first_name", help="Comma-separated field names")
    ppend.add_argument("--limit", type=int, default=50)
    ppend.add_argument("--json", action="store_true")

    pstat = sub.add_parser("personalize-status", help="Lead personalization summary")
    pstat.add_argument("--json", action="store_true")

    cpset = sub.add_parser("company-personalize-set", help="Write company personalization (company_name, company_*)")
    cpset.add_argument("--company-id", type=int)
    cpset.add_argument("--domain")
    cpset.add_argument("--name", help="Company name lookup")
    cpset.add_argument("--field")
    cpset.add_argument("--value")
    cpset.add_argument("--date", help="Optional ISO date")
    cpset.add_argument("--batch", action="store_true")
    cpset.add_argument("--json", dest="json_input", help="JSON: [{company_id|domain|name, field, value, date?}]")

    cpget = sub.add_parser("company-personalize-get", help="Read company personalization")
    cpget.add_argument("--company-id", type=int)
    cpget.add_argument("--domain")
    cpget.add_argument("--name")
    cpget.add_argument("--json", action="store_true")

    cppend = sub.add_parser("company-personalize-pending", help="List companies missing company fields")
    cppend.add_argument("--fields", default="company_name", help="Comma-separated field names")
    cppend.add_argument("--limit", type=int, default=50)
    cppend.add_argument("--json", action="store_true")

    cpstat = sub.add_parser("company-personalize-status", help="Company personalization summary")
    cpstat.add_argument("--json", action="store_true")

    pclear = sub.add_parser("personalize-clear", help="Clear personalization data")
    pclear.add_argument("--lead-id", type=int, help="Clear one lead")
    pclear.add_argument("--field", help="Clear specific field across all leads")
    pclear.add_argument("--all", dest="clear_all", action="store_true", help="Clear everything")

    cleanup_rules_p = sub.add_parser("cleanup-rules", help="Remove invalid campaign mapping rules")
    cleanup_rules_p.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    cleanup_rules_p.add_argument("--json", action="store_true")

    outbox_p = sub.add_parser("outbox", help="Pending local changes awaiting push")
    outbox_p.add_argument(
        "--backfill",
        action="store_true",
        help="One-time cutover: mark every synced entity dirty. Pull first, so the "
             "content-hash check can drop what the relay already holds.",
    )
    outbox_p.add_argument("--dry-run", action="store_true", help="Show counts, write nothing")
    outbox_p.add_argument("--json", action="store_true")

    junk_p = sub.add_parser(
        "cleanup-junk-leads",
        help="Stage 9: quarantine + delete the weak-identity junk leads. Destructive.",
    )
    # Default is dry-run: --yes flips it. Reporting counts is always safe;
    # deleting is not.
    junk_p.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this the command reports counts only.",
    )
    junk_p.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "export" and getattr(args, "format", None) == "sheets":
        _remap_to_lead_review_export(args)
    elif args.command == "sheets" and getattr(args, "sheets_command", None) == "export":
        _remap_to_lead_review_export(args)

    # Load synced API keys (primary + backup __N slots) before any command that may call vendors.
    if args.command not in (None, "update", "version"):
        try:
            agent_secrets_cloud.load_local_agent_secrets_to_environ()
        except OSError:
            pass
        # Hermes stores OUTREACHMAGIC_AGENT_KEY in {data_root}/.env after login.
        try:
            from om_paths import get_data_root
            from shared import load_dotenv_file

            load_dotenv_file(get_data_root() / ".env")
        except OSError:
            pass

    # Check-only update notice (never downloads). At most once per hour.
    if args.command not in (None, "update", "version"):
        _pipeline.notify_update_available(quiet=getattr(args, "cron", False))

    if args.command == "version":
        print(f"outreachmagic {_pipeline.__version__}")
        _pipeline._warn_duplicate_installs()
        return

    if args.command == "paths":
        payload: dict = {
            "install_dir": str(_pipeline.get_install_dir()),
            "data_root": str(_pipeline.get_data_root()),
            "skill_home": str(_pipeline.get_skill_home()),
            "database": str(_pipeline.get_db_path()),
            "config": str(_pipeline.get_config_path()),
            "agent_secrets": str(_pipeline.get_agent_secrets_path()),
            "cwd": str(Path.cwd()),
            **_pipeline.working_paths_payload(),
        }
        warn = _pipeline.hermes_profile_copy_warning()
        if warn:
            payload["warning"] = warn
        print(json.dumps(payload, indent=2))
        if warn:
            print(f"\n⚠ {warn}", file=sys.stderr)
        _pipeline._warn_duplicate_installs()
        return

    if args.command == "update":
        _pipeline._warn_duplicate_installs()
        if args.check:
            if not _pipeline.check_skill_update(quiet=False):
                sys.exit(1)
            print(f"Up to date ({_pipeline.__version__})")
            return
        try:
            result = _pipeline.update_skill(
                explicit_tag=args.tag,
                channel=getattr(args, "channel", "release"),
            )
            print(f"Updated to v{result['version']} from {result['source']} in {result['path']}")
            print("Files:", ", ".join(result["files"]))
        except urllib.error.HTTPError as e:
            msg = str(e)
            if e.code == 404:
                tag_hint = ""
                if not args.tag:
                    tag_hint = (
                        "\n\nTry a specific tag: pipeline.py update --tag v<VERSION>\n"
                        "  e.g. pipeline.py update --tag latest-tag"
                    )
                print(
                    f"Update failed: {msg}\n\n"
                    f"The update URL returned 404. This may mean:\n"
                    f"  1. The release tag does not exist or was removed\n"
                    f"  2. GitHub raw content URLs are temporarily unavailable\n"
                    f"  3. Your install config points to a repo that has no matching release{tag_hint}\n"
                    f"  Or try: pipeline.py update --channel main (install from the main branch)",
                    flush=True,
                )
            else:
                print(f"Update failed: {msg}", flush=True)
            sys.exit(1)
        except Exception as e:
            msg = str(e)
            print(f"Update failed: {msg}", flush=True)
            sys.exit(1)
        return

    if args.command == "rollback":
        result = _pipeline.rollback_skill()
        if result.get("status") != "rolled_back":
            print(json.dumps(result, indent=2))
            sys.exit(1)
        print(json.dumps(result, indent=2))
        return

    if args.command == "init":
        cfg = _pipeline.load_config()
        agent_choice = getattr(args, "agent", None)

        # If data_root is not yet configured and duplicates exist, ask interactively.
        if not agent_choice and not cfg.get("data_root"):
            duplicates = _pipeline.check_duplicate_installs()
            if duplicates:
                print(
                    "Outreach Magic is installed in multiple agent directories.",
                    file=sys.stderr,
                )
                print(
                    "Which agent are you using? Pick the one you run with this skill.",
                    file=sys.stderr,
                )
                print(file=sys.stderr)
                for i, name in enumerate(_pipeline.AGENT_DIR_NAMES, 1):
                    path = Path(_pipeline.AGENT_DIR_MAP[name]).expanduser()
                    installed = " ✓" if (path / "skills" / _pipeline.SKILL_NAME).exists() else ""
                    print(f"  {i}) {name}{installed}", file=sys.stderr)
                print(file=sys.stderr)
                prompt = f"Enter a number or name (1-{len(_pipeline.AGENT_DIR_NAMES)}): "
                while True:
                    try:
                        raw = input(prompt).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print(file=sys.stderr)
                        sys.exit(1)
                    if raw in _pipeline.AGENT_DIR_MAP:
                        agent_choice = raw
                        break
                    if raw.isdigit() and 1 <= int(raw) <= len(_pipeline.AGENT_DIR_NAMES):
                        agent_choice = _pipeline.AGENT_DIR_NAMES[int(raw) - 1]
                        break
                    print(
                        f"Please enter 1-{len(_pipeline.AGENT_DIR_NAMES)} or a name "
                        f"({', '.join(_pipeline.AGENT_DIR_NAMES)})",
                    )

        if agent_choice:
            data_root = Path(_pipeline.AGENT_DIR_MAP[agent_choice]).expanduser()
            cfg["data_root"] = str(data_root)
            _pipeline.save_config(cfg)
            # Redirect all path resolution in this process so init_db() and
            # get_db_path() use the newly-configured canonical root — not the
            # script-location-inferred root that get_data_root() still caches.
            _pipeline.set_data_root_override(data_root)
            print(f"✓ data_root set to {data_root}")
            print(f"  All skill copies will now use the same database and config.")
        else:
            # data_root already configured or no duplicates — use whatever is resolved
            print(f"Using existing data_root: {_pipeline.get_data_root()}")

        # Detect duplicates that have their own DB and suggest symlinking.
        duplicates = _pipeline.check_duplicate_installs()
        if duplicates:
            active = _pipeline.get_install_dir()
            for dup in duplicates:
                other_db = Path(dup["path"]) / "databases" / "outreachmagic.db"
                if other_db.exists() and not dup["is_symlink"]:
                    print(
                        f"Note: {other_db} exists in a separate copy at {dup['path']}.",
                        file=sys.stderr,
                    )
                    print(
                        f"  To share one database, symlink instead:\n"
                        f"  rm -rf '{dup['path']}' && ln -s '{active}' '{dup['path']}'",
                        file=sys.stderr,
                    )
        _pipeline.init_db()
        _pipeline.set_last_sync(datetime.now(timezone.utc).isoformat())
        from_tag = getattr(args, "from_tag", None)
        if from_tag:
            _pipeline.record_install_source(from_tag)
        print(f"Outreach Magic v{_pipeline.__version__} installed.")
        print(f"Database initialized: {_pipeline.get_db_path()}")
        paths = _pipeline.working_paths_payload()
        print(f"Working files: {paths['working_root']}")
        for key in ("imports", "exports", "batches", "sheets", "archive", "logs"):
            print(f"  {key:16} → {paths[key]}")
        print()
        print("Next: ask Outreach Magic to connect (login step).")
        return

    # Commands that only talk to the app API (no local DB required)
    if args.command == "status":
        _pipeline.cmd_status(json_output=getattr(args, "json", False))
        return

    if args.command == "whoami":
        _pipeline.cmd_whoami(json_output=getattr(args, "json", False))
        return

    if args.command == "refresh":
        _pipeline.cmd_refresh(args)
        return

    if args.command == "restore":
        _pipeline.cmd_restore(args)
        return

    if args.command == "login":
        _pipeline._warn_duplicate_installs()
        _pipeline.login(
            platform=getattr(args, "platform", None),
            generate_url=getattr(args, "generate_url", False),
            claim_token=getattr(args, "claim_token", False),
            device_code=getattr(args, "device_code", None),
            wait_seconds=getattr(args, "wait", 30),
            force=getattr(args, "force", False),
        )
        return
    if args.command == "logout":
        _pipeline.logout()
        return

    if args.command == "sync-secrets":
        result = _pipeline.sync_agent_secrets_cli(
            check_only=getattr(args, "check", False),
            as_json=getattr(args, "json", False),
            quiet=getattr(args, "cron", False),
        )
        if not result.get("ok", True) and getattr(args, "check", False) is False:
            sys.exit(1)
        return

    if args.command == "api-keys":
        _pipeline.api_keys_cli(as_json=getattr(args, "json", False), push=getattr(args, "push", False))
        return

    if args.command not in _pipeline._DB_OPTIONAL_COMMANDS and not _pipeline.database_has_schema():
        print(_pipeline.format_database_recovery_message(), file=sys.stderr)
        sys.exit(1)

    if args.command == "sync":
        if getattr(args, "inspect", None):
            inspect_type = getattr(args, "type", None) or "lead"
            value = args.inspect.strip()
            conn = _pipeline.get_conn()
            try:
                if inspect_type == "lead":
                    if not getattr(args, "workspace", None):
                        print(json.dumps({"error": "--workspace is required with sync --inspect --type lead"}))
                        sys.exit(1)
                    # A weak-identity lead (LinkedIn/name+company only, no email
                    # ever found) can't be looked up by email at all -- a bare
                    # digit string is unambiguous, since no real email is ever
                    # purely numeric, so it's treated as a lead id.
                    if value.isdigit():
                        lead_row = conn.execute(
                            "SELECT id FROM leads WHERE id = ?", (int(value),),
                        ).fetchone()
                        lead_id = lead_row["id"] if lead_row else None
                    else:
                        lead = _pipeline.find_lead(email=value.lower())
                        lead_id = lead["id"] if lead else None
                    if not lead_id:
                        print(json.dumps({"error": f"lead not found: {value}"}))
                        sys.exit(1)
                    result = _pipeline.inspect_sync_lead(
                        conn, _pipeline.DEFAULT_ORG_ID, lead_id, workspace_slug=args.workspace,
                    )
                elif inspect_type == "company":
                    row = conn.execute(
                        "SELECT id FROM companies WHERE domain = ? OR lower(name) = lower(?)",
                        (value.lower(), value),
                    ).fetchone()
                    if not row:
                        print(json.dumps({"error": f"company not found: {value}"}))
                        sys.exit(1)
                    result = _pipeline.inspect_sync_company(conn, row["id"])
                elif inspect_type == "sender_account":
                    sender_account_id = _pipeline.find_sender_account_id_by_email(conn, value.lower())
                    if not sender_account_id:
                        print(json.dumps({"error": f"sender account not found: {value}"}))
                        sys.exit(1)
                    result = _pipeline.inspect_sync_sender_account(conn, sender_account_id)
                elif inspect_type == "sender_domain":
                    result = _pipeline.inspect_sync_sender_domain(conn, value.lower())
                    if not result:
                        print(json.dumps({"error": f"sender domain not found: {value}"}))
                        sys.exit(1)
                elif inspect_type == "event":
                    try:
                        event_id = int(value)
                    except ValueError:
                        print(json.dumps({"error": "--inspect must be an integer event id with --type event"}))
                        sys.exit(1)
                    result = _pipeline.inspect_sync_event(conn, event_id)
                    if not result:
                        print(json.dumps({"error": f"event not found: {event_id}"}))
                        sys.exit(1)
                elif inspect_type == "merge_delete":
                    result = _pipeline.inspect_sync_merge_delete(conn, value)
                    if not result:
                        print(json.dumps({"error": f"merge tombstone not found: {value}"}))
                        sys.exit(1)
                elif inspect_type == "company_merge_delete":
                    result = _pipeline.inspect_sync_company_merge_delete(conn, value)
                    if not result:
                        print(json.dumps({"error": f"company merge tombstone not found: {value}"}))
                        sys.exit(1)
                elif inspect_type == "quarantine_resolution":
                    result = _pipeline.inspect_sync_quarantine_resolution(conn, value)
                    if not result:
                        print(json.dumps({"error": f"quarantine queue item not found: {value}"}))
                        sys.exit(1)
                else:
                    print(json.dumps({"error": f"unknown --type: {inspect_type}"}))
                    sys.exit(1)
            finally:
                conn.close()
            if getattr(args, "file", None):
                Path(args.file).write_text(json.dumps(result, indent=2))
                print(json.dumps({"status": "written", "file": args.file}))
            else:
                print(json.dumps(result, indent=2))
            return
        if getattr(args, "status", False):
            status = _pipeline.get_sync_status()
            if getattr(args, "json", False):
                print(json.dumps(status, indent=2))
            else:
                mode = status.get("recommended_mode", "push")
                pending = status.get("leads_pending", 0) + status.get("workspace_leads_pending", 0)
                if pending:
                    print(
                        f"Sync status: {mode} mode recommended — "
                        f"{pending} snapshot(s) pending cloud push",
                        flush=True,
                    )
                print(json.dumps(status, indent=2))
        else:
            sync_ws = getattr(args, "workspace", None)
            if getattr(args, "dry_run", False):
                result = _pipeline.preview_sync(
                    workspace=sync_ws,
                    sample_size=getattr(args, "sample_size", 3),
                )
                if getattr(args, "file", None):
                    Path(args.file).write_text(json.dumps(result, indent=2))
                    print(json.dumps({"status": "written", "file": args.file}))
                else:
                    print(json.dumps(result, indent=2))
                return
            if getattr(args, "full_snapshot", False):
                ws_id = None
                if sync_ws:
                    conn = _pipeline.get_conn()
                    ws_row = _pipeline.resolve_workspace_identity(conn, sync_ws)
                    conn.close()
                    if not ws_row:
                        print(json.dumps({"error": f"workspace not found: {sync_ws}"}))
                        sys.exit(1)
                    ws_id = ws_row["id"]
                elif not getattr(args, "yes", False):
                    conn = _pipeline.get_conn()
                    would_mark = {
                        "leads": conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"],
                        "workspace_memberships": conn.execute(
                            "SELECT COUNT(*) AS n FROM workspace_leads"
                        ).fetchone()["n"],
                        "companies": conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"],
                        "sender_accounts": conn.execute(
                            "SELECT COUNT(*) AS n FROM sender_accounts"
                        ).fetchone()["n"],
                        "sender_domains": conn.execute(
                            "SELECT COUNT(*) AS n FROM sender_domains"
                        ).fetchone()["n"],
                    }
                    conn.close()
                    print(json.dumps({
                        "error": (
                            "--full-snapshot without --workspace marks EVERY lead, workspace "
                            "membership, company, sender account, and sender domain in the "
                            "account pending for a full resync to relay. This is expensive -- "
                            "the resulting push can take a very long time to drain depending on "
                            "database size, so re-run only when you're ready to let a full sync "
                            "run to completion. Re-run with --workspace SLUG to scope it to one "
                            "workspace's leads, or add --yes to confirm a full-account resync."
                        ),
                        "would_mark_pending": would_mark,
                    }, indent=2))
                    sys.exit(1)
                if sync_ws:
                    _pipeline.mark_all_lead_snapshots_pending(workspace_id=ws_id)
                    print(
                        f"Marked leads in workspace {sync_ws} pending for full snapshot push.",
                        file=sys.stderr, flush=True,
                    )
                else:
                    _pipeline.mark_all_entities_pending()
                    print(
                        "Marked all leads, workspace memberships, companies, sender accounts, "
                        "and sender domains pending for full snapshot push.",
                        file=sys.stderr, flush=True,
                    )
            force_bulk = None
            if getattr(args, "bulk", False) and getattr(args, "no_bulk", False):
                print(json.dumps({"error": "Use --bulk or --no-bulk, not both"}))
                sys.exit(1)
            if getattr(args, "bulk", False):
                force_bulk = True
            elif getattr(args, "no_bulk", False):
                force_bulk = False
            result = _pipeline.sync_all(
                no_health_report=getattr(args, "no_health_report", False),
                force_bulk=force_bulk,
                workspace=sync_ws,
            )
            print(json.dumps(result, indent=2))
        return

    if args.command == "activity":
        if args.activity_command == "show":
            lead = None
            if getattr(args, "lead_id", None):
                conn = _pipeline.get_conn()
                row = conn.execute("SELECT * FROM leads WHERE id = ?", (args.lead_id,)).fetchone()
                conn.close()
                lead = dict(row) if row else None
            elif getattr(args, "email", None):
                lead = _pipeline.find_lead(email=args.email.strip().lower())
            if not lead:
                print(json.dumps({"error": "lead not found (--lead-id or --email required)"}))
                sys.exit(1)
            conn = _pipeline.get_conn()
            try:
                result = _pipeline.inspect_sync_lead(
                    conn,
                    _pipeline.DEFAULT_ORG_ID,
                    lead["id"],
                    workspace_slug=getattr(args, "workspace", None),
                )
            finally:
                conn.close()
            print(json.dumps(result, indent=2))
            return
        if args.activity_command == "recompute":
            conn = _pipeline.get_conn()
            try:
                ws_slug = getattr(args, "workspace", None)
                ws_id = None
                if ws_slug:
                    ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)
                    if not ws_row:
                        print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                        sys.exit(1)
                    ws_id = ws_row["id"]
                    merged = _pipeline.refresh_lead_activity_from_events(conn, args.lead_id, ws_id)
                    conn.commit()
                    results = {ws_slug: merged}
                else:
                    rows = conn.execute(
                        "SELECT workspace_id FROM workspace_leads WHERE lead_id = ?",
                        (args.lead_id,),
                    ).fetchall()
                    results = {}
                    for row in rows:
                        merged = _pipeline.refresh_lead_activity_from_events(
                            conn, args.lead_id, row["workspace_id"],
                        )
                        results[row["workspace_id"]] = merged
                    conn.commit()
            finally:
                conn.close()
            print(json.dumps({"status": "ok", "lead_id": args.lead_id, "activity": results}, indent=2))
            return

    if args.command == "db-health":
        conn = _pipeline.get_conn()
        try:
            health = db_health.collect_db_health(
                conn,
                org_id=_pipeline.DEFAULT_ORG_ID,
                fast=not getattr(args, "full", False),
                verbose=getattr(args, "verbose", False),
                pipeline_version=_pipeline.__version__,
            )
        finally:
            conn.close()
        if getattr(args, "push", False):
            conn_push = _pipeline.get_conn()
            try:
                health["cloud"] = db_health.maybe_report_db_health_to_cloud(
                    conn_push,
                    org_id=_pipeline.DEFAULT_ORG_ID,
                    pipeline_version=_pipeline.__version__,
                    get_agent_key_fn=_pipeline.get_agent_key,
                    load_config_fn=_pipeline.load_config,
                    save_config_fn=_pipeline.save_config,
                    get_client_id_fn=_pipeline.get_or_create_client_id,
                    cloud_routing_enabled_fn=routing_cloud.cloud_routing_enabled,
                    get_api_base_fn=routing_cloud.get_api_base,
                    push_db_health_fn=routing_cloud.push_db_health,
                    fast=not getattr(args, "full", False),
                    force=True,
                    skip=False,
                )
            finally:
                conn_push.close()
        out = json.dumps(health, indent=2) if getattr(args, "json", False) or getattr(args, "push", False) else json.dumps(health)
        print(out)
        return

    if args.command == "archive":
        ws = args.workspace
        if args.dry_run:
            conn = _pipeline.get_conn()
            try:
                _ids, meta = workspace_archive.resolve_archive_lead_ids(
                    conn,
                    _pipeline.DEFAULT_ORG_ID,
                    ws,
                    resolve_workspace_identity_fn=_pipeline.resolve_workspace_identity,
                )
                ev_count = 0
                if _ids:
                    ph = ",".join("?" for _ in _ids)
                    ev_count = conn.execute(
                        f"SELECT COUNT(*) FROM events WHERE lead_id IN ({ph})",
                        tuple(_ids),
                    ).fetchone()[0]
                print(
                    json.dumps(
                        {
                            "workspace": ws,
                            "dry_run": True,
                            "lead_count": len(_ids),
                            "event_count": ev_count,
                            **meta,
                        },
                        indent=2,
                    )
                )
            finally:
                conn.close()
            return
        if not args.output:
            print(json.dumps({"error": "--output required (or use --dry-run)"}))
            sys.exit(1)
        out_path = Path(args.output).expanduser()

        def _init_archive_schema(c):
            c.executescript(_pipeline.SCHEMA_SQL)
            _pipeline.migrate_db(c)

        conn = _pipeline.get_conn()
        try:
            manifest = workspace_archive.export_workspace_archive(
                conn,
                _pipeline.DEFAULT_ORG_ID,
                ws,
                out_path,
                resolve_workspace_identity_fn=_pipeline.resolve_workspace_identity,
                init_schema_fn=_init_archive_schema,
            )
            if args.purge:
                purge_result = workspace_archive.purge_workspace_archive(
                    conn,
                    _pipeline.DEFAULT_ORG_ID,
                    ws,
                    resolve_workspace_identity_fn=_pipeline.resolve_workspace_identity,
                    vacuum=getattr(args, "vacuum", False),
                )
                manifest["purge"] = purge_result
        finally:
            conn.close()
        print(json.dumps(manifest, indent=2))
        return

    if args.command == "connections":
        _pipeline.cmd_connections(json_output=getattr(args, "json", False))
        return

    if args.command == "connect-platform":
        _pipeline.cmd_connect_platform(args.platform)
        return

    if args.command == "disconnect-platform":
        _pipeline.cmd_disconnect_platform(args.platform, skip_confirm=getattr(args, "yes", False))
        return

    if not _pipeline.db_exists():
        print("Database not initialized. Ask Outreach Magic to initialize the database.")
        sys.exit(1)

    _pipeline.migrate_db()
    _pipeline.sync_workspace_routing_mode_from_config()

    if args.command == "crm-sync":
        _cmd_crm_sync(args)
        return

    if args.command == "pull":
        _pipeline._warn_duplicate_installs()
        agent_key = _pipeline._require_agent_key()
        pull_stats = {}

        if (
            args.full
            and not args.cron
            and not getattr(args, "yes", False)
        ):
            hint = "all webhook events"
            try:
                probe = _pipeline.probe_relay_backlog(agent_key)
                pending = (probe.get("events") or {}).get("pending")
                if pending is not None:
                    hint = f"~{int(pending):,} webhook events"
            except (RuntimeError, ValueError, TypeError):
                pass
            print(
                f"This will replay {hint} from the relay (may take several minutes). "
                "Continue? [Y/n] ",
                end="",
                flush=True,
            )
            answer = input().strip().lower()
            if answer not in ("", "y", "yes"):
                print("Aborted.")
                sys.exit(0)

        if_stale = getattr(args, "if_stale", None)
        if getattr(args, "force", False):
            if_stale = None
        if if_stale:
            try:
                skip_payload = _pipeline.pull_if_stale_skip_result(if_stale, force=False)
            except ValueError as e:
                print(f"Pull failed: {e}")
                sys.exit(1)
            if skip_payload:
                if not args.cron:
                    print(json.dumps(skip_payload, indent=2))
                sys.exit(0)

        if getattr(args, "probe", False):
            try:
                _pipeline.print_relay_probe(_pipeline.probe_relay_backlog(agent_key))
            except (RuntimeError, ValueError) as e:
                print(f"Probe failed: {e}")
            return

        try:
            pull_kinds = _pipeline.parse_pull_kinds(getattr(args, "kind", None))
        except ValueError as e:
            print(f"Pull failed: {e}")
            sys.exit(0)

        if getattr(args, "reset_snapshot_cursors", False):
            _pipeline.clear_snapshot_cursors()
            if not args.cron:
                print(
                    "Reset snapshot cursors to 0 (core, workspace). "
                    "Use after a hung pull left config ahead of the local DB.",
                    flush=True,
                )

        # A first-ever pull (no cursor yet) backfills everything regardless of
        # the --full flag, but only `full=True` triggers the end-of-pull
        # last_sync bump (pipeline_sync.py) that keeps freshly-pulled data
        # from looking like locally-pending changes on the next sync
        # --dry-run. Auto-detect that case, matching the pattern already used
        # at pipeline_sync.py's own sync_from_relay_org("full=not get_last_max_id()") call site.
        effective_full = args.full or not _pipeline.get_last_max_id()
        try:
            imported, skipped = _pipeline.sync_from_relay_org(
                agent_key,
                after_id=None if effective_full else _pipeline.get_last_max_id(),
                full=effective_full,
                debug_sentiment=args.debug_sentiment,
                quiet=args.cron,
                stats=pull_stats,
                skip_routing_sync=getattr(args, "skip_routing_sync", False),
                pull_kinds=pull_kinds,
                skip_snapshots=getattr(args, "skip_snapshots", False),
            )
        except RuntimeError as e:
            if not args.cron:
                print(f"Pull failed: {_pipeline._pull_failure_message(e)}")
            sys.exit(0)

        if args.diagnose and not args.cron:
            _pipeline.print_pull_diagnostics(pull_stats)
            print()

        if imported == 0 and skipped == 0:
            if not args.cron:
                print("No events on relay.")
            sys.exit(0)

        if not args.cron:
            print(_pipeline.format_pull_summary(imported, skipped, pull_stats))
            mode = pull_stats.get("mode", "incremental")
            newest = pull_stats.get("newest_relay_id_seen")
            print(f"[mode={mode}, newest_relay_id={newest or '-'}]")
            if args.full:
                print("Full replay complete.")
            if pull_stats.get("cursor_stalled"):
                print(
                    "Warning: pull cursor stalled on a full relay page; "
                    "investigate relay max_id pagination."
                )
            print("Ask Outreach Magic to show the pipeline.")
        return

    if args.command == "reprocess":
        _pipeline._warn_duplicate_installs()
        agent_key = _pipeline._require_agent_key()
        kinds = args.kind if args.kind != ["all"] else ["events", "core", "workspace", "company"]

        if not args.dry_run and not args.force:
            print(
                f"This will reprocess {len(kinds)} kind(s): {', '.join(kinds)}. "
                "Existing metadata will be re-extracted and updated in place. "
                "Continue? [Y/n] ",
                end="",
                flush=True,
            )
            answer = input().strip().lower()
            if answer not in ("", "y", "yes"):
                print("Aborted.")
                sys.exit(0)

        import reprocess

        for kind in kinds:
            if kind == "events":
                result = reprocess.reprocess_events(
                    agent_key,
                    from_id=args.from_id or 0,
                    to_id=args.to_id,
                    platform=args.platform,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                    reingest=args.reingest,
                )
            else:
                print(
                    f"[reprocess] {kind} snapshots not yet implemented",
                    file=sys.stderr,
                )
                continue

            # ── Summary card ────────────────────────────────────────────────
            fetched = result.get("fetched", 0)
            updated = result.get("updated", 0)
            pages = result.get("pages", 0)
            elapsed = result.get("elapsed_s", 0)
            rate = result.get("rate", 0)
            errors = result.get("errors", 0)
            total_count = result.get("total_count")
            kind_label = kind.upper()

            elapsed_fmt = f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s" if elapsed >= 60 else f"{elapsed:.0f}s"
            dry_tag = " [DRY RUN]" if args.dry_run else ""
            check = "✅" if errors == 0 else "⚠️"
            count_line = f"   Events:     {fetched} fetched"
            if total_count:
                count_line += f" / {total_count} total"
            count_line += f" · {updated} updated"

            print(
                file=sys.stderr,
            )
            print(
                f"  ────────────────────────────────────────{dry_tag}",
                file=sys.stderr,
            )
            print(
                f"  {check} {kind_label} reprocess complete",
                file=sys.stderr,
            )
            print(
                f"   Platform:   {args.platform or 'all'}",
                file=sys.stderr,
            )
            print(
                count_line,
                file=sys.stderr,
            )
            print(
                f"   Pages:      {pages}",
                file=sys.stderr,
            )
            print(
                f"   Duration:   {elapsed_fmt}",
                file=sys.stderr,
            )
            print(
                f"   Rate:       {int(rate)} ev/s",
                file=sys.stderr,
            )
            if errors:
                print(
                    f"   Errors:     {errors} ⚠️",
                    file=sys.stderr,
                )
            else:
                print(
                    f"   Errors:     0",
                    file=sys.stderr,
                )
            print(
                f"  ────────────────────────────────────────",
                file=sys.stderr,
            )

        # Clean up quarantine rows for reprocessed events (only if not dry-run).
        if not args.dry_run:
            _pipeline.cleanup_stale_quarantine_for_reprocessed()
            print("[reprocess] quarantine cleanup complete.", file=sys.stderr)

        return

    pull_before_commands = ("show", "lead-table", "stats", "campaigns", "summary")
    if args.command in pull_before_commands and getattr(args, "pull", False):
        agent_key = _pipeline.get_agent_key()
        if agent_key:
            try:
                skip_payload = _pipeline.pull_if_stale_skip_result(
                    "5m",
                    force=getattr(args, "force_pull", False),
                )
                if skip_payload:
                    print(skip_payload.get("freshness_message", "Pull skipped (fresh)."))
                else:
                    imported, _ = _pipeline.sync_from_relay_org(
                        agent_key,
                        after_id=_pipeline.get_last_max_id(),
                        quiet=True,
                        skip_snapshots=True,
                    )
                    if imported:
                        print(f"Pulled from relay: {imported} new events imported.")
                    else:
                        print("Pulled from relay: 0 new events imported.")
            except (RuntimeError, ValueError):
                pass
        print()

    if args.command == "show":
        query_cli.cmd_pipeline_view(args, table_formatter=_pipeline.format_pipeline_table)
    elif args.command == "lead-table":
        query_cli.cmd_pipeline_view(
            args,
            table_formatter=lambda leads: _pipeline.format_lead_table(
                leads, markdown=getattr(args, "markdown", False)
            ),
        )
    elif args.command == "query":
        query_cli.cmd_query(args)
    elif args.command == "schema":
        query_cli.cmd_schema(args)
    elif args.command == "tag-summary":
        query_cli.cmd_tag_summary(args)
    elif args.command == "sheets" and getattr(args, "sheets_command", None) == "campaign-stats":
        _cmd_sheets_campaign_stats(args)
    elif args.command == "email-finding-candidates":
        conn = _pipeline.get_conn()
        try:
            lead_ids = None
            if getattr(args, "lead_ids", None):
                lead_ids = [
                    int(x.strip()) for x in str(args.lead_ids).split(",") if x.strip()
                ]
            elif getattr(args, "file", None):
                in_path = _pipeline.resolve_project_path(args.file, kind="input")
                batch_rows = json.loads(in_path.read_text(encoding="utf-8"))
                if not isinstance(batch_rows, list):
                    raise ValueError("--file must be a JSON array of lead rows")
                lead_ids = []
                for row in batch_rows:
                    if not isinstance(row, dict):
                        continue
                    lid = row.get("lead_id") or row.get("id")
                    if lid is not None:
                        lead_ids.append(int(lid))
            linkedin_only = getattr(args, "linkedin_only", False)
            scope_leads = pipeline_lead_review.load_workspace_leads_for_review(
                conn,
                args.workspace,
                tag=getattr(args, "tag", None),
                stage=getattr(args, "stage", None),
                since=getattr(args, "since", None),
                limit=args.limit,
                never_contacted=getattr(args, "never_contacted", False),
                no_email=False,
                require_domain=False if linkedin_only else getattr(args, "require_domain", False),
                lead_ids=lead_ids,
                enrich_fn=_pipeline.enrich_lead_rows,
            )
            skipped_has_email = sum(
                1 for lead in scope_leads if (lead.get("email") or "").strip()
            )
            pool = scope_leads
            if getattr(args, "no_email", True):
                pool = [
                    lead for lead in scope_leads if not (lead.get("email") or "").strip()
                ]
            if linkedin_only:
                candidates = pipeline_lead_review.email_finder_candidates_linkedin_only(pool)
                skipped_key = "skipped_no_linkedin_or_has_domain"
            else:
                candidates = pipeline_lead_review.email_finder_candidates_from_leads(pool)
                skipped_key = "skipped_no_domain"
            print(json.dumps({
                "status": "ok",
                "workspace": args.workspace,
                "scanned": len(scope_leads),
                "skipped_has_email": skipped_has_email,
                skipped_key: len(pool) - len(candidates),
                "count": len(candidates),
                "candidates": candidates,
            }, indent=2))
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        finally:
            conn.close()
    elif args.command == "export":
        try:
            result = _pipeline.export_leads(
                workspace=args.workspace,
                tag=getattr(args, "tag", None),
                stage=getattr(args, "stage", None),
                since=getattr(args, "since", None),
                limit=args.limit,
                fmt=args.format,
                file_path=getattr(args, "file", None),
                never_contacted=getattr(args, "never_contacted", False),
                no_email=getattr(args, "no_email", False),
                require_domain=getattr(args, "require_domain", False),
            )
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        if args.format == "json" and not getattr(args, "file", None):
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result))
    elif args.command == "summary":
        import read_queries as rq

        digest = rq.daily_digest(
            since=getattr(args, "since", None) or "today",
            workspace=getattr(args, "workspace", None),
            campaign_prefix=getattr(args, "campaign_prefix", None),
        )
        digest = _pipeline.attach_freshness(digest, last_pull=_pipeline.get_last_pull())
        if getattr(args, "json", False):
            print(json.dumps(digest, indent=2))
        else:
            _pipeline.print_freshness_stderr(_pipeline.get_last_pull())
            print(rq.format_daily_digest(digest))
    elif args.command in ("sync-preview", "sync-diff", "sync-audit"):
        import sync_debug
        if args.command == "sync-preview":
            return sync_debug.cmd_preview(
                lead_id=args.lead_id, email=args.email, as_json=args.json,
            )
        if args.command == "sync-diff":
            return sync_debug.cmd_diff(
                lead_id=args.lead_id, email=args.email, as_json=args.json,
            )
        return sync_debug.cmd_audit(
            lead_id=args.lead_id, email=args.email,
            limit=args.last, errors_only=args.errors, as_json=args.json,
        )
    elif args.command == "stats":
        total_events = _pipeline.get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        _pipeline.print_freshness_stderr(_pipeline.get_last_pull(), total_events=total_events)
        stats = _pipeline.attach_freshness(_pipeline.get_stats(), last_pull=_pipeline.get_last_pull())
        print(json.dumps(stats, indent=0) if getattr(args, "json", False) else _pipeline.format_stats(stats))
    elif args.command == "campaigns":
        total_events = _pipeline.get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
        _pipeline.print_freshness_stderr(_pipeline.get_last_pull(), total_events=total_events)
        stats = _pipeline.attach_freshness(_pipeline.get_campaign_stats(), last_pull=_pipeline.get_last_pull())
        if getattr(args, "json", False):
            print(json.dumps(stats, indent=2))
        else:
            lines = _pipeline.format_campaign_stats(stats, include_header=False)
            print("\n".join(lines) if lines else "No campaign data yet.")
    elif args.command == "platform-map":
        _pipeline.cmd_platform_map(getattr(args, "platform", None))
    elif args.command == "add-lead":
        result = _pipeline.add_lead(name=args.name, company=args.company, title=args.title,
                          industry=args.industry, headcount=args.headcount,
                          email=args.email, linkedin_url=args.linkedin,
                          channel=args.channel, stage=args.stage, notes=args.notes)
        ws_slug = getattr(args, "workspace", None)
        if ws_slug and result.get("id"):
            conn = _pipeline.get_conn()
            ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)
            if ws_row:
                _pipeline.upsert_workspace_lead(conn, _pipeline.DEFAULT_ORG_ID, ws_row["id"], result["id"],
                                      status=args.stage or "prospecting")
                conn.commit()
                result["workspace"] = ws_row["slug"]
            else:
                result["workspace_error"] = f"workspace not found: {ws_slug}"
            conn.close()
        print(json.dumps(result))
    elif args.command == "import-profiles":
        rows: list[dict] = []
        if args.file and args.json_data:
            print(json.dumps({"error": "Use --file or --json, not both"}))
            sys.exit(1)
        if args.file:
            try:
                path = _pipeline.resolve_project_path(args.file, kind="input")
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if not path.is_file():
                print(json.dumps({"error": f"File not found: {path}"}))
                sys.exit(1)
            try:
                rows = _pipeline.load_profile_rows_from_file(path)
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
        elif args.json_data:
            raw = sys.stdin.read() if args.json_data.strip() == "-" else args.json_data
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON: {e}"}))
                sys.exit(1)
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = [data]
            else:
                print(json.dumps({"error": "JSON must be an array of objects or a single object"}))
                sys.exit(1)
        else:
            print(json.dumps({"error": "Provide --file or --json"}))
            sys.exit(1)
        summary = _pipeline.import_profiles(
            rows,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            channel=args.channel,
            stage=args.stage,
            notes=args.notes,
            workspace=getattr(args, "workspace", None),
            sender_profile=getattr(args, "sender_profile", None),
            source=getattr(args, "source", None),
            source_detail=getattr(args, "source_detail", None),
            import_batch_id=getattr(args, "import_batch_id", None),
            import_format=getattr(args, "import_format", None),
        )
        # import_profiles() already reports a sync_hint when leads are pending
        # (see pipeline.py) without ever auto-syncing — network push only runs
        # on an explicit `pipeline.py sync` (sync_all's own docstring). The
        # auto-sync block that used to live here duplicated that logic AND
        # violated it: it re-pushed the entire never-relay-seen lead backlog
        # on every single-lead edit (unsynced_lead_clause has no updated_at
        # cursor), turning a <1s import into a 30-60s+ full-workspace push.
        print(json.dumps(summary, indent=2))
    elif args.command == "apply-email-find-results":
        rows: list[dict] = []
        if args.file and args.json_data:
            print(json.dumps({"error": "Use --file or --json, not both"}))
            sys.exit(1)
        if args.file:
            try:
                path = _pipeline.resolve_project_path(args.file, kind="input")
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if not path.is_file():
                print(json.dumps({"error": f"File not found: {path}"}))
                sys.exit(1)
            try:
                rows = _pipeline.load_profile_rows_from_file(path)
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
        elif args.json_data:
            raw = sys.stdin.read() if args.json_data.strip() == "-" else args.json_data
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON: {e}"}))
                sys.exit(1)
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = [data]
            else:
                print(json.dumps({"error": "JSON must be an array of objects or a single object"}))
                sys.exit(1)
        else:
            print(json.dumps({"error": "Provide --file or --json"}))
            sys.exit(1)
        summary = _pipeline.apply_email_find_results(
            rows,
            workspace=args.workspace,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            source=getattr(args, "source", None),
            source_detail=getattr(args, "source_detail", None),
        )
        print(json.dumps(summary, indent=2))
    elif args.command == "tag":
        action = getattr(args, "tag_action", None)
        if action == "repair":
            if not _pipeline.db_exists():
                print(json.dumps({"error": "Database not initialized. Ask Outreach Magic to initialize."}))
                sys.exit(1)
            _pipeline.migrate_db()
            conn = _pipeline.get_conn()
            try:
                print(json.dumps(_pipeline.repair_malformed_tags(conn, dry_run=getattr(args, "dry_run", False)), indent=2))
            finally:
                conn.close()
            return
        tag_ws = getattr(args, "workspace", None)
        if not tag_ws:
            print(json.dumps({"error": "--workspace required"}))
            sys.exit(1)
        conn = _pipeline.get_conn()
        ws_row = _pipeline.resolve_workspace_identity(conn, tag_ws)
        conn.close()
        if not ws_row:
            print(json.dumps({"error": f"workspace not found: {tag_ws}"}))
            sys.exit(1)
        ws_id = ws_row["id"]
        if action == "add":
            print(json.dumps(_pipeline.tag_add(ws_id, args.lead_id, args.tag)))
        elif action == "remove":
            print(json.dumps(_pipeline.tag_remove(ws_id, args.lead_id, args.tag)))
        elif action == "set":
            tags_list = _pipeline._parse_cli_tags(args.tags)
            print(json.dumps(_pipeline.tag_set(ws_id, args.lead_id, tags_list)))
        elif action == "list":
            print(json.dumps(_pipeline.tag_list(ws_id, lead_id=getattr(args, "lead_id", None))))
        elif action == "bulk":
            tags_list = _pipeline._parse_cli_tags(args.tags)
            identity_type = getattr(args, "identity_type", None)
            identity_values_raw = getattr(args, "identity_values", None)
            if identity_type or identity_values_raw:
                if not identity_type or not identity_values_raw:
                    print(json.dumps({"error": "--identity-type and --identity-values must be used together"}))
                    sys.exit(1)
                if getattr(args, "lead_ids", None):
                    print(json.dumps({"error": "use --lead-ids or --identity-type/--identity-values, not both"}))
                    sys.exit(1)
                values = [v.strip() for v in identity_values_raw.split(",") if v.strip()]
                print(json.dumps(_pipeline.tag_bulk_by_identity(
                    ws_id, identity_type, values, tags_list, remove=getattr(args, "remove", False),
                )))
            else:
                if not getattr(args, "lead_ids", None):
                    print(json.dumps({"error": "--lead-ids or --identity-type/--identity-values required"}))
                    sys.exit(1)
                lead_ids = [int(x.strip()) for x in args.lead_ids.split(",") if x.strip()]
                print(json.dumps(_pipeline.tag_bulk(ws_id, lead_ids, tags_list, remove=getattr(args, "remove", False))))
        else:
            print(json.dumps({"error": "tag subcommand required: add, remove, set, list, bulk, repair"}))
    elif args.command == "batch-job":
        bj_action = getattr(args, "batch_job_action", None)
        if bj_action == "record":
            metadata = json.loads(args.metadata) if getattr(args, "metadata", None) else None
            print(json.dumps(_pipeline.record_batch_job(
                provider=args.provider, kind=args.kind, job_id=args.job_id,
                item_count=args.item_count, item_set_hash=args.item_hash,
                workspace=getattr(args, "workspace", None), metadata=metadata,
            )))
        elif bj_action == "find-pending":
            job = _pipeline.find_pending_batch_job(provider=args.provider, item_set_hash=args.item_hash)
            print(json.dumps({"job": job}))
        elif bj_action == "mark-status":
            print(json.dumps(_pipeline.mark_batch_job_status(
                provider=args.provider, job_id=args.job_id, status=args.status,
            )))
        elif bj_action == "list":
            jobs = _pipeline.list_batch_jobs(
                provider=getattr(args, "provider", None), workspace=getattr(args, "workspace", None),
            )
            print(json.dumps({"jobs": jobs, "count": len(jobs)}))
        else:
            print(json.dumps({"error": "batch-job subcommand required: record, find-pending, mark-status, list"}))
    elif args.command == "provider-attempt":
        pa_action = getattr(args, "provider_attempt_action", None)
        if pa_action == "bulk":
            lead_ids = [int(x.strip()) for x in args.lead_ids.split(",") if x.strip()]
            print(json.dumps(_pipeline.record_provider_attempts_bulk(
                lead_ids, args.provider, status=args.status,
            )))
        elif pa_action == "list":
            attempts = _pipeline.list_provider_attempts(args.lead_id)
            print(json.dumps({"lead_id": args.lead_id, "attempts": attempts, "count": len(attempts)}))
        else:
            print(json.dumps({"error": "provider-attempt subcommand required: bulk, list"}))
    elif args.command == "verify-email":
        if getattr(args, "batch", False):
            try:
                items = _pipeline.load_json_array_from_cli(
                    json_input=getattr(args, "json_input", None),
                    file_path=getattr(args, "file", None),
                )
            except (json.JSONDecodeError, ValueError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            print(json.dumps(_pipeline.verify_email_batch(items), indent=2))
        else:
            lid = getattr(args, "lead_id", None)
            st = getattr(args, "status", None)
            src = getattr(args, "source", None)
            if not lid or not st or not src:
                print(json.dumps({"error": "--lead-id, --status, and --source required (or use --batch --json)"}))
                sys.exit(1)
            print(json.dumps(_pipeline.verify_email(
                lid, st, src,
                sub_status=getattr(args, "sub_status", None),
                source_detail=getattr(args, "source_detail", None),
                smtp_provider=getattr(args, "smtp_provider", None),
            ), indent=2))
    elif args.command == "verify-status":
        print(json.dumps(_pipeline.verify_status(
            lead_id=getattr(args, "lead_id", None),
            email=getattr(args, "email", None),
        ), indent=2))
    elif args.command == "verify-pending":
        result = _pipeline.verify_pending(limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"{len(result)} leads pending verification:")
            for r in result:
                print(f"  [{r['id']}] {r.get('name') or '?'} — {r.get('email') or ''}")
    elif args.command == "verification-candidates":
        print(json.dumps(
            _pipeline.leads_needing_verification(
                args.workspace,
                max_age_days=getattr(args, "max_age", 30),
                skip_mv_days=getattr(args, "skip_mv_days", 7),
                limit=getattr(args, "limit", 5000),
                never_contacted_only=getattr(args, "never_contacted", False),
                skip_mv_attempted_tag=not getattr(args, "include_mv_attempted", False),
            ),
            indent=2,
        ))
    elif args.command == "bounce-list":
        rows = _pipeline.list_bounce_events(
            platform=getattr(args, "platform", None),
            bounce_type=getattr(args, "bounce_type", None),
            sender=getattr(args, "sender", None),
            since=getattr(args, "since", None),
            limit=args.limit,
        )
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2))
        else:
            if not rows:
                print("No bounce records found.")
            else:
                print(f"{'Lead':<28} {'Sender':<28} {'Type':<8} {'MX':<18} {'Seen':<12} {'Msg'}")
                print("-" * 120)
                for row in rows:
                    msg = (row.get("bounce_message") or "")[:60]
                    print(
                        f"{(row.get('lead_email') or '—'):<28} "
                        f"{(row.get('sender_email') or '—'):<28} "
                        f"{(row.get('bounce_type') or '—'):<8} "
                        f"{(row.get('recipient_mx') or '—'):<18} "
                        f"{(row.get('last_seen_at') or '')[:10]:<12} "
                        f"{msg}"
                    )
    elif args.command == "bounce-stats":
        stats = _pipeline.bounce_stats(since=getattr(args, "since", None))
        if getattr(args, "json", False):
            print(json.dumps(stats, indent=2))
        else:
            print(
                f"Unique bounces: {stats['total_unique_bounces']} | "
                f"Suppressed duplicate webhooks: {stats['suppressed_duplicate_webhooks']}"
            )
            if stats["by_platform"]:
                print("By platform: " + ", ".join(
                    f"{r['platform']}={r['c']}" for r in stats["by_platform"]
                ))
            if stats["by_bounce_type"]:
                print("By type: " + ", ".join(
                    f"{r['bounce_type']}={r['c']}" for r in stats["by_bounce_type"]
                ))
    elif args.command == "agent-changes":
        result = _pipeline.export_local_changes(
            all_leads=getattr(args, "all", False),
            workspace=getattr(args, "workspace", None),
            sample_limit=getattr(args, "limit", None),
        )
        if getattr(args, "file", None):
            _pipeline.write_export_csv(result, args.file)
        else:
            print(json.dumps(result, indent=2))
    elif args.command == "update-stage":
        ws_slug = getattr(args, "workspace", None)
        conn = _pipeline.get_conn()
        routing_config = _pipeline.get_org_routing_config(conn, _pipeline.DEFAULT_ORG_ID)
        ws_row = None
        if routing_config.mode == _pipeline.WORKSPACE_ROUTING_MULTI:
            if not ws_slug:
                conn.close()
                print(json.dumps({"error": "Multi-workspace mode: --workspace is required for update-stage"}))
                sys.exit(1)
            ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)
            if not ws_row:
                conn.close()
                print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                sys.exit(1)
        elif ws_slug:
            ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)
        conn.close()

        _pipeline.update_lead_stage(args.id, args.stage, args.next_action)

        result = {"status": "updated", "id": args.id, "stage": args.stage}
        if ws_row:
            conn = _pipeline.get_conn()
            ws_lead_id = _pipeline.upsert_workspace_lead(
                conn, _pipeline.DEFAULT_ORG_ID, ws_row["id"], args.id, status=args.stage,
                current_status_label=getattr(args, "label", None),
                current_status_sentiment=getattr(args, "sentiment", None))
            stage_ts = datetime.now(timezone.utc).isoformat()
            update_sets = ["status = ?", "stage_entered_at = ?"]
            update_params = [args.stage, stage_ts]
            sentiment_val = getattr(args, "sentiment", None)
            label_val = getattr(args, "label", None)
            if sentiment_val:
                update_sets.append("current_status_sentiment = ?")
                update_params.append(sentiment_val)
            if label_val:
                update_sets.append("current_status_label = ?")
                update_params.append(label_val)
            update_params.append(ws_lead_id)
            conn.execute(
                f"UPDATE workspace_leads SET {', '.join(update_sets)} WHERE id = ?",
                update_params)
            conn.commit()
            conn.close()
            result["workspace"] = ws_row["slug"]

        # Log an event for the stage change
        event_metadata = {
            "lead_status_raw": args.stage,
            "lead_status_display": args.stage.replace("_", " "),
        }
        if getattr(args, "sentiment", None):
            event_metadata["lead_status_sentiment"] = args.sentiment
        if getattr(args, "label", None):
            event_metadata["lead_status_display"] = args.label
        _pipeline.log_event(
            lead_id=args.id,
            event_type="lead_status_updated",
            direction="inbound",
            metadata=event_metadata,
        )

        print(json.dumps(result))
        if getattr(args, "crm_sync", False) and ws_slug:
            _maybe_trigger_crm_sync(lead_id=args.id, stage=args.stage, workspace_slug=ws_slug)
    elif args.command == "log-event":
        ws_slug = getattr(args, "workspace", None)
        conn = _pipeline.get_conn()
        routing_config = _pipeline.get_org_routing_config(conn, _pipeline.DEFAULT_ORG_ID)
        ws_row = None
        if routing_config.mode == _pipeline.WORKSPACE_ROUTING_MULTI:
            if not ws_slug:
                conn.close()
                print(json.dumps({"error": "Multi-workspace mode: --workspace is required for log-event"}))
                sys.exit(1)
            ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)
            if not ws_row:
                conn.close()
                print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                sys.exit(1)
        elif ws_slug:
            ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)
        conn.close()

        metadata = json.loads(args.metadata) if getattr(args, "metadata", None) else None
        logged_event_id = _pipeline.log_event(
                  lead_id=args.lead_id, event_type=args.event_type, direction=args.direction,
                  channel=args.channel, subject=args.subject, body_preview=args.body,
                  metadata=metadata)

        result = {"status": "logged", "lead_id": args.lead_id}
        if ws_row:
            conn = _pipeline.get_conn()
            status_defaults = {
                "email_sent": "contacted",
                "linkedin_connect": "contacted",
                "linkedin_message": "contacted",
                "email_reply": "replied",
                "linkedin_reply": "replied",
                "linkedin_connection_accepted": "replied",
                "meeting_booked": "scheduled",
            }
            initial_status = status_defaults.get(args.event_type, "prospecting")
            _pipeline.upsert_workspace_lead(
                conn, _pipeline.DEFAULT_ORG_ID, ws_row["id"], args.lead_id,
                status=initial_status)
            idem_key = f"agent_cli_{args.lead_id}_{args.event_type}_{datetime.now(timezone.utc).isoformat()}"
            # The subject/body/channel written just above by log_event are the
            # record; this row only indexes it into the workspace.
            _pipeline.append_workspace_event(
                conn, _pipeline.DEFAULT_ORG_ID, ws_row["id"], args.lead_id,
                event_id=logged_event_id,
                event_type=args.event_type,
                event_at=_pipeline.utc_now_for_storage(),
                idempotency_key=idem_key)
            conn.commit()
            conn.close()
            result["workspace"] = ws_row["slug"]
        print(json.dumps(result))
        if getattr(args, "crm_sync", False) and ws_slug:
            _maybe_trigger_crm_sync(lead_id=args.lead_id, workspace_slug=ws_slug)
    elif args.command == "find-domains":
        result = _pipeline.find_domains_for_workspace(
            args.workspace,
            limit=getattr(args, "limit", None),
            force=getattr(args, "force", False),
            dry_run=getattr(args, "dry_run", False),
            debug=getattr(args, "debug", False),
            max_queries=getattr(args, "max_queries", None),
        )
        if result.get("status") == "error":
            print(json.dumps(result))
            sys.exit(1)
        # Per-company rows are dropped from the normal summary (a real run is
        # thousands of them), but --dry-run exists precisely to show which
        # companies would be searched, so it keeps them.
        drop = set() if getattr(args, "dry_run", False) else {"results"}
        summary = {k: v for k, v in result.items() if k not in drop}
        print(json.dumps(summary, indent=2))
    elif args.command == "review":
        if args.review_command == "templates" and args.templates_command == "list":
            print(json.dumps({"templates": ["dedup-review", "lead-review"]}, indent=2))
            sys.exit(0)
        elif args.review_command == "presets":
            template = pipeline_lead_review.normalize_review_template(args.template)
            tok = _pipeline.get_agent_key()
            api_base = review_cloud.get_api_base(_pipeline.load_config)
            if tok and review_cloud.review_enabled(_pipeline.load_config, _pipeline.get_agent_key):
                try:
                    print(json.dumps(review_cloud.list_presets(api_base, tok, template=template), indent=2))
                except RuntimeError:
                    print(json.dumps(pipeline_lead_review.list_presets(template), indent=2))
            else:
                print(json.dumps(pipeline_lead_review.list_presets(template), indent=2))
            sys.exit(0)
        elif args.review_command == "export-payload":
            if not getattr(args, "workspace", None):
                print(json.dumps({"error": "--workspace required"}))
                sys.exit(1)
            custom_fields = None
            if getattr(args, "fields", None):
                custom_fields = [f.strip() for f in args.fields.split(",") if f.strip()]
            conn = _pipeline.get_conn()
            try:
                payload = pipeline_lead_review.build_export_payload(
                    conn,
                    workspace=args.workspace,
                    detail=getattr(args, "detail", "standard"),
                    title=args.title,
                    custom_fields=custom_fields,
                    tag=getattr(args, "tag", None),
                    stage=getattr(args, "stage", None),
                    since=getattr(args, "since", None),
                    limit=getattr(args, "limit", 5000),
                    never_contacted=getattr(args, "never_contacted", False),
                    no_email=getattr(args, "no_email", False),
                    require_domain=getattr(args, "require_domain", False),
                    enrich_fn=_pipeline.enrich_lead_rows,
                    **pipeline_lead_review.review_export_filter_kwargs(args),
                )
            except (ValueError, OSError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            finally:
                conn.close()
            print(json.dumps(payload, indent=2))
            sys.exit(0)
        elif args.review_command == "apply-sync":
            if not getattr(args, "workspace", None):
                print(json.dumps({"error": "--workspace required"}))
                sys.exit(1)
            in_path = _pipeline.resolve_project_path(args.input, kind="input")
            try:
                sheet_rows = json.loads(in_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(json.dumps({"error": f"invalid --input JSON: {e}"}))
                sys.exit(1)
            if not isinstance(sheet_rows, list):
                print(json.dumps({"error": "--input must be a JSON array of row objects"}))
                sys.exit(1)
            conn = _pipeline.get_conn()
            try:
                ws_row = _pipeline.resolve_workspace_identity(conn, args.workspace)
                if not ws_row:
                    print(json.dumps({"error": f"workspace not found: {args.workspace}"}))
                    sys.exit(1)
                summary = pipeline_lead_review.apply_lead_review_sync(
                    conn,
                    ws_row["id"],
                    sheet_rows,
                    upsert_workspace_lead_fn=_pipeline.upsert_workspace_lead,
                    org_id=_pipeline.DEFAULT_ORG_ID,
                    dry_run=args.dry_run or not args.commit,
                )
            finally:
                conn.close()
            print(json.dumps(summary, indent=2))
            sys.exit(0)
        elif args.review_command in ("export", "sync"):
            tok = _pipeline.get_agent_key()
            if not tok:
                print(json.dumps({"error": "login required — ask Outreach Magic to log in"}))
                sys.exit(1)
            api_base = review_cloud.get_api_base(_pipeline.load_config)
            template = pipeline_lead_review.normalize_review_template(args.template)
        if args.review_command == "export":
            try:
                if template == "lead-review":
                    if not getattr(args, "workspace", None):
                        print(json.dumps({"error": "--workspace required for lead-review export"}))
                        sys.exit(1)
                    custom_fields = None
                    if getattr(args, "fields", None):
                        custom_fields = [f.strip() for f in args.fields.split(",") if f.strip()]
                    conn = _pipeline.get_conn()
                    try:
                        payload = pipeline_lead_review.build_export_payload(
                            conn,
                            workspace=args.workspace,
                            detail=getattr(args, "detail", "standard"),
                            title=args.title,
                            custom_fields=custom_fields,
                            tag=getattr(args, "tag", None),
                            stage=getattr(args, "stage", None),
                            since=getattr(args, "since", None),
                            limit=getattr(args, "limit", 5000),
                            never_contacted=getattr(args, "never_contacted", False),
                            no_email=getattr(args, "no_email", False),
                            require_domain=getattr(args, "require_domain", False),
                            enrich_fn=_pipeline.enrich_lead_rows,
                            **pipeline_lead_review.review_export_filter_kwargs(args),
                        )
                    finally:
                        conn.close()
                    if not payload.get("rows"):
                        print(json.dumps({"error": "no leads matched export filters"}))
                        sys.exit(1)
                    share_email, public_link = _pipeline.resolve_sheets_export_access(args)
                    sheet_id = getattr(args, "sheet_id", None)
                    parent_sheet_id = getattr(args, "parent_sheet_id", None)
                    tab_name = getattr(args, "tab_name", None)
                    result = _export_lead_review_chunked(
                        api_base,
                        tok,
                        template=template,
                        title=args.title,
                        share_email=share_email,
                        public_link=public_link,
                        sheet_id=str(sheet_id).strip() if sheet_id else None,
                        parent_sheet_id=str(parent_sheet_id).strip() if parent_sheet_id else None,
                        tab_title=tab_name or getattr(args, "stage", None),
                        detail=payload.get("detail"),
                        headers=payload.get("headers"),
                        rows=payload.get("rows"),
                        workspace=args.workspace,
                        columns=payload.get("columns"),
                        freeze_header=payload.get("freeze_header"),
                    )
                else:
                    if not getattr(args, "input", None):
                        print(json.dumps({"error": "--input required for dedup-review export"}))
                        sys.exit(1)
                    in_path = _pipeline.resolve_project_path(args.input, kind="input")
                    payload = pipeline_dedup.load_candidates_file(str(in_path))
                    candidates = payload.get("candidates") or []
                    if not candidates:
                        print(json.dumps({"error": "no candidates in input file"}))
                        sys.exit(1)
                    result = review_cloud.export_review(
                        api_base,
                        tok,
                        template=template,
                        candidates=candidates,
                        title=args.title,
                        share_email=_pipeline.require_share_email_for_export(getattr(args, "share_email", None)),
                    )
            except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if template == "lead-review" and isinstance(result, dict) and result.get("sheet_id"):
                try:
                    meta_path = _pipeline.save_sheets_export_record(
                        workspace=args.workspace,
                        title=args.title,
                        sheet_id=str(result["sheet_id"]),
                        url=str(result.get("url") or result.get("spreadsheet_url") or ""),
                        detail=str(payload.get("detail") or getattr(args, "detail", "") or ""),
                        tag=getattr(args, "tag", None),
                        stage=getattr(args, "stage", None),
                        since=getattr(args, "since", None),
                        limit=getattr(args, "limit", 5000),
                        never_contacted=getattr(args, "never_contacted", False),
                        no_email=getattr(args, "no_email", False),
                        require_domain=getattr(args, "require_domain", False),
                        **pipeline_lead_review.review_export_filter_kwargs(args),
                    )
                    result = dict(result)
                    result["metadata_path"] = str(meta_path)
                except OSError:
                    pass
            print(json.dumps(result, indent=2))
        elif args.review_command == "sync":
            field_keys = None
            baseline_rows = None
            if template == "lead-review":
                ws_slug = getattr(args, "workspace", None)
                if not ws_slug:
                    print(json.dumps({"error": "--workspace required for lead-review sync"}))
                    sys.exit(1)
                detail = getattr(args, "detail", None) or "standard"
                custom_fields = None
                if getattr(args, "fields", None):
                    custom_fields = [f.strip() for f in args.fields.split(",") if f.strip()]
                conn = _pipeline.get_conn()
                try:
                    ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)
                    if not ws_row:
                        print(json.dumps({"error": f"workspace not found: {ws_slug}"}))
                        sys.exit(1)
                    senders = (
                        pipeline_lead_review.list_workspace_senders(conn, ws_row["id"])
                        if detail == "full"
                        else []
                    )
                    columns = pipeline_lead_review.resolve_columns(
                        detail, custom_fields=custom_fields, sender_profiles=senders,
                    )
                    field_keys = {label: key for label, key in columns}

                    # Resolve export filters from stored metadata + CLI override
                    stored = _pipeline.find_sheets_export_record(args.sheet_id)
                    stored_filters = (stored or {}).get("filters") or {}
                    effective_tag = getattr(args, "tag", None) or stored_filters.get("tag")
                    effective_stage = getattr(args, "stage", None) or stored_filters.get("stage")
                    effective_since = getattr(args, "since", None) or stored_filters.get("since")
                    effective_never = (
                        getattr(args, "sync_never_contacted", False)
                        or stored_filters.get("never_contacted", False)
                    )
                    effective_no_email = (
                        getattr(args, "sync_no_email", False)
                        or stored_filters.get("no_email", False)
                    )
                    effective_domain = (
                        getattr(args, "sync_require_domain", False)
                        or stored_filters.get("require_domain", False)
                    )
                    effective_limit = (
                        getattr(args, "limit", None)
                        or stored_filters.get("limit")
                        or 5000
                    )

                    if stored:
                        # Primary path: build a targeted baseline using stored filters
                        # plus any CLI overrides the user supplied.
                        export_payload = pipeline_lead_review.build_export_payload(
                            conn,
                            workspace=ws_slug,
                            detail=detail,
                            title="baseline",
                            custom_fields=custom_fields,
                            tag=effective_tag,
                            stage=effective_stage,
                            since=effective_since,
                            limit=effective_limit,
                            never_contacted=effective_never,
                            no_email=effective_no_email,
                            require_domain=effective_domain,
                            enrich_fn=_pipeline.enrich_lead_rows,
                        )
                        baseline_rows = []
                        for row in export_payload.get("rows") or []:
                            obj: dict[str, Any] = {"lead_id": row[0] if row else ""}
                            for i, (_label, key) in enumerate(columns):
                                if i < len(row):
                                    obj[key] = row[i]
                            baseline_rows.append(obj)
                    else:
                        # Fallback: no stored metadata — skip the baseline.
                        # The cloud API returns all sheet rows, and
                        # apply_lead_review_sync() diffs each one against
                        # current DB state via _current_row_state(), which is
                        # the authoritative comparison. Slightly more data
                        # over the wire, but always correct.
                        baseline_rows = None
                finally:
                    conn.close()
            try:
                read_result = review_cloud.sync_read(
                    api_base,
                    tok,
                    sheet_id=args.sheet_id,
                    template=template,
                    field_keys=field_keys,
                    baseline_rows=baseline_rows,
                )
            except RuntimeError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            if template == "lead-review":
                sheet_rows = read_result.get("rows") or []
                if not sheet_rows:
                    print(json.dumps({
                        "status": "noop",
                        "rows": 0,
                        "changed_count": read_result.get("changed_count", 0),
                        "message": "no rows to sync",
                    }))
                    sys.exit(0)
                conn = _pipeline.get_conn()
                try:
                    ws_row = _pipeline.resolve_workspace_identity(conn, ws_slug)

                    summary = pipeline_lead_review.apply_lead_review_sync(
                        conn,
                        ws_row["id"],
                        sheet_rows,
                        upsert_workspace_lead_fn=_pipeline.upsert_workspace_lead,
                        org_id=_pipeline.DEFAULT_ORG_ID,
                        dry_run=args.dry_run or not args.commit,
                    )
                finally:
                    conn.close()
                if args.commit and summary.get("updated"):
                    write_results = [
                        {"lead_id": int(chg["lead_id"]), "result": "✓ Synced"}
                        for chg in summary.get("changes") or []
                        if chg.get("lead_id") is not None
                    ]
                    if write_results:
                        try:
                            review_cloud.sync_write_results(
                                api_base,
                                tok,
                                sheet_id=args.sheet_id,
                                template=template,
                                results=write_results,
                            )
                        except RuntimeError:
                            pass
                print(json.dumps(summary, indent=2))
                sys.exit(0)

            approved = read_result.get("approved") or []
            if not approved:
                print(json.dumps({"status": "noop", "approved": 0, "message": "no rows to sync"}))
                sys.exit(0)
            merge_candidates = [
                {"keep_id": int(row["keep_id"]), "merge_id": int(row["merge_id"])}
                for row in approved
            ]
            if args.dry_run or not args.commit:
                print(json.dumps({
                    "status": "dry_run",
                    "approved": len(merge_candidates),
                    "approved_pairs": merge_candidates,
                }, indent=2))
                sys.exit(0)
            conn = _pipeline.get_conn()
            try:
                merge_result = pipeline_dedup.batch_merge_candidates(
                    conn,
                    merge_candidates,
                    commit=True,
                    reason="dedup_review",
                    merge_leads_fn=_pipeline.merge_leads,
                )
            finally:
                conn.close()
            failures = {
                (int(f.get("keep_id")), int(f.get("merge_id"))): f.get("error", "failed")
                for f in (merge_result.get("failures") or [])
            }
            sheet_results = []
            for pair in merge_candidates:
                key = (pair["keep_id"], pair["merge_id"])
                if key in failures:
                    text = f"✗ {failures[key]}"
                else:
                    text = "✓ Merged"
                sheet_results.append({
                    "keep_id": pair["keep_id"],
                    "merge_id": pair["merge_id"],
                    "result": text,
                })
            try:
                write_result = review_cloud.sync_write_results(
                    api_base,
                    tok,
                    sheet_id=args.sheet_id,
                    results=sheet_results,
                    template=template,
                )
            except RuntimeError as e:
                merge_result["sheet_write_error"] = str(e)
                print(json.dumps({
                    "status": "completed_with_sheet_error",
                    "merge": merge_result,
                }, indent=2))
                sys.exit(1)
            print(json.dumps({
                "status": "completed",
                "merge": merge_result,
                "sheet": write_result,
            }, indent=2))
        else:
            print(json.dumps({"error": f"unknown review subcommand: {args.review_command}"}))
            sys.exit(1)
    elif args.command == "dedup":
        if args.dedup_command == "find":
            conn = _pipeline.get_conn()
            try:
                payload = pipeline_dedup.find_duplicates(
                    conn,
                    workspace_slug=args.workspace,
                    tag_filter=args.tag,
                    min_confidence=args.min_confidence,
                    resolve_workspace_fn=_pipeline.resolve_workspace_identity,
                    normalize_tag_fn=_pipeline.normalize_tag,
                )
            except ValueError as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            finally:
                conn.close()
            text = json.dumps(payload, indent=2)
            if args.output:
                out = _pipeline.resolve_project_path(args.output, kind="export", for_write=True)
                out.write_text(text + "\n", encoding="utf-8")
                print(json.dumps({"status": "written", "file": str(out), "stats": payload["stats"]}))
            else:
                print(text)
        elif args.dedup_command == "merge":
            try:
                cand_path = _pipeline.resolve_project_path(args.candidates, kind="input")
                payload = pipeline_dedup.load_candidates_file(str(cand_path))
            except (OSError, ValueError, json.JSONDecodeError) as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            filtered = pipeline_dedup.filter_candidates(
                payload, min_confidence=args.min_confidence,
            )
            conn = _pipeline.get_conn()
            try:
                result = pipeline_dedup.batch_merge_candidates(
                    conn,
                    filtered,
                    commit=args.commit,
                    reason=args.reason,
                    merge_leads_fn=_pipeline.merge_leads,
                )
            finally:
                conn.close()
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({"error": f"unknown dedup subcommand: {args.dedup_command}"}))
            sys.exit(1)
    elif args.command == "dedup-events":
        import event_dedup

        conn = _pipeline.get_conn()
        try:
            result = event_dedup.dedupe_reply_events(conn, commit=args.commit)
        finally:
            conn.close()
        print(json.dumps(result, indent=2))
    elif args.command == "merge-leads":
        if args.keep and args.merge:
            result = _pipeline.merge_leads(args.keep, args.merge, reason="manual_cli")
        elif args.email and args.linkedin:
            keep_lead = _pipeline.find_lead(email=args.email)
            merge_lead = _pipeline.find_lead(linkedin=args.linkedin)
            if not keep_lead or not merge_lead:
                print(json.dumps({"error": "Could not resolve both leads by email and linkedin"}))
                sys.exit(1)
            if keep_lead["id"] == merge_lead["id"]:
                result = {"status": "noop", "keep_id": keep_lead["id"]}
            else:
                conn = _pipeline.get_conn()
                keep_id, merge_id = _pipeline._pick_merge_keep_id(
                    conn, keep_lead["id"], merge_lead["id"]
                )
                conn.close()
                result = _pipeline.merge_leads(keep_id, merge_id, reason="manual_email_linkedin")
        else:
            print(json.dumps({"error": "Provide --keep and --merge, or --email and --linkedin"}))
            sys.exit(1)
        print(json.dumps(result, indent=2))
    elif args.command == "merge-review":
        mr_action = getattr(args, "merge_review_action", None)
        if mr_action == "list":
            print(json.dumps(_pipeline.list_merge_proposals(
                status=getattr(args, "status", "pending"),
                reason=getattr(args, "reason", None),
                limit=getattr(args, "limit", 50),
            ), indent=2))
        elif mr_action == "approve":
            print(json.dumps(_pipeline.approve_merge_proposal(args.id), indent=2))
        elif mr_action == "reject":
            print(json.dumps(_pipeline.reject_merge_proposal(args.id, note=getattr(args, "note", None)), indent=2))
        else:
            print(json.dumps({"error": "merge-review subcommand required: list, approve, reject"}))
    elif args.command == "company":
        if args.company_command == "dedup-audit":
            conn = _pipeline.get_conn()
            try:
                report = _pipeline.company_dedup_baseline_audit(conn, limit=getattr(args, "limit", None))
            finally:
                conn.close()
            print(json.dumps({"status": "ok", "group_count": len(report), "groups": report}, indent=2))
        elif args.company_command == "backfill-candidates":
            print(json.dumps(_pipeline.company_merge_candidates_backfill(), indent=2))
        elif args.company_command == "domain-stats":
            conn = _pipeline.get_conn()
            try:
                report = _pipeline.company_domain_stats_report(conn, args.id)
            finally:
                conn.close()
            if not report:
                print(json.dumps({"error": f"company not found: {args.id}"}))
                sys.exit(1)
            print(json.dumps(report, indent=2))
        elif args.company_command == "domain-label":
            print(json.dumps(
                _pipeline.set_company_domain_label(args.company_id, args.domain, args.label), indent=2,
            ))
        elif args.company_command == "merge-review":
            cmr_action = getattr(args, "company_merge_review_action", None)
            if cmr_action == "list":
                print(json.dumps(_pipeline.list_company_merge_candidates(
                    status=getattr(args, "status", "pending"),
                    reason=getattr(args, "reason", None),
                    limit=getattr(args, "limit", 50),
                    min_confidence=getattr(args, "min_confidence", "ALL"),
                ), indent=2))
            elif cmr_action == "approve":
                print(json.dumps(_pipeline.approve_company_merge_candidate(args.id), indent=2))
            elif cmr_action == "reject":
                print(json.dumps(
                    _pipeline.reject_company_merge_candidate(args.id, note=getattr(args, "note", None)), indent=2,
                ))
            else:
                print(json.dumps({"error": "company merge-review subcommand required: list, approve, reject"}))
                sys.exit(1)
        elif args.company_command == "merge":
            print(json.dumps(_pipeline.merge_companies(args.keep, args.merge, reason="manual_cli"), indent=2))
        else:
            print(json.dumps({"error": f"unknown company subcommand: {args.company_command}"}))
            sys.exit(1)
    elif args.command == "import-linkedin-connections":
        try:
            path = _pipeline.resolve_project_path(args.file, kind="input")
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        if not path.is_file():
            print(json.dumps({"error": f"File not found: {path}"}))
            sys.exit(1)
        from linkedin_connections import import_linkedin_connections

        summary = import_linkedin_connections(
            str(path),
            workspace=args.workspace,
            sender=args.sender,
            tag=getattr(args, "tag", None),
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
        print(json.dumps(summary, indent=2))
    elif args.command == "import-sender-accounts":
        print(json.dumps(_pipeline.import_sender_accounts(
            args.file, workspace=getattr(args, "workspace", None),
        ), indent=2))
    elif args.command == "sender-accounts":
        sa_action = getattr(args, "sender_accounts_action", None)
        if sa_action == "list":
            conn = _pipeline.get_conn()
            try:
                if getattr(args, "workspace", None):
                    rows = conn.execute(
                        """SELECT sa.* FROM sender_accounts sa
                           INNER JOIN workspace_sender_accounts wsa ON wsa.sender_account_id = sa.id
                           INNER JOIN workspaces w ON w.id = wsa.workspace_id
                           WHERE w.slug = ?
                           ORDER BY sa.email""",
                        (args.workspace,),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM sender_accounts ORDER BY email").fetchall()
                accounts = [dict(r) for r in rows]
            finally:
                conn.close()
            if getattr(args, "json", False):
                print(json.dumps({"accounts": accounts, "count": len(accounts)}, indent=2))
            else:
                print(f"{len(accounts)} sender account(s):")
                for a in accounts:
                    print(f"  {a['email']} — health={a.get('overall_health_score')} "
                          f"status={a.get('status')} warmup={a.get('warmup_status')}")
        elif sa_action == "update":
            print(json.dumps(_pipeline.update_sender_account(
                args.email,
                provider=getattr(args, "provider", None),
                first_name=getattr(args, "first_name", None),
                last_name=getattr(args, "last_name", None),
                daily_limit=getattr(args, "daily_limit", None),
                status=getattr(args, "status", None),
                warmup_status=getattr(args, "warmup_status", None),
                channel=getattr(args, "channel", None),
            ), indent=2))
        elif sa_action in ("link", "unlink"):
            print(json.dumps(_pipeline.set_sender_account_workspace_link(
                args.email, args.workspace, linked=(sa_action == "link"),
            ), indent=2))
        else:
            print(json.dumps({"error": "sender-accounts subcommand required: list, update, link, unlink"}))
    elif args.command == "sender-domains":
        sd_action = getattr(args, "sender_domains_action", None)
        if sd_action == "list":
            domains = _pipeline.sender_domains_report()
            if getattr(args, "json", False):
                print(json.dumps({"domains": domains, "count": len(domains)}, indent=2))
            else:
                print(f"{len(domains)} domain(s):")
                for d in domains:
                    cost = f"${d['domain_cost']:.2f}" if d["domain_cost"] is not None else "—"
                    per = f"${d['cost_per_account']:.2f}/account" if d["cost_per_account"] is not None else ""
                    note = f" — note: {d['notes']}" if d.get("notes") else ""
                    print(f"  {d['domain']} — {d['sender_count']} sender(s), "
                          f"reseller={d.get('reseller') or '—'}, cost={cost} {per}{note}")
        elif sd_action == "set":
            print(json.dumps(_pipeline.set_sender_domain_cost(
                args.domain, reseller=getattr(args, "reseller", None),
                domain_cost=getattr(args, "cost", None), currency=getattr(args, "currency", None),
                notes=getattr(args, "notes", None), sending_ip=getattr(args, "ip", None),
            ), indent=2))
        elif sd_action == "blacklist-check":
            result = _pipeline.run_blacklist_check(
                domain=getattr(args, "domain", None), tier=getattr(args, "tier", "all"),
            )
            if result.get("newly_listed"):
                print(
                    f"[blacklist] NEWLY LISTED: {', '.join(result['newly_listed'])}",
                    file=sys.stderr,
                )
            if getattr(args, "json", False):
                print(json.dumps(result, indent=2))
            else:
                print(f"Checked {result['domains_checked']} domain(s), tier={result['tier']}:")
                for r in result["results"]:
                    s = r["summary"]
                    state = "CLEAN" if r["all_clean"] else "LISTED"
                    print(
                        f"  {r['domain']} — {state} "
                        f"(clean={s['clean']}, listed={s['listed']}, errors={s['errors']})"
                    )
            if result.get("any_listed"):
                sys.exit(1)
        elif sd_action == "blacklist-status":
            result = _pipeline.blacklist_status_report(
                domain=getattr(args, "domain", None), stale_hours=getattr(args, "stale_hours", None),
            )
            if getattr(args, "json", False):
                print(json.dumps(result, indent=2))
            else:
                c = result["counts"]
                print(
                    f"clean={c['clean']}, listed={c['listed']}, "
                    f"unchecked={c['unchecked']}, stale={c['stale']}"
                )
                for d in result["domains"]:
                    print(f"  {d['domain']} — {d['state']}"
                          + (" (stale)" if d.get("stale") else ""))
        elif sd_action == "cost":
            months = getattr(args, "months", None)
            if getattr(args, "workspace", None):
                result = _pipeline.workspace_sender_cost_report(args.workspace, months=months)
            elif getattr(args, "reseller", None):
                result = _pipeline.reseller_cost_report(args.reseller, months=months)
            else:
                result = {"status": "error", "error": "pass --workspace or --reseller"}
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({"error": "sender-domains subcommand required: list, set, cost, blacklist-check, blacklist-status"}))
    elif args.command == "sender-insights":
        conn = _pipeline.get_conn()
        try:
            insights = _pipeline.sender_insights(
                conn, workspace=getattr(args, "workspace", None), since=getattr(args, "since", None),
            )
        finally:
            conn.close()
        if getattr(args, "json", False):
            print(json.dumps({"accounts": insights, "count": len(insights)}, indent=2))
        else:
            print(f"{len(insights)} sender account(s):")
            for a in insights:
                print(f"  {a['email']} — health={a.get('overall_health_score')} "
                      f"reply_rate={a.get('reply_rate')} bounce_rate={a.get('bounce_rate')} "
                      f"sent={a.get('sent_count')}")
    elif args.command == "batch-lead-lookup":
        try:
            items = _pipeline.load_json_array_from_cli(
                json_input=getattr(args, "json_input", None),
                file_path=getattr(args, "file", None),
            )
            print(json.dumps(
                _pipeline.batch_lead_lookup(items, workspace=getattr(args, "workspace", None)),
                indent=2,
            ))
        except (json.JSONDecodeError, ValueError) as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
    elif args.command == "history":
        try:
            lead = _pipeline.find_lead(
                lead_id=args.id, email=args.email,
                linkedin=getattr(args, "linkedin", None), name=args.name,
                workspace=getattr(args, "workspace", None),
            )
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            sys.exit(1)
        if not lead:
            print(json.dumps({"error": "Lead not found"}))
            sys.exit(1)

        enriched = _pipeline.enrich_lead_rows([lead], workspace=getattr(args, "workspace", None))
        lead = enriched[0] if enriched else lead

        events = _pipeline.get_lead_events(lead["id"], args.limit)
        if args.json:
            print(json.dumps({"lead": lead, "events": events}, indent=2))
        else:
            print(_pipeline.format_event_timeline(lead, events))
    elif args.command == "copy-insights":
        try:
            insights = _pipeline.get_copy_insights(
                lead_status=args.lead_status,
                limit=args.limit,
                workspace=getattr(args, "workspace", None),
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        print(json.dumps(insights, indent=2) if args.json else _pipeline.format_copy_insights(insights))
    elif args.command == "segment-insights":
        try:
            insights = _pipeline.get_segment_insights(
                positive_lead_status=args.positive_lead_status,
                positive_sentiment=args.positive_sentiment,
                fields=args.fields,
                min_sent=args.min_sent,
                top=args.top,
                workspace=getattr(args, "workspace", None),
            )
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        print(json.dumps(insights, indent=2) if args.json else _pipeline.format_segment_insights(insights))
    elif args.command == "workspace":
        if args.workspace_cmd == "summary":
            summary = _pipeline.get_workspace_summary(
                args.workspace,
                tags_only=getattr(args, "tags_only", False),
            )
            if summary.get("error"):
                print(json.dumps(summary, indent=2) if getattr(args, "json", False) else summary["error"])
                sys.exit(1)
            if getattr(args, "json", False):
                summary["local_pending"] = _pipeline.get_local_pending_counts()
                print(json.dumps(summary, indent=2))
            else:
                print(_pipeline.format_workspace_summary(summary))
        elif args.workspace_cmd == "create":
            print(json.dumps(_pipeline.create_workspace(args.name, args.slug, sync=getattr(args, "sync", False)), indent=2))
        elif args.workspace_cmd == "sync":
            print(json.dumps(_pipeline.sync_workspaces_to_cloud(), indent=2))
        elif args.workspace_cmd == "routing":
            if args.workspace_routing_cmd == "set":
                print(json.dumps(
                    _pipeline.set_workspace_routing(args.mode, workspace_slug=args.workspace),
                    indent=2,
                ))
            else:
                print(json.dumps(_pipeline.get_workspace_routing(), indent=2))
        elif args.workspace_cmd == "list":
            workspaces = _pipeline.list_workspaces()
            if getattr(args, "json", False):
                print(json.dumps(
                    _pipeline.attach_freshness({"workspaces": workspaces}, last_pull=_pipeline.get_last_pull()),
                    indent=2,
                ))
            else:
                _pipeline.print_freshness_stderr(_pipeline.get_last_pull())
                for ws in workspaces:
                    print(f"  {ws.get('slug') or ws.get('name')}: {ws.get('name')}")
        else:
            print(json.dumps(_pipeline.list_workspaces(), indent=2))
    elif args.command == "campaign-map":
        if args.campaign_map_cmd == "add":
            print(json.dumps(
                _pipeline.add_campaign_map_cli(
                    args.platform,
                    args.workspace,
                    campaign_platform_id=args.campaign_platform_id,
                    campaign_name=args.campaign_name,
                    match_strategy=args.match_strategy,
                    priority=args.priority,
                ),
                indent=2,
            ))
        elif args.campaign_map_cmd == "conflicts":
            result = _pipeline.campaign_map_conflicts_cli(platform=getattr(args, "platform", None))
            if getattr(args, "json", False):
                print(json.dumps(result, indent=2))
            else:
                conflicts = result["conflicts"]
                if not conflicts:
                    print("No shadow conflicts found.")
                else:
                    print(f"{len(conflicts)} shadow conflict(s):")
                    for c in conflicts:
                        print(
                            f"  name_exact '{c['campaign_name']}' -> "
                            f"{c.get('name_exact_workspace_slug') or c['name_exact_workspace_id']} "
                            f"(map id {c['name_exact_map_id']}) is shadowed by rule "
                            f"'{c['shadowing_rule_pattern']}' -> "
                            f"{c.get('shadowing_rule_workspace_slug') or c['shadowing_rule_workspace_id']}"
                        )
                    print("Clear a stale row with: campaign-map deactivate --id <name_exact map id>")
        elif args.campaign_map_cmd == "deactivate":
            print(json.dumps(_pipeline.deactivate_campaign_map_cli(args.id), indent=2))
        elif args.campaign_map_cmd == "reconcile":
            result = _pipeline.reconcile_campaign_routing_cli(
                platform=getattr(args, "platform", None),
                workspace_slug=getattr(args, "workspace", None),
                dry_run=getattr(args, "dry_run", False),
                limit=getattr(args, "limit", 0),
            )
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(_pipeline.list_campaign_maps(), indent=2))
    elif args.command == "quarantine":
        if args.quarantine_cmd == "skip":
            if getattr(args, "all", False):
                print(json.dumps(_pipeline.skip_quarantine_bulk(all_pending=True), indent=2))
            elif getattr(args, "reason", None):
                print(json.dumps(
                    _pipeline.skip_quarantine_bulk(
                        reason=args.reason,
                        platform=getattr(args, "platform", None),
                    ),
                    indent=2,
                ))
            elif getattr(args, "campaign_platform_id", None):
                print(json.dumps(
                    _pipeline.skip_quarantine_bulk(
                        campaign_platform_id=args.campaign_platform_id,
                        platform=getattr(args, "platform", None),
                    ),
                    indent=2,
                ))
            elif getattr(args, "id", None):
                print(json.dumps(_pipeline.skip_quarantine(args.id), indent=2))
            else:
                print("Error: quarantine skip requires --id, --campaign-platform-id, --reason, or --all")
                sys.exit(1)
        elif args.quarantine_cmd == "backfill-no-campaign":
            print(json.dumps(
                _pipeline.backfill_null_campaign_quarantine(
                    auto_skip=not getattr(args, "keep_pending", False),
                    quiet=False,
                ),
                indent=2,
            ))
        elif args.quarantine_cmd == "assign":
            print(json.dumps(_pipeline.assign_quarantine(args.id, args.workspace), indent=2))
        elif args.quarantine_cmd == "replay":
            print(json.dumps(_pipeline.replay_pending_quarantine(args.workspace, args.limit), indent=2))
        else:
            status = getattr(args, "status", "pending") or "pending"
            _pipeline.print_freshness_stderr(_pipeline.get_last_pull())
            if getattr(args, "json", False):
                raw_limit = getattr(args, "limit", 0) or 0
                limit = raw_limit if raw_limit > 0 else 1000000
                rows = _pipeline.list_quarantine(status=status, limit=limit)
                print(json.dumps(
                    _pipeline.attach_freshness({"items": rows}, last_pull=_pipeline.get_last_pull()),
                    indent=2,
                ))
            elif status == "pending":
                rows = _pipeline.list_quarantine(status=status, limit=50)
                if not rows:
                    print("No pending quarantined events.")
                else:
                    print(f"Pending quarantine ({len(rows)} row(s), showing up to 50):")
                    for row in rows:
                        campaign = row.get("campaign_name_raw") or row.get("campaign_platform_id") or "unknown"
                        print(
                            f"  {row.get('id')}  {row.get('source_platform') or '-'}  {campaign}"
                        )
                    print()
                    print(_pipeline.format_quarantine_campaign_summary(_pipeline.get_quarantine_campaign_summary()))
            else:
                print(json.dumps(_pipeline.list_quarantine(status=status, limit=50), indent=2))
    elif args.command == "personalize-set":
        if args.batch:
            items = json.loads(args.json_input or "[]")
            print(json.dumps(_pipeline.personalize_set_batch(items), indent=2))
        else:
            if not args.lead_id or not args.field or args.value is None:
                print("Error: --lead-id, --field, and --value are required (or use --batch --json)")
                sys.exit(1)
            print(json.dumps(_pipeline.personalize_set(
                args.lead_id, args.field, args.value, field_date=getattr(args, "date", None),
            ), indent=2))
    elif args.command == "personalize-get":
        result = _pipeline.personalize_get(args.lead_id, layer=getattr(args, "layer", "merged"))
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            if not result:
                print(f"No personalization for lead {args.lead_id}")
            else:
                for k, v in sorted(result.items()):
                    print(f"  {k}: {v}")
    elif args.command == "personalize-pending":
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        result = _pipeline.personalize_pending(fields, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"{len(result)} leads pending (fields: {', '.join(fields)})")
            for r in result:
                print(f"  [{r['id']}] {r['name'] or '?'} — {r['email'] or ''}")
    elif args.command == "personalize-status":
        result = _pipeline.personalize_status()
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"Total leads: {result['total_leads']}")
            print(f"Personalized: {result['personalized']}")
            print(f"Pending: {result['pending']}")
            print(f"Stale: {result['stale']}")
    elif args.command == "company-personalize-set":
        if args.batch:
            items = json.loads(args.json_input or "[]")
            print(json.dumps(_pipeline.company_personalize_set_batch(items), indent=2))
        else:
            if not args.field or args.value is None:
                print("Error: --field and --value required (plus --company-id, --domain, or --name)")
                sys.exit(1)
            if not any([args.company_id, args.domain, args.name]):
                print("Error: --company-id, --domain, or --name required")
                sys.exit(1)
            print(json.dumps(_pipeline.company_personalize_set(
                args.field, args.value,
                company_id=args.company_id, domain=args.domain, name=args.name,
                field_date=getattr(args, "date", None),
            ), indent=2))
    elif args.command == "company-personalize-get":
        result = _pipeline.company_personalize_get(
            company_id=args.company_id, domain=args.domain, name=args.name,
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            for k, v in sorted(result.items()):
                print(f"  {k}: {v}")
    elif args.command == "company-personalize-pending":
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        result = _pipeline.company_personalize_pending(fields, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"{len(result)} companies pending (fields: {', '.join(fields)})")
            for r in result:
                print(f"  [{r['company_id']}] {r['name']} — {r['domain'] or ''} ({r['lead_count']} leads)")
    elif args.command == "company-personalize-status":
        result = _pipeline.company_personalize_status()
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"Total companies: {result['total_companies']}")
            print(f"Personalized: {result['personalized']}")
            print(f"Pending: {result['pending']}")
            print(f"Stale: {result['stale']}")
    elif args.command == "personalize-clear":
        result = _pipeline.personalize_clear(
            lead_id=args.lead_id,
            field=args.field,
            clear_all=getattr(args, "clear_all", False),
        )
        print(json.dumps(result, indent=2))
    elif args.command == "outbox":
        import outbox as _outbox
        from db_conn import get_conn as _get_conn

        if args.backfill:
            result = _pipeline.backfill_outbox(dry_run=getattr(args, "dry_run", False))
            if getattr(args, "json", False):
                print(json.dumps(result, indent=2))
            else:
                verb = "Would queue" if result["dry_run"] else "Queued"
                for kind, n in result["queued"].items():
                    print(f"  {kind:16} {n:>9,}")
                print(f"{verb} {result['total']:,} entities for push.")
                if not result["dry_run"]:
                    print("Run `sync` to drain. Unchanged content is dropped by content hash.")
        else:
            c = _get_conn()
            counts = _outbox.count_dirty(c)
            shadow = c.execute("SELECT COUNT(*) AS n FROM sync_shadow").fetchone()["n"]
            c.close()
            if getattr(args, "json", False):
                print(json.dumps({"pending": counts, "sync_shadow": shadow}, indent=2))
            else:
                if not counts:
                    print("Outbox empty — nothing pending.")
                for k, n in sorted(counts.items()):
                    print(f"  {k:24} {n:>9,}")
                print(f"sync_shadow rows: {shadow:,}")

    elif args.command == "cleanup-junk-leads":
        import junk_cleanup

        dry_run = not getattr(args, "yes", False)
        if not dry_run:
            print(
                "!!! DESTRUCTIVE: about to delete every lead matching the junk "
                "predicate. Rows are copied to leads_junk_quarantine first.",
                file=sys.stderr,
            )
        result = junk_cleanup.cleanup_junk_leads(dry_run=dry_run, confirm=not dry_run)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            verb = "Would delete" if result["dry_run"] else "Deleted"
            print(f"{verb} {result['selected']:,} junk leads")
            if not result["dry_run"]:
                print(
                    f"  quarantined:        {result['quarantined']:,}\n"
                    f"  deleted:            {result['deleted']:,}\n"
                    f"  tombstones dropped: {result['tombstones_dropped']:,}"
                )
            print("\nTop original_source_detail values (up to 20):")
            for row in result["distribution"]["top_sources"]:
                sd = row["source_detail"]
                print(f"  {row['count']:>7,}  {sd}")
            print("\nBy month:")
            for row in result["distribution"]["by_month"]:
                print(f"  {row['month']}: {row['count']:>7,}")
            if result["dry_run"]:
                print("\n(dry-run) Re-run with --yes to actually delete.")

    elif args.command == "cleanup-rules":
        result = _pipeline.cleanup_campaign_rules(dry_run=getattr(args, "dry_run", False))
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            if result["dry_run"]:
                print(f"Would remove {result['found']} invalid rules")
            else:
                print(f"Removed {result['removed']} invalid mapping rules")
    else:
        if not _pipeline.db_exists():
            _pipeline.init_db()
        leads = _pipeline.get_pipeline()
        print(_pipeline.format_pipeline_table(leads))
        print()
        print(_pipeline.format_stats(_pipeline.get_stats()))

    if (
        args.command in ("workspace", "campaign-map", "quarantine", "pull", "enrich", "stage", "import-profiles", None)
        and not getattr(args, "json", False)
    ):
        try:
            hint = _pipeline.format_local_sync_hint(_pipeline.get_local_pending_counts())
            if hint:
                print(hint, file=sys.stderr)
        except Exception:
            pass
