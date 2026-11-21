"""End-to-end boxed print mode."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import ROOT, TMPDIR


def vela(*args, extra_env=None):
    if "--boxed" in args and "--strict" not in args:
        args = (*args, "--strict")
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "vela", *args], cwd=ROOT,
                          capture_output=True, timeout=120, env=env)


class BoxedTests(unittest.TestCase):
    def test_boxed_print_int(self):
        src = os.path.join(TMPDIR, "boxed_print.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(42)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"42", p.stdout)

    def test_boxed_print_scalars(self):
        src = os.path.join(TMPDIR, "boxed_scalars.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(True, False, None, 3.5, 1 < 2)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        for token in ("True", "False", "None", "3.5"):
            self.assertIn(token, out)

    def test_boxed_function_returns(self):
        src = os.path.join(TMPDIR, "boxed_ret.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def answer():\n    return 42\n\n"
                    "def inc(x):\n    return x + 1\n\n"
                    "print(answer(), inc(2))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("42", out)
        self.assertIn("3", out)

    def test_boxed_function_params(self):
        src = os.path.join(TMPDIR, "boxed_params.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a + b\n\nprint(add(2, 3))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"5", p.stdout)

    def test_boxed_print_str(self):
        src = os.path.join(TMPDIR, "boxed_str.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"hello\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"hello", p.stdout)

    def test_boxed_locals(self):
        src = os.path.join(TMPDIR, "boxed_locals.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = 2\ny = 3\nprint(x + y)\nx = x + 1\nprint(x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("5", out)
        self.assertIn("3", out)

    def test_boxed_branch_unassigned_local(self):
        src = os.path.join(TMPDIR, "boxed_branch.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def f(c):\n    if c:\n        x = 1\n    return 0\n\nprint(f(False))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"0", p.stdout)

    def test_boxed_str_params_returns(self):
        src = os.path.join(TMPDIR, "boxed_str_abi.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def echo(s):\n    return s\n\nprint(echo(\"hi\"))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"hi", p.stdout)

    def test_boxed_str_concat(self):
        src = os.path.join(TMPDIR, "boxed_concat.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = \"ab\"\ny = \"cd\"\nprint(x + y)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"abcd", p.stdout)

    def test_boxed_list_literal(self):
        src = os.path.join(TMPDIR, "boxed_list.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print([1, 2, 3])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"[1, 2, 3]", p.stdout)

    def test_boxed_list_subscript(self):
        src = os.path.join(TMPDIR, "boxed_sub.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print([10, 20, 30][1])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"20", p.stdout)

    def test_boxed_list_variable(self):
        src = os.path.join(TMPDIR, "boxed_list_var.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2, 3]\nprint(x)\nprint(x[2])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("[1, 2, 3]", out)
        self.assertIn("3", out)

    def test_boxed_list_len(self):
        src = os.path.join(TMPDIR, "boxed_list_len.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2, 3]\nprint(len(x), len(\"abc\"))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("3", out)

    def test_boxed_for_list(self):
        src = os.path.join(TMPDIR, "boxed_for_list.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("for x in [1, 2, 3]:\n    print(x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        for token in ("1", "2", "3"):
            self.assertIn(token, out)

    def test_boxed_list_setitem(self):
        src = os.path.join(TMPDIR, "boxed_setitem.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2, 3]\nx[1] = 20\nprint(x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"[1, 20, 3]", p.stdout)

    def test_boxed_list_append(self):
        src = os.path.join(TMPDIR, "boxed_append.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2]\nx.append(3)\nprint(x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"[1, 2, 3]", p.stdout)

    def test_boxed_list_append_many(self):
        src = os.path.join(TMPDIR, "boxed_append_many.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2]\nfor i in range(20):\n    x.append(i)\nprint(len(x))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"22", p.stdout)

    def test_boxed_dict_lookup(self):
        src = os.path.join(TMPDIR, "boxed_dict.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {1: 10, 2: 20}\nprint(d[2])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"20", p.stdout)

    def test_boxed_dict_setitem(self):
        src = os.path.join(TMPDIR, "boxed_dict_set.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {1: 10, 2: 20}\nd[1] = 30\nd[3] = 40\nprint(d[1], d[3])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("30", out)
        self.assertIn("40", out)

    def test_boxed_dict_print(self):
        src = os.path.join(TMPDIR, "boxed_dict_print.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {1: 10, 2: 20}\nprint(d)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"{1: 10, 2: 20}", p.stdout)

    def test_boxed_str_eq(self):
        src = os.path.join(TMPDIR, "boxed_str_eq.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"ab\" == \"ab\", \"ab\" != \"cd\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("True", out)

    def test_boxed_tuple(self):
        src = os.path.join(TMPDIR, "boxed_tuple.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("t = (1, 2, 3)\nprint(t)\nprint(t[1])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("(1, 2, 3)", out)
        self.assertIn("2", out)

    def test_boxed_set(self):
        src = os.path.join(TMPDIR, "boxed_set.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("s = {1, 2, 3}\nprint(s)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"{1, 2, 3}", p.stdout)

    def test_boxed_container_len(self):
        src = os.path.join(TMPDIR, "boxed_len_all.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(len([1, 2]), len((1, 2, 3)), len({1, 2}), len({1: 2}))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("2 3 2 1", out)

    def test_boxed_for_tuple(self):
        src = os.path.join(TMPDIR, "boxed_for_tuple.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("for x in (1, 2, 3):\n    print(x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        for token in ("1", "2", "3"):
            self.assertIn(token, out)

    def test_boxed_for_dict(self):
        src = os.path.join(TMPDIR, "boxed_for_dict.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {1: 10, 2: 20}\nfor k in d:\n    print(k)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        for token in ("1", "2"):
            self.assertIn(token, out)

    def test_boxed_str_index(self):
        src = os.path.join(TMPDIR, "boxed_stridx.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"abc\"[1])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"b", p.stdout)

    def test_boxed_dict_get(self):
        src = os.path.join(TMPDIR, "boxed_dict_get.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {1: 10}\nprint(d.get(1, 0), d.get(2, 99))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"10 99", p.stdout)

    def test_boxed_try_except(self):
        src = os.path.join(TMPDIR, "boxed_try.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    raise ValueError(\"boom\")\nexcept:\n    print(\"caught\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"caught", p.stdout)

    def test_boxed_try_normal_path(self):
        src = os.path.join(TMPDIR, "boxed_try_ok.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    print(\"ok\")\nexcept:\n    print(\"bad\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("ok", out)
        self.assertNotIn("bad", out)

    def test_boxed_set_add(self):
        src = os.path.join(TMPDIR, "boxed_set_add.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("s = {1, 2}\ns.add(2)\ns.add(3)\nprint(s)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"{1, 2, 3}", p.stdout)

    def test_boxed_import(self):
        helper = os.path.join(TMPDIR, "helper.vl")
        with open(helper, "w", encoding="utf-8") as f:
            f.write("def double(x):\n    return x * 2\n\n"
                    "def quad(x):\n    return double(double(x))\n")
        main = os.path.join(TMPDIR, "use_helper.vl")
        with open(main, "w", encoding="utf-8") as f:
            f.write("import helper\nprint(helper.double(3), helper.quad(2))\n")
        p = vela("run", "--boxed", main)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("6", out)
        self.assertIn("8", out)

    def test_boxed_str_upper(self):
        src = os.path.join(TMPDIR, "boxed_upper.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"Abc\".upper())\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"ABC", p.stdout)

    def test_boxed_str_lower(self):
        src = os.path.join(TMPDIR, "boxed_lower.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"AbC\".lower())\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"abc", p.stdout)

    def test_boxed_exception_message(self):
        src = os.path.join(TMPDIR, "boxed_exc_msg.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    raise ValueError(\"boom\")\nexcept Exception as e:\n    print(e)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"boom", p.stdout)

    def test_boxed_startswith(self):
        src = os.path.join(TMPDIR, "boxed_startswith.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"hello\".startswith(\"he\"), \"hello\".startswith(\"xx\"))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("True", out)
        self.assertIn("False", out)

    def test_boxed_str_slice(self):
        src = os.path.join(TMPDIR, "boxed_slice.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"hello\"[1:3], \"hello\"[2:])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("el", out)
        self.assertIn("llo", out)

    def test_boxed_endswith(self):
        src = os.path.join(TMPDIR, "boxed_endswith.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"hello\".endswith(\"lo\"), \"hello\".endswith(\"he\"))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("True", out)
        self.assertIn("False", out)

    def test_boxed_nested_try(self):
        src = os.path.join(TMPDIR, "boxed_try_nested.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    try:\n        raise ValueError(\"x\")\n    except:\n        print(\"inner\")\nexcept:\n    print(\"outer\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("inner", out)
        self.assertNotIn("outer", out)

    def test_boxed_raise_from_function(self):
        src = os.path.join(TMPDIR, "boxed_try_call.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def boom():\n    raise ValueError(\"x\")\n\ntry:\n    boom()\nexcept:\n    print(\"caught\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"caught", p.stdout)

    def test_boxed_try_else(self):
        src = os.path.join(TMPDIR, "boxed_try_else.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    print(\"body\")\nexcept:\n    print(\"bad\")\nelse:\n    print(\"else\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("body", out)
        self.assertIn("else", out)

    def test_boxed_try_else_skipped_on_raise(self):
        src = os.path.join(TMPDIR, "boxed_try_else_skip.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    raise ValueError(\"x\")\nexcept:\n    print(\"caught\")\nelse:\n    print(\"else\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("caught", out)
        self.assertNotIn("else", out)

    def test_boxed_try_finally(self):
        normal = os.path.join(TMPDIR, "boxed_finally_ok.vl")
        with open(normal, "w", encoding="utf-8") as f:
            f.write("try:\n    print(\"body\")\nexcept:\n    print(\"bad\")\nfinally:\n    print(\"finally\")\n")
        p = vela("run", "--boxed", normal)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("body", out)
        self.assertIn("finally", out)

        raised = os.path.join(TMPDIR, "boxed_finally_raise.vl")
        with open(raised, "w", encoding="utf-8") as f:
            f.write("try:\n    raise ValueError(\"x\")\nexcept:\n    print(\"caught\")\nfinally:\n    print(\"finally\")\n")
        p = vela("run", "--boxed", raised)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("caught", out)
        self.assertIn("finally", out)

    def test_boxed_list_pop(self):
        src = os.path.join(TMPDIR, "boxed_pop.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2, 3]\nprint(x.pop(), x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("3", out)
        self.assertIn("[1, 2]", out)

    def test_boxed_global_constants(self):
        src = os.path.join(TMPDIR, "boxed_globals.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("G = 10\nS = \"hi\"\ndef f():\n    return G + 1\nprint(f(), S)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("11", out)
        self.assertIn("hi", out)

    def test_boxed_except_type_match(self):
        src = os.path.join(TMPDIR, "boxed_exc_type_ok.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    raise ValueError(\"x\")\nexcept ValueError:\n    print(\"value\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("value", out)
        self.assertNotIn("type", out)

    def test_boxed_except_type_mismatch_uncaught(self):
        src = os.path.join(TMPDIR, "boxed_exc_type_bad.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    raise TypeError(\"x\")\nexcept ValueError:\n    print(\"wrong\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 1)

    def test_boxed_multi_except(self):
        src = os.path.join(TMPDIR, "boxed_multi_except.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("try:\n    raise TypeError(\"x\")\nexcept ValueError:\n    print(\"v\")\nexcept TypeError:\n    print(\"t\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("t", out)
        self.assertNotIn("v", out)

    def test_boxed_bytes_print(self):
        src = os.path.join(TMPDIR, "boxed_bytes.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(b\"abc\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"b'abc'", p.stdout)

    def test_boxed_dict_growth(self):
        src = os.path.join(TMPDIR, "boxed_dict_growth.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {}\nfor i in range(20):\n    d[i] = i\nprint(len(d), d[19])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("20", out)
        self.assertIn("19", out)

    def test_boxed_mutable_global(self):
        src = os.path.join(TMPDIR, "boxed_global_mut.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("G = 0\ndef bump():\n    global G\n    G = G + 1\nbump()\nbump()\nprint(G)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"2", p.stdout)

    def test_boxed_bigint_literal(self):
        src = os.path.join(TMPDIR, "boxed_biglit.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(9223372036854775808)\nprint(99999999999999999999)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("9223372036854775808", out)
        self.assertIn("99999999999999999999", out)

    def test_boxed_finally_on_return(self):
        src = os.path.join(TMPDIR, "boxed_finally_ret.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def f():\n    try:\n        return 1\n    except:\n        return 2\n    finally:\n        print(\"fin\")\n\nprint(f())\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("fin", out)
        self.assertIn("1", out)

    def test_boxed_list_growth(self):
        src = os.path.join(TMPDIR, "boxed_list_growth.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = []\nfor i in range(300):\n    x.append(i)\nprint(len(x), x[299])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("300", out)
        self.assertIn("299", out)

    def test_boxed_membership(self):
        src = os.path.join(TMPDIR, "boxed_in.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(2 in [1, 2, 3], 5 not in {1, 2}, 1 in {1: 10})\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("True", out)

    def test_boxed_str_dict_keys(self):
        src = os.path.join(TMPDIR, "boxed_strdict.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {\"a\": 1, \"b\": 2}\nprint(d[\"a\"], d.get(\"b\", 0), \"b\" in d)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("1", out)
        self.assertIn("2", out)
        self.assertIn("True", out)

    def test_boxed_str_find(self):
        src = os.path.join(TMPDIR, "boxed_find.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(\"hello\".find(\"ll\"), \"hello\".find(\"x\"))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("2", out)
        self.assertIn("-1", out)

    def test_boxed_list_extend(self):
        src = os.path.join(TMPDIR, "boxed_extend.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2]\ny = [3, 4]\nx.extend(y)\nprint(x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"[1, 2, 3, 4]", p.stdout)

    def test_boxed_list_count_index(self):
        src = os.path.join(TMPDIR, "boxed_count.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2, 3, 2]\nprint(x.count(2), x.index(3))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("2", out)
        self.assertIn("2", out)

    def test_boxed_tuple_unpack(self):
        src = os.path.join(TMPDIR, "boxed_unpack.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("a, b = (1, 2)\nprint(a + b)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"3", p.stdout)

    def test_boxed_assert(self):
        src = os.path.join(TMPDIR, "boxed_assert.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("assert 1 == 1\ntry:\n    assert 1 == 2\nexcept Exception as e:\n    print(\"caught\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"caught", p.stdout)

    def test_boxed_list_pop_at(self):
        src = os.path.join(TMPDIR, "boxed_pop_at.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x = [1, 2, 3]\nprint(x.pop(0), x)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("1", out)
        self.assertIn("[2, 3]", out)

    def test_boxed_set_ops(self):
        src = os.path.join(TMPDIR, "boxed_setop.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print({1, 2} | {2, 3}, {1, 2} & {2, 3})\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("{1, 2, 3}", out)
        self.assertIn("{2}", out)

    def test_boxed_dict_keys_values(self):
        src = os.path.join(TMPDIR, "boxed_dkv.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {1: 10, 2: 20}\nprint(d.keys(), d.values())\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("[1, 2]", out)
        self.assertIn("[10, 20]", out)

    def test_boxed_dynamic_str_key(self):
        src = os.path.join(TMPDIR, "boxed_dynkey.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {\"ab\": 7}\nk = \"a\" + \"b\"\nprint(d[k])\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"7", p.stdout)

    def test_boxed_dynamic_key_update(self):
        src = os.path.join(TMPDIR, "boxed_dynkey_set.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {\"ab\": 1}\nk = \"a\" + \"b\"\nd[k] = 9\nprint(d[\"ab\"], len(d))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("9", out)
        self.assertIn("1", out)

    def test_boxed_dynamic_key_membership(self):
        src = os.path.join(TMPDIR, "boxed_dynkey_in.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("d = {\"ab\": 1}\nk = \"a\" + \"b\"\nprint(k in d)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"True", p.stdout)

    def test_boxed_print_tuple(self):
        src = os.path.join(TMPDIR, "boxed_print_tuple.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print((1, 2))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"(1, 2)", p.stdout)

    def test_boxed_500_line_script(self):
        src = os.path.join(ROOT, "tests", "fixtures", "long500.vl")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        py = subprocess.run([sys.executable, src], capture_output=True, timeout=120)
        self.assertEqual(p.stdout, py.stdout)

    def test_boxed_nested_function(self):
        src = os.path.join(TMPDIR, "boxed_nested.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def outer(x):\n    def inner(y):\n        return y + 1\n    return inner(x)\nprint(outer(2))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"3", p.stdout)

    def test_boxed_from_import(self):
        mod = os.path.join(TMPDIR, "mathx.vl")
        with open(mod, "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a + b\n")
        src = os.path.join(TMPDIR, "from_import.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("from mathx import add\nprint(add(20, 22))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"42", p.stdout)

    def test_boxed_closure_capture(self):
        src = os.path.join(TMPDIR, "boxed_closure.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def outer(x):\n    def inner(y):\n        return x + y\n    return inner(1)\nprint(outer(2))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"3", p.stdout)

    def test_boxed_unhandled_message(self):
        src = os.path.join(TMPDIR, "boxed_unhandled.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("raise ValueError(\"boom\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"boom", p.stdout + p.stderr)
        self.assertIn(b"line 1", p.stdout + p.stderr)

    def test_boxed_nonlocal_cell(self):
        src = os.path.join(TMPDIR, "boxed_nonlocal.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def outer():\n    x = 0\n    def inc():\n        nonlocal x\n        x = x + 1\n    inc()\n    inc()\n    return x\nprint(outer())\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"2", p.stdout)

    def test_boxed_package_import(self):
        pkg = os.path.join(TMPDIR, "pkg")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "mod.vl"), "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a + b\n")
        src = os.path.join(TMPDIR, "pkg_import.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("from pkg.mod import add\nprint(add(40, 2))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"42", p.stdout)

    def test_boxed_relative_import(self):
        mod = os.path.join(TMPDIR, "relmod.vl")
        with open(mod, "w", encoding="utf-8") as f:
            f.write("def inc(x):\n    return x + 1\n")
        src = os.path.join(TMPDIR, "rel_import.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("from .relmod import inc\nprint(inc(41))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"42", p.stdout)

    def test_boxed_bigint_arithmetic(self):
        src = os.path.join(TMPDIR, "boxed_bigexpr.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(99999999999999999999 + 1)\nprint(99999999999999999999 - 1)\nprint(12345678901234567890 * 9876543210)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn(str(99999999999999999999 + 1), out)
        self.assertIn(str(99999999999999999999 - 1), out)
        self.assertIn(str(12345678901234567890 * 9876543210), out)

    def test_boxed_unhandled_function(self):
        src = os.path.join(TMPDIR, "boxed_unhandled_fn.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def boom():\n    raise ValueError(\"bad\")\nboom()\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 1)
        combined = p.stdout + p.stderr
        self.assertIn(b"bad", combined)
        self.assertIn(b"in boom", combined)
        self.assertIn(b"line 2", combined)

    def test_boxed_bigint_division(self):
        src = os.path.join(TMPDIR, "boxed_bigdiv.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(99999999999999999999 // 7, 99999999999999999999 % 7)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn(str(99999999999999999999 // 7), out)
        self.assertIn(str(99999999999999999999 % 7), out)

    def test_boxed_module_constant(self):
        mod = os.path.join(TMPDIR, "constmod.vl")
        with open(mod, "w", encoding="utf-8") as f:
            f.write("VALUE = 7\nNAME = \"ok\"\n")
        src = os.path.join(TMPDIR, "const_import.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("import constmod\nprint(constmod.VALUE, constmod.NAME)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("7", out)
        self.assertIn("ok", out)

    def test_boxed_traceback_frames(self):
        src = os.path.join(TMPDIR, "boxed_trace.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("def c():\n    raise ValueError(\"deep\")\ndef b():\n    c()\ndef a():\n    b()\na()\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 1)
        combined = p.stdout + p.stderr
        for token in (b"in c", b"in b", b"in a", b"line 2", b"line 7"):
            self.assertIn(token, combined)

    def test_boxed_bigint_compare(self):
        src = os.path.join(TMPDIR, "boxed_bigcmp.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(99999999999999999999 > 1, 99999999999999999999 == 99999999999999999999, 1 < 99999999999999999999)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"True True True", p.stdout)

    def test_boxed_exception_cause(self):
        src = os.path.join(TMPDIR, "boxed_cause.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("raise ValueError(\"top\") from ValueError(\"inner\")\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 1)
        combined = p.stdout + p.stderr
        self.assertIn(b"top", combined)
        self.assertIn(b"caused by inner", combined)

    def test_boxed_bigint_div_large(self):
        src = os.path.join(TMPDIR, "boxed_bigdiv_large.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(99999999999999999999 // 777777777777777777, 99999999999999999999 % 777777777777777777)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn(str(99999999999999999999 // 777777777777777777), out)
        self.assertIn(str(99999999999999999999 % 777777777777777777), out)

    def test_boxed_bigint_div_too_large(self):
        src = os.path.join(TMPDIR, "boxed_bigdiv_too_large.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(99999999999999999999 // 7777777777777777777)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"bigint too large", p.stderr + p.stdout)

    def test_boxed_bigint_len(self):
        src = os.path.join(TMPDIR, "boxed_biglen.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(len(99999999999999999999))\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"20", p.stdout)

    def test_boxed_bigint_big_div_const(self):
        src = os.path.join(TMPDIR, "boxed_bigbig_const.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(99999999999999999999 // 77777777777777777777, 99999999999999999999 % 77777777777777777777)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("1", out)
        self.assertIn("22222222222222222222", out)

    def test_boxed_bigint_variables(self):
        src = os.path.join(TMPDIR, "boxed_bigvars.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("a = 99999999999999999999\nb = 88888888888888888888\nprint(a + b)\nprint(a - b)\nprint(a * 3)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn(str(99999999999999999999 + 88888888888888888888), out)
        self.assertIn(str(99999999999999999999 - 88888888888888888888), out)
        self.assertIn(str(99999999999999999999 * 3), out)

    def test_boxed_bigint_var_div_explicit_error(self):
        src = os.path.join(TMPDIR, "boxed_bigvar_div.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("a = 99999999999999999999 + 1\nb = 77777777777777777777\nprint(a // b)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"compile-time constant", p.stderr + p.stdout)

    def test_boxed_bigint_var_div_const_prop(self):
        src = os.path.join(TMPDIR, "boxed_bigvar_div_const.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("a = 99999999999999999999\nb = 77777777777777777777\nprint(a // b, a % b)\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("1", out)
        self.assertIn("22222222222222222222", out)

    def test_boxed_dict_items(self):
        src = os.path.join(TMPDIR, "boxed_items.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print({1: 10, 2: 20}.items())\n")
        p = vela("run", "--boxed", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"[(1, 10), (2, 20)]", p.stdout)

    def test_default_print_unchanged(self):
        src = os.path.join(TMPDIR, "boxed_default.vl")
        with open(src, "w", encoding="utf-8") as f:
            f.write("print(42)\n")
        p = vela("run", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"42", p.stdout)


if __name__ == "__main__":
    unittest.main()
