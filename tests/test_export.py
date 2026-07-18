"""Smoke test — build the demo export and assert the output is coherent.

Pure stdlib (unittest), no third-party test runner, so CI needs zero installs.
"""
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Point the exporter at the synthetic demo sessions before importing it
# (PROJECTS_DIR is resolved at import time).
os.environ["MEMORIUM_PROJECTS_DIR"] = os.path.join(REPO, "demo", "sessions")
sys.path.insert(0, REPO)

import export  # noqa: E402


class DemoExportTest(unittest.TestCase):
    def test_build_from_demo(self):
        import webbrowser
        webbrowser.open = lambda *a, **k: True  # don't pop a browser in CI

        out = tempfile.mkdtemp(prefix="memorium-test-")
        argv = sys.argv
        sys.argv = ["export.py", out]
        try:
            export.main()
        finally:
            sys.argv = argv

        # index + one JS file per demo session
        self.assertTrue(os.path.exists(os.path.join(out, "index.html")))
        sessions = [f for f in os.listdir(os.path.join(out, "sessions")) if f.endswith(".js")]
        self.assertEqual(len(sessions), 3)

        # the manifest carries the three project names
        with open(os.path.join(out, "index.html"), encoding="utf-8") as f:
            index = f.read()
        for project in ("webapp", "api", "cli"):
            self.assertIn(project, index)

        # the full-text search index actually captured session content
        with open(os.path.join(out, "searchindex.js"), encoding="utf-8") as f:
            search = f.read()
        self.assertIn("discount", search)
        self.assertIn("401", search)


if __name__ == "__main__":
    unittest.main()
