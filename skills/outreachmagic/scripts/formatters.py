"""CLI table and timeline formatters for pipeline output."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from constants import STAGE_EMOJI


def format_pipeline_table(leads):
    if not leads:
        return "No leads in pipeline. Time to do some outreach!"
    lines = [f"{'Lead':<28} {'Company':<20} {'Stage':<14} {'Last':<12} {'Next Action'}", "-" * 95]
    for lead in leads:
        name = (lead["name"] or "")[:26]
        company = (lead.get("company_display") or lead.get("company") or "")[:18]
        stage = lead["stage"] or "?"
        emoji = STAGE_EMOJI.get(stage, "  ")
        last = lead.get("last_contact_at") or lead.get("last_event_at") or ""
        if last:
            try:
                dt = datetime.fromisoformat(last)
                now = datetime.now(timezone.utc)
                delta = now - dt.replace(tzinfo=timezone.utc)
                if delta.days:
                    last = f"{delta.days}d ago"
                elif delta.seconds >= 3600:
                    last = f"{delta.seconds // 3600}h ago"
                else:
                    last = f"{delta.seconds // 60}m ago"
            except (ValueError, TypeError):
                last = last[:10]
        next_action = (lead.get("next_action") or "")[:30]
        status_bits = []
        if lead.get("current_sentiment"):
            status_bits.append(lead["current_sentiment"])
        if lead.get("current_is_auto_reply"):
            status_bits.append("auto")
        status_suffix = f" [{','.join(status_bits)}]" if status_bits else ""
        lines.append(
            f"{name:<28} {company:<20} {emoji} {stage:<12} {last:<12} {next_action}{status_suffix}"
        )
    return "\n".join(lines)


def format_lead_table(leads, markdown: bool = False):
    """Render stable lead rows from canonical show/get_pipeline fields."""
    if not leads:
        return "No leads found."

    headers = ["Lead", "Company", "Stage", "Last Event", "Last Event At", "Events", "Notes"]
    rows = []
    for lead in leads:
        rows.append(
            [
                (lead.get("name") or "—").strip() or "—",
                (lead.get("company_display") or lead.get("company") or "—").strip() or "—",
                (lead.get("stage") or "—").strip() or "—",
                (lead.get("last_event") or "—").strip() or "—",
                (lead.get("last_event_at") or "—").strip() or "—",
                str(int(lead.get("event_count") or 0)),
                (lead.get("notes") or "—").strip() or "—",
            ]
        )

    if markdown:
        lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
        ]
        for row in rows:
            safe_cells = [str(cell).replace("\n", " ").replace("|", "\\|") for cell in row]
            lines.append("| " + " | ".join(safe_cells) + " |")
        return "\n".join(lines)

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))
    lines = [
        "  ".join(f"{h:<{widths[i]}}" for i, h in enumerate(headers)),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(f"{str(cell):<{widths[i]}}" for i, cell in enumerate(row)))
    return "\n".join(lines)


def format_campaign_stats(stats, include_header=False):
    campaigns = stats.get("campaigns") or []
    no_campaign = stats.get("no_campaign_events", 0)
    if not campaigns and not no_campaign:
        return []
    lines = []
    if include_header:
        lines.append("Campaigns:")
    workspace_w = max((len(c.get("workspace") or "-") for c in campaigns), default=9)
    workspace_w = max(workspace_w, len("Workspace"), 9)
    name_w = max((len(c.get("campaign_name") or c.get("campaign") or "") for c in campaigns), default=12)
    name_w = max(name_w, len("(no campaign)"), len("Campaign"), 12)
    lines.append(
        f"{'Workspace':<{workspace_w}}  {'Campaign':<{name_w}}  {'Events':>7}  {'Leads':>6}  {'Interested':>10}"
    )
    lines.append("-" * (workspace_w + name_w + 31))
    for row in campaigns:
        workspace = row.get("workspace") or "-"
        campaign_name = row.get("campaign_name") or row.get("campaign") or ""
        interested = int(row.get("interested_count") or 0)
        lines.append(
            f"{workspace:<{workspace_w}}  {campaign_name:<{name_w}}  "
            f"{row['event_count']:>7}  {row['lead_count']:>6}  {interested:>10}"
        )
    if no_campaign:
        lines.append(f"{'-':<{workspace_w}}  {'(no campaign)':<{name_w}}  {no_campaign:>7}  {'-':>6}  {'-':>10}")
    return lines


def format_stats(stats):
    lines = [
        f"Pipeline: {stats['active_pipeline']} active | {stats['won']} won | "
        f"{stats['lost']} lost | {stats['total_leads']} total leads",
        f"Events: {stats['total_events']} total | {stats['events_7d']} in last 7 days",
        f"Replies: {stats.get('reply_events', 0)} events across {stats.get('replied_leads', 0)} leads "
        f"(stage 'replied' currently {stats.get('stages', {}).get('replied', 0)})",
        "Breakdown: " + ", ".join(f"{s}={c}" for s, c in stats.get("stages", {}).items()),
    ]
    campaign_lines = format_campaign_stats(stats, include_header=True)
    if campaign_lines:
        lines.append("")
        lines.extend(campaign_lines)
    return "\n".join(lines)


def format_event_timeline(lead, events):
    """Format a lead's event history as a timeline."""
    emoji = STAGE_EMOJI.get(lead.get("stage", ""), "")
    lines = [
        f"Lead:    {lead['name']} ({emoji} {lead.get('stage', '?')})",
        f"Title:   {lead.get('title') or '—'}",
        f"Email:   {lead.get('email') or '—'}",
        f"LinkedIn:{lead.get('linkedin_url') or lead.get('linkedin') or '—'}",
        f"Company: {lead.get('company_display') or lead.get('company') or '—'}",
    ]

    # Add Status / Verification
    status_parts = []
    if lead.get("lead_status"): status_parts.append(lead["lead_status"])
    if lead.get("lead_sentiment"): status_parts.append(f"[{lead['lead_sentiment']}]")
    status_str = " ".join(status_parts) if status_parts else "—"
    
    verify_str = lead.get("email_verification_status") or "—"
    
    lines.append(f"Status:  {status_str:<30} | Verify: {verify_str}")

    # Add HQ / Tags if available
    hq = []
    if lead.get("hq_city"): hq.append(lead["hq_city"])
    if lead.get("hq_state"): hq.append(lead["hq_state"])
    if lead.get("hq_country"): hq.append(lead["hq_country"])
    hq_str = ", ".join(hq) if hq else "—"

    tags = ", ".join(lead.get("tags") or []) or "—"
    
    lines.extend([
        f"HQ:      {hq_str}",
        f"Industry:{lead.get('industry') or '—'}  |  Headcount: {lead.get('headcount') or '—'}",
        f"Tags:    {tags}",
        f"Notes:   {lead.get('notes') or '—'}",
    ])

    # Add LinkedIn Connection Status
    li_status = lead.get("linkedin_status")
    if li_status:
        for s in li_status:
            if s.get("is_connected"):
                lines.append(f"Connect: Connected to {s['sender_profile']}")
            elif s.get("is_request_pending"):
                lines.append(f"Connect: Request pending from {s['sender_profile']}")

    # Add Activity Summary if available
    if "email_sent_count" in lead or "linkedin_sent_count" in lead:
        sent = []
        if lead.get("email_sent_count"): sent.append(f"{lead['email_sent_count']} emails")
        if lead.get("linkedin_sent_count"): sent.append(f"{lead['linkedin_sent_count']} linkedin")
        sent_str = " + ".join(sent) if sent else "0 messages"
        replies = lead.get("total_replies_count") or 0
        last_contact = lead.get("last_contacted_at") or lead.get("last_contact_at") or "—"
        lines.append(f"Activity: {sent_str} | {replies} replies | Last: {last_contact}")

    # Add personalization
    pers = lead.get("personalization") or {}
    if pers:
        pers_clean = {k: v for k, v in pers.items() if v}
        if pers_clean:
            lines.append(f"Vars:    {json.dumps(pers_clean)}")

    lines.append("")
    if not events:
        lines.append("No events recorded yet.")
        return "\n".join(lines)

    lines.append(f"{'#':<4} {'When':<20} {'Event':<32} {'Details'}")
    lines.append("-" * 95)
    for i, e in enumerate(events, 1):
        created = e.get("created_at", "")
        try:
            dt = datetime.fromisoformat(created)
            now = datetime.now(timezone.utc)
            delta = now - dt.replace(tzinfo=timezone.utc)
            if delta.days:
                when = f"{delta.days}d ago"
            elif delta.seconds >= 3600:
                when = f"{delta.seconds // 3600}h ago"
            elif delta.seconds >= 60:
                when = f"{delta.seconds // 60}m ago"
            else:
                when = "just now"
        except (ValueError, TypeError):
            when = created[:16]

        direction = "←" if e.get("direction") == "inbound" else "→"
        evt = f"{direction} {e.get('event_type', '?')}"
        details = e.get("body_preview") or e.get("subject") or ""
        try:
            meta = json.loads(e.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        status_note = meta.get("lead_status_sentiment") or meta.get("lead_status_raw")
        if meta.get("is_auto_reply"):
            status_note = (status_note or "") + " auto_reply"
        if status_note:
            details = f"{status_note}: {details}" if details else str(status_note)
        if len(details) > 45:
            details = details[:42] + "..."
        lines.append(f"{i:<4} {when:<20} {evt:<32} {details}")

    return "\n".join(lines)


def format_copy_insights(insights: dict) -> str:
    counts = insights.get("counts") or {}
    best = insights.get("best_template")
    lines = [
        f"Positive leads: {counts.get('positive_leads', 0)}",
        f"Positive leads with copy captured: {counts.get('positive_with_copy', 0)}",
        f"Templates seen: {counts.get('templates_seen', 0)}",
        "",
        "Positive lead copy (full subject + body):",
        "-" * 95,
    ]
    for row in insights.get("positive_leads_copy") or []:
        lines.append(f"Lead #{row['lead_id']} — {row.get('lead_name') or 'Unknown'}")
        lines.append(f"Subject: {row.get('subject') or '—'}")
        lines.append("Body:")
        lines.append(row.get("body") or "—")
        lines.append("")

    lines.append("Template performance (first outbound email per lead):")
    lines.append("-" * 95)
    for t in (insights.get("templates_ranked") or [])[:10]:
        rate = round(100 * float(t.get("positive_rate") or 0), 1)
        lines.append(
            f"[{t['template_id']}] positives={t['positive_leads']}/{t['total_leads']} ({rate}%)"
        )
        lines.append(f"Subject template: {t.get('subject_template') or '—'}")
        lines.append("")

    if best:
        rate = round(100 * float(best.get("positive_rate") or 0), 1)
        lines.append("Best working template:")
        lines.append(f"- ID: {best['template_id']}")
        lines.append(
            f"- Performance: {best['positive_leads']}/{best['total_leads']} positive leads ({rate}%)"
        )
        lines.append(f"- Subject: {best.get('subject_template') or '—'}")
        lines.append("Body:")
        lines.append(best.get("body_template") or "—")

    return "\n".join(lines)


def format_segment_insights(insights: dict) -> str:
    counts = insights.get("counts") or {}
    lines = [
        f"Sent leads (at least one outbound email): {counts.get('sent_leads', 0)}",
        f"Positive leads matching filter: {counts.get('positive_leads_matching_filter', 0)}",
        f"Positive leads with sent email: {counts.get('positive_leads_with_sent_email', 0)}",
        "",
    ]

    insights_by_field = insights.get("insights_by_field") or {}
    for field in insights.get("filter", {}).get("fields") or []:
        rows = insights_by_field.get(field) or []
        lines.append(f"Best converting {field} values:")
        lines.append("-" * 95)
        if not rows:
            lines.append("No rows met your min-sent threshold.")
            lines.append("")
            continue
        for row in rows:
            rate = round(100 * float(row.get("conversion_rate") or 0), 1)
            lines.append(
                f"{row.get('value') or '—'}: {row.get('positive_leads', 0)}/{row.get('sent_leads', 0)} positive ({rate}%)"
            )
        lines.append("")

    titles = insights.get("recommended_job_titles") or []
    if titles:
        lines.append("Recommended job titles to source next:")
        lines.append("-" * 95)
        for title in titles[:10]:
            lines.append(f"- {title}")

    return "\n".join(lines).rstrip()
