#!/usr/bin/env python3
"""Generate lead-review-presets.json from pipeline_lead_review.py (single source of truth)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/outreachmagic/scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline_lead_review as plr  # noqa: E402

SKILL_OUT = ROOT / "skills/outreachmagic/data/lead-review-presets.json"
WBHK_OUT = ROOT.parent / "wbhk-app/src/lib/data/lead-review-presets.json"


def _field_defs() -> list[dict]:
    out: list[dict] = []
    for key, defn in plr.FIELD_DEFS.items():
        if key == "linkedin":
            continue
        entry: dict = {
            "key": key,
            "type": defn.get("type", "string"),
            "editable": bool(defn.get("editable")),
            "in_presets": list(defn.get("presets", ())),
        }
        scope = defn.get("scope")
        if scope:
            entry["scope"] = scope
        out.append(entry)
    out.sort(key=lambda item: item["key"])
    return out


def build_payload() -> dict:
    return {
        "version": Path(SCRIPTS / "VERSION").read_text(encoding="utf-8").strip(),
        "sheet_legend_note": plr.SHEET_LEGEND_NOTE,
        "field_aliases": plr.FIELD_ALIASES,
        "column_groups": plr.COLUMN_GROUPS,
        "field_defs": _field_defs(),
        "preset_keys": plr.PRESET_KEYS,
        "lead_review_presets": list(plr.PRESET_KEYS.keys()),
        "pipeline_stages": list(plr.PIPELINE_STAGES),
        "lead_sentiment_values": list(plr.LEAD_SENTIMENT_VALUES),
        "field_display_names": plr.FIELD_DISPLAY_NAMES,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    payload = build_payload()
    write_json(SKILL_OUT, payload)
    print(f"Wrote {SKILL_OUT}")
    if WBHK_OUT.parent.exists():
        write_json(WBHK_OUT, payload)
        print(f"Wrote {WBHK_OUT}")
    else:
        print(f"Skipped wbhk sync (missing {WBHK_OUT.parent})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
