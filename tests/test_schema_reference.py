#!/usr/bin/env python3
"""references/schema.md must match the live schema, and every table needs a purpose.

The agent brief that prompted this reported "there is no schema introspection"
while `pipeline.py schema` was already shipping — the gap was that nothing
documented it. A generated, committed reference only helps if it cannot go
stale, so this test is the thing that keeps it honest.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
REFERENCE = ROOT / "skills" / "outreachmagic" / "references" / "schema.md"


def test_schema_reference_is_current():
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "generate_schema_reference.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"{result.stdout}{result.stderr}\n"
        "Run: python3 skills/outreachmagic/scripts/generate_schema_reference.py"
    )


def test_every_table_has_a_purpose():
    text = REFERENCE.read_text(encoding="utf-8")
    assert "TODO — undocumented table" not in text, (
        "A table has no entry in read_queries.TABLE_PURPOSE — add one so agents "
        "can tell what it is for."
    )


def test_reference_answers_the_workspace_scoping_question():
    """The most common wrong first guess is leads.workspace_id."""
    text = REFERENCE.read_text(encoding="utf-8")
    assert "no column on leads" in text
    assert "events.campaign_id -> campaigns.workspace_id" in text
    assert "campaign_platform_id" in text
