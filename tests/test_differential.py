"""Run the differential corpus against CPython as part of unittest."""
from __future__ import annotations

import glob
import os
import unittest

from corpus_scale import TOTAL as CORPUS_TOTAL, verify_corpus_diversity
from differential import check_gate, main as diff_main, run_paths
from support import TMPDIR

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
        self.assertTrue(check_gate(report))

    def test_differential_rejects_skipped_files_by_default(self):
        bad_file = os.path.join(TMPDIR, "unsupported_skip.vl")
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write("async def foo():\n    pass\n")
        report = run_paths([bad_file])
        self.assertEqual(report["passed"], 0)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertFalse(check_gate(report, allow_skip=False))
        self.assertFalse(check_gate(report, allow_skip=True))
        self.assertEqual(diff_main([bad_file]), 1)

    def test_corpus_scale_diversity_gate(self):
        unique_progs, unique_skels = verify_corpus_diversity(CORPUS_TOTAL)
        self.assertEqual(unique_progs, CORPUS_TOTAL)
        self.assertGreaterEqual(unique_skels, 400)


if __name__ == "__main__":
    unittest.main()
