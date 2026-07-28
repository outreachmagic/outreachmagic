"""Generic company mailboxes: info@, hello@, careers@ — and reaching a person at one.

The problem this solves: we cannot find Bill Smith's address, but the company
publishes hello@acme.com. Writing to hello@ *asking for Bill* beats writing to
nobody, and beats writing to hello@ addressed to no one.

Three ways to model that, and why this is the third:

  1. **Put hello@ on Bill.** Dedup matches on email through lead_identities, so
     the second contact given the same fallback merges into the first. Two real
     people become one lead, irreversibly, and nothing warns you.
  2. **Attach it as a lead_emails secondary.** Same failure, same cause:
     lead_emails calls upsert_identity_alias by design, because that is exactly
     right for a person's second address and exactly wrong for a shared one.
     Making it conditional means teaching a module whose contract is "always
     alias" to sometimes not.
  3. **A separate record, plus an explicit pointer.** The mailbox is a real,
     verifiable, sendable thing that simply is not a person, which is what
     leads.record_type already exists to say. Bill keeps his own row and points
     at it via fallback_email_lead_id.

What makes (3) right rather than merely workable: the day Bill's real address
turns up, leads.email stops being NULL and effective_email stops using the
fallback. No cleanup, no stale flag, no "which one is live now?". Both other
models require remembering to undo something later.

Because lead_filter_clause already defaults to record_type = 'contact', public
mailboxes are absent from the contacts list, the exports, CRM sync and the
email finder from the moment they exist -- no new exclusions to write, and none
to forget.

One hazard, stated plainly: two contacts falling back to the same mailbox and
both entering a campaign means mailing hello@ twice with two different
salutations. `fallback_collisions()` finds those; it reports rather than
resolves, because which colleague to address is a judgement and silently
dropping one from a campaign is worse than a visible warning.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from constants import (
    GENERIC_EMAIL_LOCAL_PARTS,
    RECORD_TYPE_PUBLIC_EMAIL,
    SHARED_EMAIL_DOMAINS,
)
from db_conn import get_conn
from workspace_routing import DEFAULT_ORG_ID, normalize_email


class PublicEmailError(ValueError):
    """User-facing failure (bad address, wrong record type, unknown lead)."""


# SQL for "the address to actually send to": the lead's own if it has one,
# otherwise its fallback mailbox. Exported so exports and campaign-add use the
# same expression rather than each writing their own COALESCE.
EFFECTIVE_EMAIL_SQL = (
    "COALESCE(NULLIF(TRIM(l.email), ''), NULLIF(TRIM(fb.email), ''))"
)
EFFECTIVE_EMAIL_JOIN = (
    "LEFT JOIN leads fb ON fb.id = l.fallback_email_lead_id"
)


def is_generic_local_part(email: str) -> bool:
    """Does this address name an organisation rather than a person?"""
    local = (email or "").strip().lower().partition("@")[0]
    return local in GENERIC_EMAIL_LOCAL_PARTS


def classify_email(email: str) -> str:
    """"public" (a shareable company mailbox), "personal", or "" (unusable).

    A shared mailbox domain is never a company mailbox however generic the local
    part looks -- info@gmail.com belongs to Google, not to anyone's employer.
    """
    normalized = normalize_email(email) or ""
    if "@" not in normalized:
        return ""
    domain = normalized.partition("@")[2]
    if domain in SHARED_EMAIL_DOMAINS:
        return ""
    return "public" if is_generic_local_part(normalized) else "personal"


def create_public_email(
    email: str, *, company_id: Optional[int] = None, title: Optional[str] = None,
    source: str = "manual", conn: Optional[sqlite3.Connection] = None,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Create (or return) the public_email lead for `email`.

    Idempotent on the normalized address: one row per mailbox per org, however
    many leads' research turned it up.
    """
    normalized = normalize_email(email)
    if not normalized:
        raise PublicEmailError(f"not an email address: {email!r}")
    kind = classify_email(normalized)
    if kind != "public":
        raise PublicEmailError(
            f"{normalized} is not a generic company mailbox "
            f"({'shared mailbox domain' if not kind else 'looks like a person'}). "
            "Personal addresses belong to one contact, not to a shared record.")

    own_conn = conn is None
    conn = conn or get_conn()
    try:
        existing = conn.execute(
            "SELECT id, record_type FROM leads WHERE LOWER(email) = ?", (normalized,),
        ).fetchone()
        if existing:
            if existing["record_type"] != RECORD_TYPE_PUBLIC_EMAIL:
                raise PublicEmailError(
                    f"{normalized} already belongs to lead {existing['id']} "
                    f"as a {existing['record_type']}; not reclassifying it.")
            return {"status": "exists", "lead_id": existing["id"], "email": normalized}

        domain = normalized.partition("@")[2]
        if company_id is None:
            from lead_sync import ensure_company
            company_id = ensure_company(conn, domain=domain)
        # Created through the ordinary add_lead path rather than a hand-rolled
        # INSERT: that is what mints the uid the relay addresses rows by and
        # registers the email identity, and a second creation path would drift
        # from it. Only record_type and the company link are set afterwards.
        #
        # The name is the address itself. leads.name is NOT NULL, and every
        # alternative ("Acme", "Info") reads like a person in a list; the
        # address never does. record_type is what actually keeps this row out
        # of contact lists and templates -- the name is only for humans.
        import pipeline as _om

        # add_lead manages its own connection, so this one's writes have to be
        # committed around it rather than held open across the call.
        if not own_conn:
            conn.commit()
        lead_id = _om.add_lead(name=normalized, email=normalized, title=title)["id"]
        conn.execute(
            "UPDATE leads SET record_type = ?, company_id = COALESCE(?, company_id), "
            "updated_at = datetime('now') WHERE id = ?",
            (RECORD_TYPE_PUBLIC_EMAIL, company_id, lead_id))
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return {"status": "created", "lead_id": lead_id, "email": normalized,
            "company_id": company_id}


def link_fallback(
    lead_id: int, public_lead_id: int, *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Point `lead_id` at a public mailbox for sending."""
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        lead = conn.execute(
            "SELECT id, name, email, record_type, company_id FROM leads WHERE id = ?",
            (lead_id,)).fetchone()
        if lead is None:
            raise PublicEmailError(f"lead not found: {lead_id}")
        if lead["record_type"] == RECORD_TYPE_PUBLIC_EMAIL:
            raise PublicEmailError(
                "a public mailbox cannot fall back to another public mailbox")
        target = conn.execute(
            "SELECT id, email, record_type, company_id FROM leads WHERE id = ?",
            (public_lead_id,)).fetchone()
        if target is None:
            raise PublicEmailError(f"lead not found: {public_lead_id}")
        if target["record_type"] != RECORD_TYPE_PUBLIC_EMAIL:
            raise PublicEmailError(
                f"lead {public_lead_id} is a {target['record_type']}, not a public "
                "mailbox. Falling back to a real person's address would send mail "
                "meant for one contact to another.")
        conn.execute(
            "UPDATE leads SET fallback_email_lead_id = ?, updated_at = datetime('now') "
            "WHERE id = ?", (public_lead_id, lead_id))
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return {
        "status": "linked", "lead_id": lead_id, "fallback_lead_id": public_lead_id,
        "fallback_email": target["email"],
        # Said out loud because it is the whole point of the design.
        "note": ("The fallback is used only while this lead has no email of its "
                 "own; finding a real address retires it automatically."),
    }


def unlink_fallback(lead_id: int, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    own_conn = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            "UPDATE leads SET fallback_email_lead_id = NULL, updated_at = datetime('now') "
            "WHERE id = ?", (lead_id,))
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    return {"status": "unlinked", "lead_id": lead_id}


def list_for_company(conn: sqlite3.Connection, company_id: int) -> dict:
    """A company's public mailboxes, with who falls back to each."""
    rows = conn.execute(
        """SELECT l.id AS lead_id, l.email, l.title, l.email_verification_status,
                  (SELECT COUNT(*) FROM leads f WHERE f.fallback_email_lead_id = l.id)
                      AS fallback_count
             FROM leads l
            WHERE l.company_id = ? AND l.record_type = ?
            ORDER BY l.email""",
        (company_id, RECORD_TYPE_PUBLIC_EMAIL),
    ).fetchall()
    return {"company_id": company_id, "public_emails": [dict(r) for r in rows]}


def effective_email(conn: sqlite3.Connection, lead_id: int) -> dict:
    """What we would actually send to, and whether it is the lead's own."""
    row = conn.execute(
        f"""SELECT l.email AS own_email, fb.email AS fallback_email,
                   {EFFECTIVE_EMAIL_SQL} AS effective_email
              FROM leads l {EFFECTIVE_EMAIL_JOIN}
             WHERE l.id = ?""",
        (lead_id,)).fetchone()
    if row is None:
        raise PublicEmailError(f"lead not found: {lead_id}")
    d = dict(row)
    d["is_fallback"] = bool(d["effective_email"]) and not (d["own_email"] or "").strip()
    d["lead_id"] = lead_id
    return d


def fallback_collisions(
    conn: sqlite3.Connection, workspace_id: Optional[str] = None,
    lead_ids: Optional[list[int]] = None,
) -> dict:
    """Public mailboxes that more than one selected lead would send to.

    Reported, never auto-resolved. Mailing hello@ twice with two salutations is
    bad; silently dropping one of two contacts from a campaign, without saying
    which or why, is worse.
    """
    where = ["l.fallback_email_lead_id IS NOT NULL"]
    params: list = []
    join = ""
    if workspace_id:
        join = "JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?"
        params.append(workspace_id)
    if lead_ids:
        where.append(f"l.id IN ({','.join('?' * len(lead_ids))})")
        params += [int(i) for i in lead_ids]
    rows = conn.execute(
        f"""SELECT fb.id AS public_lead_id, fb.email AS mailbox,
                   COUNT(*) AS leads,
                   GROUP_CONCAT(COALESCE(l.name, '?'), ' | ') AS lead_names
              FROM leads l
              {join}
              JOIN leads fb ON fb.id = l.fallback_email_lead_id
             WHERE {' AND '.join(where)}
             GROUP BY fb.id HAVING COUNT(*) > 1
             ORDER BY leads DESC, fb.email""",
        params,
    ).fetchall()
    return {"collisions": [dict(r) for r in rows]}


def ingest_serper_emails(
    conn: sqlite3.Connection, lead_id: int, candidates: list[dict], *,
    auto_link: bool = False,
) -> dict:
    """Turn a research run's extracted addresses into public_email records.

    Only generic local parts on a domain we already associate with the lead's
    company become records. A *personal* address found on a company site
    belongs to one human -- it is a lead for the email finder, never a shared
    mailbox -- so it is returned for review and nothing is created.

    `auto_link` is off by default. Deciding that this person should be reached
    at that mailbox is a judgement; the extractor's job ends at "this mailbox
    exists".
    """
    lead = conn.execute(
        "SELECT id, email, company_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        raise PublicEmailError(f"lead not found: {lead_id}")

    created, skipped, personal = [], [], []
    for c in candidates or []:
        email = (c.get("email") or "").strip().lower()
        if classify_email(email) != "public":
            if classify_email(email) == "personal":
                personal.append(email)
            continue
        if not c.get("matches_company_domain"):
            # A generic address on some unrelated domain in the search results
            # is somebody else's mailbox.
            skipped.append(email)
            continue
        try:
            result = create_public_email(
                email, company_id=lead["company_id"], title=c.get("context") or None,
                source="serper", conn=conn)
        except PublicEmailError as exc:
            skipped.append(f"{email}: {exc}")
            continue
        created.append(result)
        if auto_link and not (lead["email"] or "").strip():
            link_fallback(lead_id, result["lead_id"], conn=conn)
    conn.commit()
    return {
        "lead_id": lead_id,
        "created": created, "skipped": skipped,
        # Surfaced rather than swallowed: these are email-finder candidates for
        # a real person, and losing them here is losing the best lead we had.
        "personal_addresses_for_review": personal,
    }
