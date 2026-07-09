"""
Generic tracking for async provider batch jobs (MillionVerifier bulk
verification, Scrubby deep verification, and future bulk providers of either
kind — email verification or email finding).

One table (`provider_batch_jobs`), one small set of functions, keyed by a
`provider` string — adding a new provider needs no new table and no new code
path here, just a new `provider` value from the caller.
"""

import json
from typing import Any, Optional

from db_conn import get_conn
from workspace_routing import resolve_workspace_identity


def record_batch_job(
    *,
    provider: str,
    kind: str,
    job_id: str,
    item_count: int,
    item_set_hash: str,
    workspace: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict:
    """Record a newly-submitted provider batch job."""
    conn = get_conn()
    try:
        workspace_id = None
        if workspace:
            ws_row = resolve_workspace_identity(conn, workspace)
            workspace_id = ws_row["id"] if ws_row else None
        cur = conn.execute(
            """INSERT INTO provider_batch_jobs
               (provider, kind, job_id, workspace_id, item_count, item_set_hash, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (provider, kind, job_id, workspace_id, item_count, item_set_hash,
             json.dumps(metadata) if metadata else None),
        )
        conn.commit()
        return {"status": "recorded", "id": cur.lastrowid, "provider": provider, "job_id": job_id}
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("metadata_json"):
        try:
            d["metadata"] = json.loads(d["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = None
    return d


def find_pending_batch_job(*, provider: str, item_set_hash: str) -> Optional[dict]:
    """Most recent not-yet-downloaded job for this provider + exact item set."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT * FROM provider_batch_jobs
               WHERE provider = ? AND item_set_hash = ? AND status != 'downloaded'
               ORDER BY submitted_at DESC LIMIT 1""",
            (provider, item_set_hash),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def mark_batch_job_status(*, provider: str, job_id: str, status: str) -> dict:
    conn = get_conn()
    try:
        completed_at = "datetime('now')" if status in ("completed", "downloaded", "failed") else "completed_at"
        conn.execute(
            f"""UPDATE provider_batch_jobs SET status = ?, completed_at = {completed_at}
                WHERE provider = ? AND job_id = ?""",
            (status, provider, job_id),
        )
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


def list_batch_jobs(*, provider: Optional[str] = None, workspace: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    try:
        clauses, params = [], []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if workspace:
            ws_row = resolve_workspace_identity(conn, workspace)
            clauses.append("workspace_id = ?")
            params.append(ws_row["id"] if ws_row else "")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM provider_batch_jobs {where} ORDER BY submitted_at DESC", params,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
