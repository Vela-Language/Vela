"""v0.10 acceptance tests: 100% bilingual i18n key parity, --set-lang persistent
default language vs --lang temporary override, bilingual CLI/stats/bench/emit-ir/
emit-asm messages, full diagnostic translation snapshots (zh vs en), Chinese
compiled .exe runtime error diagnostics (divzero/overflow/negexp/stepzero),
residual bare-English scan, and installer ChineseSimplified.isl verification.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from support import ROOT, TMPDIR  # noqa: E402

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vela import i18n  # noqa: E402


def _vela(*args: str, cfg_path: str | None = None, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    env.pop("VELA_LANG", None)
    env["VELA_LANG_CONFIG"] = cfg_path or os.path.join(TMPDIR, "default_test_lang.cfg")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "vela", *args],
        cwd=ROOT,
        capture_output=True,
        timeout=60,
        env=env,
    )


class V010BilingualI18nTests(unittest.TestCase):
    def setUp(self):
        i18n.set_lang(None)
        cfg = os.path.join(TMPDIR, "default_test_lang.cfg")
        if os.path.exists(cfg):
            os.unlink(cfg)

    def tearDown(self):
        i18n.set_lang(None)

    def test_en_zh_key_and_placeholder_100_percent_parity(self):
        en_keys = set(i18n.EN.keys())
        zh_keys = set(i18n.ZH.keys())
        self.assertEqual(en_keys, zh_keys, f"key mismatch: EN-ZH={en_keys - zh_keys}, ZH-EN={zh_keys - en_keys}")
        ph_re = re.compile(r"\{([a-zA-Z0-9_]+)\}")
        for k in sorted(en_keys):
            en_ph = set(ph_re.findall(i18n.EN[k]))
            zh_ph = set(ph_re.findall(i18n.ZH[k]))
            self.assertEqual(en_ph, zh_ph, f"placeholder mismatch for key '{k}': EN={en_ph} vs ZH={zh_ph}")

    def test_set_lang_persistence_and_lang_temporary_override(self):
        cfg = os.path.join(TMPDIR, "v010_lang_persist.cfg")
        if os.path.exists(cfg):
            os.unlink(cfg)
        # 1. Default without config is English
        p0 = _vela("--help", cfg_path=cfg)
        self.assertEqual(p0.returncode, 0)
        self.assertIn("usage:", p0.stdout.decode("utf-8"))

        # 2. Permanently set default language to Chinese via --set-lang zh
        p1 = _vela("--set-lang", "zh", cfg_path=cfg)
        self.assertEqual(p1.returncode, 0)
        self.assertIn("已将默认显示语言永久设置为 'zh'", p1.stdout.decode("utf-8"))
        self.assertTrue(os.path.isfile(cfg))

        # 3. Subsequent invocation without --lang uses persisted Chinese default
        p2 = _vela("--help", cfg_path=cfg)
        self.assertEqual(p2.returncode, 0)
        self.assertIn("用法:", p2.stdout.decode("utf-8"))

        # 4. --lang en temporarily overrides persisted Chinese for one invocation
        p3 = _vela("--help", "--lang", "en", cfg_path=cfg)
        self.assertEqual(p3.returncode, 0)
        self.assertIn("usage:", p3.stdout.decode("utf-8"))

        # 5. Next invocation without --lang still uses persisted Chinese
        p4 = _vela("--help", cfg_path=cfg)
        self.assertEqual(p4.returncode, 0)
        self.assertIn("用法:", p4.stdout.decode("utf-8"))

        # 6. --set-lang=en resets persisted default back to English
        p5 = _vela("--set-lang=en", cfg_path=cfg)
        self.assertEqual(p5.returncode, 0)
        self.assertIn("permanently set to 'en'", p5.stdout.decode("utf-8"))
        p6 = _vela("--help", cfg_path=cfg)
        self.assertIn("usage:", p6.stdout.decode("utf-8"))

    def test_bilingual_cli_status_stats_ir_asm_and_bench(self):
        src = os.path.join(TMPDIR, "v010_cli_ok.vl")
        exe = os.path.join(TMPDIR, "v010_cli_ok.exe")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def f(x: int) -> int:\n    return x + 1\nprint(f(41))\n")

        p_zh = _vela("build", src, "-o", exe, "--stats", "--emit-ir", "--emit-asm", "--lang", "zh")
        self.assertEqual(p_zh.returncode, 0)
        out_zh = p_zh.stdout.decode("utf-8")
        err_zh = p_zh.stderr.decode("utf-8")
        self.assertIn("已生成", out_zh)
        self.assertIn("个函数", err_zh)
        self.assertIn("IR 已写入", err_zh)
        self.assertIn("汇编清单已写入", err_zh)

        p_bench_zh = _vela("bench", src, "--lang", "zh")
        self.assertEqual(p_bench_zh.returncode, 0)
        self.assertIn("编译", p_bench_zh.stdout.decode("utf-8"))
        self.assertIn("退出码 0", p_bench_zh.stdout.decode("utf-8"))

        p_en = _vela("build", src, "-o", exe, "--stats", "--emit-ir", "--emit-asm", "--lang", "en")
        self.assertEqual(p_en.returncode, 0)
        self.assertIn("vela: built", p_en.stdout.decode("utf-8"))
        self.assertIn("functions,", p_en.stderr.decode("utf-8"))
        self.assertIn("IR written to", p_en.stderr.decode("utf-8"))
        self.assertIn("assembly written to", p_en.stderr.decode("utf-8"))

    def test_compile_time_diagnostic_snapshots_zh_and_en(self):
        cases = [
            ("syntax_err", "def f(\n", "语法错误", "syntax error"),
            ("type_mix", "print(1 + 'a')\n", "类型错误: 运算符 Add 需要数值类型", "type error: operator Add requires numbers"),
            ("var_reassign", "x = 1\nx = 1.5\nx = 's'\n", "原生 profile 要求每个变量保持单一静态类型", "the native profile requires one static type per variable"),
            ("before_assign", "print(unknown_var)\n", "在赋值前被使用", "is used before assignment"),
            ("len_arity", "print(len('a', 'b'))\n", "错误: len() 需要且仅需要 1 个参数", "error: len() takes exactly one argument"),
            ("range_step0", "for i in range(0, 10, 0):\n    print(i)\n", "range() 步长不能为零", "range() step must not be zero"),
            ("call_arity", "def f(a: int, b: int) -> int:\n    return a + b\nprint(f(1))\n", "需要 2 个参数，但传入了 1 个", "takes 2 arguments but 1 were given"),
            ("break_out", "break\n", "'break' 位于循环外部", "'break' outside a loop"),
            ("unsupported_feat", "xs = [1, 2, 3]\n", "Vela 原生 profile（v0.1）不支持：容器字面量", "unsupported by the Vela native profile (v0.1): container literals"),
        ]
        for name, code, expect_zh, expect_en in cases:
            path = os.path.join(TMPDIR, f"v010_diag_{name}.vl")
            out_exe = os.path.join(TMPDIR, f"v010_diag_{name}.exe")
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            p_zh = _vela("build", path, "-o", out_exe, "--lang", "zh")
            self.assertEqual(p_zh.returncode, 1, f"{name} zh should fail")
            err_zh = p_zh.stderr.decode("utf-8")
            self.assertIn(expect_zh, err_zh, f"{name} zh stderr={err_zh!r}")

            p_en = _vela("build", path, "-o", out_exe, "--lang", "en")
            self.assertEqual(p_en.returncode, 1, f"{name} en should fail")
            err_en = p_en.stderr.decode("utf-8")
            self.assertIn(expect_en, err_en, f"{name} en stderr={err_en!r}")

    def test_chinese_exe_runtime_errors_follow_compile_lang(self):
        rt_cases = [
            ("divzero", "def f(z: int) -> int:\n    return 10 // z\nprint(f(0))\n",
             "Vela 运行时错误：整数除以零", "Vela runtime error: integer division by zero"),
            ("overflow", "def f(x: int) -> int:\n    return x + 1\nprint(f(9223372036854775807))\n",
             "Vela 运行时错误：整数溢出（超出 int64 范围）", "Vela runtime error: integer overflow (int64 range exceeded)"),
            ("negexp", "def f(e: int) -> int:\n    return 2 ** e\nprint(f(-1))\n",
             "Vela 运行时错误：原生整数 profile 不支持负整数指数", "Vela runtime error: negative integer exponent is not supported"),
            ("stepzero", "def f(s: int) -> int:\n    acc = 0\n    for i in range(0, 10, s):\n        acc = acc + i\n    return acc\nprint(f(0))\n",
             "Vela 运行时错误：range() 步长不能为零", "Vela runtime error: range() step must not be zero"),
        ]
        for name, code, zh_msg, en_msg in rt_cases:
            src = os.path.join(TMPDIR, f"v010_rt_{name}.vl")
            exe_zh = os.path.join(TMPDIR, f"v010_rt_{name}_zh.exe")
            exe_en = os.path.join(TMPDIR, f"v010_rt_{name}_en.exe")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)

            b_zh = _vela("build", src, "-o", exe_zh, "--lang", "zh")
            self.assertEqual(b_zh.returncode, 0, b_zh.stderr)
            r_zh = subprocess.run([exe_zh], capture_output=True, timeout=15)
            self.assertEqual(r_zh.returncode, 1)
            self.assertIn(zh_msg, r_zh.stderr.decode("utf-8", "replace"))

            b_en = _vela("build", src, "-o", exe_en, "--lang", "en")
            self.assertEqual(b_en.returncode, 0, b_en.stderr)
            r_en = subprocess.run([exe_en], capture_output=True, timeout=15)
            self.assertEqual(r_en.returncode, 1)
            self.assertIn(en_msg, r_en.stderr.decode("utf-8", "replace"))

    def test_residual_bare_english_scan_on_zh_table(self):
        cjk_re = re.compile(r"[\u4e00-\u9fff]")
        for k, val in i18n.ZH.items():
            self.assertIsNotNone(cjk_re.search(val), f"ZH key '{k}' has no Chinese characters: {val!r}")
        for feat, val in i18n.FEATURES_ZH.items():
            if feat == "f-strings":
                continue
            self.assertIsNotNone(cjk_re.search(val), f"FEATURES_ZH['{feat}'] has no Chinese characters: {val!r}")

    def test_installer_chinese_simplified_isl_present(self):
        isl_path = os.path.join(ROOT, "installer", "ChineseSimplified.isl")
        iss_path = os.path.join(ROOT, "installer", "vela.iss")
        self.assertTrue(os.path.isfile(isl_path))
        self.assertTrue(os.path.isfile(iss_path))
        with open(iss_path, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf", "vela.iss must have UTF-8 BOM for Inno Setup")
        with open(isl_path, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf", "ChineseSimplified.isl must have UTF-8 BOM for Inno Setup")
        with open(iss_path, "r", encoding="utf-8-sig") as f:
            iss_text = f.read()
        self.assertIn("ChineseSimplified.isl", iss_text)


if __name__ == "__main__":
    unittest.main()
