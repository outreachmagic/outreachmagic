"""Duplicate lead detection and batch merge for outreachmagic."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

CONFIDENCE_ORDER = ("HIGH", "MEDIUM", "LOW", "ALL")
MIN_CONFIDENCE_DEFAULT_FIND = "MEDIUM"
MIN_CONFIDENCE_DEFAULT_MERGE = "ALL"

NAME_SUFFIX_RE = re.compile(
    r",\s*(?:MBA|M\.?Ed|Ph\.?D|MA|M\.?A|BCC|SHRM-CP|PHR|CPCC|CPRW|CCSP|M\.?S\.?Ed|"
    r"GCDF|LCPC|Ed\.?D|MPA|MPH|ASW|CPC|CIH|CSP|MEM|CEIC|CRA|J\.?D|NCC|P\.?E)\b",
    re.I,
)
TRAILING_SUFFIX_RE = re.compile(
    r"\b(?:MBA|M\.?Ed|Ph\.?D|MA|M\.?A|BCC|SHRM-CP|PHR|CPCC|CPRW|CCSP|M\.?S\.?Ed|"
    r"GCDF|LCPC|Ed\.?D|MPA|MPH|ASW|CPC|CIH|CSP|MEM|CEIC|CRA|J\.?D|NCC|P\.?E)\.?\s*$",
    re.I,
)
DR_PREFIX_RE = re.compile(r"^dr\.?\s+", re.I)

GENERIC_WORDS_RE = re.compile(
    r"\b(?:university|college|corp|corporation|inc|llc|ltd|co|company|technologies|"
    r"technology|systems|services|solutions|group|associates|partners|school|of|the|and|at|"
    r"incorporated)\b",
    re.I,
)
PUNCT_RE = re.compile(r"[,.\-()&]")


def normalize_name(name: Optional[str]) -> str:
    """Strip titles/suffixes, collapse whitespace, lowercase."""
    if not name:
        return ""
    text = str(name).strip()
    text = DR_PREFIX_RE.sub("", text)
    text = NAME_SUFFIX_RE.sub("", text)
    text = TRAILING_SUFFIX_RE.sub("", text)
    return " ".join(text.split()).lower()


def is_first_name_only(name: Optional[str]) -> bool:
    norm = normalize_name(name)
    return bool(norm) and " " not in norm


def normalize_company(company: Optional[str]) -> str:
    """Strip generic words and punctuation for comparison (not for acronyms)."""
    if not company:
        return ""
    text = PUNCT_RE.sub(" ", str(company))
    text = GENERIC_WORDS_RE.sub(" ", text)
    return " ".join(text.split()).lower()


ACRONYM_OMIT_WORDS = frozenset({
    "of", "the", "and", "at", "inc", "llc", "ltd", "corp", "corporation", "co",
    "company", "incorporated",
})


def is_acronym(short: Optional[str], long: Optional[str]) -> bool:
    """True when short is an acronym of long (check on original long name)."""
    if not short or not long:
        return False
    s = re.sub(r"[^a-zA-Z]", "", short).upper()
    if not s or len(s) > 12:
        return False
    words = re.sub(r"[^a-zA-Z\s]", " ", long).split()
    if not words:
        return False
    letters = "".join(
        w[0] for w in words
        if w and w.lower() not in ACRONYM_OMIT_WORDS
    ).upper()
    return letters == s


def _token_set(company: Optional[str]) -> set[str]:
    norm = normalize_company(company)
    return {t for t in norm.split() if len(t) > 1}


def companies_match(a: Optional[str], b: Optional[str]) -> bool:
    """Return True when two company names likely refer to the same org."""
    if not a or not b:
        return False
    a_raw, b_raw = a.strip(), b.strip()
    if not a_raw or not b_raw:
        return False
    if a_raw.lower() == b_raw.lower():
        return True
    a_norm, b_norm = normalize_company(a_raw), normalize_company(b_raw)
    if a_norm and b_norm and a_norm == b_norm:
        return True
    if a_norm and b_norm and (a_norm in b_norm or b_norm in a_norm):
        return True
    if is_acronym(a_raw, b_raw) or is_acronym(b_raw, a_raw):
        return True
    ta, tb = _token_set(a_raw), _token_set(b_raw)
    if not ta or not tb:
        return False
    overlap = ta & tb
    if len(overlap) >= 2:
        return True
    if len(overlap) >= 1 and (len(ta) <= 2 or len(tb) <= 2):
        return True
    return False


def company_match_tier(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """'exact', 'similar', or None."""
    if not a or not b:
        return None
    if a.strip().lower() == b.strip().lower():
        return "exact"
    a_norm, b_norm = normalize_company(a), normalize_company(b)
    if a_norm and b_norm and a_norm == b_norm:
        return "exact"
    if companies_match(a, b):
        return "similar"
    return None


def confidence_rank(level: str) -> int:
    return {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "ALL": 0}.get(level, 0)


def meets_min_confidence(level: str, minimum: str) -> bool:
    if minimum == "ALL":
        return True
    return confidence_rank(level) >= confidence_rank(minimum)


def _keep_score(lead: dict[str, Any]) -> tuple:
    name_parts = len(normalize_name(lead.get("name")).split())
    has_email = 1 if (lead.get("email") or "").strip() else 0
    has_li = 1 if (lead.get("linkedin_url") or "").strip() else 0
    return (has_email, has_li, name_parts, -int(lead["id"]))


def pick_keep_lead(leads: list[dict[str, Any]]) -> dict[str, Any]:
    return max(leads, key=_keep_score)


def _name_variations(leads: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lead in leads:
        raw = (lead.get("name") or "").strip()
        if raw and raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def _pair_key(keep_id: int, merge_id: int) -> tuple[int, int]:
    return (min(keep_id, merge_id), max(keep_id, merge_id))


def score_pair(
    keep: dict[str, Any],
    other: dict[str, Any],
    *,
    first_name_match: bool = False,
) -> tuple[str, str]:
    tier = company_match_tier(keep.get("company"), other.get("company"))
    if tier == "exact":
        return "HIGH", "exact_company"
    if tier == "similar":
        method = "first_name_similar_company" if first_name_match else "similar_company"
        return "MEDIUM", method
    return "LOW", "different_company"


def load_workspace_leads(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    tag_filter: Optional[str] = None,
    normalize_tag_fn: Callable[[str], str],
) -> list[dict[str, Any]]:
    join_tags = ""
    params: list[Any] = [workspace_id]
    if tag_filter:
        norm_tag = normalize_tag_fn(tag_filter)
        if "%" in norm_tag:
            join_tags = (
                " INNER JOIN workspace_lead_tags wlt "
                " ON wlt.workspace_id = wl.workspace_id AND wlt.lead_id = l.id "
                " AND wlt.tag LIKE ? "
            )
            params.append(norm_tag)
        else:
            join_tags = (
                " INNER JOIN workspace_lead_tags wlt "
                " ON wlt.workspace_id = wl.workspace_id AND wlt.lead_id = l.id "
                " AND wlt.tag = ? "
            )
            params.append(norm_tag)

    rows = conn.execute(
        f"""
        SELECT l.id, l.name, l.company, l.email, l.linkedin_url, l.created_at
        FROM leads l
        INNER JOIN workspace_leads wl ON wl.lead_id = l.id AND wl.workspace_id = ?
        {join_tags}
        ORDER BY l.id
        """,
        tuple(params),
    ).fetchall()
    lead_ids = [int(r["id"]) for r in rows]
    tags_by_lead: dict[int, list[str]] = {lid: [] for lid in lead_ids}
    if lead_ids:
        placeholders = ",".join("?" for _ in lead_ids)
        tag_rows = conn.execute(
            f"""
            SELECT lead_id, tag FROM workspace_lead_tags
            WHERE workspace_id = ? AND lead_id IN ({placeholders})
            ORDER BY tag
            """,
            (workspace_id, *lead_ids),
        ).fetchall()
        for tr in tag_rows:
            tags_by_lead.setdefault(int(tr["lead_id"]), []).append(tr["tag"])

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["tags"] = tags_by_lead.get(int(row["id"]), [])
        out.append(item)
    return out


def find_duplicates(
    conn: sqlite3.Connection,
    *,
    workspace_slug: str,
    tag_filter: Optional[str] = None,
    min_confidence: str = MIN_CONFIDENCE_DEFAULT_FIND,
    resolve_workspace_fn: Callable,
    normalize_tag_fn: Callable[[str], str],
) -> dict[str, Any]:
    ws_row = resolve_workspace_fn(conn, workspace_slug)
    if not ws_row:
        raise ValueError(f"workspace not found: {workspace_slug}")

    leads = load_workspace_leads(
        conn,
        workspace_id=ws_row["id"],
        tag_filter=tag_filter,
        normalize_tag_fn=normalize_tag_fn,
    )
    candidates: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()

    by_norm_name: dict[str, list[dict[str, Any]]] = {}
    for lead in leads:
        norm = normalize_name(lead.get("name"))
        if not norm:
            continue
        by_norm_name.setdefault(norm, []).append(lead)

    stats = {
        "high_confidence": 0,
        "medium_confidence": 0,
        "low_confidence": 0,
    }

    def add_candidate(keep: dict, other: dict, confidence: str, method: str) -> None:
        if int(keep["id"]) == int(other["id"]):
            return
        if not meets_min_confidence(confidence, min_confidence):
            return
        key = _pair_key(int(keep["id"]), int(other["id"]))
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        group = [keep, other]
        candidates.append(
            {
                "keep_id": int(keep["id"]),
                "merge_id": int(other["id"]),
                "keep_name": keep.get("name") or "",
                "merge_name": other.get("name") or "",
                "keep_company": keep.get("company") or "",
                "merge_company": other.get("company") or "",
                "keep_email": keep.get("email"),
                "merge_email": other.get("email"),
                "keep_linkedin": keep.get("linkedin_url"),
                "merge_linkedin": other.get("linkedin_url"),
                "keep_tags": list(keep.get("tags") or []),
                "merge_tags": list(other.get("tags") or []),
                "confidence": confidence,
                "match_method": method,
                "name_variations": _name_variations(group),
            }
        )
        bucket = f"{confidence.lower()}_confidence"
        if bucket in stats:
            stats[bucket] += 1

    # Pass 1: full-name groups
    for _norm, group in by_norm_name.items():
        if len(group) < 2:
            continue
        full_names = [g for g in group if not is_first_name_only(g.get("name"))]
        if len(full_names) >= 2:
            keep = pick_keep_lead(full_names)
            for other in full_names:
                if int(other["id"]) == int(keep["id"]):
                    continue
                confidence, method = score_pair(keep, other)
                add_candidate(keep, other, confidence, method)

    # Pass 2: first-name-only → full-name at similar company
    first_only = [lead for lead in leads if is_first_name_only(lead.get("name"))]
    full_by_first: dict[str, list[dict[str, Any]]] = {}
    for lead in leads:
        if is_first_name_only(lead.get("name")):
            continue
        parts = normalize_name(lead.get("name")).split()
        if parts:
            full_by_first.setdefault(parts[0], []).append(lead)

    for fo in first_only:
        first = normalize_name(fo.get("name")).split()[0]
        for full in full_by_first.get(first, []):
            if int(full["id"]) == int(fo["id"]):
                continue
            tier = company_match_tier(fo.get("company"), full.get("company"))
            if tier in ("exact", "similar"):
                keep = pick_keep_lead([full, fo])
                other = fo if int(keep["id"]) == int(full["id"]) else full
                if int(keep["id"]) == int(other["id"]):
                    continue
                confidence, method = score_pair(keep, other, first_name_match=True)
                if confidence == "HIGH":
                    confidence = "MEDIUM"
                    method = "first_name_similar_company"
                add_candidate(keep, other, confidence, method)

    candidates.sort(key=lambda c: (-confidence_rank(c["confidence"]), c["keep_id"], c["merge_id"]))
    return {
        "workspace": ws_row["slug"],
        "tag_filter": tag_filter,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": {
            "total_leads_scanned": len(leads),
            "candidates_found": len(candidates),
            **stats,
        },
        "candidates": candidates,
    }


def filter_candidates(
    payload: dict[str, Any],
    *,
    min_confidence: str = "ALL",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cand in payload.get("candidates") or []:
        if meets_min_confidence(str(cand.get("confidence") or ""), min_confidence):
            out.append(cand)
    return out


def validate_candidate_ids(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split candidates into valid pairs and skipped orphans."""
    if not candidates:
        return [], []
    ids = {int(c.get("keep_id")) for c in candidates} | {int(c.get("merge_id")) for c in candidates}
    placeholders = ",".join("?" for _ in ids)
    existing = {
        int(r["id"])
        for r in conn.execute(
            f"SELECT id FROM leads WHERE id IN ({placeholders})",
            tuple(sorted(ids)),
        ).fetchall()
    }
    valid: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for cand in candidates:
        keep_id = int(cand.get("keep_id"))
        merge_id = int(cand.get("merge_id"))
        if keep_id in existing and merge_id in existing:
            valid.append(cand)
        else:
            missing = []
            if keep_id not in existing:
                missing.append(f"keep_id {keep_id}")
            if merge_id not in existing:
                missing.append(f"merge_id {merge_id}")
            skipped.append({**cand, "error": "lead not found", "missing": missing})
    return valid, skipped


def batch_merge_candidates(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    commit: bool = False,
    reason: str = "dedup",
    merge_leads_fn: Callable,
    progress_every: int = 50,
) -> dict[str, Any]:
    started = time.time()
    valid, skipped = validate_candidate_ids(conn, candidates)
    attempted = len(valid)
    succeeded = 0
    failures: list[dict[str, Any]] = [
        {
            "keep_id": int(s.get("keep_id")),
            "merge_id": int(s.get("merge_id")),
            "error": s.get("error", "lead not found"),
        }
        for s in skipped
    ]

    merged_ids: set[int] = set()
    for i, cand in enumerate(valid, 1):
        keep_id = int(cand["keep_id"])
        merge_id = int(cand["merge_id"])
        if merge_id in merged_ids:
            failures.append(
                {
                    "keep_id": keep_id,
                    "merge_id": merge_id,
                    "error": "merge_id already merged in this batch",
                }
            )
            continue
        if keep_id in merged_ids:
            failures.append(
                {
                    "keep_id": keep_id,
                    "merge_id": merge_id,
                    "error": "keep_id was merged away in this batch",
                }
            )
            continue
        if commit:
            result = merge_leads_fn(keep_id, merge_id, reason=reason, conn=conn)
            if result.get("status") == "merged":
                succeeded += 1
                merged_ids.add(merge_id)
            else:
                failures.append(
                    {
                        "keep_id": keep_id,
                        "merge_id": merge_id,
                        "error": result.get("error") or result.get("status") or "merge failed",
                    }
                )
        else:
            succeeded += 1
        if progress_every and i % progress_every == 0:
            print(
                f"  [{i}/{attempted}] {succeeded} ok, {len(failures)} failed/skipped",
                flush=True,
            )

    if commit:
        conn.commit()

    elapsed = round(time.time() - started, 2)
    failed = len(failures)
    status = "completed" if commit else "dry_run"
    return {
        "status": status,
        "total_candidates": len(candidates),
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_orphans": len(skipped),
        "reason": reason,
        "elapsed_seconds": elapsed,
        "failures": failures,
    }


def load_candidates_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
