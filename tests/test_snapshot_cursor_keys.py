"""Every snapshot kind gets its own cursor slot.

sender_account and sender_domain shipped without one, and both
get_snapshot_cursor/set_snapshot_cursor defaulted an unknown kind to the
*workspace* slot. Three streams then shared one position: each page overwrote
the others, the lowest stream dragged the highest backwards, and every pull
re-downloaded the whole workspace snapshot stream -- including immediately
after a full sync.
"""

import pytest

import pipeline_sync
import pipeline_update


def test_every_pulled_kind_has_a_distinct_cursor_slot():
    keys = [
        pipeline_update._snapshot_cursor_key(kind)
        for kind in pipeline_sync._SNAPSHOT_PULL_KINDS
    ]
    assert len(set(keys)) == len(keys), f"snapshot kinds sharing a cursor slot: {keys}"


def test_unknown_kind_raises_instead_of_borrowing_the_workspace_slot():
    with pytest.raises(ValueError, match="no snapshot cursor slot"):
        pipeline_update._snapshot_cursor_key("not_a_kind")


def test_sender_cursors_do_not_move_the_workspace_cursor(monkeypatch):
    cfg: dict = {}
    monkeypatch.setattr(pipeline_update, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(pipeline_update, "save_config", lambda new: cfg.update(new))

    pipeline_update.set_snapshot_cursor(500, "workspace")
    # A lower position in a *different* stream must not drag the workspace
    # cursor back to 12 -- that is what forced the re-download.
    pipeline_update.set_snapshot_cursor(12, "sender_account")
    pipeline_update.set_snapshot_cursor(7, "sender_domain")

    assert pipeline_update.get_snapshot_cursor("workspace") == 500
    assert pipeline_update.get_snapshot_cursor("sender_account") == 12
    assert pipeline_update.get_snapshot_cursor("sender_domain") == 7


def test_clear_snapshot_cursors_clears_the_new_slots(monkeypatch):
    cfg: dict = {}
    monkeypatch.setattr(pipeline_update, "load_config", lambda: dict(cfg))
    monkeypatch.setattr(pipeline_update, "save_config", lambda new: cfg.clear() or cfg.update(new))

    for kind in pipeline_sync._SNAPSHOT_PULL_KINDS:
        pipeline_update.set_snapshot_cursor(99, kind)
    pipeline_update.clear_snapshot_cursors()

    for kind in pipeline_sync._SNAPSHOT_PULL_KINDS:
        assert pipeline_update.get_snapshot_cursor(kind) == 0, kind
