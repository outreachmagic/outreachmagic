"""Natural-language user messages for agent-driven installs (no raw CLI commands)."""

from __future__ import annotations

MSG_LOGIN = "Ask Outreach Magic to log in"
MSG_UPDATE = "Ask Outreach Magic to update"
MSG_FULL_SYNC = "Ask Outreach Magic to run a full sync"
MSG_RESTORE_BACKUP = "Ask Outreach Magic to restore from backup"
MSG_RESTORE_LATEST_YES = "Ask Outreach Magic to restore from the latest backup (confirm yes)"
MSG_PULL_SKIP_ROUTING = "Ask Outreach Magic to pull events while skipping routing sync"
MSG_PULL_SKIP_SNAPSHOTS = "Ask Outreach Magic to pull events without snapshots"
MSG_PULL_PROBE = "Ask Outreach Magic to probe relay connectivity"
MSG_SYNC = "Ask Outreach Magic to sync"
MSG_CONNECT = "Ask Outreach Magic to connect"

MSG_ACCOUNT_PENDING = (
    "Your account is pending approval. You'll receive an email when it's ready. "
    "Once approved, ask Outreach Magic to connect and it will pick up from here."
)

MSG_ACCOUNT_PENDING_SHORT = (
    "Account approval is pending — no action needed yet. "
    "Once approved, ask Outreach Magic to connect."
)

MSG_NO_AGENT_KEY = f"No agent key configured. {MSG_LOGIN}."


def metered_usage_label(plan: str = "") -> str:
    """Customer-facing usage meter label (see outreachmagic-brand/product/pricing.md)."""
    if str(plan or "").strip().lower() in ("", "free"):
        return "Webhook events"
    return "Webhook and sync events"


def no_campaign_event_message(*, platform: str = "relay") -> str:
    """Instructions when a webhook event has no campaign metadata."""
    return "\n".join([
        f"A webhook event from {platform} has no campaign id or name, so it cannot be "
        "attributed to a workspace.",
        "It was added to the quarantine skip list — no action needed unless you want to investigate.",
        "",
        "To review event details (sender, timestamp, preview text), ask Outreach Magic to "
        "show the event history for the lead.",
        "",
        "If you prefer to clear any remaining pending no-campaign items from the queue, "
        f"ask Outreach Magic to skip quarantined no-campaign events, then {MSG_SYNC.lower()}.",
    ])


def unmapped_campaign_message(*, label: str, platform: str) -> str:
    """Instructions when multi-workspace routing cannot resolve a campaign."""
    return "\n".join([
        f"Campaign '{label}' ({platform}) is not mapped to a workspace.",
        "This event was not processed and is waiting in the quarantine queue.",
        "",
        "To fix this, ask Outreach Magic to:",
        "1. Create a workspace (if needed)",
        f"2. Map this campaign to that workspace (campaign id or name: {label})",
        "3. Replay quarantined events, or skip junk items you do not need",
        f"4. {MSG_SYNC} so resolutions persist across machines",
    ])


def quarantine_summary_steps() -> list[str]:
    return [
        "",
        "Next steps:",
        "Ask Outreach Magic to map unmapped campaigns to a workspace, then replay quarantined events.",
        "Or skip items you do not need (by campaign or reason), then sync.",
    ]
