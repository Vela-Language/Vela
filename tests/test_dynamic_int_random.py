"""Randomized differential test for boxed-dyn integer promotion."""
from __future__ import annotations

import os
import random
import subprocess
import sys
import unittest

from support import ROOT, TMPDIR


def vela(*args):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "vela", *args], cwd=ROOT,
                          capture_output=True, timeout=180, env=env)


class DynamicIntRandomTests(unittest.TestCase):
    def test_random_promotion(self):
        rng = random.Random(20261123)
        lines = []
        expected = []
        for _ in range(15):
            a = rng.randrange(2 ** 60, 2 ** 64)
            b = rng.randrange(2 ** 60, 2 ** 64)
            if rng.random() < 0.5:
                b = -b
            ae = str(a)
            be = str(b) if b >= 0 else f"(0 - {abs(b)})"
            lines.append(
                f"print({ae} + {be}, {ae} - {be}, {ae} * {be}, {ae} // {be}, {ae} % {be})")
            expected.append(f"{a + b} {a - b} {a * b} {a // b} {a % b}")
        src = os.path.join(TMPDIR, "dyn_random.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        got = p.stdout.decode("utf-8", "replace").strip().splitlines()
        self.assertEqual(len(got), len(expected), got)
        for i, (g, e) in enumerate(zip(got, expected)):
            self.assertEqual(g.strip(), e, f"case {i}")


if __name__ == "__main__":
    unittest.main()
