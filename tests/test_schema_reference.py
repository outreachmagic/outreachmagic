#!/usr/bin/env python3
"""references/schema.md must match the live schema, and every table needs a purpose.

The agent brief that prompted this reported "there is no schema introspection"
while `pipeline.py schema` was already shipping — the gap was that nothing
documented it. A generated, committed reference only helps if it cannot go
stale, so this test is the thing that keeps it honest.
"""

import json
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


def test_every_column_note_points_at_a_real_column():
    """A note on a column that no longer exists is a lie that outlives it.

    COLUMN_NOTE is the one place a column's semantics are written down, and it
    is keyed by name rather than declared beside the DDL -- so nothing stops a
    rename from leaving the warning behind, still rendered, still read.

    Run out of process, like the generator check above. `_reference_db()` sets
    om_paths._DB_PATH_OVERRIDE, which outranks the per-test data root every
    other test gets from conftest -- calling it in-process pins the whole
    session to one shared database.
    """
    probe = """
import json, sys
sys.path.insert(0, %r)
import generate_schema_reference, read_queries, sqlite3
conn = sqlite3.connect(str(generate_schema_reference._reference_db()))
missing = [
    f"{t}.{c}" for t, c in read_queries.COLUMN_NOTE
    if c not in {r[1] for r in conn.execute('PRAGMA table_info("%%s")' %% t)}
]
print(json.dumps(missing))
""" % str(SCRIPTS)
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    missing = json.loads(result.stdout.strip().splitlines()[-1])
    assert not missing, (
        f"COLUMN_NOTE documents columns that no longer exist: {missing}. "
        "Update or remove those notes.")
