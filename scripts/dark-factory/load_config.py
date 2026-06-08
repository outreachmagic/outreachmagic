#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config() -> dict[str, Any]:
    root = repo_root()
    for name in ("test-config.local.json", "test-config.local.yaml", "test-config.example.json"):
        path = root / name
        if not path.is_file():
            continue
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise SystemExit(
                "Install PyYAML for YAML config, or copy test-config.example.json to test-config.local.json"
            ) from e
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise SystemExit("Missing test-config.local.json (copy from test-config.example.json)")


def expand(path: str) -> str:
    return os.path.expanduser(path)
