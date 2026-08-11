"""Stage 6: every column of every synced table must be classified.

sync_contract.SYNC_MAP is the list of tables that feed a relay entity. For each
of those tables, SYNCED_COLUMNS / NOT_SYNCED_COLUMNS in the same module must
between them cover every column SQLite actually has -- add a column to
`leads`, `workspace_leads`, `sender_accounts`, etc. and forget to classify it
here, and this test goes red. That's the "a tag is updated but updated_at
isn't" class of bug, killed by construction instead of by review.

This is a coverage/classification test, not a losslessness test: NOT_SYNCED
is a legitimate, common answer, as long as it is deliberate and justified. The
round-trip property test (extending test_pull_ingest_equivalence.py) is what
proves SYNCED columns actually survive a push+pull.
"""

from __future__ import annotations

import pipeline as om
import sync_contract


def _live_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _fresh_conn(tmp_path):
    from db_conn import get_conn
    from om_paths import set_data_root_override

    set_data_root_override(tmp_path)
    om.init_db()
    return get_conn()


def test_every_sync_map_table_is_classified():
    """No table in SYNC_MAP may be silently missing from the classification maps."""
    for table in sync_contract.SYNC_MAP:
        assert table in sync_contract.SYNCED_COLUMNS, (
            f"{table} is in SYNC_MAP but has no SYNCED_COLUMNS entry "
            f"(add one, even if it's an empty frozenset())"
        )
        assert table in sync_contract.NOT_SYNCED_COLUMNS, (
            f"{table} is in SYNC_MAP but has no NOT_SYNCED_COLUMNS entry "
            f"(add one, even if it's an empty dict)"
        )


def test_no_column_double_classified():
    """A column must be SYNCED xor NOT_SYNCED, never both, for a given table."""
    for table in sync_contract.SYNC_MAP:
        synced = sync_contract.SYNCED_COLUMNS.get(table, frozenset())
        not_synced = set(sync_contract.NOT_SYNCED_COLUMNS.get(table, {}))
        overlap = synced & not_synced
        assert not overlap, f"{table}: columns classified as both SYNCED and NOT_SYNCED: {sorted(overlap)}"


def test_every_not_synced_column_has_a_justification():
    """Stage 6's whole point: NOT_SYNCED is fine, unexplained is not."""
    for table, reasons in sync_contract.NOT_SYNCED_COLUMNS.items():
        for column, reason in reasons.items():
            assert isinstance(reason, str) and reason.strip(), (
                f"{table}.{column} is NOT_SYNCED with no (or an empty) justification"
            )


def test_classification_covers_every_live_column(tmp_path):
    """PRAGMA table_info(table) must equal SYNCED_COLUMNS | NOT_SYNCED_COLUMNS.keys().

    This is the test that actually fails the moment someone adds a column to a
    synced table's schema (schema.py or a migrate_db() ALTER TABLE) without
    classifying it here.
    """
    conn = _fresh_conn(tmp_path)
    problems: list[str] = []
    for table in sync_contract.SYNC_MAP:
        live = _live_columns(conn, table)
        classified = sync_contract.classified_columns(table)
        missing = live - classified
        stale = classified - live
        if missing:
            problems.append(
                f"{table}: column(s) {sorted(missing)} exist in the schema but are not "
                f"classified in sync_contract.SYNCED_COLUMNS/NOT_SYNCED_COLUMNS"
            )
        if stale:
            problems.append(
                f"{table}: column(s) {sorted(stale)} are classified in sync_contract.py "
                f"but no longer exist in the schema -- remove the stale entry"
            )
    conn.close()
    assert not problems, "\n" + "\n".join(problems)



# sender_domains is the one deliberate exception: its entity_id *is* the
# domain string because a domain is a genuine natural key (see Stage 3 of the
# plan -- "Leave alone. 47 rows, genuine natural key"), unlike leads/companies
# where the entity_id is a surrogate `uid` that never doubles as content.
_NATURAL_KEY_ENTITY_ID_COLUMNS = {"sender_domains": {"domain"}}


def test_entity_id_expr_columns_are_not_synced():
    """The columns an entity_id expression reads (NEW.id, NEW.lead_id, ...) are
    join/identity columns by construction -- they must not also be claimed as
    SYNCED payload fields, or the classification is lying about what travels.
    Exception: genuine natural keys (see _NATURAL_KEY_ENTITY_ID_COLUMNS)."""
    import re

    for table, (_entity_type, id_expr) in sync_contract.SYNC_MAP.items():
        referenced = set(re.findall(r"\{row\}\.(\w+)", id_expr))
        referenced -= _NATURAL_KEY_ENTITY_ID_COLUMNS.get(table, set())
        synced = sync_contract.SYNCED_COLUMNS.get(table, frozenset())
        bad = referenced & synced
        assert not bad, f"{table}: entity_id column(s) {sorted(bad)} must not be classified SYNCED"


def test_every_entity_type_resolves_in_the_outbox_panel():
    """The Sync panel must be able to describe every entity the outbox queues.

    `suppression` was a first-class entity everywhere that mattered -- it had an
    outbox trigger, an owner table and a SYNC_MAP entry -- but _resolve_outbox_
    entity had no branch for it, so clicking one in the Sync tab returned
    "Could not build payload: unknown entity_type: suppression". Nothing tied
    the two lists together, so the gap was invisible until someone clicked.
    """
    import reporting_queries

    missing = [et for et in sync_contract.ENTITY_TYPES
               if et not in reporting_queries._OUTBOX_RECORD_SQL]
    assert missing == [], (
        f"entity type(s) with no outbox-panel record SQL: {missing}. Every "
        "member of ENTITY_TYPES can appear in the outbox, so every member needs "
        "a record query and a payload branch in _resolve_outbox_entity.")


def test_unpushable_entity_types_are_declared_not_silently_stuck():
    """An entity with no relay endpoint must say so, not look like a failed push.

    The relay's snapshot allowlist has no suppression action, so those outbox
    rows sit at attempts=0 forever. That is a missing endpoint, not a stuck
    queue, and the panel has to tell them apart.
    """
    import reporting_queries

    unknown = set(reporting_queries.UNPUSHABLE_ENTITY_TYPES) - set(sync_contract.ENTITY_TYPES)
    assert unknown == set(), f"UNPUSHABLE_ENTITY_TYPES names non-entities: {unknown}"
    for entity_type, reason in reporting_queries.UNPUSHABLE_ENTITY_TYPES.items():
        assert reason and len(reason) > 40, (
            f"{entity_type} needs a reason explaining why nothing drains it")
