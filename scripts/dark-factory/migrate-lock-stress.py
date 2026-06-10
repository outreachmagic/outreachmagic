#!/usr/bin/env python3
"""SQLite migrate lock stress — simulates upgrade path with open write transactions."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

def _find_scripts_dir() -> Path:
    env = os.environ.get("OUTREACHMAGIC_SCRIPTS")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "skills" / "outreachmagic" / "scripts"
        if (candidate / "pipeline.py").is_file():
            return candidate
    vps_default = Path.home() / "hermes/instances/dark-factory/data/skills/outreachmagic/scripts"
    if (vps_default / "pipeline.py").is_file():
        return vps_default
    raise RuntimeError("cannot locate outreachmagic scripts (set OUTREACHMAGIC_SCRIPTS)")


SCRIPTS = _find_scripts_dir()
sys.path.insert(0, str(SCRIPTS))
ROOT = SCRIPTS.parents[2]


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def _pass(msg: str) -> None:
    print(f"PASS: {msg}")


def run_stress(data_root: Path) -> int:
    os.environ["OUTREACHMAGIC_DATA_ROOT"] = str(data_root)
    from om_paths import set_data_root_override  # noqa: E402

    set_data_root_override(data_root)

    import pipeline as om  # noqa: E402

    errors: list[str] = []

    # 1) migrate_db on shared connection while writes are pending
    conn = om.get_conn()
    conn.execute("INSERT INTO leads (name, email) VALUES ('Stress', 'stress@fixture.test')")
    try:
        om.migrate_db(conn)
        conn.commit()
        _pass("migrate_db(conn) with pending writes")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            errors.append(f"migrate_db shared conn: {exc}")
        else:
            raise
    finally:
        conn.close()

    # 2) backfill_null_campaign_quarantine must reuse conn (no second get_conn)
    conn = om.get_conn()
    cfg = om.load_config()
    cfg.pop("null_campaign_backfill_at", None)
    om.save_config(cfg)
    try:
        result = om.backfill_null_campaign_quarantine(quiet=True, conn=conn)
        conn.commit()
        if result.get("found", 0) < 1:
            errors.append(f"backfill expected null-campaign rows, got {result}")
        else:
            _pass(f"backfill_null_campaign on shared conn (found={result.get('found')})")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            errors.append(f"backfill shared conn: {exc}")
        else:
            raise
    finally:
        conn.close()

    # 3) migrate_db while a second connection reads (WAL should allow)
    conn_write = om.get_conn()
    conn_write.execute("BEGIN IMMEDIATE")
    conn_write.execute("UPDATE leads SET notes = 'hold' WHERE id = (SELECT MIN(id) FROM leads)")
    reader_ok = threading.Event()
    reader_err: list[str] = []

    def reader():
        try:
            c = om.get_conn()
            c.execute("SELECT COUNT(*) FROM events").fetchone()
            c.close()
            reader_ok.set()
        except Exception as exc:  # noqa: BLE001
            reader_err.append(str(exc))

    t = threading.Thread(target=reader)
    t.start()
    t.join(timeout=5)
    try:
        om.migrate_db(conn_write)
        conn_write.commit()
        _pass("migrate_db during held write transaction")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            errors.append(f"migrate during write: {exc}")
        else:
            raise
    finally:
        conn_write.close()

    if reader_err:
        errors.append(f"reader thread: {reader_err[0]}")
    elif reader_ok.is_set():
        _pass("concurrent reader during write transaction")

    # 4) CLI init path (full migrate + schema) on fixture copy
    rc = os.system(
        f"{sys.executable} {SCRIPTS / 'pipeline.py'} paths > /dev/null 2>&1"
    )
    if rc != 0:
        errors.append(f"paths command exit {rc}")
    else:
        _pass("paths on fixture data root")

    proc = os.popen(
        f"{sys.executable} {SCRIPTS / 'pipeline.py'} show --limit 3 2>&1"
    )
    out = proc.read()
    rc = proc.close()
    if rc and "database is locked" in out.lower():
        errors.append("show hit database is locked")
    elif "database is locked" in out.lower():
        errors.append("show output contains database is locked")
    else:
        _pass("show after migrate (no lock error)")

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "checks": 5}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root",
        help="Fixture data root (default: temp copy of migrate/data-root)",
    )
    args = parser.parse_args()

    if args.data_root:
        return run_stress(Path(args.data_root).expanduser().resolve())

    fixture = Path(__file__).resolve().parent.parent / "tests/dark-factory/fixtures/migrate/data-root"
    if not fixture.is_dir():
        fixture = Path.home() / "hermes/instances/dark-factory/data/dark-factory-tests/fixtures/migrate/data-root"
    if not fixture.is_dir():
        print("Building migrate fixture...", file=sys.stderr)
        build = ROOT / "tests/dark-factory/fixtures/migrate/build_fixture.py"
        rc = os.system(f"{sys.executable} {build}")
        if rc != 0:
            return _fail("fixture build failed")

    tmp = Path(tempfile.mkdtemp(prefix="df-migrate-"))
    shutil.copytree(fixture, tmp / "data-root")
    return run_stress(tmp / "data-root")


if __name__ == "__main__":
    raise SystemExit(main())
