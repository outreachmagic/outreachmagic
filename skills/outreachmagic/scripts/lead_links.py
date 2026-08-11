"""One conversation spanning two people.

A contact replies positively and forwards us to a colleague, who replies too.
That is one opportunity and two real people. Merging them is wrong -- both have
their own address, title and history, and both may keep replying -- but counting
two positives is also wrong, and it is the count that reaches the Campaigns page
and every "how did we do" conversation.

So: a non-destructive, directional link. Both records stay whole and
individually visible; only the COUNT collapses, by resolving every lead to its
conversation root and counting distinct roots.

Chains work. A forwards to B, B forwards to C: the root of all three is A, so
three positive replies inside one thread count once. Cycles cannot deadlock the
walk -- the recursive CTE stops when it revisits a lead.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Optional

from db_conn import get_conn
from workspace_routing import DEFAULT_ORG_ID

LINK_KINDS = ("forwarded_to", "same_person")


class LeadLinkError(ValueError):
    """A link that cannot exist: unknown lead, self-link, or a cycle."""


# Resolve each lead to the head of its forward chain. Anchored on leads with no
# inbound link (nobody forwarded TO them), then walked outward. A lead in no
# chain is its own root, which is what makes this safe to join unconditionally:
# COUNT(DISTINCT root) equals COUNT(DISTINCT lead_id) when no links exist.
CONVERSATION_ROOT_SQL = """
    WITH RECURSIVE chain(lead_id, root_id, depth) AS (
        SELECT l.id, l.id, 0 FROM leads l
         WHERE NOT EXISTS (SELECT 1 FROM lead_links k WHERE k.to_lead_id = l.id)
        UNION
        SELECT k.to_lead_id, c.root_id, c.depth + 1
          FROM lead_links k JOIN chain c ON c.lead_id = k.from_lead_id
         -- A guard, not a feature: link_leads() rejects cycles at write time,
         -- but a hand-edited database must not hang a reporting query.
         WHERE c.depth < 16
    )
    SELECT lead_id, root_id FROM chain
"""


def conversation_root_sql(alias: str = "cr") -> str:
    """The recursive CTE as a named subquery, for joining into a report."""
    return f"({CONVERSATION_ROOT_SQL}) {alias}"


def _lead_exists(conn: sqlite3.Connection, lead_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM leads WHERE id = ?", (int(lead_id),)).fetchone() is not None


def _would_cycle(conn: sqlite3.Connection, from_lead_id: int, to_lead_id: int) -> bool:
    """True when `to` already reaches `from` -- linking would close a loop."""
    seen = {int(to_lead_id)}
    frontier = [int(to_lead_id)]
    while frontier:
        rows = conn.execute(
            "SELECT to_lead_id FROM lead_links WHERE from_lead_id IN (%s)"
            % ",".join("?" * len(frontier)), frontier,
        ).fetchall()
        nxt = [int(r["to_lead_id"]) for r in rows if int(r["to_lead_id"]) not in seen]
        if int(from_lead_id) in nxt:
            return True
        seen.update(nxt)
        frontier = nxt
    return False


def link_leads(
    conn: sqlite3.Connection,
    from_lead_id: int,
    to_lead_id: int,
    *,
    kind: str = "forwarded_to",
    note: Optional[str] = None,
    created_by: Optional[str] = None,
    org_id: str = DEFAULT_ORG_ID,
) -> dict:
    """Record that `from_lead_id` introduced us to `to_lead_id`.

    Deliberately permissive about companies: a forward to someone at a different
    company (a consultant, a parent org, a peer at another school) is the normal
    case, and refusing it would rule out the introductions worth the most.
    """
    from_lead_id, to_lead_id = int(from_lead_id), int(to_lead_id)
    if kind not in LINK_KINDS:
        raise LeadLinkError(f"unknown link kind {kind!r}. Valid: {', '.join(LINK_KINDS)}")
    if from_lead_id == to_lead_id:
        raise LeadLinkError("a lead cannot be linked to itself")
    for lead_id in (from_lead_id, to_lead_id):
        if not _lead_exists(conn, lead_id):
            raise LeadLinkError(f"lead not found: {lead_id}")
    if _would_cycle(conn, from_lead_id, to_lead_id):
        raise LeadLinkError(
            f"linking {from_lead_id} -> {to_lead_id} would close a loop; "
            "a conversation has one origin")

    existing = conn.execute(
        "SELECT id FROM lead_links WHERE from_lead_id = ? AND to_lead_id = ? AND kind = ?",
        (from_lead_id, to_lead_id, kind),
    ).fetchone()
    if existing:
        return {"status": "exists", "id": existing["id"],
                "from_lead_id": from_lead_id, "to_lead_id": to_lead_id, "kind": kind}

    link_id = f"lnk_{uuid.uuid4().hex[:20]}"
    conn.execute(
        """INSERT INTO lead_links
               (id, org_id, from_lead_id, to_lead_id, kind, note, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (link_id, org_id, from_lead_id, to_lead_id, kind, note, created_by),
    )
    conn.commit()
    return {"status": "linked", "id": link_id, "from_lead_id": from_lead_id,
            "to_lead_id": to_lead_id, "kind": kind,
            "root_id": conversation_root(conn, to_lead_id)}


def unlink_leads(conn: sqlite3.Connection, from_lead_id: int, to_lead_id: int,
                 *, kind: Optional[str] = None) -> dict:
    where = "from_lead_id = ? AND to_lead_id = ?"
    params: list = [int(from_lead_id), int(to_lead_id)]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    cur = conn.execute(f"DELETE FROM lead_links WHERE {where}", params)
    conn.commit()
    return {"status": "unlinked", "removed": cur.rowcount}


def conversation_root(conn: sqlite3.Connection, lead_id: int) -> int:
    """The lead at the head of this one's chain -- itself, if it is in none."""
    row = conn.execute(
        f"SELECT root_id FROM ({CONVERSATION_ROOT_SQL}) WHERE lead_id = ?",
        (int(lead_id),),
    ).fetchone()
    return int(row["root_id"]) if row else int(lead_id)


def conversation(conn: sqlite3.Connection, lead_id: int) -> dict:
    """Everyone in this lead's conversation, with the root named."""
    root = conversation_root(conn, lead_id)
    rows = conn.execute(
        f"""SELECT l.id AS lead_id, l.name, l.email, l.title, l.company,
                   wl_root.root_id AS root_id
              FROM ({CONVERSATION_ROOT_SQL}) wl_root
              JOIN leads l ON l.id = wl_root.lead_id
             WHERE wl_root.root_id = ?
          ORDER BY (l.id = wl_root.root_id) DESC, l.id""",
        (root,),
    ).fetchall()
    links = conn.execute(
        """SELECT k.id, k.from_lead_id, k.to_lead_id, k.kind, k.note, k.created_at,
                  lf.name AS from_name, lt.name AS to_name
             FROM lead_links k
             JOIN leads lf ON lf.id = k.from_lead_id
             JOIN leads lt ON lt.id = k.to_lead_id
            WHERE k.from_lead_id IN (SELECT lead_id FROM (%s) WHERE root_id = ?)
               OR k.to_lead_id IN (SELECT lead_id FROM (%s) WHERE root_id = ?)
         ORDER BY k.created_at""" % (CONVERSATION_ROOT_SQL, CONVERSATION_ROOT_SQL),
        (root, root),
    ).fetchall()
    return {
        "lead_id": int(lead_id),
        "root_id": root,
        "members": [dict(r) for r in rows],
        "links": [dict(r) for r in links],
        "size": len(rows),
    }


def build_lead_link_sync_payload(conn: sqlite3.Connection, link_id: str) -> dict:
    """The wire form of one link.

    Keyed by both leads' uids rather than their local row ids: ids are per
    machine, uids are the identity the relay already stores for a lead.

    NOTE: the relay has no lead_link endpoint yet (its snapshot allowlist is
    lead_core / lead_workspace / company / sender_account / sender_domain), so
    nothing drains these rows. Everything on this side is wired -- outbox
    triggers, entity type, payload -- so the day the endpoint exists no local
    change is needed. See UNPUSHABLE_ENTITY_TYPES in reporting_queries.
    """
    row = conn.execute(
        """SELECT k.id, k.kind, k.note, k.created_at, k.created_by,
                  lf.uid AS from_uid, lt.uid AS to_uid
             FROM lead_links k
             JOIN leads lf ON lf.id = k.from_lead_id
             JOIN leads lt ON lt.id = k.to_lead_id
            WHERE k.id = ?""",
        (link_id,),
    ).fetchone()
    if not row:
        return {}
    payload = {
        "from_uid": row["from_uid"],
        "to_uid": row["to_uid"],
        "kind": row["kind"],
    }
    for wire, column in (("note", "note"), ("created_at", "created_at"),
                         ("created_by", "created_by")):
        if row[column]:
            payload[wire] = row[column]
    return payload


def list_links(conn: sqlite3.Connection, limit: int = 100) -> dict:
    rows = conn.execute(
        """SELECT k.id, k.from_lead_id, k.to_lead_id, k.kind, k.note, k.created_at,
                  lf.name AS from_name, lf.email AS from_email,
                  lt.name AS to_name, lt.email AS to_email
             FROM lead_links k
             JOIN leads lf ON lf.id = k.from_lead_id
             JOIN leads lt ON lt.id = k.to_lead_id
         ORDER BY k.created_at DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return {"links": [dict(r) for r in rows], "count": len(rows)}
