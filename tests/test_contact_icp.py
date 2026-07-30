"""Per-workspace ICP profiles: storage, canonicalization, and the property the
rest of contact sourcing is built on -- `config_hash` identifies a config's
*content*, so two ways of writing the same rules hash the same and a rule
change always moves it.

Fixture titles here are generic role names, not lifted from any live campaign.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import contact_icp  # noqa: E402
import pipeline as om  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    om.init_db()
    conn = om.get_conn()
    om.ensure_organization(conn)
    conn.commit()
    conn.close()
    om.create_workspace("Storefront", slug="storefront")


def _ws_id(slug="storefront"):
    conn = om.get_conn()
    try:
        return om.resolve_workspace_identity(conn, slug)["id"]
    finally:
        conn.close()


# ── schema ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("table", [
    "company_page_cache",
    "company_contact_observations",
    "workspace_icp_profiles",
])
def test_migration_creates_contact_sourcing_tables(table):
    conn = om.get_conn()
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    finally:
        conn.close()
    assert found, f"{table} was not created by migrate_db"


def test_migration_is_idempotent():
    """migrate_db runs on every invocation -- a second pass must be a no-op."""
    om.migrate_db()
    om.migrate_db()
    conn = om.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM workspace_icp_profiles").fetchone()["c"] == 0
    finally:
        conn.close()


# ── canonicalization and hashing ─────────────────────────────────────────────

def test_hash_is_stable_across_identical_configs():
    a = {"whitelist": ["General Manager", "Service Manager"], "blocklist": ["Intern"]}
    b = {"whitelist": ["service  manager", "GENERAL MANAGER"], "blocklist": [" intern "]}
    assert contact_icp.config_hash(a) == contact_icp.config_hash(b)


def test_hash_ignores_term_order():
    a = {"whitelist": ["a manager", "b director"]}
    b = {"whitelist": ["b director", "a manager"]}
    assert contact_icp.config_hash(a) == contact_icp.config_hash(b)


def test_hash_ignores_duplicate_terms():
    a = {"whitelist": ["general manager"]}
    b = {"whitelist": ["general manager", "General Manager"]}
    assert contact_icp.config_hash(a) == contact_icp.config_hash(b)


def test_hash_moves_when_a_rule_changes():
    base = {"whitelist": ["general manager"]}
    assert contact_icp.config_hash(base) != contact_icp.config_hash(
        {"whitelist": ["general manager"], "blocklist": ["assistant general manager"]})
    assert contact_icp.config_hash(base) != contact_icp.config_hash(
        {"whitelist": ["general manager"], "min_contacts": 3})


def test_a_term_moved_between_lists_changes_the_hash():
    """The obvious way to get this wrong is to hash a flattened term set."""
    assert contact_icp.config_hash({"whitelist": ["owner"], "blocklist": []}) != \
        contact_icp.config_hash({"whitelist": [], "blocklist": ["owner"]})


def test_comma_string_and_list_are_the_same_config():
    assert contact_icp.config_hash({"whitelist": "general manager, service manager"}) == \
        contact_icp.config_hash({"whitelist": ["general manager", "service manager"]})


def test_empty_config_is_valid_and_has_defaults():
    cfg = contact_icp.canonical_config(None)
    assert cfg == {"whitelist": [], "blocklist": [], "section_headers": [],
                   "min_contacts": contact_icp.DEFAULT_MIN_CONTACTS}


def test_unknown_field_is_rejected():
    with pytest.raises(contact_icp.IcpError, match="unknown ICP field"):
        contact_icp.canonical_config({"whitelist_": ["oops"]})


def test_negative_min_contacts_is_rejected():
    with pytest.raises(contact_icp.IcpError):
        contact_icp.canonical_config({"min_contacts": -1})


# ── round trip ───────────────────────────────────────────────────────────────

def test_set_then_show_round_trips():
    written = contact_icp.cli_set(
        "storefront", "regional-ops",
        whitelist="General Manager, Service Manager",
        blocklist="Assistant General Manager",
        min_contacts=2,
    )
    read = contact_icp.cli_show("storefront", "regional-ops")
    assert read["config"] == written["config"]
    assert read["config_hash"] == written["config_hash"]
    assert read["config"]["whitelist"] == ["general manager", "service manager"]
    assert read["config"]["blocklist"] == ["assistant general manager"]
    assert read["config"]["min_contacts"] == 2


def test_stored_hash_matches_a_freshly_computed_one():
    """Guards the case where storage and hashing canonicalize differently."""
    profile = contact_icp.cli_set("storefront", "ops", whitelist="Owner,  owner ")
    assert profile["config_hash"] == contact_icp.config_hash(profile["config"])


def test_rewriting_the_same_config_does_not_change_the_hash_or_timestamp():
    first = contact_icp.cli_set("storefront", "ops", whitelist="general manager")
    second = contact_icp.cli_set("storefront", "ops", whitelist="GENERAL  MANAGER")
    assert second["created"] is False
    assert second["config_hash"] == first["config_hash"]
    # A no-op set must not look like an edit -- observations join to a config
    # version, and "when did this last change" has to stay answerable.
    assert second["updated_at"] == first["updated_at"]


def test_partial_update_keeps_unspecified_fields():
    contact_icp.cli_set("storefront", "ops", whitelist="general manager", min_contacts=4)
    updated = contact_icp.cli_set("storefront", "ops", blocklist="intern")
    assert updated["config"]["whitelist"] == ["general manager"]
    assert updated["config"]["min_contacts"] == 4
    assert updated["config"]["blocklist"] == ["intern"]


def test_explicit_empty_string_clears_a_list():
    contact_icp.cli_set("storefront", "ops", whitelist="general manager", blocklist="intern")
    cleared = contact_icp.cli_set("storefront", "ops", blocklist="")
    assert cleared["config"]["blocklist"] == []
    assert cleared["config"]["whitelist"] == ["general manager"]


def test_replace_resets_unspecified_fields():
    contact_icp.cli_set("storefront", "ops", whitelist="general manager", min_contacts=4)
    replaced = contact_icp.cli_set("storefront", "ops", blocklist="intern", replace=True)
    assert replaced["config"]["whitelist"] == []
    assert replaced["config"]["min_contacts"] == contact_icp.DEFAULT_MIN_CONTACTS


def test_name_defaults_and_is_normalized():
    profile = contact_icp.cli_set("storefront", None, whitelist="owner")
    assert profile["name"] == contact_icp.DEFAULT_PROFILE_NAME
    named = contact_icp.cli_set("storefront", "  Regional Ops ", whitelist="owner")
    assert named["name"] == "regional ops"


def test_unknown_workspace_raises():
    with pytest.raises(contact_icp.IcpError, match="workspace not found"):
        contact_icp.cli_set("nope", "ops", whitelist="owner")


def test_show_without_name_requires_one_when_ambiguous():
    contact_icp.cli_set("storefront", "a", whitelist="owner")
    contact_icp.cli_set("storefront", "b", whitelist="manager")
    with pytest.raises(contact_icp.IcpError, match="pass --name"):
        contact_icp.cli_show("storefront")


def test_show_without_name_resolves_a_single_profile():
    contact_icp.cli_set("storefront", "only-one", whitelist="owner")
    assert contact_icp.cli_show("storefront")["name"] == "only-one"


def test_show_missing_profile_raises():
    with pytest.raises(contact_icp.IcpError, match="no ICP profile"):
        contact_icp.cli_show("storefront", "absent")


def test_list_scopes_to_a_workspace_and_reports_slugs():
    om.create_workspace("Other", slug="other")
    contact_icp.cli_set("storefront", "a", whitelist="owner")
    contact_icp.cli_set("other", "b", whitelist="manager")
    scoped = contact_icp.cli_list("storefront")
    assert [p["name"] for p in scoped["profiles"]] == ["a"]
    assert scoped["profiles"][0]["workspace"] == "storefront"
    assert contact_icp.cli_list()["count"] == 2


def test_delete_removes_the_profile():
    contact_icp.cli_set("storefront", "ops", whitelist="owner")
    contact_icp.cli_delete("storefront", "ops")
    with pytest.raises(contact_icp.IcpError):
        contact_icp.cli_show("storefront", "ops")


# ── export / import ──────────────────────────────────────────────────────────

def test_export_import_preserves_the_hash_across_workspaces():
    om.create_workspace("Other", slug="other")
    source = contact_icp.cli_set(
        "storefront", "ops", whitelist="general manager,service manager", min_contacts=2)
    doc = contact_icp.cli_export("storefront", "ops")
    imported = contact_icp.cli_import("other", payload=json.dumps(doc))
    assert imported["workspace"] == "other"
    assert imported["name"] == "ops"
    assert imported["config_hash"] == source["config_hash"]


def test_export_writes_a_file_that_imports_back(tmp_path):
    contact_icp.cli_set("storefront", "ops", whitelist="owner")
    path = tmp_path / "icp.json"
    contact_icp.cli_export("storefront", "ops", str(path))
    om.create_workspace("Other", slug="other")
    imported = contact_icp.cli_import("other", path=str(path))
    assert imported["config"]["whitelist"] == ["owner"]


def test_export_document_carries_no_workspace_id():
    contact_icp.cli_set("storefront", "ops", whitelist="owner")
    doc = contact_icp.cli_export("storefront", "ops")
    assert "workspace_id" not in doc


def test_import_accepts_a_bare_config():
    imported = contact_icp.cli_import(
        "storefront", payload='{"whitelist": ["Owner"]}', name="hand-written")
    assert imported["config"]["whitelist"] == ["owner"]


def test_import_name_flag_overrides_the_document():
    contact_icp.cli_set("storefront", "ops", whitelist="owner")
    doc = contact_icp.cli_export("storefront", "ops")
    imported = contact_icp.cli_import("storefront", payload=json.dumps(doc), name="ops-copy")
    assert imported["name"] == "ops-copy"
    assert contact_icp.cli_show("storefront", "ops")["config_hash"] == imported["config_hash"]


def test_import_rejects_a_document_whose_hash_does_not_match():
    """An edited file that kept its old hash would import under a version
    string describing rules it no longer contains."""
    contact_icp.cli_set("storefront", "ops", whitelist="owner")
    doc = contact_icp.cli_export("storefront", "ops")
    doc["config"]["whitelist"] = ["someone else"]
    with pytest.raises(contact_icp.IcpError, match="does not match"):
        contact_icp.cli_import("storefront", payload=json.dumps(doc), name="tampered")


def test_import_rejects_an_unknown_version():
    with pytest.raises(contact_icp.IcpError, match="version"):
        contact_icp.cli_import("storefront", payload=json.dumps(
            {"kind": contact_icp.EXPORT_KIND, "version": 99, "config": {}}))


def test_import_rejects_unparseable_json():
    with pytest.raises(contact_icp.IcpError, match="JSON"):
        contact_icp.cli_import("storefront", payload="{not json")


def test_import_replaces_rather_than_merges():
    contact_icp.cli_set("storefront", "ops", whitelist="owner", blocklist="intern")
    contact_icp.cli_import(
        "storefront", payload='{"whitelist": ["manager"]}', name="ops")
    assert contact_icp.cli_show("storefront", "ops")["config"]["blocklist"] == []


# ── CLI wiring ───────────────────────────────────────────────────────────────

def _run_cli(monkeypatch, capsys, *argv):
    import pipeline_cli

    monkeypatch.setattr(sys, "argv", ["pipeline.py", "icp", *argv])
    code = pipeline_cli.main()
    return code, capsys.readouterr().out


def test_cli_set_show_round_trip(monkeypatch, capsys):
    code, _ = _run_cli(
        monkeypatch, capsys, "set", "--workspace", "storefront", "--name", "ops",
        "--whitelist", "General Manager,Service Manager",
        "--blocklist", "Intern", "--min-contacts", "2", "--json")
    assert code in (None, 0)

    _, out = _run_cli(
        monkeypatch, capsys, "show", "--workspace", "storefront", "--name", "ops", "--json")
    shown = json.loads(out)
    assert shown["config"]["whitelist"] == ["general manager", "service manager"]
    assert shown["config"]["min_contacts"] == 2


def test_cli_reports_an_unknown_workspace_as_an_error(monkeypatch, capsys):
    code, out = _run_cli(
        monkeypatch, capsys, "show", "--workspace", "nope", "--json")
    assert code == 1
    assert json.loads(out)["status"] == "error"


def test_cli_export_prints_a_document_without_json_flag(monkeypatch, capsys):
    contact_icp.cli_set("storefront", "ops", whitelist="owner")
    _, out = _run_cli(monkeypatch, capsys, "export", "--workspace", "storefront", "--name", "ops")
    doc = json.loads(out)
    assert doc["kind"] == contact_icp.EXPORT_KIND
    assert doc["config"]["whitelist"] == ["owner"]


def test_cli_bare_icp_prints_usage(monkeypatch, capsys):
    code, out = _run_cli(monkeypatch, capsys)
    assert code == 1
    assert "icp" in out


def test_cli_list_human_output_names_each_profile(monkeypatch, capsys):
    contact_icp.cli_set("storefront", "ops", whitelist="owner")
    _, out = _run_cli(monkeypatch, capsys, "list", "--workspace", "storefront")
    assert "storefront/ops" in out
