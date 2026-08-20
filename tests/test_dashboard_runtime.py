#!/usr/bin/env python3
"""The dashboard script must survive being executed, not merely parsed.

`node --check` validates syntax and nothing else. It happily accepts

    wireServerPageSize("companies-size", companiesState, ...)   // line 3041
    ...
    const companiesState = { ... }                              // line 3122

because `const` is not hoisted: that is a ReferenceError thrown during
top-level execution, and because it is top-level it kills every initializer
after it. The visible symptom is not an error message -- it is a dashboard that
loads with an empty workspace dropdown and no data anywhere, which reads as a
backend problem and sends you looking in the wrong place.

This runs the script against a minimal DOM stub and asserts it does not throw.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "skills" / "outreachmagic" / "scripts" / "dashboard.html"
CHECKER = Path(__file__).parent / "support" / "dashboard_runtime_check.js"


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class DashboardRuntimeTests(unittest.TestCase):
    def test_the_top_level_script_executes_without_throwing(self):
        result = subprocess.run(
            ["node", str(CHECKER), str(DASHBOARD)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(
            result.returncode, 0,
            f"dashboard.html failed at load:\n{result.stdout}\n{result.stderr}")

    def test_the_checker_actually_catches_a_use_before_declaration(self):
        """A guard that cannot fail is not a guard."""
        source = DASHBOARD.read_text(encoding="utf-8")
        marker = "const companiesState"
        self.assertIn(marker, source)
        # Hoist a reference to companiesState above its declaration.
        broken = source.replace(
            'wireServerPageSize("contacts-size", contactsState, () => loadContacts());',
            'wireServerPageSize("contacts-size", contactsState, () => loadContacts());\n'
            'void companiesState;', 1)
        self.assertNotEqual(broken, source)
        tmp = ROOT / "tests" / "support" / "_tdz_probe.html"
        try:
            tmp.write_text(broken, encoding="utf-8")
            result = subprocess.run(
                ["node", str(CHECKER), str(tmp)],
                capture_output=True, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0,
                                "checker passed a script that must fail")
            self.assertIn("before initialization", result.stdout + result.stderr)
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
