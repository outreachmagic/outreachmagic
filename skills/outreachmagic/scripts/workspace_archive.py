"""
Export / purge workspace-scoped data from local SQLite (no network).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from workspace_routing import DEFAULT_ORG_ID


def resolve_archive_lead_ids(
    conn: sqlite3.Connection,
    org_id: str,
    workspace_slug: str,
    *,
    resolve_workspace_identity_fn: Callable,
) -> tuple[set[int], dict[str, Any]]:
    """Return lead IDs belonging to a logical workspace + metadata for dry-run."""
    ws_row = resolve_workspace_identity_fn(conn, workspace_slug, org_id=org_id)
    lead_ids: set[int] = set()
    meta: dict[str, Any] = {"workspace_slug": workspace_slug, "workspace_id": None}

    if ws_row:
        meta["workspace_id"] = ws_row["id"]
        rows = conn.execute(
            "SELECT lead_id FROM workspace_leads WHERE org_id = ? AND workspace_id = ?",
            (org_id, ws_row["id"]),
        ).fetchall()
        lead_ids.update(int(r["lead_id"]) for r in rows)

    prefix = f"{workspace_slug.strip()} |"
    rows = conn.execute(
        """
        SELECT id FROM leads
        WHERE COALESCE(original_source_detail, '') LIKE ? || '%'
           OR COALESCE(latest_source_detail, '') LIKE ? || '%'
        """,
        (prefix, prefix),
    ).fetchall()
    lead_ids.update(int(r["id"]) for r in rows)

    campaign_prefixes: list[str] = []
    if ws_row:
        maps = conn.execute(
            """
            SELECT campaign_name_normalized FROM campaign_workspace_map
            WHERE org_id = ? AND workspace_id = ? AND is_active = 1
            """,
            (org_id, ws_row["id"]),
        ).fetchall()
        campaign_prefixes = [r["campaign_name_normalized"] for r in maps if r["campaign_name_normalized"]]

    for camp in campaign_prefixes:
        rows = conn.execute(
            """
            SELECT DISTINCT lead_id FROM events
            WHERE json_extract(metadata_json, '$.campaign') = ?
            """,
            (camp,),
        ).fetchall()
        for r in rows:
            if r["lead_id"]:
                lead_ids.add(int(r["lead_id"]))

    meta["lead_count"] = len(lead_ids)
    meta["campaign_prefixes"] = campaign_prefixes
    return lead_ids, meta


def _copy_table_rows(
    src: sqlite3.Connection,
    dest: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple,
    *,
    dest_columns: Optional[list[str]] = None,
):
    if dest_columns:
        cols = dest_columns
    else:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()]
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    rows = src.execute(f"SELECT {col_list} FROM {table} WHERE {where_sql}", params).fetchall()
    if not rows:
        return 0
    dest.executemany(
        f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
        [tuple(r) for r in rows],
    )
    return len(rows)


def export_workspace_archive(
    conn: sqlite3.Connection,
    org_id: str,
    workspace_slug: str,
    output_path: Path,
    *,
    resolve_workspace_identity_fn: Callable,
    init_schema_fn: Callable[[sqlite3.Connection], None],
) -> dict[str, Any]:
    lead_ids, meta = resolve_archive_lead_ids(
        conn, org_id, workspace_slug, resolve_workspace_identity_fn=resolve_workspace_identity_fn
    )
    if not lead_ids:
        raise ValueError(f"No leads matched workspace '{workspace_slug}'")

    output_path = output_path.resolve()
    if output_path.exists():
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    dest = sqlite3.connect(str(output_path))
    dest.row_factory = sqlite3.Row
    try:
        init_schema_fn(dest)
        dest.commit()

        id_list = sorted(lead_ids)
        placeholders = ",".join("?" for _ in id_list)

        company_ids: set[int] = set()
        for row in conn.execute(
            f"SELECT DISTINCT company_id FROM leads WHERE id IN ({placeholders}) AND company_id IS NOT NULL",
            id_list,
        ).fetchall():
            company_ids.add(int(row["company_id"]))

        if company_ids:
            cp = ",".join("?" for _ in company_ids)
            _copy_table_rows(
                conn, dest, "companies", f"id IN ({cp})", tuple(company_ids)
            )

        n_leads = _copy_table_rows(conn, dest, "leads", f"id IN ({placeholders})", tuple(id_list))

        _copy_table_rows(
            conn,
            dest,
            "lead_identities",
            f"org_id = ? AND lead_id IN ({placeholders})",
            (org_id, *id_list),
        )

        _copy_table_rows(
            conn, dest, "events", f"lead_id IN ({placeholders})", tuple(id_list)
        )

        ws_id = meta.get("workspace_id")
        if ws_id:
            _copy_table_rows(
                conn,
                dest,
                "workspace_leads",
                f"org_id = ? AND workspace_id = ? AND lead_id IN ({placeholders})",
                (org_id, ws_id, *id_list),
            )
            _copy_table_rows(
                conn,
                dest,
                "workspace_lead_events",
                f"org_id = ? AND workspace_id = ? AND lead_id IN ({placeholders})",
                (org_id, ws_id, *id_list),
            )
            _copy_table_rows(
                conn,
                dest,
                "workspace_lead_tags",
                f"workspace_id = ? AND lead_id IN ({placeholders})",
                (ws_id, *id_list),
            )

        n_events = dest.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        dest.commit()
    finally:
        dest.close()

    manifest = {
        "workspace": workspace_slug,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "lead_count": n_leads,
        "event_count": n_events,
        "file_bytes": output_path.stat().st_size,
        "output": str(output_path),
    }
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest["manifest"] = str(manifest_path)
    return manifest


def purge_workspace_archive(
    conn: sqlite3.Connection,
    org_id: str,
    workspace_slug: str,
    *,
    resolve_workspace_identity_fn: Callable,
    vacuum: bool = False,
) -> dict[str, Any]:
    lead_ids, meta = resolve_archive_lead_ids(
        conn, org_id, workspace_slug, resolve_workspace_identity_fn=resolve_workspace_identity_fn
    )
    if not lead_ids:
        return {"purged_leads": 0, "message": "No matching leads"}

    id_list = sorted(lead_ids)
    placeholders = ",".join("?" for _ in id_list)
    ws_id = meta.get("workspace_id")

    if ws_id:
        conn.execute(
            f"DELETE FROM workspace_lead_events WHERE workspace_id = ? AND lead_id IN ({placeholders})",
            (ws_id, *id_list),
        )
        conn.execute(
            f"DELETE FROM workspace_lead_tags WHERE workspace_id = ? AND lead_id IN ({placeholders})",
            (ws_id, *id_list),
        )
        conn.execute(
            f"DELETE FROM workspace_leads WHERE workspace_id = ? AND lead_id IN ({placeholders})",
            (ws_id, *id_list),
        )

    conn.execute(f"DELETE FROM events WHERE lead_id IN ({placeholders})", tuple(id_list))

    purged = 0
    for lid in id_list:
        other = conn.execute(
            "SELECT 1 FROM workspace_leads WHERE lead_id = ? LIMIT 1", (lid,)
        ).fetchone()
        ev = conn.execute("SELECT 1 FROM events WHERE lead_id = ? LIMIT 1", (lid,)).fetchone()
        if other or ev:
            continue
        conn.execute("DELETE FROM lead_identities WHERE lead_id = ?", (lid,))
        conn.execute("DELETE FROM lead_personalization WHERE lead_id = ?", (lid,))
        conn.execute("DELETE FROM leads WHERE id = ?", (lid,))
        purged += 1

    conn.commit()
    if vacuum:
        conn.execute("VACUUM")

    return {"purged_leads": purged, "workspace": workspace_slug, "matched_leads": len(lead_ids)}
