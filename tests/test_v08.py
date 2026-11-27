"""v0.8 acceptance tests: observable refcount/__del__/weakref/gc alignment,
multi-level `raise ... from` exception chains, `traceback` module,
`ExceptionGroup` + multi-handler `except*`, CPython suite exemption classification,
and 10,000-program random AST differential fuzzing gate.
"""
from __future__ import annotations

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

from fuzz_10k import TOTAL_PROGRAMS, _run_batch, verify_10k_diversity  # noqa: E402
from vela.driver import compile_source  # noqa: E402


def _build_and_run(name: str, src: str) -> subprocess.CompletedProcess:
    src_path = os.path.join(TMPDIR, f"v08_{name}.vl")
    exe_path = os.path.join(TMPDIR, f"v08_{name}.exe")
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(src)
    res = compile_source(src, src_path, boxed=True)
    with open(exe_path, "wb") as f:
        f.write(res.exe)
    return subprocess.run([exe_path], capture_output=True, timeout=30)


class V08ConformanceTests(unittest.TestCase):
    def test_class_del_finalizer_and_gc_collect(self):
        src = (
            "import gc\n"
            "DEL_LOG = 0\n"
            "class Tracked:\n"
            "    def __init__(self, tag: int):\n"
            "        self.tag = tag\n"
            "    def __del__(self):\n"
            "        global DEL_LOG\n"
            "        DEL_LOG = DEL_LOG * 10 + self.tag\n"
            "t = Tracked(7)\n"
            "gc.collect()\n"
            "before_del = DEL_LOG\n"
            "del t\n"
            "gc.collect()\n"
            "after_del = DEL_LOG\n"
            "gc.collect()\n"
            "after_second_gc = DEL_LOG\n"
            "print(before_del, after_del, after_second_gc)\n"
        )
        p = _build_and_run("class_del", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertEqual(p.stdout.strip(), b"0 7 7")

    def test_cycle_del_and_weakref_lifecycle_alignment(self):
        src = (
            "import gc\n"
            "import weakref\n"
            "FIN_COUNT = 0\n"
            "class Node:\n"
            "    def __init__(self, v: int):\n"
            "        self.v = v\n"
            "        self.peer = 0\n"
            "    def __del__(self):\n"
            "        global FIN_COUNT\n"
            "        FIN_COUNT = FIN_COUNT + 1\n"
            "a = Node(1)\n"
            "b = Node(2)\n"
            "a.peer = b\n"
            "b.peer = a\n"
            "ra = weakref.ref(a)\n"
            "rb = weakref.ref(b)\n"
            "gc.collect()\n"
            "alive_before = ra.alive() and rb.alive()\n"
            "cnt_before = FIN_COUNT\n"
            "del a\n"
            "del b\n"
            "gc.collect()\n"
            "alive_after = ra.alive() or rb.alive()\n"
            "val_after = ra()\n"
            "cnt_after = FIN_COUNT\n"
            "print(alive_before, cnt_before, alive_after, val_after, cnt_after)\n"
        )
        p = _build_and_run("cycle_weakref", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertEqual(p.stdout.strip(), b"True 0 False 0 2")

    def test_weakref_finalize_callback(self):
        src = (
            "import gc\n"
            "import weakref\n"
            "CB_RUNS = 0\n"
            "def on_collect(obj):\n"
            "    global CB_RUNS\n"
            "    CB_RUNS = CB_RUNS + 1\n"
            "class Payload:\n"
            "    def __init__(self, x: int):\n"
            "        self.x = x\n"
            "p = Payload(99)\n"
            'weakref.finalize(p, "on_collect")\n'
            "gc.collect()\n"
            "c0 = CB_RUNS\n"
            "del p\n"
            "gc.collect()\n"
            "c1 = CB_RUNS\n"
            "gc.collect()\n"
            "c2 = CB_RUNS\n"
            "print(c0, c1, c2)\n"
        )
        p = _build_and_run("weakref_fin", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertEqual(p.stdout.strip(), b"0 1 1")

    def test_multilevel_exception_chain_raise_from(self):
        src = (
            "def level0():\n"
            '    raise ValueError("root_cause_0")\n'
            "def level1():\n"
            "    try:\n"
            "        level0()\n"
            "    except ValueError as e0:\n"
            '        raise ValueError("mid_cause_1") from e0\n'
            "def level2():\n"
            "    try:\n"
            "        level1()\n"
            "    except ValueError as e1:\n"
            '        raise TypeError("top_error_2") from e1\n'
            "level2()\n"
        )
        p = _build_and_run("multilevel_cause", src)
        self.assertEqual(p.returncode, 1)
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("top_error_2", out)
        self.assertIn("caused by mid_cause_1", out)
        self.assertIn("caused by root_cause_0", out)
        self.assertLess(out.index("caused by mid_cause_1"), out.index("caused by root_cause_0"))

    def test_traceback_module(self):
        src = (
            "import traceback\n"
            "def fail_inner():\n"
            '    raise ValueError("tb_detail_msg")\n'
            "msg = \"\"\n"
            "try:\n"
            "    fail_inner()\n"
            "except ValueError:\n"
            "    msg = traceback.format_exc()\n"
            "    traceback.print_exc()\n"
            "print(msg)\n"
        )
        p = _build_and_run("tb_mod", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace")
        self.assertIn("tb_detail_msg", out)
        self.assertIn("in __main__", out)

    def test_exception_group_and_multi_except_star(self):
        src = (
            "hits = 0\n"
            "fin = 0\n"
            "try:\n"
            '    raise ExceptionGroup("eg", [ValueError("v"), TypeError("t")])\n'
            "except* ValueError:\n"
            "    hits = hits + 10\n"
            "except* KeyError:\n"
            "    hits = hits + 100\n"
            "except* TypeError:\n"
            "    hits = hits + 2\n"
            "finally:\n"
            "    fin = 1\n"
            "print(hits, fin)\n"
        )
        p = _build_and_run("eg_trystar", src)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertEqual(p.stdout.strip(), b"12 1")

    def test_cpython_exemptions_doc_exists(self):
        ex_path = os.path.join(ROOT, "docs", "CPYTHON-EXEMPTIONS.md")
        self.assertTrue(os.path.isfile(ex_path))
        with open(ex_path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("100.00%", text)
        self.assertIn("c_extension_or_non_a3_stdlib", text)

    def test_fuzz_10k_diversity_and_sample_execution(self):
        unique_progs, unique_skels = verify_10k_diversity(TOTAL_PROGRAMS)
        self.assertEqual(unique_progs, TOTAL_PROGRAMS)
        self.assertGreaterEqual(unique_skels, 2000)
        # Run 2 batches (50 random AST programs) inside quick unittest; full 10k runs via tests/fuzz_10k.py
        for b in (0, 199):
            _, cnt, err = _run_batch((b, TMPDIR))
            self.assertIsNone(err, err)
            self.assertEqual(cnt, 50)


if __name__ == "__main__":
    unittest.main()
