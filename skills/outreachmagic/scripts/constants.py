"""Shared constants for outreachmagic pipeline scripts."""

from platform_registry import (
    PLUSVIBE_BOUNCE_EVENTS,
    PLUSVIBE_REPLY_EVENTS,
    PLUSVIBE_SENT_EVENTS,
)

MAX_EVENT_BODY_STORAGE_CHARS = 65536
RELAY_PUSH_BATCH_SIZE = 200
RELAY_PUSH_MAX_BULK = 5000
RELAY_PUSH_EVENTS_BULK = 1500  # event_log backfill: smaller pages avoid D1 memory spikes
RELAY_PUSH_SNAPSHOT_BULK = 1000  # lead_core / lead_workspace snapshot pages
RELAY_PUSH_ROUTINE_MAX = 500
RELAY_PULL_PAGE_SIZE = 1000
RELAY_PULL_MAX = 5000  # legacy cap for ?limit= on relay; pull client never requests this for events
RELAY_PULL_EVENT_MAX = 1000
RELAY_PULL_SNAPSHOT_MAX = 1000  # match RELAY_PUSH_SNAPSHOT_BULK — 5k ingest/D1 spikes on pull
RELAY_PULL_COMPANY_MAX = 1000  # company_update ingest is heavy on the agent DB
RELAY_BULK_THRESHOLD = 2500
RELAY_PUSH_TIMEOUT_SECONDS = 120
RELAY_PUSH_MAX_ATTEMPTS = 3
RELAY_PUSH_RETRY_BASE_SECONDS = 2

BILLING_UPGRADE_URL = "https://app.outreachmagic.io/dashboard/billing"
USAGE_WARNING_PERCENT = 80

PIPELINE_STAGES = [
    "prospecting", "contacted", "replied", "interested",
    "proposal", "won", "lost",
]

STAGE_EMOJI = {
    "prospecting": "○", "contacted": "●", "replied": "↔",
    "interested": "★", "proposal": "■", "won": "✔", "lost": "✖",
}

ATTRIBUTE_INSIGHT_FIELDS = ("title", "industry", "headcount")

# Personal inboxes — skip domain-wide company sync (would touch unrelated leads)
SHARED_EMAIL_DOMAINS = frozenset({
    "126.com", "163.com", "aim.com", "alice.it", "aol.com", "ameritech.net", "att.net",
    "bellsouth.net", "bigpond.com", "btinternet.com", "charter.net", "comcast.net", "cox.net", "cs.com",
    "daum.net", "earthlink.net", "email.com", "excite.com", "facebook.com", "flash.net", "free.fr",
    "frontier.com", "gmail.com", "gmx.com", "gmx.net", "googlemail.com", "hanmail.net", "hey.com",
    "hotmail.com", "hushmail.com", "icloud.com", "inbox.com", "instagram.com", "interia.pl", "juno.com",
    "laposte.net", "libero.it", "linkedin.com", "live.com", "lycos.com", "mac.com", "mail.com",
    "mail.ru", "mailfence.com", "me.com", "mindspring.com", "msn.com", "naver.com", "netscape.net",
    "netzero.net", "ntlworld.com", "o2.pl", "onet.pl", "optonline.net", "orange.fr", "outlook.com",
    "pacbell.net", "pm.me", "prodigy.net", "proton.me", "protonmail.com", "qq.com", "rediffmail.com",
    "roadrunner.com", "rocketmail.com", "rogers.com", "runbox.com", "sbcglobal.net", "sfr.fr", "shaw.ca",
    "sina.com", "sky.com", "swbell.net", "sympatico.ca", "talktalk.net", "t-online.de", "tuta.io",
    "tutanota.com", "twc.com", "verizon.net", "virgilio.it", "virginmedia.com", "wanadoo.fr", "web.de",
    "windstream.net", "wp.pl", "yahoo.com", "yandex.com", "yandex.ru", "ymail.com",
})

PLUSVIBE_PLATFORMS = frozenset({"plusvibe"})

AUTO_REPLY_LABELS = frozenset({
    "out_of_office",
    "ooo",
    "automatic_reply",
    "auto_reply",
})
