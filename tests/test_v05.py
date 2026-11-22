"""v0.5 acceptance tests: dynamic function objects, precise GC + cycle finalizers,
200 CPython subset programs, and list/dict/str >= 2x CPython microbenchmarks."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from support import ROOT, TMPDIR

sys.path.insert(0, ROOT)

from cpython_subset import build_cases, run_subset  # noqa: E402
from test_runtime_obj import _build_ir_exe, _make_counter_finalizer  # noqa: E402
from vela.driver import compile_source  # noqa: E402
from vela.ir import BOOL, INT, OBJ, Function  # noqa: E402


def _best_time(fn, repeats: int = 3) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


class V05AcceptanceTests(unittest.TestCase):
    # -- 1. Dynamic function objects & negative checks ---------------------
    def test_code_call_argv_arity_mismatch_traps(self):
        target = Function("fn_wrap_t1", OBJ)
        a0 = target.new_vreg(OBJ, "argv")
        target.params = [a0]
        target.entry.add("arg", a0, 0)
        target.entry.add("ret", None, a0)

        fn = Function("fn_arity_err", INT)
        e = fn.entry
        addr = fn.new_vreg(INT, "addr")
        two = fn.new_vreg(INT, "two")
        one = fn.new_vreg(INT, "one")
        zero = fn.new_vreg(INT, "zero")
        code = fn.new_vreg(OBJ, "code")
        argv = fn.new_vreg(OBJ, "argv")
        res = fn.new_vreg(OBJ, "res")
        e.add("const", two, 2)
        e.add("const", one, 1)
        e.add("const", zero, 0)
        e.add("addr", addr, target.name)
        e.add("call", code, "rt_code_new", (addr, two))
        e.add("call", argv, "rt_list_new", (one,))
        e.add("call", res, "rt_code_call_argv", (code, argv))
        e.add("ret", None, zero)

        exe = _build_ir_exe([target, fn], fn)
        path = os.path.join(TMPDIR, "code_arity_err.exe")
        with open(path, "wb") as f:
            f.write(exe)
        proc = subprocess.run([path], capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"code object arity", proc.stderr)

    def test_code_call_argv_type_mismatch_traps(self):
        fn = Function("fn_code_type_err", INT)
        e = fn.entry
        one = fn.new_vreg(INT, "one")
        zero = fn.new_vreg(INT, "zero")
        not_code = fn.new_vreg(OBJ, "not_code")
        argv = fn.new_vreg(OBJ, "argv")
        res = fn.new_vreg(OBJ, "res")
        e.add("const", one, 1)
        e.add("const", zero, 0)
        e.add("call", not_code, "rt_box_int", (one,))
        e.add("call", argv, "rt_list_new", (zero,))
        e.add("call", res, "rt_code_call_argv", (not_code, argv))
        e.add("ret", None, zero)

        exe = _build_ir_exe([fn], fn)
        path = os.path.join(TMPDIR, "code_type_err.exe")
        with open(path, "wb") as f:
            f.write(exe)
        proc = subprocess.run([path], capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"unbox type mismatch", proc.stderr)

    # -- 2. Precise GC & cycle finalizer topologies ------------------------
    def test_gc_loop_safepoint_liveness(self):
        """An OBJ defined before a loop and used after the loop survives
        multiple rt_gc_collect() calls inside the loop, while dead cycles
        allocated inside each iteration are collected."""
        fn = Function("fn_gc_loop", INT)
        e = fn.entry
        keep = fn.new_vreg(OBJ, "keep")
        tmp1 = fn.new_vreg(OBJ, "tmp1")
        tmp2 = fn.new_vreg(OBJ, "tmp2")
        zero = fn.new_vreg(INT, "zero")
        one = fn.new_vreg(INT, "one")
        three = fn.new_vreg(INT, "three")
        i = fn.new_vreg(INT, "i")
        nxt = fn.new_vreg(INT, "nxt")
        live = fn.new_vreg(INT, "live")
        klen = fn.new_vreg(INT, "klen")
        lcond = fn.new_vreg(BOOL, "lcond")
        c1 = fn.new_vreg(BOOL, "c1")
        c2 = fn.new_vreg(BOOL, "c2")
        head = fn.new_block("head")
        body = fn.new_block("body")
        after = fn.new_block("after")
        chk = fn.new_block("chk")
        ok = fn.new_block("ok")
        bad = fn.new_block("bad")

        e.add("const", zero, 0)
        e.add("const", one, 1)
        e.add("const", three, 3)
        e.add("call", keep, "rt_list_new", (three,))
        e.add("mov", i, zero)
        e.add("jmp", None, head.label)

        head.add("cmp", lcond, "lt", i, three)
        head.add("br", None, lcond, body.label, after.label)

        body.add("call", tmp1, "rt_list_new", (one,))
        body.add("call", tmp2, "rt_list_new", (one,))
        body.add("call", None, "rt_list_set", (tmp1, zero, tmp2))
        body.add("call", None, "rt_list_set", (tmp2, zero, tmp1))
        body.add("call", None, "rt_gc_collect", ())
        body.add("bin", nxt, "add", i, one)
        body.add("mov", i, nxt)
        body.add("jmp", None, head.label)

        after.add("call", live, "rt_gc_live", ())
        after.add("call", klen, "rt_list_len", (keep,))
        after.add("cmp", c1, "eq", live, one)
        after.add("br", None, c1, chk.label, bad.label)
        chk.add("cmp", c2, "eq", klen, three)
        chk.add("br", None, c2, ok.label, bad.label)
        ok.add("ret", None, zero)
        bad.add("ret", None, one)

        exe = _build_ir_exe([fn], fn)
        path = os.path.join(TMPDIR, "gc_loop_live.exe")
        with open(path, "wb") as f:
            f.write(exe)
        proc = subprocess.run([path], capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_gc_linked_cycles_finalizers(self):
        """Two cycles linked together (a1 <-> a2 -> b1 <-> b2): all 4 finalizers
        execute and all 4 objects are swept."""
        fin = _make_counter_finalizer("fn_fin_linked", 0)
        fn = Function("fn_gc_linked", INT)
        e = fn.entry
        a1 = fn.new_vreg(OBJ, "a1")
        a2 = fn.new_vreg(OBJ, "a2")
        b1 = fn.new_vreg(OBJ, "b1")
        b2 = fn.new_vreg(OBJ, "b2")
        zero = fn.new_vreg(INT, "zero")
        one = fn.new_vreg(INT, "one")
        two = fn.new_vreg(INT, "two")
        four = fn.new_vreg(INT, "four")
        faddr = fn.new_vreg(INT, "faddr")
        live = fn.new_vreg(INT, "live")
        cnt = fn.new_vreg(INT, "cnt")
        c1 = fn.new_vreg(BOOL, "c1")
        c2 = fn.new_vreg(BOOL, "c2")
        chk = fn.new_block("chk")
        ok = fn.new_block("ok")
        bad = fn.new_block("bad")

        e.add("const", zero, 0)
        e.add("const", one, 1)
        e.add("const", two, 2)
        e.add("const", four, 4)
        e.add("call", None, "rt_global_set", (zero, zero))
        e.add("addr", faddr, fin.name)
        e.add("call", a1, "rt_list_new", (one,))
        e.add("call", a2, "rt_list_new", (two,))
        e.add("call", b1, "rt_list_new", (one,))
        e.add("call", b2, "rt_list_new", (one,))
        e.add("call", None, "rt_list_set", (a1, zero, a2))
        e.add("call", None, "rt_list_set", (a2, zero, a1))
        e.add("call", None, "rt_list_set", (a2, one, b1))
        e.add("call", None, "rt_list_set", (b1, zero, b2))
        e.add("call", None, "rt_list_set", (b2, zero, b1))
        for obj in (a1, a2, b1, b2):
            e.add("call", None, "rt_set_finalizer", (obj, faddr))
        e.add("call", None, "rt_gc_collect", ())
        e.add("call", live, "rt_gc_live", ())
        e.add("call", cnt, "rt_global_get", (zero,))
        e.add("cmp", c1, "eq", live, zero)
        e.add("br", None, c1, chk.label, bad.label)
        chk.add("cmp", c2, "eq", cnt, four)
        chk.add("br", None, c2, ok.label, bad.label)
        ok.add("ret", None, zero)
        bad.add("ret", None, one)

        exe = _build_ir_exe([fin, fn], fn)
        path = os.path.join(TMPDIR, "gc_linked.exe")
        with open(path, "wb") as f:
            f.write(exe)
        proc = subprocess.run([path], capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_gc_resuscitation_into_live_container(self):
        """Finalizer attaches unreachable cycle a<->b into a live container
        stored in the caller frame (passed via global[1] pointer to container).
        Phase 3 re-traverses already-marked container children and keeps a and b alive."""
        fin = Function("fn_fin_cont_resusc", INT)
        obj = fin.new_vreg(OBJ, "obj")
        fin.params = [obj]
        fe = fin.entry
        fe.add("arg", obj, 0)
        z = fin.new_vreg(INT, "z")
        o = fin.new_vreg(INT, "o")
        holder = fin.new_vreg(OBJ, "holder")
        fe.add("const", z, 0)
        fe.add("const", o, 1)
        fe.add("call", holder, "rt_global_get", (o,))
        fe.add("call", None, "rt_list_set", (holder, z, obj))
        fe.add("ret", None, o)

        fn = Function("fn_gc_cont_resusc", INT)
        e = fn.entry
        holder = fn.new_vreg(OBJ, "holder")
        a = fn.new_vreg(OBJ, "a")
        b = fn.new_vreg(OBJ, "b")
        got_a = fn.new_vreg(OBJ, "got_a")
        got_b = fn.new_vreg(OBJ, "got_b")
        blen = fn.new_vreg(INT, "blen")
        zero = fn.new_vreg(INT, "zero")
        one = fn.new_vreg(INT, "one")
        three = fn.new_vreg(INT, "three")
        faddr = fn.new_vreg(INT, "faddr")
        live = fn.new_vreg(INT, "live")
        c1 = fn.new_vreg(BOOL, "c1")
        c2 = fn.new_vreg(BOOL, "c2")
        chk = fn.new_block("chk")
        ok = fn.new_block("ok")
        bad = fn.new_block("bad")

        e.add("const", zero, 0)
        e.add("const", one, 1)
        e.add("const", three, 3)
        e.add("call", holder, "rt_list_new", (one,))
        e.add("call", None, "rt_global_set", (one, holder))
        e.add("call", a, "rt_list_new", (one,))
        e.add("call", b, "rt_list_new", (one,))
        e.add("call", None, "rt_list_set", (a, zero, b))
        e.add("call", None, "rt_list_set", (b, zero, a))
        e.add("addr", faddr, fin.name)
        e.add("call", None, "rt_set_finalizer", (a, faddr))
        e.add("call", None, "rt_gc_collect", ())
        e.add("call", live, "rt_gc_live", ())
        e.add("call", got_a, "rt_list_get", (holder, zero))
        e.add("call", got_b, "rt_list_get", (got_a, zero))
        e.add("call", blen, "rt_list_len", (got_b,))
        e.add("cmp", c1, "eq", live, three)
        e.add("br", None, c1, chk.label, bad.label)
        chk.add("cmp", c2, "eq", blen, one)
        chk.add("br", None, c2, ok.label, bad.label)
        ok.add("ret", None, zero)
        bad.add("ret", None, one)

        exe = _build_ir_exe([fin, fn], fn)
        path = os.path.join(TMPDIR, "gc_cont_resusc.exe")
        with open(path, "wb") as f:
            f.write(exe)
        proc = subprocess.run([path], capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    # -- 3. 200 CPython subset programs (10 categories x 20 cases) ---------
    def _assert_subset_slice(self, start: int, stop: int) -> None:
        all_cases = build_cases()[start:stop]
        self.assertEqual(len(all_cases), 20)
        passed, errors = run_subset(all_cases)
        self.assertEqual(errors, [], "\n".join(errors))
        self.assertEqual(passed, 20)

    def test_cpython_subset_01_int(self):
        self._assert_subset_slice(0, 20)

    def test_cpython_subset_02_bigint(self):
        self._assert_subset_slice(20, 40)

    def test_cpython_subset_03_float(self):
        self._assert_subset_slice(40, 60)

    def test_cpython_subset_04_bool_compare(self):
        self._assert_subset_slice(60, 80)

    def test_cpython_subset_05_controlflow(self):
        self._assert_subset_slice(80, 100)

    def test_cpython_subset_06_functions_closures(self):
        self._assert_subset_slice(100, 120)

    def test_cpython_subset_07_str_bytes(self):
        self._assert_subset_slice(120, 140)

    def test_cpython_subset_08_list_tuple_set(self):
        self._assert_subset_slice(140, 160)

    def test_cpython_subset_09_dict_exceptions(self):
        self._assert_subset_slice(160, 180)

    def test_cpython_subset_10_funcobj(self):
        self._assert_subset_slice(180, 200)

    # -- 4. Performance gate: list / dict / str >= 2x CPython --------------
    def _assert_bench_2x(self, name: str, src: str) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        exe_path = os.path.join(TMPDIR, f"bench_{name}.exe")
        py_path = os.path.join(TMPDIR, f"bench_{name}.py")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write(src)
        res = compile_source(src, py_path, boxed=True)
        with open(exe_path, "wb") as f:
            f.write(res.exe)
        nat = subprocess.run([exe_path], capture_output=True, timeout=60)
        py = subprocess.run([sys.executable, py_path], capture_output=True,
                            timeout=60, env=env)
        self.assertEqual(nat.returncode, 0, nat.stderr.decode("utf-8", "replace"))
        self.assertEqual(nat.stdout, py.stdout)
        nat_t = _best_time(lambda: subprocess.run([exe_path], capture_output=True, timeout=60), 3)
        py_t = _best_time(lambda: subprocess.run([sys.executable, py_path],
                                                 capture_output=True, timeout=60, env=env), 3)
        speedup = py_t / max(nat_t, 1e-6)
        self.assertGreaterEqual(
            speedup, 2.0,
            f"{name} benchmark speedup {speedup:.2f}x < 2.0x "
            f"(vela={nat_t * 1000:.1f}ms, cpython={py_t * 1000:.1f}ms)",
        )

    def test_perf_list_ge_2x_cpython(self):
        src = (
            "xs = [1, 2, 3, 4, 5, 6, 7, 8]\n"
            "total = 0\n"
            "for i in range(120000):\n"
            "    idx = i % 8\n"
            "    v = xs[idx]\n"
            "    xs[idx] = v + 1\n"
            "    total = total + v\n"
            "print(total, len(xs), xs[0], xs[7])\n"
        )
        self._assert_bench_2x("list", src)

    def test_perf_dict_ge_2x_cpython(self):
        src = (
            'd = {"a": 1, "b": 2, "c": 3, "d": 4}\n'
            "total = 0\n"
            "for i in range(100000):\n"
            '    total = total + d["a"] + d["b"] + d["c"] + d["d"]\n'
            '    d["a"] = d["a"] + 1\n'
            'print(total, len(d), d["a"], "c" in d)\n'
        )
        self._assert_bench_2x("dict", src)

    def test_perf_str_ge_2x_cpython(self):
        src = (
            's = "hello_vela_runtime_string"\n'
            "total = 0\n"
            "for i in range(150000):\n"
            '    if s.startswith("hello") and s.endswith("string"):\n'
            '        total = total + len(s) + s.find("vela")\n'
            "print(total, s[6:10])\n"
        )
        self._assert_bench_2x("str", src)


if __name__ == "__main__":
    unittest.main()
