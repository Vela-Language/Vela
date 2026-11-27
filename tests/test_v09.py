"""v0.9 acceptance tests: P4 register coalescing + immediate folding + box/unbox
elimination, P5 jump threading + block reordering + fallthrough jump elimination,
P6 LICM + counting loop folding + 4x unrolling + parity if-conversion + depth-3
self-recursive unrolling, P7 magic-multiplier constant division/modulo,
--emit-asm assembly listing, installer/dist verification, and >=3x object / >=10x
numerical performance gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from support import ROOT, TMPDIR  # noqa: E402

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vela.cli import main as cli_main  # noqa: E402
from vela.driver import compile_source  # noqa: E402
from vela.ir_dump import dump_asm  # noqa: E402
from vela.regalloc import find_imm_only_vregs  # noqa: E402


def _run_src(name: str, src: str, boxed: bool = False) -> bytes:
    src_path = os.path.join(TMPDIR, f"v09_{name}.vl")
    exe_path = os.path.join(TMPDIR, f"v09_{name}.exe")
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(src)
    res = compile_source(src, src_path, boxed=boxed)
    with open(exe_path, "wb") as f:
        f.write(res.exe)
    p = subprocess.run([exe_path], capture_output=True, timeout=30)
    if p.returncode != 0:
        raise AssertionError(f"{name} failed (rc={p.returncode}): {p.stderr!r}")
    return p.stdout.replace(b"\r\n", b"\n")


class V09PerformanceAndEngineeringTests(unittest.TestCase):
    def test_p4_immediate_folding_and_copy_coalescing(self):
        src = (
            "def compute(n: int) -> int:\n"
            "    acc = 0\n"
            "    i = 0\n"
            "    while i < n:\n"
            "        acc = (acc * 3 + 13) ^ 255\n"
            "        i = i + 1\n"
            "    return acc\n"
            "print(compute(20))\n"
        )
        res = compile_source(src, "p4.vl")
        fn = next(f for f in res.program.functions if f.name == "fn_compute")
        const_ints, imm_only = find_imm_only_vregs(fn)
        self.assertGreaterEqual(len(imm_only), 3)
        for v in imm_only:
            self.assertNotIn(v, res.allocs["fn_compute"].regs)
            self.assertNotIn(v, res.allocs["fn_compute"].slots)
        out = _run_src("p4_imm", src)
        py = subprocess.run([sys.executable, "-c", src], capture_output=True, timeout=15)
        self.assertEqual(out, py.stdout.replace(b"\r\n", b"\n"))

    def test_p4_box_unbox_elimination_on_inlined_methods(self):
        src = (
            "class Acc:\n"
            "    def __init__(self, base: int):\n"
            "        self.base = base\n"
            "    def step(self, x: int) -> int:\n"
            "        return x * 3 + 5\n"
            "a = Acc(10)\n"
            "s = a.base\n"
            "i = 0\n"
            "while i < 50:\n"
            "    s = s + a.step(i)\n"
            "    i = i + 1\n"
            "print(s)\n"
        )
        out = _run_src("p4_box_unbox", src, boxed=True)
        py = subprocess.run([sys.executable, "-c", src], capture_output=True, timeout=15)
        self.assertEqual(out, py.stdout.replace(b"\r\n", b"\n"))

    def test_p5_jump_threading_and_dead_block_pruning(self):
        src = (
            "def loop_sum() -> int:\n"
            "    s = 0\n"
            "    for i in range(1, 20):\n"
            "        if i < 10:\n"
            "            s = s + i\n"
            "        else:\n"
            "            s = s + 2 * i\n"
            "    return s\n"
            "print(loop_sum())\n"
        )
        res = compile_source(src, "p5.vl")
        calls = [
            ins.args[0]
            for fn in res.program.functions
            for b in fn.blocks
            for ins in b.instrs
            if ins.op == "call"
        ]
        # Dead `for_bad_step` block with rt_range_error must be pruned for literal step=1
        self.assertNotIn("rt_range_error", calls)
        out = _run_src("p5_jmp", src)
        self.assertEqual(out.strip(), b"335")

    def test_p6_licm_loop_folding_and_unrolling(self):
        src = (
            "def nested_count() -> int:\n"
            "    n = 0\n"
            "    a = 0\n"
            "    while a < 100:\n"
            "        b = 0\n"
            "        while b < 100:\n"
            "            n = n + 3\n"
            "            b = b + 1\n"
            "        a = a + 1\n"
            "    return n\n"
            "def parity_branch() -> int:\n"
            "    c = 0\n"
            "    i = 0\n"
            "    while i < 1000:\n"
            "        if i % 2 == 0:\n"
            "            c = c + 1\n"
            "        else:\n"
            "            c = c - 1\n"
            "        i = i + 1\n"
            "    return c\n"
            "print(nested_count(), parity_branch())\n"
        )
        out = _run_src("p6_loops", src)
        self.assertEqual(out.strip(), b"30000 0")

    def test_p7_constant_divmod_magic_multiplier_signed(self):
        src = (
            "def check_divmod(x: int) -> int:\n"
            "    q3 = x // 3\n"
            "    r3 = x % 3\n"
            "    q7 = x // 7\n"
            "    r7 = x % 7\n"
            "    qm = x // 1000003\n"
            "    rm = x % 1000003\n"
            "    return q3 * 1000 + r3 * 100 + q7 * 10 + r7 + qm - rm\n"
            "vals = [-1000004, -1000003, -1000002, -15, -14, -13, -7, -3, -1, 0, 1, 3, 7, 13, 14, 15, 1000002, 1000003, 1000004]\n"
            "for v in vals:\n"
            "    print(v, check_divmod(v))\n"
        )
        res = compile_source(src, "p7.vl", boxed=True)
        ops = [
            ins.args[0]
            for fn in res.program.functions
            for b in fn.blocks
            for ins in b.instrs
            if ins.op == "bin"
        ]
        self.assertIn("idiv_c", ops)
        self.assertIn("imod_c", ops)
        out = _run_src("p7_divmod", src, boxed=True)
        py = subprocess.run([sys.executable, "-c", src], capture_output=True, timeout=15)
        self.assertEqual(out, py.stdout.replace(b"\r\n", b"\n"))

    def test_emit_asm_cli_and_dump_asm(self):
        src = "def sq(x: int) -> int:\n    return x * x\nprint(sq(9))\n"
        src_path = os.path.join(TMPDIR, "v09_emit_asm.vl")
        exe_path = os.path.join(TMPDIR, "v09_emit_asm.exe")
        asm_path = os.path.join(TMPDIR, "v09_emit_asm.asm")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(src)
        rc = cli_main(["build", src_path, "-o", exe_path, "--emit-asm"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(asm_path))
        with open(asm_path, "r", encoding="utf-8") as f:
            asm_text = f.read()
        self.assertIn("; Vela x86-64 native assembly & symbol map", asm_text)
        self.assertIn(".text+0x", asm_text)
        self.assertIn("fn___vela_main__:", asm_text)

    def test_local_dist_and_installer_verification(self):
        launcher = os.path.join(ROOT, "vela.cmd")
        self.assertTrue(os.path.isfile(launcher))
        p = subprocess.run([launcher, "version"], capture_output=True, timeout=15)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"Vela ", p.stdout)
        iss_path = os.path.join(ROOT, "installer", "vela.iss")
        self.assertTrue(os.path.isfile(iss_path))

    def test_v09_benchmark_gates_object_3x_and_numerical_10x(self):
        peer_json = os.path.join(ROOT, "bench", "peers", "results-py3.15.json")
        self.assertTrue(os.path.isfile(peer_json))
        with open(peer_json, encoding="utf-8") as f:
            data = json.load(f)
        rows = {r["name"]: r for r in data["rows"]}
        self.assertEqual(len(rows), 14)
        for obj_case in ("list_ops", "dict_ops", "str_ops", "class_oop"):
            self.assertIn(obj_case, rows)
            sp = rows[obj_case]["speedup_vs_cpython_host"]
            self.assertGreaterEqual(sp, 3.0, f"{obj_case} speedup {sp:.2f}x < 3.0x")
        for num_case in ("empty_call", "float_sum", "mulmod", "branch", "fib30", "loop", "fib", "collatz", "primes"):
            self.assertIn(num_case, rows)
            sp = rows[num_case]["speedup_vs_cpython_host"]
            self.assertGreaterEqual(sp, 10.0, f"{num_case} speedup {sp:.2f}x < 10.0x")


if __name__ == "__main__":
    unittest.main()
