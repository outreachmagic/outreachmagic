"""Shared constants for outreachmagic pipeline scripts."""

MAX_EVENT_BODY_STORAGE_CHARS = 65536
RELAY_PUSH_BATCH_SIZE = 50
RELAY_PUSH_TIMEOUT_SECONDS = 120
RELAY_PUSH_MAX_ATTEMPTS = 3
RELAY_PUSH_RETRY_BASE_SECONDS = 2

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

PLUSVIBE_REPLY_EVENTS = frozenset({
    "all_email_replies",
    "first_email_replies",
    "all_positive_replies",
})

PLUSVIBE_SENT_EVENTS = frozenset({"email_sent"})
PLUSVIBE_BOUNCE_EVENTS = frozenset({"bounced_email"})

AUTO_REPLY_LABELS = frozenset({
    "out_of_office",
    "ooo",
    "automatic_reply",
    "auto_reply",
})
