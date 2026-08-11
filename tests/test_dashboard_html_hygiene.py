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

  * A <dialog> parented inside a hidden `#tab-*` section. showModal() on a
    dialog in a `display:none` subtree renders nothing but still enters the
    modal state: the top layer engages and the rest of the document goes inert.
    The page becomes unclickable with no dialog visible, and only a reload
    clears it. The merge dialog lived inside #tab-contacts, so merging from
    Campaigns -- or from the company pane with Companies active -- froze the
    whole page.

None of these is visible in review. All are trivial to detect.
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

    def test_dialogs_are_not_inside_tab_sections(self):
        """A <dialog> must be a sibling of the tab sections, never a child.

        showModal() on a dialog inside a `hidden` section engages the top layer
        and inerts the document while rendering nothing -- an unclickable page
        with no visible dialog, recoverable only by reloading. Merge is opened
        from Campaigns and from the company pane; both run with #tab-contacts
        hidden, so both were dead.
        """
        # Walk the markup tracking section depth, and flag any <dialog> opened
        # while inside a tab section.
        offenders = []
        depth, section_line = 0, None
        for match in re.finditer(
                r"<section id=\"tab-[\w-]+\"|</section>|<dialog\b[^>]*id=\"([\w-]+)\"",
                self.source):
            text = match.group(0)
            line = self.source.count("\n", 0, match.start()) + 1
            if text.startswith("<section"):
                depth += 1
                section_line = line
            elif text == "</section>":
                depth = max(0, depth - 1)
            elif depth > 0:
                offenders.append(f"#{match.group(1)} (line {line}, inside the "
                                 f"section opened at line {section_line})")
        self.assertEqual(
            offenders, [],
            "<dialog> element(s) parented inside a #tab-* section: "
            f"{offenders}. A dialog in a hidden section still enters the modal "
            "state on showModal() but renders nothing, which freezes the page. "
            "Move it out to be a sibling of the sections.")


if __name__ == "__main__":
    unittest.main()
