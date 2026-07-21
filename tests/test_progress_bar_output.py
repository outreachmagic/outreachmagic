"""Progress bar feature requests from the trykitt debug report: ISO-8601
timestamps, resumed-count display, worker status line, and flushing so piped
output (`| tail -30`) doesn't stay blank until the process exits."""

import io
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import progress  # noqa: E402


class _FlushTrackingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        return super().flush()


def _stats():
    return {"found": 3, "not_found": 2, "errors": 0, "api_calls": {"trykitt": 5}}


def test_progress_line_has_iso8601_timestamp():
    buf = _FlushTrackingStream()
    progress.print_progress(5, 10, _stats(), time.time() - 60, file=buf)
    out = buf.getvalue()
    assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] PROGRESS:", out)


def test_progress_flushes_the_stream():
    buf = _FlushTrackingStream()
    progress.print_progress(5, 10, _stats(), time.time() - 60, file=buf)
    assert buf.flush_count >= 1


def test_progress_shows_resumed_count_when_present():
    buf = _FlushTrackingStream()
    progress.print_progress(5, 10, _stats(), time.time() - 60, file=buf, resumed=575)
    out = buf.getvalue()
    assert "5 new + 575 resumed" in out


def test_progress_omits_resumed_line_when_zero():
    buf = _FlushTrackingStream()
    progress.print_progress(5, 10, _stats(), time.time() - 60, file=buf, resumed=0)
    assert "resumed" not in buf.getvalue()


def test_progress_shows_worker_status_line():
    buf = _FlushTrackingStream()
    progress.print_progress(
        5, 10, _stats(), time.time() - 60, file=buf,
        active_workers=3, pool_size=5, tick_rate=0.8, slowest_call_s=12.4,
    )
    out = buf.getvalue()
    assert "Workers: 3/5 active" in out
    assert "this tick: 0.8/s" in out
    assert "slowest call: 12.4s" in out


def test_progress_omits_worker_line_when_not_provided():
    buf = _FlushTrackingStream()
    progress.print_progress(5, 10, _stats(), time.time() - 60, file=buf)
    assert "Workers:" not in buf.getvalue()
