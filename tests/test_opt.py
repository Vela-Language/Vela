"""Optimizer tests: constant folding, DCE, and trap preservation."""
from __future__ import annotations

import unittest

from support import VelaTestCase, out_text, run_native

from vela.driver import compile_source


def ir_ops(source: str) -> list[str]:
    result = compile_source(source, "opt_test.vl")
    return [ins.op for fn in result.program.functions
            for block in fn.blocks for ins in block.instrs]


class OptimizerTests(VelaTestCase):
    def test_constant_folding_chain(self):
        ops = ir_ops("print(1 + 2 * 3 + 4)\n")
        self.assertNotIn("bin", ops)

    def test_constant_folding_float(self):
        ops = ir_ops("print(1.5 + 2.25)\n")
        self.assertNotIn("fadd", ops)
        proc = run_native("print(1.5 + 2.25)\n")
        self.assertEqual(out_text(proc), "3.75\n")

    def test_dead_float_op_removed(self):
        src = "def f(a):\n    x = a + 1.5\n    return 0\nprint(f(1.0))\n"
        ops = ir_ops(src)
        self.assertNotIn("fadd", ops)
        proc = run_native(src)
        self.assertEqual(out_text(proc), "0\n")

    def test_overflow_not_folded(self):
        src = "print(9223372036854775807 + 1)\n"
        self.assertIn("bin", ir_ops(src))
        proc = run_native(src)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"integer overflow", proc.stderr)

    def test_division_by_zero_not_folded(self):
        src = "print(7 // 0)\n"
        proc = run_native(src)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"division by zero", proc.stderr)

    def test_shift_out_of_range_not_folded(self):
        src = "print(1 << 64)\n"
        ops = ir_ops(src)
        self.assertIn("bin", ops)


    def test_inline_empty_function(self):
        src = ("def empty():\n    return 0\n\n"
               "def f():\n    i = 0\n    while i < 3:\n        empty()\n        i = i + 1\n    return i\n"
               "print(f())\n")
        result = compile_source(src, "inline_empty.vl")
        calls = [ins.args[0] for fn in result.program.functions
                 for b in fn.blocks for ins in b.instrs if ins.op == "call"]
        self.assertNotIn("fn_empty", calls)
        proc = run_native(src)
        self.assertEqual(out_text(proc), "3\n")

    def test_inline_add_and_param_isolation(self):
        src = ("def inc(x):\n    x = x + 1\n    return x\n\n"
               "def add(a, b):\n    return a + b\n\n"
               "v = 5\nprint(inc(v), add(v, 2), v)\n")
        result = compile_source(src, "inline_add.vl")
        calls = [ins.args[0] for fn in result.program.functions
                 for b in fn.blocks for ins in b.instrs if ins.op == "call"]
        self.assertNotIn("fn_inc", calls)
        self.assertNotIn("fn_add", calls)
        proc = run_native(src)
        self.assertEqual(out_text(proc), "6 7 5\n")

    def test_recursive_not_inlined_and_loop_callee_inlined(self):
        src = ("def fib(n):\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\n\n"
               "def gcd(a, b):\n    while b != 0:\n        t = a % b\n        a = b\n        b = t\n    return a\n"
               "print(fib(5), gcd(12, 18))\n")
        result = compile_source(src, "inline_neg.vl")
        calls = [ins.args[0] for fn in result.program.functions
                 for b in fn.blocks for ins in b.instrs if ins.op == "call"]
        self.assertIn("fn_fib", calls)          # recursion must stay a call
        self.assertNotIn("fn_gcd", calls)       # branch/loop callee is inlined
        proc = run_native(src)
        self.assertEqual(out_text(proc), "5 6\n")

    def test_inline_branching_callee(self):
        src = ("def sign(x):\n    if x < 0:\n        return -1\n    if x > 0:\n        return 1\n    return 0\n"
               "print(sign(-5), sign(0), sign(7))\n")
        result = compile_source(src, "inline_branch.vl")
        calls = [ins.args[0] for fn in result.program.functions
                 for b in fn.blocks for ins in b.instrs if ins.op == "call"]
        self.assertNotIn("fn_sign", calls)
        proc = run_native(src)
        self.assertEqual(out_text(proc), "-1 0 1\n")

    def test_inline_loop_callee(self):
        src = ("def sum_to(n):\n    s = 0\n    for i in range(n):\n        s = s + i\n    return s\n"
               "print(sum_to(10))\n")
        result = compile_source(src, "inline_loop.vl")
        calls = [ins.args[0] for fn in result.program.functions
                 for b in fn.blocks for ins in b.instrs if ins.op == "call"]
        self.assertNotIn("fn_sum_to", calls)
        proc = run_native(src)
        self.assertEqual(out_text(proc), "45\n")


    def test_inline_call_chain(self):
        src = ("def inc(x):\n    return x + 1\n\n"
               "def inc2(x):\n    return inc(inc(x))\n\n"
               "def inc3(x):\n    return inc2(inc(x))\n\n"
               "print(inc3(1))\n")
        result = compile_source(src, "inline_chain.vl")
        calls = [ins.args[0] for fn in result.program.functions
                 for b in fn.blocks for ins in b.instrs if ins.op == "call"]
        for name in ("fn_inc", "fn_inc2", "fn_inc3"):
            self.assertNotIn(name, calls)
        proc = run_native(src)
        self.assertEqual(out_text(proc), "4\n")

    def test_mutual_recursion_not_inlined(self):
        src = ("def even(n):\n    if n == 0:\n        return True\n    return odd(n - 1)\n\n"
               "def odd(n):\n    if n == 0:\n        return False\n    return even(n - 1)\n\n"
               "print(even(10), odd(10))\n")
        result = compile_source(src, "inline_mutual.vl")
        calls = [ins.args[0] for fn in result.program.functions
                 for b in fn.blocks for ins in b.instrs if ins.op == "call"]
        self.assertIn("fn_even", calls)
        self.assertIn("fn_odd", calls)

    def test_semantics_unchanged(self):
        src = ("a = 17\nb = 5\nprint(a + b, a - b, a * b, -a // b, a % b)\n"
               "print(2 ** 10, 6 & 3, 6 | 3, 6 ^ 3, 1024 >> 3)\n")
        proc = run_native(src)
        self.assertEqual(out_text(proc), "22 12 85 -4 2\n1024 2 7 5 128\n")


if __name__ == "__main__":
    unittest.main()
