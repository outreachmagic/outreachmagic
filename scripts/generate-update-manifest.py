#!/usr/bin/env python3
"""Generate skills/outreachmagic/update-manifest.json with SHA256 checksums."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "outreachmagic"
SCRIPTS = SKILL / "scripts"
MANIFEST_FILES = (
    "pipeline.py",
    "pipeline_dedup.py",
    "pipeline_lead_review.py",
    "review_cloud.py",
    "constants.py",
    "db_conn.py",
    "formatters.py",
    "bounces.py",
    "activity_sync.py",
    "event_classification.py",
    "lead_sync.py",
    "platform_registry.py",
    "relay_ingest.py",
    "quarantine_resolutions.py",
    "relay_extractors.py",
    "workspace_routing.py",
    "workspace_archive.py",
    "routing_cloud.py",
    "agent_secrets_cloud.py",
    "api_key_pool.py",
    "connections_cloud.py",
    "db_health.py",
    "om_paths.py",
    "device_login.py",
    "read_queries.py",
    "query_cli.py",
    "data_freshness.py",
    "schema.py",
    "schema_views.py",
    "VERSION",
    "SKILL.md",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    version = (SCRIPTS / "VERSION").read_text().strip()
    files: dict[str, str] = {}
    for name in MANIFEST_FILES:
        path = SCRIPTS / name if name != "SKILL.md" else SKILL / "SKILL.md"
        if not path.exists():
            raise SystemExit(f"missing file for manifest: {path}")
        files[name] = sha256_file(path)

    manifest = {"version": version, "files": files}
    out = SKILL / "update-manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {out} for version {version}")


if __name__ == "__main__":
    main()
