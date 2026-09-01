#!/usr/bin/env python3
"""Generate references/schema.md from the live schema.

Agents read files; they do not run commands they have not heard of. The
`schema` command has existed for a while and the brief that prompted this still
reported "there is no schema introspection" -- because nothing in the skill's
documentation mentioned it. So the reference is generated and committed, and
`pipeline.py schema` stays the interactive twin of the same data.

A table with no entry in read_queries.TABLE_PURPOSE renders as TODO, which
tests/test_schema_reference.py fails on -- a new table cannot arrive here
undocumented.

Usage: python3 scripts/generate_schema_reference.py [--check]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HEADER = """# Outreach Magic — database schema

**Generated** by `scripts/generate_schema_reference.py`. Do not hand-edit; run
`make manifests` (or the script directly) after a schema change.

Interactive equivalents:

```bash
python3 scripts/pipeline.py schema                 # every table, with its purpose
python3 scripts/pipeline.py schema leads           # one table's columns and FKs
python3 scripts/pipeline.py schema --relations     # the join graph
```

## Scoping anything to a workspace

This is the question that costs the most time, because the answer is different
per entity. `leads` has **no** `workspace_id` column.

| Entity | How it is scoped |
|--------|------------------|
"""

FOOTER = """
## Two different campaign IDs

| Name | What it is |
|------|-----------|
| `campaigns.id` | The local campaign row. **This** is what `events.campaign_id` joins to. |
| `campaign_workspace_map.campaign_platform_id` | The *sequencer's own* campaign id, used for routing inbound webhooks (`campaignPlatformId` on the wire). It is **not** a join path from `events`. |

Reaching a workspace from an event goes `events.campaign_id → campaigns.id →
campaigns.workspace_id`, never through `campaign_workspace_map`.

## Ready-made helpers

`pipeline.py sql --workspace <slug>` pre-binds these CTEs so the joins above are
already done: `ws_workspace`, `ws_leads`, `ws_campaigns`, `ws_events`,
`ws_sender_accounts`.

```bash
python3 scripts/pipeline.py sql "SELECT stage, COUNT(*) FROM ws_leads GROUP BY stage" \\
  --workspace acme --json
```

Higher-level answers already have commands — prefer them over raw SQL:

```bash
python3 scripts/pipeline.py query deliverability --workspace acme --since 7d --json
python3 scripts/pipeline.py deliverability domains --workspace acme --json
python3 scripts/pipeline.py deliverability bounces --workspace acme --group-by signature --json
python3 scripts/pipeline.py campaign-replies --workspace acme --kind human --json
```
"""


def build_reference(db_path: Path) -> str:
    import read_queries

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
        out = [HEADER]
        for entity, how in read_queries.WORKSPACE_SCOPING.items():
            out.append(f"| `{entity}` | {how} |\n")
        out.append("\n## Tables\n\n")
        for table in tables:
            purpose = read_queries.TABLE_PURPOSE.get(table, "TODO — undocumented table")
            out.append(f"### `{table}`\n\n{purpose}\n\n")
            out.append("| Column | Type | Notes |\n|---|---|---|\n")
            fks = {fk["from"]: f"{fk['table']}.{fk['to'] or 'id'}"
                   for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()}
            for col in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
                notes = []
                if col["pk"]:
                    notes.append("primary key")
                if col["notnull"]:
                    notes.append("not null")
                if col["name"] in fks:
                    notes.append(f"→ `{fks[col['name']]}`")
                # The semantic warning goes last and in bold: it is the thing a
                # reader skimming for "what is this column" most needs to see,
                # and the structural notes beside it are all guessable.
                note = read_queries.COLUMN_NOTE.get((table, col["name"]))
                if note:
                    notes.append(f"**{note}**")
                out.append(f"| `{col['name']}` | {col['type'] or ''} | {', '.join(notes)} |\n")
            out.append("\n")
        out.append(FOOTER)
        return "".join(out)
    finally:
        conn.close()


def _reference_db() -> Path:
    """A throwaway database at current schema — never the user's own."""
    import om_paths

    tmp = Path(tempfile.mkdtemp()) / "schema-reference.db"
    om_paths.set_db_path_override(tmp)
    import pipeline as om

    om.init_db()
    return tmp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if the committed file is out of date")
    args = parser.parse_args()

    target = SCRIPTS.parent / "references" / "schema.md"
    content = build_reference(_reference_db())
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != content:
            print(f"{target} is out of date — run "
                  "python3 scripts/generate_schema_reference.py", file=sys.stderr)
            return 1
        print(f"{target} is up to date.")
        return 0
    target.write_text(content, encoding="utf-8")
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
