"""Randomized differential test for runtime bigint floor division."""
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


class BigintDivisionRandomTests(unittest.TestCase):
    def test_random_floor_divmod(self):
        rng = random.Random(20261122)
        pairs = []
        for _ in range(30):
            digits = rng.randint(20, 40)
            a = rng.randrange(10 ** (digits - 1), 10 ** digits)
            b = rng.randrange(10 ** 19, 10 ** 20)
            if rng.random() < 0.5:
                a = -a
            if rng.random() < 0.5:
                b = -b
            pairs.append((a, b))
        lines = ["def div(a, b):", "    return a // b",
                 "def mod(a, b):", "    return a % b"]
        expected = []
        for a, b in pairs:
            ae = str(a) if a >= 0 else f"(0 - {abs(a)})"
            be = str(b) if b >= 0 else f"(0 - {abs(b)})"
            lines.append(f"print(div({ae}, {be}), mod({ae}, {be}))")
            expected.append(f"{a // b} {a % b}")
        src = os.path.join(TMPDIR, "bigdiv_random.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        p = vela("run", "--boxed", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        got = p.stdout.decode("utf-8", "replace").strip().splitlines()
        self.assertEqual(len(got), len(expected), got)
        for i, (g, e) in enumerate(zip(got, expected)):
            self.assertEqual(g.strip(), e, f"case {i}: {pairs[i]}")


if __name__ == "__main__":
    unittest.main()
