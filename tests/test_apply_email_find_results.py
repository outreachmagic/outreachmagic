"""Tests for apply-email-find-results fast path and companion routing."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
EMAIL_SCRIPTS = ROOT / "skills" / "email-finder" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(EMAIL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(EMAIL_SCRIPTS))

import pipeline as om  # noqa: E402

import companion_common as ef_cc  # noqa: E402


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


@pytest.fixture(autouse=True)
def fresh_db():
    _reset_db()
    yield


def test_apply_email_find_results_updates_email_tags_and_verification():
    lead = om.resolve_lead(
        email=None,
        name="Fast Path",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    lead_id = int(lead["id"])
    om.create_workspace("Fast WS", slug="fastws")

    summary = om.apply_email_find_results(
        [{
            "id": lead_id,
            "email": "fast@acme.com",
            "tags": ["trykitt_attempted"],
            "list_source": "trykitt",
            "notes": "trykitt valid",
            "_verify_provider": "trykitt",
            "_verify_validity": "valid",
        }],
        workspace="fastws",
        source="trykitt",
        source_detail="email-finder/batch",
    )
    assert summary["matched"] == 1
    assert summary["enriched"] == 1
    assert summary["tagged"] == 1
    assert summary["recorded"] == 1
    assert summary["mode"] == "apply_email_find_results"

    conn = om.get_conn()
    row = conn.execute(
        "SELECT email, latest_source FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    tag = conn.execute(
        "SELECT tag FROM workspace_lead_tags WHERE lead_id = ? AND tag = ?",
        (lead_id, "trykitt_attempted"),
    ).fetchone()
    ver = conn.execute(
        "SELECT status, source FROM lead_email_verification WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    assert row["email"] == "fast@acme.com"
    assert row["latest_source"] == "trykitt"
    assert tag is not None
    assert ver["status"] == "valid"
    assert ver["source"] == "trykitt"


def test_import_rows_all_have_lead_id():
    assert om.import_rows_all_have_lead_id([{"id": 1}, {"lead_id": 2}])
    assert not om.import_rows_all_have_lead_id([{"id": 1}, {"name": "x"}])


def _make_workspace(slug: str = "fastws") -> str:
    om.create_workspace("Fast WS", slug=slug)
    return slug


def _profile_row(
    lead_id: int,
    *,
    email: str = "",
    validity: str = "valid",
    include_email_tag: bool = True,
) -> dict:
    tags = ["trykitt_attempted"]
    row: dict = {
        "id": lead_id,
        "tags": tags,
        "list_source": "trykitt",
        "_verify_provider": "trykitt",
        "_verify_validity": validity,
    }
    if email:
        row["email"] = email
    return row


def test_collision_skips_email_keeps_tags_and_verification():
    ws = _make_workspace("collision-ws")
    owner = om.resolve_lead(
        email="shared@acme.com",
        name="Owner Lead",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    ghost = om.resolve_lead(
        email=None,
        name="Ghost Lead",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    owner_id = int(owner["id"])
    ghost_id = int(ghost["id"])
    assert owner_id != ghost_id

    summary = om.apply_email_find_results(
        [_profile_row(ghost_id, email="shared@acme.com")],
        workspace=ws,
        source="trykitt",
        source_detail="email-finder/batch",
    )

    assert summary["matched"] == 1
    assert summary["email_conflicts"] == 1
    assert summary["tagged"] == 1
    assert summary["recorded"] == 1
    assert summary["results"][0]["email_skipped"] is True
    assert summary["results"][0]["email_conflict_lead_id"] == owner_id

    conn = om.get_conn()
    owner_row = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (owner_id,),
    ).fetchone()
    ghost_row = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (ghost_id,),
    ).fetchone()
    ghost_tag = conn.execute(
        "SELECT tag FROM workspace_lead_tags WHERE lead_id = ? AND tag = ?",
        (ghost_id, "trykitt_attempted"),
    ).fetchone()
    ver = conn.execute(
        "SELECT email, status FROM lead_email_verification WHERE lead_id = ?",
        (ghost_id,),
    ).fetchone()
    dupe_count = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE email = ?",
        ("shared@acme.com",),
    ).fetchone()["n"]
    conn.close()

    assert owner_row["email"] == "shared@acme.com"
    assert ghost_row["email"] in (None, "")
    assert ghost_tag is not None
    assert ver["email"] == "shared@acme.com"
    assert ver["status"] == "valid"
    assert dupe_count == 1


def test_batch_with_collision_completes_all_rows():
    ws = _make_workspace("batch-collision")
    owner = om.resolve_lead(
        email="only@acme.com",
        name="Owner",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    ghost = om.resolve_lead(
        email=None,
        name="Ghost",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    clean = om.resolve_lead(
        email=None,
        name="Clean",
        company="Beta",
        company_domain="beta.com",
        source="manual",
    )
    owner_id = int(owner["id"])
    ghost_id = int(ghost["id"])
    clean_id = int(clean["id"])

    summary = om.apply_email_find_results(
        [
            _profile_row(clean_id, email="clean@beta.com"),
            _profile_row(ghost_id, email="only@acme.com"),
            _profile_row(clean_id, email="clean@beta.com", validity="catch_all"),
        ],
        workspace=ws,
        source="trykitt",
    )

    assert summary["processed"] == 3
    assert summary["matched"] == 3
    assert summary["email_conflicts"] == 1
    assert summary["enriched"] >= 2
    assert not any("integrity" in str(err).lower() for err in summary["errors"])

    conn = om.get_conn()
    clean_email = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (clean_id,),
    ).fetchone()["email"]
    ghost_email = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (ghost_id,),
    ).fetchone()["email"]
    owner_still = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (owner_id,),
    ).fetchone()["email"]
    conn.close()

    assert clean_email == "clean@beta.com"
    assert ghost_email in (None, "")
    assert owner_still == "only@acme.com"


def test_large_batch_mixed_conflicts_and_success():
    ws = _make_workspace("large-batch")
    owner = om.resolve_lead(
        email="shared@bigco.com",
        name="Shared Owner",
        company="BigCo",
        company_domain="bigco.com",
        source="manual",
    )
    owner_id = int(owner["id"])
    rows = []
    lead_ids = []
    for i in range(12):
        lead = om.resolve_lead(
            email=None,
            name=f"Lead {i}",
            company="BigCo",
            company_domain="bigco.com",
            source="manual",
        )
        lid = int(lead["id"])
        lead_ids.append(lid)
        email = "shared@bigco.com" if i % 3 == 0 else f"lead{i}@bigco.com"
        rows.append(_profile_row(lid, email=email))

    summary = om.apply_email_find_results(rows, workspace=ws, source="trykitt")

    assert summary["processed"] == 12
    assert summary["matched"] == 12
    assert summary["email_conflicts"] == 4
    assert len(summary["errors"]) == 0

    conn = om.get_conn()
    dupe_rows = conn.execute(
        """SELECT email, COUNT(*) AS n FROM leads
           WHERE email IS NOT NULL AND email != ''
           GROUP BY email HAVING n > 1""",
    ).fetchall()
    owner_row = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (owner_id,),
    ).fetchone()
    saved = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE email LIKE 'lead%@bigco.com'",
    ).fetchone()["n"]
    conn.close()

    assert dupe_rows == []
    assert owner_row["email"] == "shared@bigco.com"
    assert saved == 8


def test_overwrite_does_not_steal_email_from_other_lead():
    ws = _make_workspace("overwrite-conflict")
    owner = om.resolve_lead(
        email="taken@acme.com",
        name="Owner",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    ghost = om.resolve_lead(
        email=None,
        name="Ghost",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    owner_id = int(owner["id"])
    ghost_id = int(ghost["id"])

    summary = om.apply_email_find_results(
        [_profile_row(ghost_id, email="taken@acme.com")],
        workspace=ws,
        source="trykitt",
        overwrite=True,
    )

    assert summary["email_conflicts"] == 1
    conn = om.get_conn()
    assert conn.execute(
        "SELECT email FROM leads WHERE id = ?", (owner_id,),
    ).fetchone()["email"] == "taken@acme.com"
    assert conn.execute(
        "SELECT email FROM leads WHERE id = ?", (ghost_id,),
    ).fetchone()["email"] in (None, "")
    conn.close()


def test_lead_already_has_same_email_is_idempotent():
    ws = _make_workspace("idempotent")
    lead = om.resolve_lead(
        email="same@acme.com",
        name="Same",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    lead_id = int(lead["id"])

    summary = om.apply_email_find_results(
        [_profile_row(lead_id, email="same@acme.com")],
        workspace=ws,
        source="trykitt",
    )

    assert summary["email_conflicts"] == 0
    assert summary["matched"] == 1

    conn = om.get_conn()
    email = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()["email"]
    conn.close()
    assert email == "same@acme.com"


def test_not_found_row_still_gets_attempted_tag():
    ws = _make_workspace("not-found")
    lead = om.resolve_lead(
        email=None,
        name="No Email",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    lead_id = int(lead["id"])

    summary = om.apply_email_find_results(
        [{
            "id": lead_id,
            "tags": ["trykitt_attempted"],
            "list_source": "trykitt",
        }],
        workspace=ws,
        source="trykitt",
    )

    assert summary["matched"] == 1
    assert summary["email_conflicts"] == 0
    assert summary["tagged"] == 1
    assert summary["recorded"] == 0

    conn = om.get_conn()
    tag = conn.execute(
        "SELECT tag FROM workspace_lead_tags WHERE lead_id = ? AND tag = ?",
        (lead_id, "trykitt_attempted"),
    ).fetchone()
    email = conn.execute(
        "SELECT email FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()["email"]
    conn.close()
    assert tag is not None
    assert email in (None, "")


def test_cli_apply_email_find_results_handles_collision(tmp_path):
    ws = _make_workspace("cli-collision")
    owner = om.resolve_lead(
        email="cli@acme.com",
        name="CLI Owner",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    ghost = om.resolve_lead(
        email=None,
        name="CLI Ghost",
        company="Acme",
        company_domain="acme.com",
        source="manual",
    )
    payload = [
        _profile_row(int(ghost["id"]), email="cli@acme.com"),
        _profile_row(int(owner["id"]), email="cli@acme.com"),
    ]
    env = {**os.environ, "OUTREACHMAGIC_DATA_ROOT": str(tmp_path)}
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "pipeline.py"),
            "apply-email-find-results",
            "--workspace",
            ws,
            "--source",
            "trykitt",
            "--json",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        cwd=str(SCRIPTS),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["matched"] == 2
    assert summary["email_conflicts"] == 1

    conn = om.get_conn()
    dupe_count = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE email = ?",
        ("cli@acme.com",),
    ).fetchone()["n"]
    conn.close()
    assert dupe_count == 1


class TestCompanionFastPath(unittest.TestCase):
    def test_chunk_timeout_200_leads(self):
        self.assertEqual(ef_cc._chunk_timeout(200), 160)

    def test_chunk_timeout_cap(self):
        self.assertEqual(ef_cc._chunk_timeout(1000), 300)

    @patch.object(ef_cc, "_run_subprocess_json")
    @patch.object(ef_cc, "_append_json_or_file")
    def test_save_email_find_profiles_uses_fast_apply_when_ids_and_workspace(
        self, mock_append, mock_run,
    ):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {
            "matched": 2,
            "enriched": 2,
            "recorded": 1,
            "mode": "apply_email_find_results",
            "results": [],
        }
        profiles = [{"id": 1, "email": "a@acme.com"}, {"id": 2, "email": "b@acme.com"}]
        ef_cc.save_email_find_profiles(
            Path("/tmp/om"),
            profiles,
            workspace="ws1",
            source="trykitt",
        )
        cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("apply-email-find-results", cmd)
        self.assertNotIn("import-profiles", cmd)
        # Second call should be sync (explicit push from save_email_find_profiles)
        self.assertIn("sync", mock_run.call_args_list[1][0][0])

    @patch.object(ef_cc, "_run_subprocess_json")
    @patch.object(ef_cc, "_append_json_or_file")
    def test_run_import_profiles_never_uses_fast_apply(self, mock_append, mock_run):
        mock_append.side_effect = lambda cmd, payload, **kw: (cmd + ["--json", "[]"], None)
        mock_run.return_value = {"matched": 1, "results": []}
        profiles = [{"id": 1, "email": "a@acme.com"}]
        ef_cc.run_import_profiles(Path("/tmp/om"), profiles, workspace="ws1", source="trykitt")
        cmd = mock_run.call_args[0][0]
        self.assertIn("import-profiles", cmd)
        self.assertNotIn("apply-email-find-results", cmd)

    @patch.object(ef_cc.subprocess, "run")
    def test_subprocess_timeout_becomes_runtime_error(self, mock_run):
        mock_run.side_effect = ef_cc.subprocess.TimeoutExpired(
            cmd=["python", "pipeline.py", "import-profiles"],
            timeout=60,
        )
        with self.assertRaises(RuntimeError) as ctx:
            ef_cc._run_subprocess_json(
                ["python", "pipeline.py", "import-profiles"],
                temp_path=None,
                timeout=60,
                skill_dir=None,
            )
        self.assertIn("timed out after 60s", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
