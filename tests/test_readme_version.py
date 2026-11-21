"""README version sync helper."""
from __future__ import annotations

import os
import sys
import unittest

from support import ROOT

sys.path.insert(0, os.path.join(ROOT, "tools"))
import sync_readme_version as sync


class ReadmeVersionTests(unittest.TestCase):
    def test_rewrite_tagline(self):
        text = "# Vela\n\n> v0.2.0 开发中 · Windows\n"
        self.assertEqual(sync.rewrite(text, "0.3.1"),
                         "# Vela\n\n> v0.3.1 开发中 · Windows\n")

    def test_rewrite_idempotent(self):
        text = "> v0.3.1 开发中\n"
        self.assertEqual(sync.rewrite(text, "0.3.1"), text)


if __name__ == "__main__":
    unittest.main()
