#!/usr/bin/env python3
"""Read-only platform detection for Outreach Magic agent installs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def detect_platform() -> dict:
    home = Path.home()
    if os.environ.get("HERMES_HOME") or (home / ".hermes" / "skills").is_dir():
        hermes = Path(os.environ.get("HERMES_HOME", home / ".hermes")).expanduser()
        return {
            "platform": "hermes",
            "skills_dir": str(hermes / "skills"),
        }
    if (home / ".cursor" / "skills").is_dir() or os.environ.get("CURSOR_AGENT"):
        return {
            "platform": "cursor",
            "skills_dir": str(home / ".cursor" / "skills"),
        }
    if (home / ".claude" / "skills").is_dir() or os.environ.get("CLAUDE_CODE"):
        return {
            "platform": "claude",
            "skills_dir": str(home / ".claude" / "skills"),
        }
    return {"platform": None, "skills_dir": None}


def main() -> None:
    print(json.dumps(detect_platform(), indent=2))


if __name__ == "__main__":
    main()
