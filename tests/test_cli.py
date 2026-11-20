"""CLI tests: build / run / check / fallback."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from support import ROOT, TMPDIR, out_text


def vela(*args: str, cwd: str = ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    return subprocess.run([sys.executable, "-m", "vela", *args], cwd=cwd,
                          capture_output=True, timeout=120, env=env)


class CliTests(unittest.TestCase):
    def write(self, name: str, src: str) -> str:
        path = os.path.join(TMPDIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        return path

    def test_version(self):
        p = vela("version")
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"Vela 0.1.0", p.stdout)

    def test_build_and_run(self):
        src = self.write("cli_hello.py", "print(6 * 7)\n")
        out = os.path.join(TMPDIR, "cli_hello.exe")
        p = vela("build", src, "-o", out, "--stats")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(out))
        run = subprocess.run([out], capture_output=True, timeout=10)
        self.assertEqual(run.stdout, b"42\r\n")
        p = vela("run", src)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"42\r\n")

    def test_vl_extension(self):
        src = self.write("cli_ext.vl", "print(21 * 2)\n")
        out = os.path.join(TMPDIR, "cli_ext.exe")
        p = vela("build", src, "-o", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        run = subprocess.run([out], capture_output=True, timeout=10)
        self.assertEqual(run.stdout, b"42\r\n")

    def test_check(self):
        src = self.write("cli_ok.py", "def f(a: int) -> int:\n    return a + 1\nprint(f(1))\n")
        p = vela("check", src)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"within the Vela native profile", p.stdout)

    def test_check_rejects(self):
        src = self.write("cli_bad.py", "xs = [1]\n")
        p = vela("check", src)
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"container literals", p.stderr)

    def test_run_falls_back(self):
        src = self.write("cli_list.py", "xs = [1, 2, 3]\nprint(sum(xs))\n")
        p = vela("run", src)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(out_text(p), "6\n")
        self.assertIn(b"falling back to CPython", p.stderr)

    def test_run_strict_refuses(self):
        src = self.write("cli_list2.py", "xs = [1]\nprint(xs)\n")
        p = vela("run", src, "--strict")
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"container literals", p.stderr)

    def test_program_args(self):
        src = self.write("cli_args.py", "print('hi')\n")
        p = vela("run", src)
        self.assertEqual(p.stdout, b"hi\r\n")

    def test_build_syntax_error(self):
        src = self.write("cli_syntax.py", "def f(:\n    pass\n")
        p = vela("build", src)
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"syntax error", p.stderr)


if __name__ == "__main__":
    unittest.main()
