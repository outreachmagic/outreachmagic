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


def register_query_parser(sub) -> None:
    q = sub.add_parser(
        "query",
        help="Read-only SQL analytics (presets or SELECT)",
    )
    q.add_argument(
        "preset",
        nargs="?",
        choices=("engagement", "replies", "interested"),
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
    q.add_argument("--file", help="Read SQL from a file (workspace input/ or absolute path)")
    q.add_argument("--limit", type=int, default=read_queries.DEFAULT_ROW_LIMIT)
    q.add_argument("--json", action="store_true", help="JSON output for agents")
    q.set_defaults(command="query")


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
        elif getattr(args, "sql", None):
            params = _parse_params(getattr(args, "params", None))
            result = read_queries.run_readonly_sql(
                args.sql,
                params=params,
                limit=getattr(args, "limit", read_queries.DEFAULT_ROW_LIMIT),
            )
        elif getattr(args, "file", None):
            path = resolve_project_path(args.file, kind="input")
            sql = path.read_text(encoding="utf-8")
            params = _parse_params(getattr(args, "params", None))
            result = read_queries.run_readonly_sql(
                sql,
                params=params,
                limit=getattr(args, "limit", read_queries.DEFAULT_ROW_LIMIT),
            )
            result["file"] = str(path)
        else:
            print(
                "Usage: pipeline.py query <engagement|replies|interested> --workspace <slug> --since 48h\n"
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
    print_freshness_stderr(last_pull)
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
        print(json.dumps(attach_freshness(leads, last_pull=om.get_last_pull()), indent=2))
    else:
        print(table_formatter(leads))
