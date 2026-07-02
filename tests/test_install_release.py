"""Install release security: SHA256 verify pattern, git noise, doc sync."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
VERSION_FILE = ROOT / "skills" / "outreachmagic" / "scripts" / "VERSION"
SYNC_SCRIPT = ROOT / "scripts" / "sync_install_docs.py"

def _read_tag() -> str:
    ver = VERSION_FILE.read_text(encoding="utf-8").strip()
    return ver if ver.startswith("v") else f"v{ver}"


def test_sha256_verify_pattern_with_install_sh_filename(tmp_path: Path):
    """Documented flow: save as install.sh inside INSTALL_DIR, then shasum --check."""
    install_sh = tmp_path / "install.sh"
    payload = b"#!/bin/bash\necho ok\n"
    install_sh.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  install.sh\n", encoding="utf-8")

    proc = subprocess.run(
        f"grep ' install.sh$' SHA256SUMS | shasum -a 256 --check",
        shell=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "install.sh: OK" in proc.stdout


def test_sha256_verify_fails_with_wrong_filename(tmp_path: Path):
    """Broken pattern: om_install.sh on disk but checksum lists install.sh."""
    wrong = tmp_path / "om_install.sh"
    payload = b"#!/bin/bash\n"
    wrong.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  install.sh\n", encoding="utf-8")

    proc = subprocess.run(
        f"grep ' install.sh$' SHA256SUMS | shasum -a 256 --check",
        shell=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


def test_install_sh_suppresses_detached_head_advice():
    text = INSTALL.read_text(encoding="utf-8")
    clones = re.findall(r"git[^\n]*clone[^\n]*", text)
    assert clones, "expected git clone calls in install.sh"
    for line in clones:
        assert "advice.detachedHead=false" in line, line


def test_local_dry_run_has_no_detached_head_noise():
    proc = subprocess.run(
        ["bash", str(INSTALL), "--local", "--dry-run", "--platform", "cursor"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "detached HEAD" not in proc.stdout.lower()
    assert "detached HEAD" not in proc.stderr.lower()


def test_sync_install_docs_check_passes():
    proc = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_no_broken_sha256_doc_patterns():
    tag = _read_tag()
    proc = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "om_install.sh" not in (ROOT / "AGENTS-INSTALL.md").read_text(encoding="utf-8")
    assert f"OM_VERSION={tag}" in (ROOT / "AGENTS-INSTALL.md").read_text(encoding="utf-8")


def test_agents_install_documents_install_dir_flow():
    text = (ROOT / "AGENTS-INSTALL.md").read_text(encoding="utf-8")
    assert "INSTALL_DIR=$(mktemp -d)" in text
    assert '${INSTALL_DIR}/install.sh' in text
    assert "om_install.sh" not in text
