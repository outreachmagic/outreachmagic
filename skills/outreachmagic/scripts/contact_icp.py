"""Who counts as a contact worth keeping, per workspace.

An ICP profile is a small, boring document -- title phrases that qualify, title
phrases that disqualify, the page sections worth reading, and how many contacts
a page has to yield before it stops being "the regex handled it". `find-contacts`
reads one; `contact_extract.score_against_icp` applies it. Nothing here touches
the network, and nothing here scores anything: this module stores profiles and
hands them back.

Two properties the rest of the feature leans on:

  * **The hash is of the config, not of the row.** Two workspaces that write the
    same whitelist get the same `config_hash`, and rewriting a profile with the
    same terms in a different order does not change it. That is what makes
    `company_contact_observations.icp_config_hash` a version you can join
    against -- if the hash moved every time someone re-ran `icp set`, every
    historical run would look like it used a config nobody can reconstruct.
  * **Storage is canonical.** Terms are lowercased, whitespace-collapsed,
    deduped and sorted on the way in, so the stored `config_json` is the thing
    that was hashed. A reader never has to re-normalize to compare, and
    `icp export | icp import` round-trips to the identical hash.

Config lives in the database rather than a vault YAML so `pull`, sync and the
dashboard can all see it. `export`/`import` (JSON -- this repo carries no YAML
dependency) give back the file ergonomics without the drift.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable, Optional

from db_conn import get_conn
from workspace_routing import resolve_workspace_identity

# Used when a workspace has exactly one profile and the caller didn't name it,
# and as the name `icp set` writes when --name is omitted.
DEFAULT_PROFILE_NAME = "default"

# The export envelope. `version` exists so a future shape change can be
# detected on import rather than silently misread as the current one.
EXPORT_KIND = "outreachmagic.icp"
EXPORT_VERSION = 1

# Every key a canonical config has. A profile is exactly these -- an unknown key
# in an imported document is rejected rather than stored, because a typo'd
# "whitelist_" that round-trips cleanly but scores nothing is the worst
# possible failure here.
TERM_FIELDS = ("whitelist", "blocklist", "section_headers")
CONFIG_FIELDS = TERM_FIELDS + ("min_contacts",)

DEFAULT_MIN_CONTACTS = 1

_WHITESPACE_RE = re.compile(r"\s+")


class IcpError(ValueError):
    """User-facing failure (unknown workspace, unknown profile, bad config)."""


# ── canonicalization ─────────────────────────────────────────────────────────

def normalize_term(term: Any) -> str:
    """One title phrase, in the form everything downstream compares against.

    Lowercase and whitespace-collapsed, because "General  Manager" and
    "general manager" are the same rule and storing both would let a profile
    disagree with itself.
    """
    return _WHITESPACE_RE.sub(" ", str(term or "")).strip().lower()


def normalize_terms(values: Any) -> list[str]:
    """A term list from either a list or a comma-separated string.

    Sorted and deduped: the CLI takes `--whitelist "a,b"` and an import takes a
    JSON array, and the two must produce the same bytes or they produce
    different hashes for the same intent.
    """
    if values is None:
        return []
    if isinstance(values, str):
        items: Iterable[Any] = values.split(",")
    elif isinstance(values, (list, tuple, set, frozenset)):
        items = values
    else:
        raise IcpError(f"expected a list or comma-separated string, got {type(values).__name__}")
    return sorted({t for t in (normalize_term(v) for v in items) if t})


def canonical_config(raw: Optional[dict]) -> dict:
    """A partial config filled out and normalized into its stored form."""
    raw = dict(raw or {})
    unknown = sorted(set(raw) - set(CONFIG_FIELDS))
    if unknown:
        raise IcpError(
            f"unknown ICP field(s): {', '.join(unknown)} "
            f"(known: {', '.join(CONFIG_FIELDS)})"
        )
    cfg: dict[str, Any] = {field: normalize_terms(raw.get(field)) for field in TERM_FIELDS}
    min_contacts = raw.get("min_contacts")
    if min_contacts in (None, ""):
        min_contacts = DEFAULT_MIN_CONTACTS
    try:
        min_contacts = int(min_contacts)
    except (TypeError, ValueError):
        raise IcpError(f"min_contacts must be an integer, got {min_contacts!r}") from None
    if min_contacts < 0:
        raise IcpError("min_contacts cannot be negative")
    cfg["min_contacts"] = min_contacts
    return cfg


def config_hash(config: Optional[dict]) -> str:
    """Stable id for a config's *content*.

    Canonicalizes first, so callers can hand this either a stored config or a
    hand-written one and get the same answer for the same rules.
    """
    canonical = canonical_config(config)
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=16).hexdigest()


# ── storage ──────────────────────────────────────────────────────────────────

def _resolve_workspace(conn: sqlite3.Connection, workspace: Optional[str]) -> dict:
    ws = resolve_workspace_identity(conn, workspace)
    if not ws:
        raise IcpError(f"workspace not found: {workspace}")
    return ws


def _row_to_profile(row: sqlite3.Row, ws: Optional[dict] = None) -> dict:
    profile = {
        "name": row["name"],
        "workspace_id": row["workspace_id"],
        "config": json.loads(row["config_json"]),
        "config_hash": row["config_hash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if ws:
        profile["workspace"] = ws["slug"]
    return profile


def get_profile(
    conn: sqlite3.Connection,
    workspace_id: str,
    name: Optional[str] = None,
) -> Optional[dict]:
    """One profile by name, or the workspace's only profile when unnamed.

    Returns None when there is nothing to return; raises only when the request
    is genuinely ambiguous (no name given, several profiles exist), because
    silently picking one would make `find-contacts` score against a config the
    operator did not choose.
    """
    if name:
        row = conn.execute(
            "SELECT * FROM workspace_icp_profiles WHERE workspace_id = ? AND name = ?",
            (workspace_id, normalize_term(name)),
        ).fetchone()
        return _row_to_profile(row) if row else None

    rows = conn.execute(
        "SELECT * FROM workspace_icp_profiles WHERE workspace_id = ? ORDER BY name",
        (workspace_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise IcpError(
            "several ICP profiles exist for this workspace -- pass --name "
            f"({', '.join(r['name'] for r in rows)})"
        )
    return _row_to_profile(rows[0])


def require_profile(conn: sqlite3.Connection, workspace_id: str, name: Optional[str] = None) -> dict:
    """get_profile(), but a missing profile is an error rather than a None."""
    profile = get_profile(conn, workspace_id, name)
    if profile is None:
        raise IcpError(
            f"no ICP profile{f' named {name!r}' if name else ''} for this workspace "
            "-- create one with `pipeline.py icp set`"
        )
    return profile


def set_profile(
    conn: sqlite3.Connection,
    workspace_id: str,
    name: Optional[str] = None,
    *,
    whitelist: Any = None,
    blocklist: Any = None,
    section_headers: Any = None,
    min_contacts: Any = None,
    replace: bool = False,
) -> dict:
    """Create or update one profile. Returns it, with a `created` flag.

    Fields left as None keep whatever the existing profile holds, so tightening
    a blocklist doesn't require restating the whitelist. Passing an explicit
    empty string or empty list *does* clear a field -- that is the only way to
    empty one, and `replace=True` resets every unspecified field to its default
    for the case where the whole profile is being rewritten.
    """
    name = normalize_term(name) or DEFAULT_PROFILE_NAME
    existing = conn.execute(
        "SELECT * FROM workspace_icp_profiles WHERE workspace_id = ? AND name = ?",
        (workspace_id, name),
    ).fetchone()

    base: dict[str, Any] = {}
    if existing and not replace:
        base = json.loads(existing["config_json"])

    supplied = {
        "whitelist": whitelist,
        "blocklist": blocklist,
        "section_headers": section_headers,
        "min_contacts": min_contacts,
    }
    merged = dict(base)
    for field, value in supplied.items():
        if value is not None:
            merged[field] = value

    config = canonical_config(merged)
    chash = config_hash(config)
    config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))

    if existing:
        # updated_at only moves when the content does. A no-op `icp set` that
        # bumped the timestamp would make "when did this config last change?"
        # unanswerable, and that question is the whole point of versioning it.
        if existing["config_hash"] != chash:
            conn.execute(
                """UPDATE workspace_icp_profiles
                      SET config_json = ?, config_hash = ?, updated_at = datetime('now')
                    WHERE workspace_id = ? AND name = ?""",
                (config_json, chash, workspace_id, name),
            )
        created = False
    else:
        conn.execute(
            """INSERT INTO workspace_icp_profiles (workspace_id, name, config_json, config_hash)
               VALUES (?, ?, ?, ?)""",
            (workspace_id, name, config_json, chash),
        )
        created = True

    row = conn.execute(
        "SELECT * FROM workspace_icp_profiles WHERE workspace_id = ? AND name = ?",
        (workspace_id, name),
    ).fetchone()
    return {**_row_to_profile(row), "created": created}


def list_profiles(conn: sqlite3.Connection, workspace_id: Optional[str] = None) -> list[dict]:
    """Every profile, or every profile in one workspace, with slugs attached."""
    if workspace_id:
        rows = conn.execute(
            """SELECT p.*, w.slug AS ws_slug FROM workspace_icp_profiles p
               LEFT JOIN workspaces w ON w.id = p.workspace_id
               WHERE p.workspace_id = ? ORDER BY p.name""",
            (workspace_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT p.*, w.slug AS ws_slug FROM workspace_icp_profiles p
               LEFT JOIN workspaces w ON w.id = p.workspace_id
               ORDER BY w.slug, p.name"""
        ).fetchall()
    out = []
    for row in rows:
        profile = _row_to_profile(row)
        profile["workspace"] = row["ws_slug"]
        out.append(profile)
    return out


def delete_profile(conn: sqlite3.Connection, workspace_id: str, name: str) -> bool:
    cur = conn.execute(
        "DELETE FROM workspace_icp_profiles WHERE workspace_id = ? AND name = ?",
        (workspace_id, normalize_term(name)),
    )
    return cur.rowcount > 0


# ── export / import ──────────────────────────────────────────────────────────

def export_document(profile: dict) -> dict:
    """A portable, workspace-free document for one profile.

    Deliberately carries no workspace_id: the point of exporting is to move a
    config to another workspace (or another machine), and an embedded id would
    either be ignored or, worse, honoured.
    """
    return {
        "kind": EXPORT_KIND,
        "version": EXPORT_VERSION,
        "name": profile["name"],
        "config": profile["config"],
        "config_hash": profile["config_hash"],
    }


def parse_document(doc: Any) -> tuple[Optional[str], dict]:
    """(name, config) from an export envelope or a bare config dict.

    A bare config is accepted so a hand-written `{"whitelist": [...]}` imports
    without ceremony. When the envelope carries a `config_hash`, it is verified
    against the config it travels with -- a document that has been edited
    without updating its hash would otherwise import under a version string
    that describes different rules.
    """
    if not isinstance(doc, dict):
        raise IcpError("ICP document must be a JSON object")
    if doc.get("kind") == EXPORT_KIND or "config" in doc:
        if doc.get("kind") not in (None, EXPORT_KIND):
            raise IcpError(f"not an ICP document: kind={doc.get('kind')!r}")
        version = doc.get("version", EXPORT_VERSION)
        if version != EXPORT_VERSION:
            raise IcpError(f"unsupported ICP document version: {version!r}")
        config = canonical_config(doc.get("config"))
        stated = doc.get("config_hash")
        if stated and stated != config_hash(config):
            raise IcpError(
                "config_hash in the document does not match its config "
                "(the file was edited without rehashing)"
            )
        return (doc.get("name"), config)
    return (None, canonical_config(doc))


# ── CLI entry points (own their connection) ──────────────────────────────────

def cli_set(workspace: str, name: Optional[str] = None, **fields: Any) -> dict:
    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, workspace)
        profile = set_profile(conn, ws["id"], name, **fields)
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "workspace": ws["slug"], **profile}


def cli_show(workspace: str, name: Optional[str] = None) -> dict:
    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, workspace)
        profile = require_profile(conn, ws["id"], name)
    finally:
        conn.close()
    return {"status": "ok", "workspace": ws["slug"], **profile}


def cli_list(workspace: Optional[str] = None) -> dict:
    conn = get_conn()
    try:
        ws_slug = None
        ws_id = None
        if workspace:
            ws = _resolve_workspace(conn, workspace)
            ws_slug, ws_id = ws["slug"], ws["id"]
        profiles = list_profiles(conn, ws_id)
    finally:
        conn.close()
    return {"status": "ok", "workspace": ws_slug, "count": len(profiles), "profiles": profiles}


def cli_delete(workspace: str, name: str) -> dict:
    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, workspace)
        removed = delete_profile(conn, ws["id"], name)
        conn.commit()
    finally:
        conn.close()
    if not removed:
        raise IcpError(f"no ICP profile named {name!r} in {ws['slug']}")
    return {"status": "ok", "workspace": ws["slug"], "name": normalize_term(name), "deleted": True}


def cli_export(workspace: str, name: Optional[str] = None, path: Optional[str] = None) -> dict:
    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, workspace)
        profile = require_profile(conn, ws["id"], name)
    finally:
        conn.close()
    doc = export_document(profile)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return {"status": "ok", "path": path, **doc}
    return {"status": "ok", **doc}


def cli_import(
    workspace: str,
    *,
    path: Optional[str] = None,
    payload: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    if not path and not payload:
        raise IcpError("icp import needs --file or --json")
    if path and payload:
        raise IcpError("pass either --file or --json, not both")
    raw = payload
    if path:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        doc = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        raise IcpError(f"could not parse ICP document as JSON: {exc}") from None

    doc_name, config = parse_document(doc)
    # --name overrides whatever the document calls itself, so one exported
    # profile can be imported into a workspace under several names.
    target = normalize_term(name) or normalize_term(doc_name) or DEFAULT_PROFILE_NAME

    conn = get_conn()
    try:
        ws = _resolve_workspace(conn, workspace)
        # replace=True: an import states the whole profile. Merging it into an
        # existing one of the same name would produce a config that matches
        # neither the file nor what was there before.
        profile = set_profile(conn, ws["id"], target, replace=True, **config)
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "workspace": ws["slug"], **profile}
