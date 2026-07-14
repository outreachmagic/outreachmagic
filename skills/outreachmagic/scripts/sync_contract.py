"""Declarative map of which local tables feed which relay entity.

One source of truth, consumed by three things:
  * the outbox triggers (pipeline_migration.build_outbox_triggers)
  * the push loop, to resolve an outbox row back to an entity
  * Stage 6's coverage test, which asserts every column of every synced table
    is classified

Adding a synced table is one line here, not three hand-written triggers.

Why an outbox at all: dirtiness used to be *derived* at push time from
`leads.updated_at` (40.7% of which is older than `created_at`, so the cursor
lies), and the derivation missed writes that never touched the parent row --
`record_provider_attempt()` bumps no parent timestamp, so a provider attempt
never marked its lead dirty. Recording dirtiness at write time, in the
database, makes those functions correct without changing them: the child-table
write itself fires the trigger.
"""

# entity_type -> the relay snapshot kind it maps to. These are the kinds the
# relay's pull/push endpoints already speak.
ENTITY_TYPES = (
    "lead_core",
    "lead_workspace",
    "company",
    "sender_account",
    "sender_domain",
)

# The table that *owns* each entity. A DELETE here means the entity itself is
# gone (tombstone); a DELETE on any other mapped table is just a content change
# to a still-living parent (upsert).
OWNER_TABLES = {
    "leads": "lead_core",
    "workspace_leads": "lead_workspace",
    "companies": "company",
    "sender_accounts": "sender_account",
    "sender_domains": "sender_domain",
}

# table -> (entity_type, entity_id expression). `{row}` is substituted with NEW
# or OLD depending on the trigger. entity_id is TEXT: lead_workspace needs a
# composite, and a single type keeps the outbox PK simple.
#
# lead_email_verification and lead_provider_attempts are here even though the
# plan's target map names `lead_provider_observations` instead -- that table is
# Stage 7. Until it exists, these two are the tables whose writes must mark a
# lead dirty, and they are exactly the ones that were silently failing to.
# Stage 7 retargets these two entries at the observations log; nothing else moves.
SYNC_MAP = {
    # --- lead_core -------------------------------------------------------
    "leads":                         ("lead_core", "{row}.id"),
    "lead_identities":               ("lead_core", "{row}.lead_id"),
    "lead_personalization":          ("lead_core", "{row}.lead_id"),
    "lead_emails":                   ("lead_core", "{row}.lead_id"),
    "lead_email_verification":       ("lead_core", "{row}.lead_id"),
    "lead_provider_attempts":        ("lead_core", "{row}.lead_id"),
    # --- lead_workspace --------------------------------------------------
    "workspace_leads":               ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    "workspace_lead_tags":           ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    "workspace_lead_linkedin_status": ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    "crm_entity_map":                ("lead_workspace", "{row}.lead_id || ':' || {row}.workspace_id"),
    # --- company ---------------------------------------------------------
    "companies":                     ("company", "{row}.id"),
    "company_personalization":       ("company", "{row}.company_id"),
    # --- sender ----------------------------------------------------------
    # sender_domains is keyed by the domain itself, not a surrogate id.
    "sender_accounts":               ("sender_account", "{row}.id"),
    "sender_domains":                ("sender_domain", "{row}.domain"),
}


def entity_id_expr(table: str, row: str) -> str:
    """The SQL expression yielding an entity_id for `table`, bound to NEW/OLD."""
    return SYNC_MAP[table][1].format(row=row)


def entity_type_for(table: str) -> str:
    return SYNC_MAP[table][0]


def tables_for(entity_type: str) -> list[str]:
    return [t for t, (et, _) in SYNC_MAP.items() if et == entity_type]
