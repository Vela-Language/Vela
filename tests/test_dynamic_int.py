"""v0.4 dynamic-int mode: boxed ints promote to bigint on overflow."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import ROOT, TMPDIR


def vela(*args):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "vela", *args], cwd=ROOT,
                          capture_output=True, timeout=120, env=env)


class DynamicIntTests(unittest.TestCase):
    def test_mul_overflow_promotes(self):
        src = os.path.join(TMPDIR, "dyn_mul.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = 4611686018427387904\ny = x * 4\nprint(y)\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(str(2 ** 62 * 4).encode(), p.stdout)

    def test_function_return_promotes(self):
        src = os.path.join(TMPDIR, "dyn_fn.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def f(x):\n    return x * x\nprint(f(4611686018427387904))\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(str(2 ** 62 * 2 ** 62).encode(), p.stdout)

    def test_negative_promotes(self):
        src = os.path.join(TMPDIR, "dyn_neg.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = -4611686018427387904\nprint(x * 3)\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(str(-(2 ** 62) * 3).encode(), p.stdout)

    def test_range_loop(self):
        src = os.path.join(TMPDIR, "dyn_loop.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("total = 0\nfor i in range(3):\n    total = total + i\nprint(total)\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"3", p.stdout)

    def test_while_loop(self):
        src = os.path.join(TMPDIR, "dyn_while.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("i = 0\ns = 0\nwhile i < 3:\n    s = s + i\n    i = i + 1\nprint(s)\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"3", p.stdout)

    def test_divmod(self):
        src = os.path.join(TMPDIR, "dyn_div.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(10 // 3, 10 % 3)\nx = 4611686018427387904\nprint(x // 3, x % 3)\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("3 1", out)
        self.assertIn(str((2 ** 62) // 3), out)

    def test_containers(self):
        src = os.path.join(TMPDIR, "dyn_cont.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2, 3]\ns = 0\nfor v in x:\n    s = s + v\nprint(s)\nd = {1: 10, 2: 20}\nprint(d[1] + d[2])\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("6", out)
        self.assertIn("30", out)

    def test_abs_negative(self):
        src = os.path.join(TMPDIR, "dyn_abs.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = -4611686018427387904\nprint(abs(x))\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(str(2 ** 62).encode(), p.stdout)

    def test_add_and_compare(self):
        src = os.path.join(TMPDIR, "dyn_add.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("a = 2\nb = 3\nprint(a + b, a < b)\n")
        p = vela("run", "--boxed-dyn", "--strict", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"5 True", p.stdout)


if __name__ == "__main__":
    unittest.main()
