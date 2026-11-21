"""i18n: bilingual CLI help and diagnostic framing."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import ROOT, TMPDIR


def vela(*args: str, extra_env: dict | None = None):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "vela", *args], cwd=ROOT,
                          capture_output=True, timeout=120, env=env)


class I18nTests(unittest.TestCase):
    def test_help_zh(self):
        p = vela("--help", "--lang", "zh")
        text = p.stdout.decode("utf-8")
        self.assertEqual(p.returncode, 0)
        self.assertIn("用法", text)
        self.assertIn("命令", text)
        self.assertIn("native profile", text)

    def test_help_en(self):
        p = vela("--help")
        text = p.stdout.decode("utf-8")
        self.assertIn("usage:", text)
        self.assertIn("commands:", text)

    def test_env_lang(self):
        p = vela("--help", extra_env={"VELA_LANG": "zh"})
        self.assertIn("用法", p.stdout.decode("utf-8"))

    def test_invalid_lang(self):
        p = vela("--help", "--lang", "fr")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unsupported language", p.stderr.decode("utf-8"))

    def test_unsupported_zh(self):
        path = os.path.join(TMPDIR, "i18n_bad.vl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("xs = [1, 2]\n")
        p = vela("check", path, "--lang", "zh")
        err = p.stderr.decode("utf-8")
        self.assertEqual(p.returncode, 1)
        self.assertIn("不支持", err)
        self.assertIn("容器字面量", err)

    def test_unsupported_en_default(self):
        path = os.path.join(TMPDIR, "i18n_bad_en.vl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("xs = [1, 2]\n")
        p = vela("check", path)
        err = p.stderr.decode("utf-8")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unsupported by the Vela native profile", err)
        self.assertIn("container literals", err)

    def test_build_message_zh(self):
        src = os.path.join(TMPDIR, "i18n_ok.vl")
        out = os.path.join(TMPDIR, "i18n_ok.exe")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(1 + 2)\n")
        p = vela("build", src, "-o", out, "--lang", "zh")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("已生成", p.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
