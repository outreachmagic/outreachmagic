"""Static checks on dashboard.html.

There is no bundler and no linter in front of this file, so two whole classes of
bug ship silently. Both have already happened:

  * Two top-level `function renderTagChips` declarations. JavaScript hoisting
    means the second wins for the entire script, so the contacts table's Tags
    column called the tag FILTER BAR renderer, printed its `undefined` return
    value into every row, and re-rendered the filter bar once per row as a side
    effect.

  * `api("/api/tags")` where `get()` was meant. Only `get()` appends
    `?workspace=`, and the workspace-scoped routes reject a request without it,
    so the lead panel's tag dropdown threw into a `.catch(() => {})` and sat
    empty forever.

Neither is visible in review. Both are trivial to detect.
"""

from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "skills" / "outreachmagic" / "scripts" / "dashboard.html"

# `function name(` at the start of a line — top-level declarations only. A
# nested or assigned function is scoped and cannot shadow anything globally.
_TOP_LEVEL_FN = re.compile(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


class DashboardHtmlHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_no_duplicate_top_level_function_declarations(self):
        counts = Counter(_TOP_LEVEL_FN.findall(self.source))
        duplicates = {name: n for name, n in counts.items() if n > 1}
        self.assertEqual(
            duplicates, {},
            "Duplicate top-level function declaration(s) in dashboard.html: "
            f"{sorted(duplicates)}. The later definition silently replaces the "
            "earlier one everywhere, including in callers written against the "
            "first. Rename one.")

    def test_workspace_scoped_routes_are_fetched_with_get_not_api(self):
        # Routes that _workspace_scoped wraps: without ?workspace= they raise
        # "workspace query parameter is required".
        scoped = ("/api/tags", "/api/contacts", "/api/contacts/stats",
                  "/api/companies", "/api/suppression", "/api/linkedin/senders")
        offenders = []
        for route in scoped:
            for match in re.finditer(
                    r"\bapi\(\s*[\"'`]" + re.escape(route) + r"(?![\w/])", self.source):
                line = self.source.count("\n", 0, match.start()) + 1
                offenders.append(f"{route} (line {line})")
        self.assertEqual(
            offenders, [],
            "Workspace-scoped route(s) fetched with api() instead of get(): "
            f"{offenders}. api() does not append ?workspace=, so the request is "
            "rejected and any .catch() swallows it into an empty control.")

    def test_the_tags_cell_renderer_returns_a_string(self):
        # The specific regression: whatever the Tags column renders with must be
        # the row-taking function, not the zero-arg filter-bar renderer.
        self.assertIn("render: renderTagChips", self.source)
        self.assertIn("function renderTagChips(r) {", self.source)
        self.assertIn("function renderTagFilterBar() {", self.source)


if __name__ == "__main__":
    unittest.main()
