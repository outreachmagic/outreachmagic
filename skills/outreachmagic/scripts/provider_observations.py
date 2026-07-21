"""Unified append-only log of every real provider observation for a lead.

Stage 7 of the round-trip fix: replaces two mutable, upsert-per-key tables --
`lead_provider_attempts` (did we try calling a provider? what came back?) and
`lead_email_verification` (what did a verification check/bounce say?) -- with
one append-only log, one row per real API call or webhook bounce. Both old
names survive as read-only VIEWs (see pipeline_migration.py) projecting
"latest row per provider", so every existing reader keeps working unchanged.
Only the two writers -- record_provider_attempt() in
pipeline_provider_attempts.py, and verify_email()/record_platform_bounce() in
bounces.py -- call into record_observation() here.

Why append-only: the old tables' `ON CONFLICT ... DO UPDATE` silently
discarded history -- a re-verification overwrote the prior result instead of
recording that a second, different check happened. That's also *why* neither
table survived a relay round trip: overwriting in place produces the same row
forever, so there was nothing new to notice and push.

`kind` is the semantic purpose of the observation (what was being checked);
`origin` is which of the two legacy writers produced it. They're correlated
but not identical -- kind='email_verification' can come from *either* writer
(bounces.verify_email, or record_provider_attempt against millionverifier/
scrubby), which is exactly the case the compat views need `origin` to split.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional

from workspace_routing import DEFAULT_ORG_ID

KIND_EMAIL_VERIFICATION = "email_verification"
KIND_EMAIL_FIND = "email_find"
KIND_RESEARCH = "research"
KIND_PLATFORM_BOUNCE = "platform_bounce"
# Predates this module (2,373 legacy rows, provider='serper', written by an
# earlier standalone script) -- kept as its own kind rather than folded into
# KIND_RESEARCH so existing rows and new domain_discovery.py writes share one
# lineage. record_observation() silently no-ops on any kind not listed here,
# so this must stay in sync with every kind actually written anywhere.
KIND_DOMAIN_LOOKUP = "domain_lookup"

KINDS = frozenset({
    KIND_EMAIL_VERIFICATION, KIND_EMAIL_FIND, KIND_RESEARCH, KIND_PLATFORM_BOUNCE,
    KIND_DOMAIN_LOOKUP,
})

ORIGIN_VERIFICATION = "verification"  # bounces.py: verify_email / record_platform_bounce
ORIGIN_ATTEMPT = "attempt"           # pipeline_provider_attempts.py: record_provider_attempt

ORIGINS = frozenset({ORIGIN_VERIFICATION, ORIGIN_ATTEMPT})

# pipeline_provider_attempts.PROVIDER_DOMAINS values -> the kind enum above.
# Kept separate from PROVIDER_DOMAINS itself (which is keyed by provider name
# for a different purpose) because "email_finding" there and "email_find" here
# have always been two different strings.
_PROVIDER_DOMAIN_TO_KIND = {
    "email_finding": KIND_EMAIL_FIND,
    "research": KIND_RESEARCH,
    "email_verification": KIND_EMAIL_VERIFICATION,
}

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lead_provider_observations (
    obs_uid         TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    origin          TEXT NOT NULL,
    provider        TEXT NOT NULL,
    email           TEXT,
    status          TEXT NOT NULL,
    sub_status      TEXT,
    domain          TEXT,
    source_detail   TEXT,
    bounce_message  TEXT,
    free_email      INTEGER,
    mx_found        INTEGER,
    smtp_provider   TEXT,
    result_email    TEXT,
    result_validity TEXT,
    -- Opaque: provider_batch_jobs has 0 rows in production, so this is not an
    -- FK (see Stage 7 plan note on the FK-check landmine). batch_runner.py can
    -- start populating that table without a migration here.
    batch_id        INTEGER,
    metadata_json   TEXT,
    observed_at     TEXT NOT NULL,
    completed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lpo_lead_kind ON lead_provider_observations(lead_id, kind, observed_at);
CREATE INDEX IF NOT EXISTS idx_lpo_lead_provider ON lead_provider_observations(lead_id, provider, observed_at);
CREATE INDEX IF NOT EXISTS idx_lpo_org_lead ON lead_provider_observations(org_id, lead_id);
"""

# The old names, kept as read-only VIEWs projecting "latest row per provider"
# (ROW_NUMBER() OVER (... ORDER BY observed_at DESC) WHERE rn = 1) so every
# existing reader -- _lev_sources_for_lead, get_provider_attempts_map,
# has_attempted, _compute_verification_status, the CLI -- keeps working
# unchanged. SQLite views are read-only: any writer this migration missed
# fails loudly at runtime instead of silently, which is the property we want.
#
# `origin` (not `kind`) is what splits the two views: a millionverifier
# *attempt* (origin='attempt', kind='email_verification') and a millionverifier
# *result* (origin='verification', kind='email_verification') are different
# observations with disjoint columns -- see the Stage 7 plan note on the
# 2,703-lead intersection. Filtering on kind alone could not tell them apart.
COMPAT_VIEWS_SQL = """
CREATE VIEW IF NOT EXISTS lead_email_verification AS
SELECT obs_uid AS id, org_id, lead_id, email, status, sub_status,
       provider AS source, source_detail, bounce_message, free_email,
       mx_found, smtp_provider, observed_at AS verified_at, created_at
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY org_id, lead_id, provider ORDER BY observed_at DESC, obs_uid DESC
    ) AS rn
    FROM lead_provider_observations
    WHERE origin = 'verification'
)
WHERE rn = 1;

CREATE VIEW IF NOT EXISTS lead_provider_attempts AS
SELECT obs_uid AS id, lead_id, provider, domain, observed_at AS attempted_at,
       completed_at, status, result_email, result_validity, batch_id, metadata_json
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY lead_id, provider ORDER BY observed_at DESC, obs_uid DESC
    ) AS rn
    FROM lead_provider_observations
    WHERE origin = 'attempt'
)
WHERE rn = 1;
"""


def kind_for_provider_domain(domain: Optional[str]) -> str:
    return _PROVIDER_DOMAIN_TO_KIND.get((domain or "").strip().lower(), KIND_EMAIL_FIND)


def _lead_uid(conn: sqlite3.Connection, lead_id: int) -> Optional[str]:
    row = conn.execute("SELECT uid FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return row["uid"] if row else None


def compute_obs_uid(
    org_id: Optional[str],
    lead_uid: Optional[str],
    provider: Optional[str],
    kind: Optional[str],
    origin: Optional[str],
    observed_at: Optional[str],
    *,
    email: Optional[str] = None,
    status: Optional[str] = None,
    sub_status: Optional[str] = None,
    domain: Optional[str] = None,
    source_detail: Optional[str] = None,
    bounce_message: Optional[str] = None,
    free_email: Optional[bool] = None,
    mx_found: Optional[bool] = None,
    smtp_provider: Optional[str] = None,
    result_email: Optional[str] = None,
    result_validity: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> str:
    """Deterministic id for one observation -- same fact, same id, always.

    Makes a snapshot replay onto a wiped DB an `INSERT OR IGNORE` no-op instead
    of a duplicate: without this, a full pull re-ingesting the same ~13k rows
    of history would double them (and triple on the pull after that).

    Deliberately hashes the *whole* fact (every content column), not just the
    plan's original {org, lead_uid, provider, kind, observed_at, email}: two
    genuinely different facts -- e.g. record_provider_attempt(status='pending')
    immediately followed by record_provider_attempt(status='completed') for the
    same lead+provider -- can land in the same wall-clock second, since
    observed_at here has only second resolution (utc_now_for_storage(),
    matching every other *_at column in this schema). Hashing only the
    identity fields would collide the two calls and silently drop the second
    write. Hashing the full content means only a byte-identical repeat (a true
    replay) collapses -- which is exactly the idempotency this id is for.
    """
    parts = (
        org_id or "", lead_uid or "", provider or "", kind or "", origin or "",
        (email or "").strip().lower(), status or "", sub_status or "", domain or "",
        source_detail or "", bounce_message or "",
        "" if free_email is None else str(int(bool(free_email))),
        "" if mx_found is None else str(int(bool(mx_found))),
        smtp_provider or "", result_email or "", result_validity or "",
        observed_at or "", completed_at or "",
    )
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=16)
    return digest.hexdigest()


def record_observation(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    kind: str,
    origin: str,
    provider: str,
    status: str,
    org_id: str = DEFAULT_ORG_ID,
    email: Optional[str] = None,
    sub_status: Optional[str] = None,
    domain: Optional[str] = None,
    source_detail: Optional[str] = None,
    bounce_message: Optional[str] = None,
    free_email: Optional[bool] = None,
    mx_found: Optional[bool] = None,
    smtp_provider: Optional[str] = None,
    result_email: Optional[str] = None,
    result_validity: Optional[str] = None,
    batch_id: Optional[int] = None,
    metadata_json: Optional[str] = None,
    observed_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    lead_uid: Optional[str] = None,
) -> Optional[str]:
    """Append one real observation. Returns its obs_uid, or None if lead_id/provider/kind is bad.

    Idempotent by construction (obs_uid is a content hash): calling this twice
    for the exact same fact is a no-op, not a duplicate row.
    """
    from pipeline_update import utc_now_for_storage

    if not lead_id or not provider or kind not in KINDS or origin not in ORIGINS:
        return None
    observed_at = observed_at or utc_now_for_storage()
    if lead_uid is None:
        lead_uid = _lead_uid(conn, lead_id)
    obs_uid = compute_obs_uid(
        org_id, lead_uid, provider, kind, origin, observed_at,
        email=email, status=status, sub_status=sub_status, domain=domain,
        source_detail=source_detail, bounce_message=bounce_message,
        free_email=free_email, mx_found=mx_found, smtp_provider=smtp_provider,
        result_email=result_email, result_validity=result_validity,
        completed_at=completed_at,
    )
    conn.execute(
        """INSERT INTO lead_provider_observations (
               obs_uid, org_id, lead_id, kind, origin, provider, email, status,
               sub_status, domain, source_detail, bounce_message, free_email,
               mx_found, smtp_provider, result_email, result_validity,
               batch_id, metadata_json, observed_at, completed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (obs_uid) DO NOTHING""",
        (
            obs_uid, org_id, lead_id, kind, origin, provider, email, status,
            sub_status, domain, source_detail, bounce_message,
            1 if free_email else (0 if free_email is not None else None),
            1 if mx_found else (0 if mx_found is not None else None),
            smtp_provider, result_email, result_validity,
            batch_id, metadata_json, observed_at, completed_at,
        ),
    )
    return obs_uid


def apply_provider_observations_payload(
    conn: sqlite3.Connection,
    lead_id: int,
    observations: list,
) -> None:
    """Apply a pulled `lead_core_update.provider_observations` array.

    Generic across kinds/origins -- unlike the legacy `provider_attempts` apply
    path (pipeline_provider_attempts.apply_provider_attempts_payload), this is
    the first wire path that can carry verification/platform_bounce
    observations at all, so any verification-shaped row lands here recomputes
    the derived `leads.email_verification_status` cache.
    """
    if not observations:
        return
    touched_verification = False
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        kind = str(obs.get("kind") or "").strip().lower()
        origin = str(obs.get("origin") or "").strip().lower()
        provider = str(obs.get("provider") or "").strip().lower()
        if kind not in KINDS or origin not in ORIGINS or not provider:
            continue
        record_observation(
            conn, lead_id,
            kind=kind,
            origin=origin,
            provider=provider,
            status=str(obs.get("status") or "unknown"),
            email=obs.get("email"),
            sub_status=obs.get("sub_status"),
            domain=obs.get("domain"),
            source_detail=obs.get("source_detail"),
            bounce_message=obs.get("bounce_message"),
            free_email=obs.get("free_email"),
            mx_found=obs.get("mx_found"),
            smtp_provider=obs.get("smtp_provider"),
            result_email=obs.get("result_email"),
            result_validity=obs.get("result_validity"),
            observed_at=obs.get("observed_at"),
            completed_at=obs.get("completed_at"),
            metadata_json=obs.get("metadata_json"),
        )
        if origin == ORIGIN_VERIFICATION:
            touched_verification = True
    if touched_verification:
        from bounces import _compute_verification_status

        _compute_verification_status(conn, lead_id)
