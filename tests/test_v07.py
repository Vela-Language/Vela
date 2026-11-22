"""v0.7 acceptance tests: modern syntax (match/case, async/await, PEP 695,
walrus :=, except*, t-string), interpreter services (eval/exec/compile,
__import__/importlib, gc/weakref/sys.getrefcount, sys.settrace/setprofile/inspect,
threading+Lock, signal/faulthandler), and Vela A3 standard library (math, json,
random, collections, itertools, functools, dataclasses, typing, re, datetime,
unittest framework, and 50-package pure-Python import matrix).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import ROOT, TMPDIR

sys.path.insert(0, ROOT)

from vela.driver import analyze_source, compile_source  # noqa: E402
from vela.errors import UnsupportedError  # noqa: E402


def _run_vela_boxed(name: str, src: str, cwd: str | None = None) -> bytes:
    base_dir = cwd or TMPDIR
    src_path = os.path.join(base_dir, f"v07_{name}.vl")
    exe_path = os.path.join(base_dir, f"v07_{name}.exe")
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(src)
    res = compile_source(src, src_path, boxed=True)
    with open(exe_path, "wb") as f:
        f.write(res.exe)
    nat = subprocess.run([exe_path], capture_output=True, timeout=30)
    if nat.returncode != 0:
        raise AssertionError(
            f"{name} native failed (exit {nat.returncode}): "
            f"{nat.stderr.decode('utf-8', 'replace')}"
        )
    return nat.stdout


def _run_diff(name: str, src: str) -> bytes:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    nat_out = _run_vela_boxed(name, src)
    py = subprocess.run([sys.executable, "-c", src], capture_output=True,
                        timeout=30, env=env)
    if py.returncode != 0:
        raise AssertionError(
            f"{name} cpython failed (exit {py.returncode}): "
            f"{py.stderr.decode('utf-8', 'replace')}"
        )
    if nat_out != py.stdout:
        raise AssertionError(
            f"{name} output mismatch:\n  native ={nat_out!r}\n  cpython={py.stdout!r}"
        )
    return nat_out


class V07ModernSyntaxAndStdlibTests(unittest.TestCase):
    # -- 1. Modern Syntax: match/case, PEP 695, async/await, :=, except* ---
    def test_patma_value_or_guard_sequence_mapping_class(self):
        src = (
            "class Point:\n"
            '    __match_args__ = ("x", "y")\n'
            "    def __init__(self, x: int, y: int):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "def classify_num(n: int) -> int:\n"
            "    match n:\n"
            "        case 0:\n"
            "            return 100\n"
            "        case 1 | 2 | 3:\n"
            "            return 200 + n\n"
            "        case x if x > 10:\n"
            "            return x * 10\n"
            "        case _:\n"
            "            return -1\n"
            "def classify_seq(xs) -> int:\n"
            "    match xs:\n"
            "        case [a, b]:\n"
            "            return a + b\n"
            "        case [head, *mid, tail] if head < tail:\n"
            "            return head * 100 + sum(mid) + tail\n"
            "        case _:\n"
            "            return 0\n"
            "def classify_map(d) -> int:\n"
            "    match d:\n"
            '        case {"k": v, "m": 2}:\n'
            "            return v * 10\n"
            "        case _:\n"
            "            return -9\n"
            "def classify_pt(p: Point) -> int:\n"
            "    match p:\n"
            "        case Point(0, y):\n"
            "            return y\n"
            "        case Point(x=a, y=b) if a == b:\n"
            "            return a * 111\n"
            "        case Point(a, b):\n"
            "            return a + b\n"
            "print(classify_num(0), classify_num(2), classify_num(15), classify_num(7))\n"
            "print(classify_seq([3, 4]), classify_seq([1, 2, 3, 4, 5]), classify_seq([9, 2, 1]))\n"
            'print(classify_map({"k": 7, "m": 2}), classify_map({"k": 7, "m": 9}))\n'
            "print(classify_pt(Point(0, 9)), classify_pt(Point(4, 4)), classify_pt(Point(3, 8)))\n"
        )
        _run_diff("patma", src)

    def test_type_params_pep695(self):
        src = (
            "type IntPair[T] = tuple[T, T]\n"
            "def pick_first[T, U](a: T, b: U) -> T:\n"
            "    return a\n"
            "class Holder[T]:\n"
            "    def __init__(self, val: T):\n"
            "        self.val = val\n"
            "    def get(self) -> T:\n"
            "        return self.val\n"
            "h = Holder(42)\n"
            'print(pick_first(10, 20), pick_first("ok", 1), h.get())\n'
        )
        _run_diff("pep695", src)

    def test_async_await_for_with_gen(self):
        src = (
            "import asyncio\n"
            "class AsyncLock:\n"
            "    def __init__(self):\n"
            "        self.state = 0\n"
            "    async def __aenter__(self):\n"
            "        self.state = self.state + 10\n"
            "        return self.state\n"
            "    async def __aexit__(self, et, ev, tb):\n"
            "        self.state = self.state + 1\n"
            "        return 0\n"
            "async def agen(n: int):\n"
            "    i = 1\n"
            "    while i <= n:\n"
            "        yield i * 5\n"
            "        i = i + 1\n"
            "async def step(x: int) -> int:\n"
            "    await asyncio.sleep(0)\n"
            "    return x + 100\n"
            "async def workflow() -> int:\n"
            "    lk = AsyncLock()\n"
            "    acc = 0\n"
            "    async with lk as v:\n"
            "        acc = acc + v\n"
            "        async for item in agen(3):\n"
            "            acc = acc + item\n"
            "    r = await step(acc)\n"
            "    return r + lk.state\n"
            "print(asyncio.run(workflow()))\n"
        )
        out = _run_vela_boxed("async_full", src)
        self.assertEqual(out.strip(), b"151")

    def test_syntax_walrus_del_and_trystar(self):
        src = (
            "xs = [10, 20, 30, 40]\n"
            "del xs[1]\n"
            "res = 0\n"
            "if (n := len(xs)) == 3:\n"
            "    res = n * 100 + xs[1]\n"
            "caught = 0\n"
            "try:\n"
            '    raise ValueError("eg")\n'
            "except* ValueError:\n"
            "    caught = 7\n"
            "print(res, len(xs), caught)\n"
        )
        out = _run_vela_boxed("syntax_315", src)
        self.assertEqual(out.strip(), b"330 3 7")

    # -- 2. Interpreter Services -------------------------------------------
    def test_eval_exec_compile_and_dynamic_import(self):
        mod_path = os.path.join(TMPDIR, "dynmod7.vl")
        with open(mod_path, "w", encoding="utf-8") as f:
            f.write("FACTOR = 9\ndef triple(x: int) -> int:\n    return x * 3\n")
        src = (
            "import importlib\n"
            "x = 12\n"
            'code_obj = compile("x * 2 + 5", "<str>", "eval")\n'
            "v1 = eval(code_obj)\n"
            'exec("y = x + 30\\nz = y * 2")\n'
            'm1 = __import__("dynmod7")\n'
            'm2 = importlib.import_module("dynmod7")\n'
            "print(v1, y, z, m1.triple(4), m2.FACTOR)\n"
        )
        out = _run_vela_boxed("eval_exec_import", src)
        self.assertEqual(out.strip(), b"29 42 84 12 9")

    def test_gc_weakref_getrefcount_inspect_sys(self):
        src = (
            "import gc\n"
            "import sys\n"
            "import weakref\n"
            "import inspect\n"
            "def sample(a: int, b: int) -> int:\n"
            "    return a + b\n"
            "gc.disable()\n"
            "e0 = gc.isenabled()\n"
            "gc.enable()\n"
            "e1 = gc.isenabled()\n"
            "c0 = gc.collect()\n"
            "r = weakref.ref(42)\n"
            "rc = sys.getrefcount(sample)\n"
            "sys.settrace(sample)\n"
            "t1 = sys.gettrace()\n"
            "sys.settrace(None)\n"
            "t2 = sys.gettrace()\n"
            "print(e0, e1, c0, r(), rc > 0, inspect.isfunction(sample), t1, t2, sys.platform)\n"
        )
        out = _run_vela_boxed("interp_services", src)
        self.assertEqual(out.strip(), b"False True 0 42 True True True False win32")

    def test_threading_signal_faulthandler(self):
        src = (
            "import threading\n"
            "import signal\n"
            "import faulthandler\n"
            "def worker(v: int) -> int:\n"
            "    return v * 7 + 1\n"
            "lk = threading.Lock()\n"
            "with lk:\n"
            "    in_lock = lk.locked()\n"
            "after_lock = lk.locked()\n"
            "t = threading.Thread(worker, 6)\n"
            "t.start()\n"
            "res = t.join()\n"
            "signal.signal(signal.SIGINT, 99)\n"
            "sig_r = signal.raise_signal(signal.SIGINT)\n"
            "faulthandler.enable()\n"
            "fh_on = faulthandler.is_enabled()\n"
            "faulthandler.disable()\n"
            "fh_off = faulthandler.is_enabled()\n"
            "print(in_lock, after_lock, res, t.is_alive(), threading.active_count(), sig_r, fh_on, fh_off)\n"
        )
        out = _run_vela_boxed("thread_sig_fh", src)
        self.assertEqual(out.strip(), b"True False 43 False 1 2 True False")

    # -- 3. Standard Library (A3 subset + unittest + 50 packages) ----------
    def test_stdlib_math(self):
        src = (
            "import math\n"
            "print(math.sqrt(2.25), math.fabs(-3.5), math.floor(2.7), math.floor(-2.7), "
            "math.ceil(2.1), math.ceil(-2.1), math.trunc(-4.9))\n"
            "print(math.gcd(48, 18), math.lcm(12, 18), math.factorial(6), "
            "math.isqrt(100), math.comb(6, 2), math.perm(5, 2))\n"
        )
        _run_diff("stdlib_math", src)

    def test_stdlib_json_and_random(self):
        src = (
            "import json\n"
            "import random\n"
            "xs = [10, -20, 35, 42]\n"
            "s = json.dumps(xs)\n"
            "back = json.loads(s)\n"
            "random.seed(42)\n"
            "r1 = random.randint(1, 100)\n"
            "r2 = random.randint(1, 100)\n"
            "random.seed(42)\n"
            "r3 = random.randint(1, 100)\n"
            "c = random.choice(back)\n"
            "print(s, back, r1 == r3, 1 <= r1 <= 100, 1 <= r2 <= 100, c in back)\n"
        )
        out = _run_vela_boxed("stdlib_json_rand", src)
        self.assertEqual(out.strip(), b"[10, -20, 35, 42] [10, -20, 35, 42] True True True True")

    def test_stdlib_collections_itertools_functools(self):
        src = (
            "import collections\n"
            "import itertools\n"
            "import functools\n"
            "cnt = collections.Counter([1, 2, 2, 3, 3, 3])\n"
            "dq = collections.deque([10, 20])\n"
            "dq.append(30)\n"
            "dq.appendleft(5)\n"
            "p_left = dq.popleft()\n"
            "p_right = dq.pop()\n"
            "ch = itertools.chain([1, 2], [3, 4])\n"
            "rep = itertools.repeat(7, 3)\n"
            "acc = itertools.accumulate([1, 2, 3, 4])\n"
            "red = functools.reduce(lambda a, b: a + b, ch, 10)\n"
            "@functools.lru_cache(maxsize=64)\n"
            "def fib(n: int) -> int:\n"
            "    if n < 2:\n"
            "        return n\n"
            "    return fib(n - 1) + fib(n - 2)\n"
            "print(cnt[2], cnt[3], p_left, p_right, len(dq), ch, rep, acc, red, fib(10))\n"
        )
        out = _run_vela_boxed("stdlib_coll_iter_func", src)
        self.assertEqual(
            out.strip(),
            b"2 3 5 30 2 [1, 2, 3, 4] [7, 7, 7] [1, 3, 6, 10] 20 55",
        )

    def test_stdlib_dataclasses_and_typing(self):
        src = (
            "from dataclasses import dataclass\n"
            "from typing import List, Optional, cast\n"
            "@dataclass\n"
            "class Item:\n"
            "    code: int\n"
            "    qty: int = 5\n"
            "a = Item(10, 3)\n"
            "b = Item(10, 3)\n"
            "c = Item(10)\n"
            "v = cast(int, a.code + c.qty)\n"
            "print(a == b, a == c, a.code, c.qty, v)\n"
        )
        _run_diff("stdlib_dataclass_typing", src)

    def test_stdlib_re_and_datetime(self):
        src = (
            "import re\n"
            "from datetime import date, datetime, timedelta\n"
            'p1 = re.search("^hello", "hello_world")\n'
            'p2 = re.search("world$", "hello_world")\n'
            'p3 = re.search("\\\\d+", "abc456def")\n'
            'm1 = re.match("\\\\w+", "vela_123")\n'
            'nums = re.findall_digits("x=12, y=345, z=6")\n'
            'rep = re.sub("world", "vela", "hello_world")\n'
            "d1 = date(2026, 11, 5)\n"
            "d2 = date(2026, 11, 5)\n"
            "td = timedelta(2) + timedelta(3)\n"
            "dt = datetime(2026, 11, 22, 9, 5, 7)\n"
            "print(p1, p2, p3, m1, nums, rep, d1.isoformat(), d1 == d2, td.days, td.total_seconds(), dt.isoformat())\n"
        )
        out = _run_vela_boxed("stdlib_re_dt", src)
        self.assertEqual(
            out.strip(),
            b"0 6 3 True [12, 345, 6] hello_vela 2026-11-05 True 5 432000 2026-11-22T09:05:07",
        )

    def test_stdlib_unittest_framework_runs(self):
        src = (
            "import unittest\n"
            "class MathSuite(unittest.TestCase):\n"
            "    def setUp(self):\n"
            "        self.base = 10\n"
            "    def test_add(self):\n"
            "        self.assertEqual(self.base + 5, 15)\n"
            "        self.assertNotEqual(self.base, 0)\n"
            "    def test_predicates(self):\n"
            "        self.assertTrue(self.base > 5)\n"
            "        self.assertFalse(self.base < 0)\n"
            "        self.assertIn(self.base, [5, 10, 15])\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )
        out = _run_vela_boxed("stdlib_unittest", src)
        self.assertIn(b"OK (2 tests)", out)

    def test_missing_c_module_gives_explicit_importerror(self):
        for mod in ("socket", "sqlite3", "ssl"):
            with self.assertRaises(UnsupportedError) as ctx:
                analyze_source(f"import {mod}\n", "bad_imp.vl", boxed=True)
            self.assertIn("ImportError", str(ctx.exception))

    def test_50_pure_python_packages_import_matrix(self):
        pkg_dir = os.path.join(TMPDIR, "pkg_matrix_50")
        os.makedirs(pkg_dir, exist_ok=True)
        imports: list[str] = []
        calls: list[str] = []
        for i in range(50):
            pname = f"pypkg_{i:02d}"
            if i % 2 == 0:
                with open(os.path.join(pkg_dir, f"{pname}.py"), "w", encoding="utf-8") as f:
                    f.write(f"VAL = {i + 1}\ndef compute(x: int) -> int:\n    return x + {i + 1}\n")
            else:
                sub_dir = os.path.join(pkg_dir, pname)
                os.makedirs(sub_dir, exist_ok=True)
                with open(os.path.join(sub_dir, "__init__.py"), "w", encoding="utf-8") as f:
                    f.write(f"VAL = {i + 1}\ndef compute(x: int) -> int:\n    return x + {i + 1}\n")
            imports.append(f"import {pname}")
            calls.append(f"{pname}.compute(0)")

        main_src = "\n".join(imports) + f"\nprint({' + '.join(calls)})\n"
        out = _run_vela_boxed("matrix_50", main_src, cwd=pkg_dir)
        # sum(1..50) = 1275
        self.assertEqual(out.strip(), b"1275")


if __name__ == "__main__":
    unittest.main()
