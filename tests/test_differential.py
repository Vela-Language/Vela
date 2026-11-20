"""Run the differential corpus against CPython as part of unittest."""
from __future__ import annotations

import glob
import os
import unittest

from differential import run_paths

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")


class DifferentialTests(unittest.TestCase):
    def test_corpus_matches_cpython(self):
        paths = sorted(glob.glob(os.path.join(CORPUS, "*.vl")))
        self.assertGreaterEqual(len(paths), 10, "differential corpus is too small")
        report = run_paths(paths)
        if report["mismatches"]:
            self.fail("output mismatch: " + repr(report["mismatches"]))
        if report["crashes"]:
            self.fail("native crash: " + repr(report["crashes"]))
        self.assertGreaterEqual(report["passed"], 10,
                                f"only {report['passed']} corpus cases compiled")
        self.assertEqual(len(report["skipped"]), 0,
                         "corpus cases must stay inside the native profile")


if __name__ == "__main__":
    unittest.main()
