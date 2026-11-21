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

    def test_semantics_unchanged(self):
        src = ("a = 17\nb = 5\nprint(a + b, a - b, a * b, -a // b, a % b)\n"
               "print(2 ** 10, 6 & 3, 6 | 3, 6 ^ 3, 1024 >> 3)\n")
        proc = run_native(src)
        self.assertEqual(out_text(proc), "22 12 85 -4 2\n1024 2 7 5 128\n")


if __name__ == "__main__":
    unittest.main()
