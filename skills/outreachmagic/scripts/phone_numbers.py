"""Phone numbers attached to a lead or a company.

Mirrors `lead_emails.py` — same list / add / promote shape, same "one primary,
enforced in code" rule — but polymorphic, because the two examples that drove
this are a *company* switchboard from a Google Maps scrape and a *person's*
mobile from a contact provider, and both have to live somewhere a CRM mapping
can name unambiguously.

Two columns rather than one string:
  label   what kind of number it is  (mobile / main / hq / …)
  source  where it came from         (google_maps / apollo / csv_import / …)

Both are controlled vocabularies (`constants.PHONE_LABELS` / `PHONE_SOURCES`);
an unknown value is rejected with the valid list rather than silently stored,
so the CRM mapping can rely on the values meaning what they say.

Normalization goes through `workspace_routing.normalize_phone` — the same
function that builds `phone` lead identities — deliberately, not a second copy.
Two normalizers means a number stored here and the identity derived from it
eventually disagree, and then dedup stops seeing them as the same number.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from constants import PHONE_LABELS, PHONE_OWNER_TYPES, PHONE_SOURCES
from db_conn import get_conn
from workspace_routing import DEFAULT_ORG_ID, normalize_phone, upsert_identity_alias


class PhoneNumberError(ValueError):
    """User-facing failure (bad number, unknown label/source, missing owner)."""


def _check_owner_type(owner_type: str) -> str:
    ot = str(owner_type or "").strip().lower()
    if ot not in PHONE_OWNER_TYPES:
        raise PhoneNumberError(
            f"owner_type must be one of: {', '.join(PHONE_OWNER_TYPES)}")
    return ot


def _check_label(label: Optional[str]) -> str:
    lb = str(label or "other").strip().lower()
    if lb not in PHONE_LABELS:
        raise PhoneNumberError(f"label must be one of: {', '.join(PHONE_LABELS)}")
    return lb


def _check_source(source: Optional[str]) -> Optional[str]:
    if source is None or not str(source).strip():
        return None
    sc = str(source).strip().lower()
    if sc not in PHONE_SOURCES:
        raise PhoneNumberError(f"source must be one of: {', '.join(PHONE_SOURCES)}")
    return sc


def _require_owner(conn: sqlite3.Connection, owner_type: str, owner_id: int) -> None:
    table = "leads" if owner_type == "lead" else "companies"
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (owner_id,)).fetchone() is None:
        raise PhoneNumberError(f"{owner_type} not found: {owner_id}")


def _rows(conn: sqlite3.Connection, owner_type: str, owner_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, phone_e164, phone_raw, label, source, is_primary, created_at
             FROM phone_numbers
            WHERE owner_type = ? AND owner_id = ?
            ORDER BY is_primary DESC, created_at, id""",
        (owner_type, owner_id),
    ).fetchall()
    return [dict(r) for r in rows]


def list_phones(owner_type: str, owner_id: int) -> dict:
    """Every number on one lead or company, primary first."""
    ot = _check_owner_type(owner_type)
    conn = get_conn()
    try:
        _require_owner(conn, ot, owner_id)
        return {"owner_type": ot, "owner_id": owner_id, "phones": _rows(conn, ot, owner_id)}
    finally:
        conn.close()


def add_phone(
    owner_type: str,
    owner_id: int,
    phone: str,
    *,
    label: str = "other",
    source: Optional[str] = None,
    is_primary: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Attach a number. Re-adding an existing number updates its label/source
    rather than erroring — imports re-run, and a second pass that now knows the
    number is a `mobile` should be able to say so."""
    ot = _check_owner_type(owner_type)
    lb = _check_label(label)
    sc = _check_source(source)
    e164 = normalize_phone(phone)
    if not e164:
        raise PhoneNumberError(f"not a usable phone number: {phone!r}")

    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        _require_owner(conn, ot, owner_id)
        existing = conn.execute(
            "SELECT id, is_primary FROM phone_numbers "
            "WHERE owner_type = ? AND owner_id = ? AND phone_e164 = ?",
            (ot, owner_id, e164),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE phone_numbers
                      SET label = ?, source = COALESCE(?, source),
                          phone_raw = COALESCE(?, phone_raw),
                          updated_at = datetime('now')
                    WHERE id = ?""",
                (lb, sc, str(phone).strip() or None, existing["id"]),
            )
            status = "updated"
        else:
            # First number on an owner is primary whether or not anyone said so;
            # otherwise the common case (exactly one number) has no primary and
            # CRM sync has nothing to map.
            has_any = conn.execute(
                "SELECT 1 FROM phone_numbers WHERE owner_type = ? AND owner_id = ?",
                (ot, owner_id),
            ).fetchone()
            primary = 1 if (is_primary or not has_any) else 0
            if primary:
                conn.execute(
                    "UPDATE phone_numbers SET is_primary = 0, updated_at = datetime('now') "
                    "WHERE owner_type = ? AND owner_id = ?",
                    (ot, owner_id),
                )
            conn.execute(
                """INSERT INTO phone_numbers
                       (owner_type, owner_id, phone_e164, phone_raw, label, source, is_primary)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ot, owner_id, e164, str(phone).strip() or None, lb, sc, primary),
            )
            status = "added"

        # A lead's number is also an identity, so dedup can match on it. Phone is
        # deliberately NOT in AUTO_MERGE_SAFE_IDENTITY_TYPES (shared company
        # lines), so this surfaces candidates without merging anything.
        if ot == "lead":
            try:
                upsert_identity_alias(
                    conn, DEFAULT_ORG_ID, owner_id, "phone", e164, source="phone_numbers")
            except ValueError:
                pass  # already owned by another lead — the number still stands
        if own_conn:
            conn.commit()
        return {"status": status, "owner_type": ot, "owner_id": owner_id,
                "phone_e164": e164, "label": lb, "source": sc}
    finally:
        if own_conn:
            conn.close()


def promote_phone(owner_type: str, owner_id: int, phone: str) -> dict:
    """Make `phone` the owner's primary number."""
    ot = _check_owner_type(owner_type)
    e164 = normalize_phone(phone)
    if not e164:
        raise PhoneNumberError(f"not a usable phone number: {phone!r}")
    conn = get_conn()
    try:
        _require_owner(conn, ot, owner_id)
        target = conn.execute(
            "SELECT id FROM phone_numbers WHERE owner_type = ? AND owner_id = ? AND phone_e164 = ?",
            (ot, owner_id, e164),
        ).fetchone()
        if target is None:
            raise PhoneNumberError(f"{e164} is not on this {ot}")
        conn.execute(
            "UPDATE phone_numbers SET is_primary = 0, updated_at = datetime('now') "
            "WHERE owner_type = ? AND owner_id = ?",
            (ot, owner_id),
        )
        conn.execute(
            "UPDATE phone_numbers SET is_primary = 1, updated_at = datetime('now') WHERE id = ?",
            (target["id"],),
        )
        conn.commit()
        return {"status": "promoted", "owner_type": ot, "owner_id": owner_id, "phone_e164": e164}
    finally:
        conn.close()


def remove_phone(owner_type: str, owner_id: int, phone: str) -> dict:
    """Detach a number. If it was primary, the oldest survivor takes over — an
    owner with numbers left should never have none of them primary."""
    ot = _check_owner_type(owner_type)
    e164 = normalize_phone(phone)
    if not e164:
        raise PhoneNumberError(f"not a usable phone number: {phone!r}")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, is_primary FROM phone_numbers "
            "WHERE owner_type = ? AND owner_id = ? AND phone_e164 = ?",
            (ot, owner_id, e164),
        ).fetchone()
        if row is None:
            return {"status": "not_found", "owner_type": ot, "owner_id": owner_id,
                    "phone_e164": e164}
        conn.execute("DELETE FROM phone_numbers WHERE id = ?", (row["id"],))
        if row["is_primary"]:
            nxt = conn.execute(
                "SELECT id FROM phone_numbers WHERE owner_type = ? AND owner_id = ? "
                "ORDER BY created_at, id LIMIT 1",
                (ot, owner_id),
            ).fetchone()
            if nxt:
                conn.execute(
                    "UPDATE phone_numbers SET is_primary = 1, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (nxt["id"],),
                )
        if ot == "lead":
            conn.execute(
                "DELETE FROM lead_identities WHERE lead_id = ? AND identity_type = 'phone' "
                "AND identity_value_normalized = ?",
                (owner_id, e164),
            )
        conn.commit()
        return {"status": "removed", "owner_type": ot, "owner_id": owner_id, "phone_e164": e164}
    finally:
        conn.close()


def reassign_owner(
    conn: sqlite3.Connection, owner_type: str, from_id: int, to_id: int,
) -> None:
    """Carry numbers from a merged-away lead/company to the survivor.

    Must run BEFORE the row is deleted -- phone_numbers has no FK (owner_type
    is polymorphic), so the delete triggers sweep it instead, and a sweep is
    indiscriminate. `UPDATE OR IGNORE` makes the shared-number case a no-op via
    the UNIQUE key; the moved rows lose is_primary so the survivor doesn't end
    up with two, and the last statement re-establishes one if it had none.
    """
    conn.execute(
        """UPDATE OR IGNORE phone_numbers
              SET owner_id = ?, is_primary = 0, updated_at = datetime('now')
            WHERE owner_type = ? AND owner_id = ?""",
        (to_id, owner_type, from_id),
    )
    conn.execute(
        """UPDATE phone_numbers SET is_primary = 1, updated_at = datetime('now')
            WHERE id = (SELECT id FROM phone_numbers
                         WHERE owner_type = ? AND owner_id = ?
                         ORDER BY created_at, id LIMIT 1)
              AND NOT EXISTS (SELECT 1 FROM phone_numbers
                               WHERE owner_type = ? AND owner_id = ? AND is_primary = 1)""",
        (owner_type, to_id, owner_type, to_id),
    )


def primary_phone_map(
    conn: sqlite3.Connection,
    owner_type: str,
    owner_ids: list[int],
    *,
    labels: Optional[tuple[str, ...]] = None,
) -> dict[int, str]:
    """`{owner_id: phone_e164}` for a batch — the read CRM sync and the export
    use. `labels` restricts which kinds count (company fallback wants main/hq,
    not the fax)."""
    if not owner_ids:
        return {}
    ot = _check_owner_type(owner_type)
    placeholders = ",".join("?" for _ in owner_ids)
    params: list = [ot, *owner_ids]
    label_clause = ""
    if labels:
        label_clause = f" AND label IN ({','.join('?' for _ in labels)})"
        params.extend(labels)
    rows = conn.execute(
        f"""SELECT owner_id, phone_e164 FROM phone_numbers
             WHERE owner_type = ? AND owner_id IN ({placeholders}){label_clause}
             ORDER BY is_primary DESC, created_at, id""",
        params,
    ).fetchall()
    out: dict[int, str] = {}
    for r in rows:
        out.setdefault(r["owner_id"], r["phone_e164"])  # first row wins — ordered
    return out
