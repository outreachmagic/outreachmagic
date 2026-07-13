#!/usr/bin/env python3
"""Tests for DNSBL blacklist monitoring. Placeholder domains only (example.com, widgetco-mail.com)."""

import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.pop("OUTREACHMAGIC_AGENT_KEY", None)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_tmp = tempfile.mkdtemp()
from om_paths import set_data_root_override  # noqa: E402

set_data_root_override(Path(_tmp))

import pipeline as om  # noqa: E402
import blacklist_monitor as bm  # noqa: E402
from pipeline_sender_accounts import (  # noqa: E402
    blacklist_status_report,
    run_blacklist_check,
    set_sender_domain_cost,
    update_sender_domain_blacklist_status,
)


def _reset_db():
    db_path = om.get_db_path()
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            candidate.unlink()
    om.init_db()


def _listed(query):
    return ("host", [], ["127.0.0.2"])


def _nxdomain(query):
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _transient(query):
    raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


def _blocked(query):
    return ("host", [], ["127.255.255.254"])


def test_check_domain_listed_and_clean():
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_listed):
        r = bm.check_domain("example.com", "dbl.spamhaus.org")
    assert r["status"] == bm.LISTED
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_nxdomain):
        r = bm.check_domain("example.com", "dbl.spamhaus.org")
    assert r["status"] == bm.CLEAN


def test_transient_failure_is_error_not_clean():
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_transient):
        r = bm.check_ip("1.2.3.4", "zen.spamhaus.org")
    assert r["status"] == bm.CHECK_FAILED
    # A transient failure must land in summary.errors, never counted clean.
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_transient):
        block = bm.scan_domain("example.com", None, bm.select_tiers("all"))
    assert block["summary"]["errors"] >= 1
    assert block["summary"]["clean"] == 0


def test_blocked_range_is_check_failed_not_listed():
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_blocked):
        r = bm.check_domain("example.com", "dbl.spamhaus.org")
    assert r["status"] == bm.CHECK_FAILED


def test_no_sending_ip_skips_ip_checks():
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_nxdomain):
        block = bm.scan_domain("example.com", None, bm.select_tiers("all"))
    # Only domain-type checks ran; no ip-type results present.
    assert block["results"]
    assert all(r["type"] == "domain" for r in block["results"])
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_nxdomain):
        block_ip = bm.scan_domain("example.com", "9.9.9.9", bm.select_tiers("all"))
    assert any(r["type"] == "ip" for r in block_ip["results"])


def test_update_writes_only_dnsbl_status():
    _reset_db()
    set_sender_domain_cost(
        "widgetco-mail.com", reseller="VendorA", domain_cost=7.0, notes="keep me"
    )
    update_sender_domain_blacklist_status(
        "widgetco-mail.com", {"all_clean": True, "summary": {"clean": 3, "listed": 0, "errors": 0}}
    )
    conn = om.get_conn()
    row = conn.execute(
        "SELECT reseller, domain_cost, notes, dnsbl_status FROM sender_domains WHERE domain = ?",
        ("widgetco-mail.com",),
    ).fetchone()
    conn.close()
    assert row["reseller"] == "VendorA"
    assert row["domain_cost"] == 7.0
    assert row["notes"] == "keep me"
    assert json.loads(row["dnsbl_status"])["all_clean"] is True


def test_run_blacklist_check_any_listed_and_newly_listed_transition():
    _reset_db()
    set_sender_domain_cost("example.com")
    # Prior stored state: clean.
    update_sender_domain_blacklist_status(
        "example.com", {"all_clean": True, "summary": {"clean": 3, "listed": 0, "errors": 0}}
    )
    # Now every lookup resolves -> listed.
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_listed):
        result = run_blacklist_check(domain="example.com", tier="all")
    assert result["any_listed"] is True
    assert "example.com" in result["newly_listed"]

    # A second run while still listed is not a new transition.
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_listed):
        again = run_blacklist_check(domain="example.com", tier="all")
    assert again["any_listed"] is True
    assert again["newly_listed"] == []


def test_run_blacklist_check_all_clean_no_exit_signal():
    _reset_db()
    set_sender_domain_cost("example.com")
    with patch.object(bm.socket, "gethostbyname_ex", side_effect=_nxdomain):
        result = run_blacklist_check(domain="example.com", tier="all")
    assert result["any_listed"] is False
    assert result["newly_listed"] == []
    status = blacklist_status_report(domain="example.com")
    assert status["counts"]["clean"] == 1
    assert status["counts"]["listed"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
