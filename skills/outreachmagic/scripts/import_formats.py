"""CSV/JSON import row normalization, format detection, and dry-run preview."""

from __future__ import annotations

import re
from typing import Any, Optional

SALES_NAV_SIGNATURE_HEADERS = frozenset({
    "corporate website",
    "member linkedin sales nav id",
    "linkedin industry",
    "first name",
    "job title",
    "linkedin company location",
    "member linkedin id",
})

# Lowercase trimmed header → canonical column name used before PROFILE_ALIASES
HEADER_ALIASES: dict[str, str] = {
    "first name": "first_name",
    "last name": "last_name",
    "job title": "title",
    "linkedin url": "linkedin",
    "corporate website": "company_domain",
    "linkedin industry": "industry",
    "linkedin employees": "headcount",
    "linkedin company employee count": "headcount",
    "member linkedin id": "_member_linkedin_id",
    "member linkedin sales nav id": "member linkedin sales nav id",
    "phone": "phone",
    "summary": "_summary_note",
    "headline": "_headline_note",
}

OM_MAPPED_FIELDS = frozenset({
    "email", "linkedin", "name", "title", "company", "industry", "headcount",
    "location_city", "location_state", "location_country",
    "company_domain", "hq_city", "hq_state", "hq_country",
    "external_id", "mailmerge_first_name", "mailmerge_company_name", "notes",
    "first_name", "last_name",
})


def normalize_header_key(key: str) -> str:
    text = str(key or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def parse_comma_location(value: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse 'City, State, Country' (Sales Nav / Vayne style)."""
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    if not parts:
        return None, None, None
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 2:
        return parts[0], parts[1], None
    city = parts[0]
    country = parts[-1]
    state = ", ".join(parts[1:-1]) if len(parts) > 2 else parts[1]
    return city, state, country


def detect_import_format(headers: set[str]) -> tuple[str, str]:
    """Return (format_id, confidence)."""
    normalized = {normalize_header_key(h) for h in headers if h}
    hits = sum(1 for sig in SALES_NAV_SIGNATURE_HEADERS if sig in normalized)
    if hits >= 3:
        return "sales_navigator", "high" if hits >= 5 else "medium"
    if {"first_name", "last_name"}.issubset(normalized) or "corporate website" in normalized:
        return "sales_navigator", "medium"
    return "generic", "low"


def _pick_best_linkedin_from_raw(raw: dict[str, Any]) -> Optional[str]:
    """Prefer public linkedin.com/in/slug over Sales Nav hash URLs."""
    from workspace_routing import is_sales_nav_hash_slug, normalize_linkedin

    public = None
    for key, val in raw.items():
        if not key:
            continue
        nk = normalize_header_key(str(key))
        if nk not in ("linkedin", "linkedin url", "linkedin_url", "profile_url"):
            continue
        text = str(val or "").strip()
        if not text:
            continue
        norm = normalize_linkedin(text)
        if norm and not is_sales_nav_hash_slug(norm.split("/")[-1]):
            return text
        if not public:
            public = text
    return public


def _set_if_empty(row: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    text = str(value).strip() if not isinstance(value, str) else value.strip()
    if not text:
        return
    if not row.get(key):
        row[key] = text


def normalize_import_row(raw: dict[str, Any], *, import_format: Optional[str] = None) -> dict[str, Any]:
    """Map export headers (Sales Nav, Vayne, etc.) to canonical import columns."""
    row: dict[str, Any] = {}
    notes_parts: list[str] = []

    for key, val in raw.items():
        if not key or val is None:
            continue
        nk = normalize_header_key(str(key))
        target = HEADER_ALIASES.get(nk)
        if target is None:
            target = nk.replace(" ", "_")
        if target.startswith("_"):
            if target == "_summary_note" and str(val).strip():
                notes_parts.append(f"LinkedIn bio: {str(val).strip()}")
            elif target == "_headline_note" and str(val).strip():
                notes_parts.append(f"LinkedIn headline: {str(val).strip()}")
            elif target == "_member_linkedin_id" and str(val).strip():
                urn = str(val).strip()
                if not urn.startswith("sales_navigator:"):
                    urn = f"sales_navigator:{urn}"
                _set_if_empty(row, "external_id", urn)
            continue
        if target == "location":
            city, state, country = parse_comma_location(str(val))
            _set_if_empty(row, "location_city", city)
            _set_if_empty(row, "location_state", state)
            _set_if_empty(row, "location_country", country)
            continue
        if target == "linkedin company location":
            city, state, country = parse_comma_location(str(val))
            _set_if_empty(row, "hq_city", city)
            _set_if_empty(row, "hq_state", state)
            _set_if_empty(row, "hq_country", country)
            continue
        _set_if_empty(row, target, val)

    # Spaced headers not caught by alias table
    nk_map = {normalize_header_key(str(k)): v for k, v in raw.items() if k}
    if "location" in nk_map and not row.get("location_city"):
        city, state, country = parse_comma_location(str(nk_map["location"]))
        _set_if_empty(row, "location_city", city)
        _set_if_empty(row, "location_state", state)
        _set_if_empty(row, "location_country", country)
    if "linkedin company location" in nk_map and not row.get("hq_city"):
        city, state, country = parse_comma_location(str(nk_map["linkedin company location"]))
        _set_if_empty(row, "hq_city", city)
        _set_if_empty(row, "hq_state", state)
        _set_if_empty(row, "hq_country", country)

    first = str(row.pop("first_name", None) or nk_map.get("first name") or "").strip()
    last = str(row.pop("last_name", None) or nk_map.get("last name") or "").strip()
    if not row.get("name") and first:
        row["name"] = f"{first} {last}".strip() if last else first
    if first and not row.get("mailmerge_first_name"):
        row["mailmerge_first_name"] = first
    company = str(row.get("company") or nk_map.get("company") or "").strip()
    if company and not row.get("mailmerge_company_name"):
        row["mailmerge_company_name"] = company

    if notes_parts:
        existing = str(row.get("notes") or "").strip()
        combined = "\n\n".join(notes_parts)
        row["notes"] = f"{existing}\n\n{combined}".strip() if existing else combined

    # Preserve original keys needed by IMPORT_EXTRA_FIELDS (sales nav urn column)
    for preserve in (
        "member linkedin sales nav id",
        "linkedin_sales_nav_id",
        "sales_nav_id",
        "list_source",
        "import_name",
        "tags",
        "lead_status",
        "lead_sentiment",
        "contact_order",
        "is_connected_linkedin",
        "is_linkedin_request_pending",
        "unified_lead_id",
        "source_id",
        "external_id",
    ):
        for rk, rv in raw.items():
            if normalize_header_key(rk) == normalize_header_key(preserve.replace("_", " ")) or rk == preserve:
                if rv is not None and str(rv).strip() and preserve not in row:
                    row[preserve if " " in preserve or preserve in raw else rk] = rv

    best_li = _pick_best_linkedin_from_raw(raw)
    if best_li:
        row["linkedin"] = best_li

    for rk, rv in raw.items():
        if not rk or not str(rk).startswith("mailmerge_") or rv is None or not str(rv).strip():
            continue
        row.setdefault(str(rk), rv)

    return row


def preprocess_import_rows(
    rows: list[dict],
    *,
    import_format: Optional[str] = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Normalize all rows; auto-detect format when import_format is None or 'auto'."""
    if not rows:
        return [], {"detected_format": "generic", "confidence": "low", "fields_mapped": [], "fields_dropped": []}

    headers: set[str] = set()
    for row in rows[:20]:
        headers.update(row.keys())

    detected, confidence = detect_import_format(headers)
    fmt = import_format or "auto"
    if fmt in (None, "", "auto"):
        active_format = detected
    else:
        active_format = import_format

    normalized = [normalize_import_row(r, import_format=active_format) for r in rows]

    mapped: set[str] = set()
    dropped: set[str] = set()
    sample = normalized[0] if normalized else {}
    for key in headers:
        nk = normalize_header_key(key)
        if nk in SALES_NAV_SIGNATURE_HEADERS or nk in HEADER_ALIASES:
            mapped.add(nk)
        elif nk in {"company", "email", "linkedin url"}:
            mapped.add(nk)
        else:
            dropped.add(nk)

    meta = {
        "detected_format": active_format,
        "confidence": confidence if fmt in (None, "", "auto") else "explicit",
        "fields_mapped": sorted(mapped),
        "fields_dropped": sorted(d for d in dropped if d not in mapped),
        "sample_preview": {k: sample.get(k) for k in sorted(sample.keys()) if sample.get(k)},
    }
    return normalized, meta


def build_import_quality_warnings(summary: dict[str, Any]) -> list[str]:
    """Post-import hints when mapping likely failed."""
    warnings: list[str] = []
    results = summary.get("results") or []
    if not results:
        return warnings
    sample = summary.get("sample_preview") or {}
    if sample.get("name"):
        return warnings
    mapped = set(summary.get("fields_mapped") or [])
    if "first name" not in mapped and "first_name" not in mapped:
        return warnings
    unknown = sum(1 for r in results if str(r.get("name") or "") in ("", "Unknown"))
    if unknown and unknown >= max(1, len(results) // 2):
        warnings.append(
            f"{unknown}/{len(results)} leads have no name — check CSV headers "
            "(Sales Nav / Vayne exports need first name + last name columns)."
        )
    return warnings
