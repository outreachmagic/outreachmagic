"""Manage the set of email addresses attached to a lead.

`leads.email` is the de-facto *primary* address (it's what dedup matches on and
what outreach uses); `lead_emails` holds additional/secondary addresses. Until
now secondaries were only ever created during merges and there was no way to
promote one to primary. This module adds the three operations the dashboard
lead panel needs — list, add, promote — and keeps `lead_identities` in step so
dedup can match on any of a lead's addresses.

All writes go through the normal `leads` UPDATE / `lead_emails` INSERT paths, so
the outbox triggers queue them for relay push like any other edit.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db_conn import get_conn
from pipeline_utils import email_domain
from workspace_routing import DEFAULT_ORG_ID, normalize_email, upsert_identity_alias


class LeadEmailError(ValueError):
    """User-facing failure (bad address, or address owned by another lead)."""


def _verification_by_email(conn: sqlite3.Connection, lead_id: int) -> dict[str, dict]:
    """Latest verification per address for a lead, keyed by normalized email.

    lead_email_verification is latest-per-provider, so a single address can have
    rows from several providers; keep the most recent per email.
    """
    rows = conn.execute(
        """SELECT email, status, source, source_detail, verified_at
           FROM lead_email_verification
           WHERE lead_id = ? AND email IS NOT NULL
           ORDER BY verified_at DESC""",
        (lead_id,),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        key = normalize_email(r["email"]) or (r["email"] or "").strip().lower()
        if key and key not in out:  # first row wins — already ordered newest-first
            out[key] = {
                "status": r["status"],
                "provider": r["source"],
                "provider_detail": r["source_detail"],
                "verified_at": r["verified_at"],
            }
    return out


def list_lead_emails(lead_id: int) -> dict:
    """All addresses for a lead: the primary (leads.email) plus secondaries from
    lead_emails, each annotated with its latest verification result."""
    conn = get_conn()
    try:
        lead = conn.execute(
            "SELECT email FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if lead is None:
            raise LeadEmailError(f"lead not found: {lead_id}")
        primary_norm = normalize_email(lead["email"])
        verif = _verification_by_email(conn, lead_id)
        emails = []
        if lead["email"]:
            emails.append({
                "email": lead["email"],
                "is_primary": True,
                "verification": verif.get(primary_norm or ""),
            })
        secondaries = conn.execute(
            "SELECT email FROM lead_emails WHERE lead_id = ? ORDER BY created_at, id",
            (lead_id,),
        ).fetchall()
        for r in secondaries:
            norm = normalize_email(r["email"]) or (r["email"] or "").strip().lower()
            if norm and norm == primary_norm:
                continue  # don't double-list the primary if a stale row lingers
            emails.append({
                "email": r["email"],
                "is_primary": False,
                "verification": verif.get(norm),
            })
        return {"lead_id": lead_id, "emails": emails}
    finally:
        conn.close()


def add_lead_email(lead_id: int, email: str) -> dict:
    """Attach an additional (secondary) address to a lead and register it as an
    email identity so dedup can match on it."""
    email_norm = normalize_email(email)
    if not email_norm:
        raise LeadEmailError(f"not a valid email address: {email!r}")
    conn = get_conn()
    try:
        lead = conn.execute(
            "SELECT email FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if lead is None:
            raise LeadEmailError(f"lead not found: {lead_id}")
        if normalize_email(lead["email"]) == email_norm:
            return {"status": "already_primary", "lead_id": lead_id, "email": email_norm}
        conn.execute(
            "INSERT OR IGNORE INTO lead_emails (lead_id, email, is_primary) VALUES (?, ?, 0)",
            (lead_id, email_norm),
        )
        try:
            upsert_identity_alias(
                conn, DEFAULT_ORG_ID, lead_id, "email", email_norm, source="dashboard")
        except ValueError as exc:
            raise LeadEmailError(str(exc)) from exc
        conn.commit()
        return {"status": "added", "lead_id": lead_id, "email": email_norm}
    finally:
        conn.close()


def promote_lead_email(lead_id: int, email: str) -> dict:
    """Make `email` the lead's primary address: demote the current primary into
    lead_emails, remove the target from the secondary list, and point
    leads.email at it. The email identity is (re)registered as primary."""
    email_norm = normalize_email(email)
    if not email_norm:
        raise LeadEmailError(f"not a valid email address: {email!r}")
    conn = get_conn()
    try:
        lead = conn.execute(
            "SELECT email FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if lead is None:
            raise LeadEmailError(f"lead not found: {lead_id}")
        current = normalize_email(lead["email"])
        if current == email_norm:
            return {"status": "already_primary", "lead_id": lead_id, "email": email_norm}
        # Demote the current primary into the secondary table (if any).
        if current:
            conn.execute(
                "INSERT OR IGNORE INTO lead_emails (lead_id, email, is_primary) VALUES (?, ?, 0)",
                (lead_id, current),
            )
        # The promoted address leaves the secondary list.
        conn.execute(
            "DELETE FROM lead_emails WHERE lead_id = ? AND email = ? COLLATE NOCASE",
            (lead_id, email_norm),
        )
        try:
            conn.execute(
                "UPDATE leads SET email = ?, email_domain = ?, updated_at = datetime('now') WHERE id = ?",
                (email_norm, email_domain(email_norm), lead_id),
            )
        except sqlite3.IntegrityError as exc:
            # Partial unique index on leads.email — another lead owns it primary.
            raise LeadEmailError(
                f"{email_norm} is already the primary email of another lead") from exc
        try:
            upsert_identity_alias(
                conn, DEFAULT_ORG_ID, lead_id, "email", email_norm, source="dashboard")
        except ValueError as exc:
            raise LeadEmailError(str(exc)) from exc
        conn.commit()
        return {"status": "promoted", "lead_id": lead_id, "email": email_norm}
    finally:
        conn.close()
