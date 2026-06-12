#!/usr/bin/env python3
"""Read-only platform detection for Outreach Magic agent installs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def detect_platform() -> dict:
    home = Path.home()

    # Runtime agent env wins over installed skill directories.
    if os.environ.get("CURSOR_AGENT"):
        return {
            "platform": "cursor",
            "skills_dir": str(home / ".cursor" / "skills"),
        }
    if os.environ.get("CLAUDE_CODE"):
        return {
            "platform": "claude",
            "skills_dir": str(home / ".claude" / "skills"),
        }
    if os.environ.get("HERMES_HOME"):
        hermes = Path(os.environ["HERMES_HOME"]).expanduser()
        return {
            "platform": "hermes",
            "skills_dir": str(hermes / "skills"),
        }

    if (home / ".cursor" / "skills").is_dir():
        return {
            "platform": "cursor",
            "skills_dir": str(home / ".cursor" / "skills"),
        }
    if (home / ".claude" / "skills").is_dir():
        return {
            "platform": "claude",
            "skills_dir": str(home / ".claude" / "skills"),
        }
    if (home / ".hermes" / "skills").is_dir():
        return {
            "platform": "hermes",
            "skills_dir": str(home / ".hermes" / "skills"),
        }
    return {"platform": None, "skills_dir": None}


def main() -> None:
    print(json.dumps(detect_platform(), indent=2))


if __name__ == "__main__":
    main()
