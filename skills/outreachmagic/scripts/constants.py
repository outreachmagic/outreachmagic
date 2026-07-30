"""Shared constants for outreachmagic pipeline scripts."""

import re

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
RELAY_PULL_SNAPSHOT_HTTP_TIMEOUT = 120  # wall-clock budget per snapshot HTTP call
RELAY_PULL_HARD_TIMEOUT_BUFFER = 15  # thread pool budget above socket timeout
RELAY_BULK_THRESHOLD = 2500
RELAY_PUSH_TIMEOUT_SECONDS = 120
RELAY_PUSH_MAX_ATTEMPTS = 3
RELAY_PUSH_RETRY_BASE_SECONDS = 2

BILLING_UPGRADE_URL = "https://app.outreachmagic.io/settings/billing"
USAGE_WARNING_PERCENT = 80
USAGE_CRITICAL_PERCENT = 95

PIPELINE_STAGES = [
    "prospecting", "contacted", "replied", "interested",
    "scheduled", "won", "not_interested", "lost",
]

STAGE_EMOJI = {
    "prospecting": "○", "contacted": "●", "replied": "↔",
    "interested": "👍", "scheduled": "📅", "won": "✔",
    "not_interested": "✖", "lost": "🚫",
}

ATTRIBUTE_INSIGHT_FIELDS = ("title", "industry", "headcount")

# Phone numbers carry two orthogonal facts, kept in two columns so neither can
# be read as the other:
#   label  — what KIND of number it is (who picks up)
#   source — where we GOT it (which provider or import)
# "the Google Maps number" is a `main` labelled number with source
# `google_maps`; collapsing those into one string is how you end up unable to
# ask either question.
PHONE_LABELS = (
    "mobile", "direct", "main", "hq", "branch", "fax", "whatsapp", "other",
)
PHONE_SOURCES = (
    "google_maps", "apify", "serper", "apollo", "csv_import", "manual",
    "crm", "sequencer",
)
PHONE_OWNER_TYPES = ("lead", "company")

# What a PROSPECT company's known domain is for. Lives on company_identities,
# not sender_domains -- sender_domains is your own cold-email sending
# infrastructure, and rendering both under one "domains" heading is what made
# the two indistinguishable in the company pane.
#   primary       — the canonical identity; mirrors companies.domain
#   branch        — a division, region or acquired brand that is really them
#   email_finding — walk this one when searching for addresses
#   parked        — held but not in use; never guess addresses here
COMPANY_DOMAIN_PURPOSES = ("primary", "branch", "email_finding", "parked")

# What a lead row actually represents.
#   contact            — a real person (the default; every existing lead is one)
#   company_placeholder — a stand-in for a company with no known contact yet,
#                        typically a Google Maps / Apify business scrape. Real
#                        for tagging, personalization and company facts; NOT a
#                        person, so it is excluded from sending, enrichment
#                        targeting and CRM sync until a real contact replaces it.
#   public_email       — a generic company mailbox (info@, hello@, careers@).
#                        A real, verifiable, sendable address that belongs to
#                        an organisation rather than a person. It is its own
#                        row instead of a second address on somebody's contact
#                        because lead_emails aliases into lead_identities, and
#                        a shared address aliased that way collapses two real
#                        people into one lead the first time it is reused.
RECORD_TYPE_CONTACT = "contact"
RECORD_TYPE_COMPANY_PLACEHOLDER = "company_placeholder"
RECORD_TYPE_PUBLIC_EMAIL = "public_email"
LEAD_RECORD_TYPES = (
    RECORD_TYPE_CONTACT, RECORD_TYPE_COMPANY_PLACEHOLDER, RECORD_TYPE_PUBLIC_EMAIL,
)

# Local parts that name an organisation rather than a person. Shared with the
# Serper extractor (serper_candidates.GENERIC_LOCAL_PARTS) so "is this a public
# mailbox" has one answer.
GENERIC_EMAIL_LOCAL_PARTS = frozenset({
    "admin", "administration", "careers", "contact", "contactus", "enquiries",
    "enquiry", "general", "help", "hello", "hi", "hr", "info", "information",
    "jobs", "mail", "media", "office", "press", "recruitment", "sales",
    "support", "team", "welcome",
})

# Tokens that mark a name as a business rather than a person.
#
# Auto-detection requires one of these to be PRESENT, rather than trying to
# recognise a person's name and excluding it. The asymmetry is deliberate: a
# missed stub costs nothing (it just sits in the contacts list), while a real
# person misclassified as a placeholder is silently dropped from sending,
# enrichment targeting and CRM sync. Sole traders whose company is their own
# name -- "Marisol Okonkwo", "Petra Lindqvist", to use invented examples --
# match name == company exactly like a Google Maps business does, and three
# real ones were caught by an earlier version of this rule that had no such
# requirement.
COMPANY_NAME_TOKENS = frozenset({
    "inc", "incorporated", "llc", "l.l.c", "ltd", "limited", "plc", "corp",
    "corporation", "co", "company", "group", "holdings", "partners", "associates",
    "enterprises", "ventures", "industries", "international", "worldwide",
    "auto", "autos", "motors", "motor", "automotive", "cars", "car", "truck",
    "trucks", "vehicles", "vehicle", "dealership", "dealers", "dealer",
    "sales", "service", "services", "center", "centre", "shop", "garage",
    "solutions", "systems", "technologies", "consulting", "agency", "studio",
    "works", "supply", "supplies", "equipment", "rental", "rentals", "leasing",
    "brothers", "bros", "sons", "family", "management", "mobility", "imports",
    "used", "pre-owned", "wholesale", "outlet", "superstore", "mart",
    # Marques that appear as the whole dealership name
    "hyundai", "ford", "toyota", "honda", "kia", "nissan", "chevrolet", "chevy",
    "bmw", "audi", "mazda", "subaru", "jeep", "dodge", "ram", "gmc", "buick",
    "cadillac", "lexus", "acura", "infiniti", "volvo", "volkswagen", "vw",
    "mercedes", "porsche", "tesla", "chrysler", "mitsubishi", "genesis",
})


def looks_like_company_name(value: str | None) -> bool:
    """Does this name carry a positive signal that it is a business?

    A corporate token, an ampersand, or a digit. Not a person-name detector --
    see COMPANY_NAME_TOKENS for why the test runs in this direction.
    """
    text = str(value or "").strip().lower()
    if not text:
        return False
    if "&" in text or any(ch.isdigit() for ch in text):
        return True
    tokens = {t.strip(".,'\"()") for t in text.replace("/", " ").replace("-", " ").split()}
    return bool(tokens & COMPANY_NAME_TOKENS)

# Personal inboxes — skip domain-wide company sync (would touch unrelated leads)
SHARED_EMAIL_DOMAINS = frozenset({
    "126.com", "163.com", "aim.com", "alice.it", "aol.com", "ameritech.net", "att.net",
    "bellsouth.net", "bigpond.com", "btinternet.com", "charter.net", "comcast.net", "cox.net", "cs.com",
    "daum.net", "earthlink.net", "email.com", "excite.com", "facebook.com", "flash.net", "free.fr",
    "frontier.com", "gmail.com", "gmx.com", "gmx.net", "googlemail.com", "hanmail.net", "hey.com",
    "hotmail.com", "hushmail.com", "icloud.com", "inbox.com", "instagram.com", "interia.pl", "juno.com",
    "laposte.net", "libero.it", "linkedin.com", "linktr.ee", "linktree.com", "live.com", "lycos.com", "mac.com", "mail.com",
    "mail.ru", "mailfence.com", "me.com", "mindspring.com", "msn.com", "naver.com", "netscape.net",
    "netzero.net", "ntlworld.com", "o2.pl", "onet.pl", "optonline.net", "orange.fr", "outlook.com",
    "pacbell.net", "pm.me", "prodigy.net", "proton.me", "protonmail.com", "qq.com", "rediffmail.com",
    "roadrunner.com", "rocketmail.com", "rogers.com", "runbox.com", "sbcglobal.net", "sfr.fr", "shaw.ca",
    "sina.com", "sky.com", "swbell.net", "sympatico.ca", "talktalk.net", "t-online.de", "tuta.io",
    "tutanota.com", "twc.com", "verizon.net", "virgilio.it", "virginmedia.com", "wanadoo.fr", "web.de",
    "windstream.net", "wp.pl", "yahoo.com", "yandex.com", "yandex.ru", "ymail.com",
})

# "Company" text that describes employment status, not a real company — must
# never be used as a company-matching key (name or domain). Matched as exact
# whole-value equality after squashing (lowercase, non-alphanumeric stripped),
# never substring/prefix/regex — real companies like "Independent Sector",
# "Independent Publishers Group", "Self Help Africa", and "Self-Help Federal
# Credit Union" legitimately start with these words and must not be caught.
NON_COMPANY_NAMES = frozenset({
    "self", "selfemployed", "selfemployeed", "selfemployedcontractor", "selfemployedconsultant",
    "freelance", "freelancer", "freelanceselfemployed", "freelancecontract", "freelancecontracted",
    "contractfreelance", "consultantfreelance", "consultantcontractor", "contract", "contractwork",
    "independent", "independentconsultant", "independentcontractor",
    "independentcontracter", "indepentcontractor", "indpendantcontractor",
    "individual", "na", "none", "notapplicable", "unemployed", "unemployedlookingforwork",
    "retired", "retiree",
    # Job-seeking / employment-status text, as it actually appears in LinkedIn
    # Sales Navigator exports. Every entry below is an exact squashed value
    # observed in a real export -- and exact-match is what makes them safe:
    # "Kingsbridge Retirement Community", "Compass Self Storage" and "Wells
    # Fargo Private Bank" are real companies in the same dataset and squash to
    # entirely different keys.
    "recentlyretired", "currentlyretired", "nowretired",
    "seekingemployment", "seekingopportunities", "seekingnewopportunity",
    "seekingnewopportunities", "currentlyseekingnewrole", "currentlyseekingemployment",
    "activelyseekingemployment", "activelyseekingpermanentemployment",
    "activelyseeking", "activelylooking", "opentoopportunities", "opentowork",
    "opentonewopportunities", "jobseeker", "jobseeking", "betweenjobs", "betweenroles",
    "careerbreak", "sabbatical", "notemployed", "nocompany", "nonecurrently",
    # Placeholder employers -- a real name was withheld, so there is nothing to
    # search for. Note "confidential" alone is included but multi-word variants
    # must be listed explicitly; squashing never strips words.
    "confidential", "confidentialemployer", "confidentialcompany",
    "confidentialentity", "confidentialclient", "undisclosed", "undisclosedcompany",
    "privatefamilyoffice", "privatehouseholds", "privateclient",
    "various", "variouscompanies", "variousclients", "variousstartups",
    "multiplecompanies", "multipleclients",
    # Non-employment life status
    "homemaker", "stayathomeparent", "stayathomemom", "stayathomedad",
    "student", "fulltimestudent", "graduatestudent",
})


def squash_company_name(value: str | None) -> str:
    """Lowercase, strip everything but letters/digits — collapses separator
    variants ('Self-Employed' / 'self employed' / 'Self Employed.') onto one
    key for exact NON_COMPANY_NAMES membership checks."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def is_non_company_name(value: str | None) -> bool:
    squashed = squash_company_name(value)
    return bool(squashed) and squashed in NON_COMPANY_NAMES


# Shared SELECT fragment for lead+company joins (read path).
_SHARED_DOMAIN_SQL_LIST = ", ".join(f"'{d}'" for d in sorted(SHARED_EMAIL_DOMAINS))

# One domain from company_identities for this lead's company, or NULL.
#
# companies.domain is globally UNIQUE, so a brand portfolio (22 Hilton
# properties on hilton.com) can only ever record that domain against ONE of its
# companies -- the rest carry it in company_identities, whose key includes
# company_id precisely so they can. Reading only companies.domain therefore made
# every non-first property of a portfolio look domainless to the email finder.
#
# Precedence mirrors rank_company_domains() so the bulk read path and the
# per-company ranker can't give different answers for the same company:
# explicit `primary` first, then `email_finding`, then `branch`, then unlabelled
# -- `parked` means "known but do not use" and is excluded outright.
# Index-backed by idx_company_identities_company_type (company_id, identity_type).
_COMPANY_IDENTITY_DOMAIN_SQL = """(
        SELECT ci.identity_value_normalized
        FROM company_identities ci
        WHERE ci.company_id = l.company_id
          AND ci.identity_type = 'domain'
          AND TRIM(COALESCE(ci.identity_value_normalized, '')) != ''
          AND COALESCE(ci.purpose, '') != 'parked'
        ORDER BY CASE COALESCE(ci.purpose, '')
                     WHEN 'primary' THEN 0
                     WHEN 'email_finding' THEN 1
                     WHEN 'branch' THEN 2
                     ELSE 3
                 END,
                 COALESCE(ci.verified_mx, 0) DESC,
                 ci.id
        LIMIT 1
    )"""

COMPANY_DOMAIN_SQL = f"""COALESCE(
    NULLIF(TRIM(co.domain), ''),
    {_COMPANY_IDENTITY_DOMAIN_SQL},
    CASE
        WHEN l.email_domain IS NOT NULL AND TRIM(l.email_domain) != ''
             AND LOWER(l.email_domain) NOT IN ({_SHARED_DOMAIN_SQL_LIST}) THEN l.email_domain
    END
) AS company_domain"""


def require_professional_domain_clause() -> tuple[str, tuple[str, ...]]:
    """SQL AND-clause + bind values for leads with a professional company domain.

    Must stay in lockstep with COMPANY_DOMAIN_SQL: this decides which leads are
    candidates at all, so a domain source the SELECT can resolve but this clause
    filters out is a lead that silently never gets found.
    """
    placeholders = ",".join("?" * len(SHARED_EMAIL_DOMAINS))
    clause = f"""AND (
        (co.domain IS NOT NULL AND TRIM(co.domain) != '')
        OR {_COMPANY_IDENTITY_DOMAIN_SQL} IS NOT NULL
        OR (
            l.email_domain IS NOT NULL AND TRIM(l.email_domain) != ''
            AND LOWER(l.email_domain) NOT IN ({placeholders})
        )
    )"""
    return clause, tuple(SHARED_EMAIL_DOMAINS)


PLUSVIBE_PLATFORMS = frozenset({"plusvibe"})

AUTO_REPLY_LABELS = frozenset({
    "out_of_office",
    "ooo",
    "automatic_reply",
    "auto_reply",
})


# Booking tools publish an event *type* ("30 Minute Meeting", "Discovery Call")
# with every scheduled-meeting webhook. Ingest stores that as the event's
# campaign, which is right for the event and wrong for attribution: a Calendly
# event type is a slot on your calendar, not the outbound campaign that put the
# meeting there. Attribution excludes these so "last known campaign" keeps
# naming a campaign you can actually judge.
SCHEDULING_PLATFORMS = frozenset({
    "calendly",
    "cal.com",
    "calcom",
    "savvycal",
    "chilipiper",
    "chili_piper",
    "hubspot_meetings",
    "acuity",
})

# The same list as a SQL literal, for the attribution query.
SCHEDULING_PLATFORMS_SQL_LIST = ", ".join(
    f"'{p}'" for p in sorted(SCHEDULING_PLATFORMS))
