#!/usr/bin/env python3
"""Regression tests for session bug report 2026-06-11."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
DETECT = SCRIPTS / "detect_platform.py"
INSTALL_SH = ROOT / "install.sh"

_tmp = tempfile.mkdtemp()
from om_paths import (  # noqa: E402
    get_export_dir,
    get_input_dir,
    get_om_data_dir,
    resolve_project_path,
    set_data_root_override,
    set_working_root_override,
    working_paths_payload,
)

set_data_root_override(Path(_tmp))

import bounces  # noqa: E402
import pipeline as om  # noqa: E402
from workspace_routing import WORKSPACE_ROUTING_MULTI  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    work = tmp_path / "client-repo"
    work.mkdir()
    set_working_root_override(work)
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()
    om.set_workspace_routing(WORKSPACE_ROUTING_MULTI)
    om.create_workspace("Default", slug="default")
    yield work


def test_verify_email_sets_cloud_pending():
    lead = om.resolve_lead(
        email="verify@acme.com",
        name="Verify Lead",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    lead_id = int(lead["id"])
    conn = om.get_conn()
    conn.execute("UPDATE leads SET cloud_pending = 0 WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()

    result = bounces.verify_email(
        lead_id,
        status="valid",
        source="trykitt",
        source_detail="email-finder",
    )
    assert result["status"] == "recorded"

    conn = om.get_conn()
    row = conn.execute(
        "SELECT cloud_pending, email_verification_status FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["cloud_pending"] == 1
    assert row["email_verification_status"] == "valid"


def test_verify_email_batch_sets_cloud_pending():
    ids = []
    for i in range(2):
        lead = om.resolve_lead(
            email=f"batch{i}@acme.com",
            name=f"Batch {i}",
            company="Acme",
            company_domain="acme.com",
            source="manual",
        )
        ids.append(int(lead["id"]))
    conn = om.get_conn()
    conn.execute("UPDATE leads SET cloud_pending = 0")
    conn.commit()
    conn.close()

    out = bounces.verify_email_batch([
        {"lead_id": ids[0], "status": "valid", "source": "trykitt"},
        {"lead_id": ids[1], "status": "valid", "source": "trykitt"},
    ])
    assert out["recorded"] == 2

    conn = om.get_conn()
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE cloud_pending = 1"
    ).fetchone()["n"]
    conn.close()
    assert pending == 2


def test_om_paths_outreachmagic_layout():
    work = get_om_data_dir().parent
    assert get_om_data_dir() == work / "outreachmagic"
    assert get_input_dir() == work / "outreachmagic" / "imports"
    assert get_export_dir() == work / "outreachmagic" / "exports"
    paths = working_paths_payload()
    assert paths["imports"].endswith("outreachmagic/imports")
    assert paths["exports"].endswith("outreachmagic/exports")


def test_resolve_project_path_relative_to_imports():
    out = resolve_project_path("leads.csv", kind="input", for_write=True)
    assert out.parent == get_input_dir()
    assert out.name == "leads.csv"
    assert out.exists() is False
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("x", encoding="utf-8")
    assert out.read_text(encoding="utf-8") == "x"


def test_detect_platform_prefers_cursor_agent_env(tmp_path):
    home = tmp_path / "home"
    (home / ".hermes" / "skills").mkdir(parents=True)
    (home / ".cursor" / "skills").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home), "CURSOR_AGENT": "1"}
    proc = subprocess.run(
        [sys.executable, str(DETECT)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    data = json.loads(proc.stdout)
    assert data["platform"] == "cursor"
    assert data["skills_dir"].endswith(".cursor/skills")


def test_install_sh_non_tty_auto_inits(tmp_path):
    skills = tmp_path / "skills"
    om_skill = skills / "outreachmagic"
    scripts = om_skill / "scripts"
    scripts.mkdir(parents=True)
    (om_skill / "databases").mkdir(parents=True, exist_ok=True)
    pipeline = scripts / "pipeline.py"
    pipeline.write_text(
        "#!/usr/bin/env python3\nimport sys, json\n"
        "if 'init' in sys.argv: print('ok')\n"
        "else: print(json.dumps({'cloud_pending_leads': 0}))\n",
        encoding="utf-8",
    )
    pipeline.chmod(0o755)
    (scripts / "VERSION").write_text("9.9.9\n", encoding="utf-8")

    script = (
        f'YES=0; SKILLS_DIR="{skills}"; db_path="{om_skill}/databases/outreachmagic.db"\n'
        "if [[ $YES -eq 0 && -t 0 ]]; then echo INTERACTIVE; else\n"
        f'  python3 "{pipeline}" init --from-tag v9.9.9; echo INIT_OK; fi\n'
    )
    test_sh = tmp_path / "test_init.sh"
    test_sh.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(test_sh)],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert "INIT_OK" in proc.stdout
    assert "INTERACTIVE" not in proc.stdout


def test_email_finder_candidates_scoped_stats():
    ws = "default"
    for i, email in enumerate(["has@acme.com", "", "other@acme.com", ""]):
        lead = om.resolve_lead(
            email=email or None,
            name=f"Lead {i}",
            company="Acme",
            company_domain="acme.com",
            source="manual",
        )
        conn = om.get_conn()
        ws_row = om.resolve_workspace_identity(conn, ws)
        om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], int(lead["id"]))
        conn.commit()
        conn.close()

    conn = om.get_conn()
    import pipeline_lead_review as plr

    scope = plr.load_workspace_leads_for_review(
        conn,
        ws,
        no_email=False,
        require_domain=False,
        enrich_fn=om.enrich_lead_rows,
    )
    pool = [lead for lead in scope if not (lead.get("email") or "").strip()]
    candidates = plr.email_finder_candidates_from_leads(pool)
    skipped_has_email = sum(1 for lead in scope if (lead.get("email") or "").strip())
    conn.close()
    assert len(scope) == 4
    assert skipped_has_email == 2
    assert len(candidates) == 2


def test_show_json_includes_leads_alias():
    lead = om.resolve_lead(
        email="json@acme.com",
        name="JSON Lead",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    conn = om.get_conn()
    ws_row = om.resolve_workspace_identity(conn, "default")
    om.upsert_workspace_lead(conn, om.DEFAULT_ORG_ID, ws_row["id"], int(lead["id"]))
    conn.commit()
    conn.close()

    from data_freshness import attach_freshness

    leads = om.get_pipeline(workspace="default", limit=10)
    payload = attach_freshness(leads, last_pull=None)
    assert "leads" in payload
    assert "data" in payload
    assert len(payload["leads"]) >= 1
