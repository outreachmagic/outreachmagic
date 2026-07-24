"""Workspace-scoped write path shared by the CLI and the local dashboard.

A stage change or logged event is more than one table write: it must also
upsert the workspace_leads row and index the event into workspace_lead_events
so relay push (outbox triggers) and CRM sync see it. This module is the single
implementation of that bookkeeping; pipeline_cli's update-stage/log-event and
the dashboard's POST handlers are both thin wrappers around it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from db_conn import get_conn
from pipeline_tags import log_event, update_lead_stage
from pipeline_update import utc_now_for_storage
from workspace_routing import (
    DEFAULT_ORG_ID,
    WORKSPACE_ROUTING_MULTI,
    append_workspace_event,
    get_org_routing_config,
    resolve_workspace_identity,
    upsert_workspace_lead,
)


class WorkspaceResolutionError(ValueError):
    """Workspace missing (multi mode) or unknown; message is user-facing."""


# Default workspace_leads.status when an event arrives for a lead not yet in
# the workspace. Mirrors the stage each event type implies.
EVENT_STATUS_DEFAULTS = {
    "email_sent": "contacted",
    "linkedin_connect": "contacted",
    "linkedin_message": "contacted",
    "email_reply": "replied",
    "linkedin_reply": "replied",
    "linkedin_connection_accepted": "replied",
    "meeting_booked": "scheduled",
}


def _resolve_workspace_scope(workspace_slug: Optional[str], command: str) -> Optional[dict]:
    """Resolve slug -> workspace row, enforcing multi-mode's required slug."""
    conn = get_conn()
    try:
        routing_config = get_org_routing_config(conn, DEFAULT_ORG_ID)
        if routing_config.mode == WORKSPACE_ROUTING_MULTI:
            if not workspace_slug:
                raise WorkspaceResolutionError(
                    f"Multi-workspace mode: --workspace is required for {command}"
                )
            ws_row = resolve_workspace_identity(conn, workspace_slug)
            if not ws_row:
                raise WorkspaceResolutionError(f"workspace not found: {workspace_slug}")
            return ws_row
        if workspace_slug:
            return resolve_workspace_identity(conn, workspace_slug)
        return None
    finally:
        conn.close()


def change_stage_scoped(
    lead_id: int,
    stage: str,
    *,
    workspace_slug: Optional[str] = None,
    label: Optional[str] = None,
    sentiment: Optional[str] = None,
    next_action: Optional[str] = None,
) -> dict:
    """Update a lead's stage plus the workspace bookkeeping that must follow it.

    Raises WorkspaceResolutionError on missing/unknown workspace and ValueError
    (from update_lead_stage) on an invalid stage.
    """
    ws_row = _resolve_workspace_scope(workspace_slug, "update-stage")

    update_lead_stage(lead_id, stage, next_action)

    result = {"status": "updated", "id": lead_id, "stage": stage}
    if ws_row:
        conn = get_conn()
        ws_lead_id = upsert_workspace_lead(
            conn, DEFAULT_ORG_ID, ws_row["id"], lead_id, status=stage,
            current_status_label=label,
            current_status_sentiment=sentiment)
        stage_ts = datetime.now(timezone.utc).isoformat()
        update_sets = ["status = ?", "stage_entered_at = ?"]
        update_params = [stage, stage_ts]
        if sentiment:
            update_sets.append("current_status_sentiment = ?")
            update_params.append(sentiment)
        if label:
            update_sets.append("current_status_label = ?")
            update_params.append(label)
        update_params.append(ws_lead_id)
        conn.execute(
            f"UPDATE workspace_leads SET {', '.join(update_sets)} WHERE id = ?",
            update_params)
        conn.commit()
        conn.close()
        result["workspace"] = ws_row["slug"]

    event_metadata = {
        "lead_status_raw": stage,
        "lead_status_display": stage.replace("_", " "),
    }
    if sentiment:
        event_metadata["lead_status_sentiment"] = sentiment
    if label:
        event_metadata["lead_status_display"] = label
    status_event_id = log_event(
        lead_id=lead_id,
        event_type="lead_status_updated",
        direction="inbound",
        metadata=event_metadata,
    )
    if ws_row:
        # Index the status event into the workspace like relay-ingested status
        # events are — without this, agent-originated stage changes are
        # invisible to workspace-scoped analytics and CRM activity windows.
        conn = get_conn()
        append_workspace_event(
            conn, DEFAULT_ORG_ID, ws_row["id"], lead_id,
            event_id=status_event_id,
            event_type="lead_status_updated",
            event_at=utc_now_for_storage(),
            idempotency_key=(
                f"agent_stage_{lead_id}_{datetime.now(timezone.utc).isoformat()}"
            ))
        conn.commit()
        conn.close()

    return result


def log_event_scoped(
    lead_id: int,
    event_type: str,
    *,
    direction: str = "outbound",
    channel: str = "email",
    subject: Optional[str] = None,
    body: Optional[str] = None,
    metadata: Optional[dict] = None,
    workspace_slug: Optional[str] = None,
    idempotency_prefix: str = "agent_cli",
) -> dict:
    """Log an event and index it into the workspace.

    Raises WorkspaceResolutionError on missing/unknown workspace.
    """
    ws_row = _resolve_workspace_scope(workspace_slug, "log-event")

    logged_event_id = log_event(
        lead_id=lead_id, event_type=event_type, direction=direction,
        channel=channel, subject=subject, body_preview=body,
        metadata=metadata)

    result = {"status": "logged", "lead_id": lead_id}
    if ws_row:
        conn = get_conn()
        initial_status = EVENT_STATUS_DEFAULTS.get(event_type, "prospecting")
        upsert_workspace_lead(
            conn, DEFAULT_ORG_ID, ws_row["id"], lead_id,
            status=initial_status)
        idem_key = f"{idempotency_prefix}_{lead_id}_{event_type}_{datetime.now(timezone.utc).isoformat()}"
        # The subject/body/channel written just above by log_event are the
        # record; this row only indexes it into the workspace.
        append_workspace_event(
            conn, DEFAULT_ORG_ID, ws_row["id"], lead_id,
            event_id=logged_event_id,
            event_type=event_type,
            event_at=utc_now_for_storage(),
            idempotency_key=idem_key)
        conn.commit()
        conn.close()
        result["workspace"] = ws_row["slug"]
    return result
