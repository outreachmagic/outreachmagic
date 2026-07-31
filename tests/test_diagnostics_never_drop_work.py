"""A diagnostic must never be able to destroy the thing it is describing.

The bug this pins: a `pull` run by a harness that keeps stdout and closes
stderr raises `BrokenPipeError(32)` on the first stderr write. Inside
`ingest_relay_event` the diagnostics went straight to `sys.stderr`, and the
caller wraps each event in `except Exception` — so the write failure was filed
as `Warning: skipped webhook event <id>: [Errno 32] Broken pipe` and the event
was dropped. Five real webhook events were lost that way on one pull, and the
count in the summary line ("5 errors") read as if five *events* were bad.

Stdout was fine throughout, which is why the progress output looked healthy.
The failing writes were the stderr ones, so the events that got dropped were
exactly the ones interesting enough to warrant a diagnostic.
"""

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import progress  # noqa: E402


class DeadPipe(io.TextIOBase):
    """A stream whose reader has gone away, like stderr after `p.stderr.close()`."""

    def write(self, _s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_warn_swallows_a_dead_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stderr", DeadPipe())
    progress.warn("this must not raise")


def test_warn_still_writes_when_the_pipe_is_alive(monkeypatch):
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    progress.warn("hello")
    assert "hello" in captured.getvalue(), "silencing the failure must not silence the message"


def test_a_plain_print_to_a_dead_stderr_does_raise(monkeypatch):
    """The control. Without this the tests above prove nothing — they would pass
    just as well if BrokenPipeError were impossible in the first place."""
    monkeypatch.setattr(sys, "stderr", DeadPipe())
    with pytest.raises(BrokenPipeError):
        print("boom", file=sys.stderr)
        sys.stderr.flush()


# ── the ingest path, which is where it actually cost data ────────────────────

def test_no_ingest_diagnostic_writes_to_stderr_directly():
    """`ingest_relay_event` runs per event inside `except Exception`, so every
    diagnostic on that path has to go through `warn`. A plain
    `print(..., file=sys.stderr)` reintroduces the data loss silently."""
    source = (SCRIPTS / "relay_ingest.py").read_text()
    assert "file=sys.stderr" not in source, (
        "relay_ingest.py writes a diagnostic straight to stderr; use progress.warn "
        "or a failed write will be filed as a skipped event")


def test_the_pull_loops_skip_warning_cannot_itself_raise():
    """The handler that reports a skipped event must not be able to throw while
    reporting one — that turns one lost event into an aborted pull."""
    source = (SCRIPTS / "pipeline_sync.py").read_text()
    for line in source.splitlines():
        if "skipped webhook event" in line:
            assert line.strip().startswith("warn("), (
                f"skip warning must use progress.warn, got: {line.strip()}")
