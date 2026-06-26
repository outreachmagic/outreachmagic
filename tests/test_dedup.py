#!/usr/bin/env python3
"""Tests for pipeline dedup find/merge."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import pipeline_dedup as dedup  # noqa: E402


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _setup_workspace() -> str:
    ws = om.create_workspace("Dedup Test", slug="dedup-ws")
    return f"ws_{ws['slug']}"


def _add(ws_id: str, name: str, company: str, *, email: str | None = None) -> int:
    r = om.resolve_lead(name=name, company=company, email=email)
    lead_id = int(r["id"])
    conn = om.get_conn()
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_id, lead_id)
    conn.commit()
    conn.close()
    om.tag_add(ws_id, lead_id, "dedup-test")
    return lead_id


class TestMatching:
    def test_normalize_name_strips_suffix(self):
        assert dedup.normalize_name("Abby Schoenbeck, MBA") == dedup.normalize_name("Abby Schoenbeck")

    def test_acronym_amd(self):
        assert dedup.companies_match("AMD", "Advanced Micro Devices, Inc.")

    def test_acronym_unf(self):
        assert dedup.companies_match("UNF", "University of North Florida")

    def test_similar_copper_mountain(self):
        assert dedup.company_match_tier(
            "Copper Mountain Community College",
            "Copper Mountain College",
        ) == "similar"

    def test_different_company_not_match(self):
        assert not dedup.companies_match("Reese Agency", "Insight Global")


class TestFindMerge:
    def test_find_and_merge_commit(self):
        _reset_db()
        ws_id = _setup_workspace()
        id_a = _add(ws_id, "Abby Schoenbeck", "Acme University")
        id_b = _add(ws_id, "Abby Schoenbeck, MBA", "Acme University")

        conn = om.get_conn()
        payload = dedup.find_duplicates(
            conn,
            workspace_slug="dedup-ws",
            tag_filter="dedup-test",
            min_confidence="ALL",
            resolve_workspace_fn=om.resolve_workspace_identity,
            normalize_tag_fn=om.normalize_tag,
        )
        conn.close()
        assert payload["stats"]["candidates_found"] >= 1
        pair = next(c for c in payload["candidates"] if {c["keep_id"], c["merge_id"]} == {id_a, id_b})
        assert pair["confidence"] == "HIGH"

        conn = om.get_conn()
        before = conn.execute("SELECT COUNT(*) AS n FROM lead_merges WHERE reason = 'dedup'").fetchone()["n"]
        result = dedup.batch_merge_candidates(
            conn,
            [pair],
            commit=True,
            reason="dedup",
            merge_leads_fn=om.merge_leads,
        )
        after = conn.execute("SELECT COUNT(*) AS n FROM lead_merges WHERE reason = 'dedup'").fetchone()["n"]
        conn.close()
        assert result["status"] == "completed"
        assert result["succeeded"] == 1
        assert after == before + 1

    def test_merge_dry_run_no_commit(self):
        _reset_db()
        ws_id = _setup_workspace()
        _add(ws_id, "Abby Schoenbeck", "Acme University")
        _add(ws_id, "Abby Schoenbeck, MBA", "Acme University")

        conn = om.get_conn()
        payload = dedup.find_duplicates(
            conn,
            workspace_slug="dedup-ws",
            tag_filter="dedup-test",
            min_confidence="ALL",
            resolve_workspace_fn=om.resolve_workspace_identity,
            normalize_tag_fn=om.normalize_tag,
        )
        result = dedup.batch_merge_candidates(
            conn,
            payload["candidates"][:1],
            commit=False,
            merge_leads_fn=om.merge_leads,
        )
        count = conn.execute("SELECT COUNT(*) AS n FROM lead_merges").fetchone()["n"]
        conn.close()
        assert result["status"] == "dry_run"
        assert count == 0

    def test_orphan_skip(self):
        _reset_db()
        conn = om.get_conn()
        result = dedup.batch_merge_candidates(
            conn,
            [{"keep_id": 1, "merge_id": 99999}],
            commit=True,
            merge_leads_fn=om.merge_leads,
        )
        conn.close()
        assert result["failed"] >= 1
        assert result["skipped_orphans"] == 1
        assert result["failures"][0]["error"] == "lead not found"

    def test_first_name_match(self):
        _reset_db()
        ws_id = _setup_workspace()
        _add(ws_id, "Vanessa", "M.C. Dean")
        _add(ws_id, "Vanessa Fountaine", "M.C. Dean")

        conn = om.get_conn()
        payload = dedup.find_duplicates(
            conn,
            workspace_slug="dedup-ws",
            tag_filter="dedup-test",
            min_confidence="ALL",
            resolve_workspace_fn=om.resolve_workspace_identity,
            normalize_tag_fn=om.normalize_tag,
        )
        conn.close()
        methods = {c["match_method"] for c in payload["candidates"]}
        assert "first_name_similar_company" in methods
