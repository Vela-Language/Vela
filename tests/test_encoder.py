"""x86-64 encoder tests: execute generated code through VirtualAlloc."""
from __future__ import annotations

import ctypes
import unittest

from support import exec_code, VelaTestCase

from vela.x64 import Asm

I64 = ctypes.c_int64


class EncoderTests(VelaTestCase):
    def test_add_mul(self):
        a = Asm()
        a.bind("f")
        a.mov_rr("rax", "rcx")
        a.add_rr("rax", "rdx")
        a.imul_rri("rax", "rax", 3)
        a.ret()
        self.assertEqual(exec_code(a, I64, [I64, I64], 2, 3), 15)

    def test_loop_sum(self):
        a = Asm()
        a.bind("f")
        a.xor_rr("rax", "rax")
        a.mov_ri("r8", 1)
        a.bind("loop")
        a.cmp_rr("r8", "rcx")
        a.jcc("g", "done")
        a.add_rr("rax", "r8")
        a.add_ri("r8", 1)
        a.jmp("loop")
        a.bind("done")
        a.ret()
        self.assertEqual(exec_code(a, I64, [I64], 10), 55)

    def test_stack_frame_memory(self):
        a = Asm()
        a.bind("f")
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(32)
        a.mov_mr("rbp", -8, "rcx")
        a.mov_mr("rbp", -16, "rdx")
        a.mov_rm("rax", "rbp", -8)
        a.mov_rm("r10", "rbp", -16)
        a.sub_rr("rax", "r10")
        a.add_rr("rax", "rax")
        a.mov_rr("rsp", "rbp")
        a.pop_r("rbp")
        a.ret()
        self.assertEqual(exec_code(a, I64, [I64, I64], 9, 4), 10)

    def test_sse_double(self):
        a = Asm()
        a.bind("f")
        a.cvtsi2sd("xmm0", "rcx")
        a.mov_ri("rax", 0x4004000000000000)   # 2.5
        a.movq_xmm_r("xmm1", "rax")
        a.mulsd("xmm0", "xmm1")
        a.cvttsd2si("rax", "xmm0")
        a.ret()
        self.assertEqual(exec_code(a, I64, [I64], 4), 10)

    def test_setcc(self):
        for value, expected in ((3, 1), (30, 0)):
            a = Asm()
            a.bind("f")
            a.cmp_ri("rcx", 10)
            a.setcc("l", "rax")
            a.movzx8("rax", "rax")
            a.ret()
            self.assertEqual(exec_code(a, I64, [I64], value), expected)

    def test_save_restore_callee_reg(self):
        a = Asm()
        a.bind("f")
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(32)
        a.mov_mr("rbp", -8, "rbx")
        a.mov_ri("rbx", 999)
        a.mov_rm("rbx", "rbp", -8)
        a.mov_ri("rax", 55)
        a.mov_rr("rsp", "rbp")
        a.pop_r("rbp")
        a.ret()
        self.assertEqual(exec_code(a, I64, []), 55)

    def test_byte_store_and_div(self):
        # prints nothing; checks an unsigned div loop computes digits of 1234
        a = Asm()
        a.bind("f")
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(32)
        a.mov_ri("rax", 1234)
        a.mov_ri("r11", 10)
        a.mov_ri("r8", 0)          # digit count
        a.bind("digits")
        a.xor_rr("rdx", "rdx")
        a.div_r("r11")
        a.add_ri("r8", 1)
        a.test_rr("rax", "rax")
        a.jcc("ne", "digits")
        a.mov_rr("rax", "r8")
        a.mov_rr("rsp", "rbp")
        a.pop_r("rbp")
        a.ret()
        self.assertEqual(exec_code(a, I64, []), 4)


if __name__ == "__main__":
    unittest.main()
