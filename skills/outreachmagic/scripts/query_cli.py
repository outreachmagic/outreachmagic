"""CLI handlers for pipeline.py query (read-only analytics)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import read_queries
from data_freshness import attach_freshness, print_freshness_stderr
from om_paths import resolve_project_path

try:
    import pipeline as _pipeline
except ImportError:
    _pipeline = None  # type: ignore

try:
    from campaign_stats import build_campaign_stats_payload
except ImportError:
    build_campaign_stats_payload = None  # type: ignore


def register_query_parser(sub) -> None:
    q = sub.add_parser(
        "query",
        help="Read-only SQL analytics (presets or SELECT)",
    )
    q.add_argument(
        "preset",
        nargs="?",
        choices=("engagement", "replies", "interested", "campaign-stats", "deliverability"),
        help="Blessed analytics preset (preferred)",
    )
    q.add_argument("--workspace", help="Workspace slug; campaign names use '<slug> | …'")
    q.add_argument(
        "--campaign-prefix",
        help="LIKE prefix for campaigns.name (overrides --workspace prefix)",
    )
    q.add_argument(
        "--since",
        help="Time filter: 48h, 7d, today, or YYYY-MM-DD",
    )
    q.add_argument(
        "--direction",
        default="inbound",
        help="For engagement preset (default: inbound)",
    )
    q.add_argument(
        "--event-types",
        help="Comma-separated event_type filter for engagement preset",
    )
    q.add_argument("--sql", help="Single SELECT/WITH statement (advanced)")
    q.add_argument(
        "--params",
        help='JSON array of SQL bind parameters (example: ["acme"])',
    )
    q.add_argument("--file", help="Read SQL from a file (outreachmagic/imports/ or absolute path)")
    q.add_argument("--limit", type=int, default=read_queries.DEFAULT_ROW_LIMIT,
                   help="Row cap for --sql/--file (0 = no cap)")
    q.add_argument("--json", action="store_true", help="JSON output for agents")
    q.add_argument("--quiet", action="store_true",
                   help="Suppress the stderr freshness line (machine consumers)")
    q.set_defaults(command="query")


def register_sql_parser(sub) -> None:
    """Shortcut for `query --sql "..."` — same handler, no separate implementation to drift."""
    p = sub.add_parser("sql", help='Shortcut for query --sql "..." (ad-hoc SELECT)')
    p.add_argument("sql", help="SELECT/WITH statement (quote it)")
    p.add_argument(
        "--params",
        help='JSON array of SQL bind parameters (example: ["acme"])',
    )
    p.add_argument("--limit", type=int, default=read_queries.DEFAULT_ROW_LIMIT,
                   help="Row cap (0 = no cap)")
    p.add_argument(
        "--workspace",
        help="Scope helper CTEs to a workspace: ws_leads, ws_campaigns, ws_events, "
             "ws_sender_accounts, ws_workspace (leads have no workspace_id column)",
    )
    p.add_argument("--json", action="store_true", help="JSON output for agents")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the stderr freshness line (machine consumers)")
    p.set_defaults(
        command="query", preset=None, campaign_prefix=None,
        since=None, direction="inbound", event_types=None, file=None,
    )


def register_schema_parser(sub) -> None:
    s = sub.add_parser("schema", help="List tables, dump one table's columns, or show the join graph")
    s.add_argument("table", nargs="?", help="Table name; omit to list all tables")
    s.add_argument(
        "--relations",
        action="store_true",
        help="Foreign-key graph and how each entity is scoped to a workspace",
    )
    s.add_argument("--json", action="store_true", help="JSON output for agents")
    s.set_defaults(command="schema")


def cmd_schema(args) -> None:
    try:
        table = getattr(args, "table", None)
        if getattr(args, "relations", False):
            result = read_queries.table_relations([table] if table else None)
        elif table:
            result = read_queries.table_schema(table)
        else:
            result = read_queries.list_tables()
    except ValueError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
        return

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return

    print(f"db_path: {result['db_path']}")
    if "relations" in result:
        print("\nworkspace scoping:")
        for entity, how in result["workspace_scoping"].items():
            print(f"  {entity}: {how}")
        print("\nforeign keys:")
        for rel in result["relations"]:
            print(f"  {rel['from_table']}.{rel['from_column']} -> "
                  f"{rel['to_table']}.{rel['to_column']}")
    elif "tables" in result:
        purposes = result.get("purposes") or {}
        for t in result["tables"]:
            print(f"  {t:<32} {purposes.get(t, '')}")
    else:
        print(f"table: {result['table']}")
        if result.get("purpose"):
            print(f"  ({result['purpose']})")
        if result.get("workspace_scoping"):
            print(f"  workspace scoping: {result['workspace_scoping']}")
        for col in result["columns"]:
            pk = " PK" if col["pk"] else ""
            nn = " NOT NULL" if col["notnull"] else ""
            default = f" DEFAULT {col['default']}" if col["default"] is not None else ""
            print(f"  {col['name']}  {col['type']}{nn}{default}{pk}")
        for fk in result.get("foreign_keys") or []:
            print(f"  FK {fk['column']} -> {fk['references']}")


def register_tag_summary_parser(sub) -> None:
    s = sub.add_parser(
        "tag-summary",
        help="Tag-group summary: research, email quality, segments, workspace status",
    )
    s.add_argument("--tag", required=True, help="Tag to summarize (e.g. eace26)")
    s.add_argument("--workspace", required=True, help="Workspace slug (tags are workspace-scoped)")
    s.add_argument("--json", action="store_true", help="JSON output for agents")
    s.set_defaults(command="tag-summary")


def cmd_tag_summary(args) -> None:
    try:
        result = read_queries.tag_summary(workspace=args.workspace, tag=args.tag)
    except ValueError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
        return

    last_pull = _pipeline.get_last_pull() if _pipeline else None
    print_freshness_stderr(last_pull)
    if getattr(args, "json", False):
        print(json.dumps(attach_freshness(result, last_pull=last_pull), indent=2))
    else:
        print(read_queries.format_tag_summary(result))


def _quiet(args) -> bool:
    """Freshness goes to stderr (never stdout) — this silences it entirely.

    The JSON body carries the same freshness fields, so a machine consumer
    loses nothing by turning the line off.
    """
    import os

    return bool(getattr(args, "quiet", False) or os.environ.get("OM_QUIET"))


def _parse_params(raw: Optional[str]) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--params must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("--params must be a JSON array")
    return parsed


def cmd_query(args) -> None:
    try:
        if args.preset == "engagement":
            types = None
            if getattr(args, "event_types", None):
                types = [t.strip() for t in args.event_types.split(",") if t.strip()]
            result = read_queries.engagement_by_campaign(
                workspace=getattr(args, "workspace", None),
                campaign_prefix=getattr(args, "campaign_prefix", None),
                since=getattr(args, "since", None),
                direction=getattr(args, "direction", None) or "inbound",
                event_types=types,
            )
        elif args.preset == "replies":
            result = read_queries.replies_by_campaign(
                workspace=getattr(args, "workspace", None),
                campaign_prefix=getattr(args, "campaign_prefix", None),
                since=getattr(args, "since", None),
            )
        elif args.preset == "interested":
            result = read_queries.interested_by_campaign(
                workspace=getattr(args, "workspace", None),
                campaign_prefix=getattr(args, "campaign_prefix", None),
                since=getattr(args, "since", None),
            )
        elif args.preset == "campaign-stats":
            if not getattr(args, "workspace", None):
                print(json.dumps({"error": "--workspace required for campaign-stats"}))
                sys.exit(1)
            if build_campaign_stats_payload is None:
                print(json.dumps({"error": "campaign_stats module not available"}))
                sys.exit(1)
            from db_conn import get_conn
            conn = get_conn()
            try:
                raw = build_campaign_stats_payload(
                    conn,
                    workspace=args.workspace,
                    since=getattr(args, "since", None),
                )
            finally:
                conn.close()
            result = {"preset": "campaign-stats", "data": raw}
        elif args.preset == "deliverability":
            if not getattr(args, "workspace", None):
                raise ValueError("--workspace required for deliverability")
            from db_conn import get_conn as _get_conn
            from campaign_stats import build_deliverability_payload

            conn = _get_conn()
            try:
                result = build_deliverability_payload(
                    conn,
                    workspace=args.workspace,
                    since=getattr(args, "since", None),
                )
            finally:
                conn.close()
        elif getattr(args, "sql", None):
            params = _parse_params(getattr(args, "params", None))
            result = read_queries.run_readonly_sql(
                args.sql,
                params=params,
                limit=getattr(args, "limit", read_queries.DEFAULT_ROW_LIMIT),
                workspace=getattr(args, "workspace", None),
            )
        elif getattr(args, "file", None):
            path = resolve_project_path(args.file, kind="input")
            sql = path.read_text(encoding="utf-8")
            params = _parse_params(getattr(args, "params", None))
            result = read_queries.run_readonly_sql(
                sql,
                params=params,
                limit=getattr(args, "limit", read_queries.DEFAULT_ROW_LIMIT),
                workspace=getattr(args, "workspace", None),
            )
            result["file"] = str(path)
        else:
            print(
                "Usage: pipeline.py query <engagement|replies|interested|campaign-stats|deliverability>"
                " --workspace <slug> --since 48h\n"
                "   or: pipeline.py query --sql 'SELECT ...' [--params '[...]'] --json",
                file=sys.stderr,
            )
            sys.exit(2)
            return
    except ValueError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
        return

    last_pull = _pipeline.get_last_pull() if _pipeline else None
    if not _quiet(args):
        print_freshness_stderr(last_pull)
    if isinstance(result, dict) and result.get("truncated"):
        # Silent truncation at the row cap is the second-most common cause of a
        # confidently wrong answer, after reading a stale pull.
        print(
            f"WARNING: hit the {result.get('limit')}-row limit — "
            "re-run with --limit 0 for all rows.",
            file=sys.stderr,
        )
    if getattr(args, "json", False):
        print(json.dumps(attach_freshness(result, last_pull=last_pull), indent=2))
    else:
        print(read_queries.format_query_result_text(result))
        if result.get("sql"):
            print("\nSQL:\n" + result["sql"])


def cmd_pipeline_view(args, *, table_formatter, json_enricher=None) -> None:
    """Shared handler for show and lead-table."""
    import pipeline as om

    auto_reply = None
    if getattr(args, "auto_reply", None) is not None:
        auto_reply = args.auto_reply == "true"
    try:
        leads = om.get_pipeline(
            stage_filter=args.stage,
            limit=args.limit,
            sentiment=getattr(args, "sentiment", None),
            auto_reply=auto_reply,
            lead_status=getattr(args, "lead_status", None),
            sort=getattr(args, "sort", "updated_at"),
            order=getattr(args, "order", "desc"),
            workspace=getattr(args, "workspace", None),
            since=getattr(args, "since", None),
            email=getattr(args, "email", None),
            name=getattr(args, "name", None),
        )
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    print_freshness_stderr(om.get_last_pull())
    if getattr(args, "json", False):
        enrich = json_enricher or om.enrich_lead_rows
        leads = enrich(leads, workspace=getattr(args, "workspace", None))
        payload = attach_freshness(leads, last_pull=om.get_last_pull())
        if isinstance(payload, dict) and "data" in payload:
            payload["leads"] = payload["data"]
        print(json.dumps(payload, indent=2))
    else:
        print(table_formatter(leads))
