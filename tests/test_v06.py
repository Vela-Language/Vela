"""v0.6 acceptance tests: class & function semantics (classes, MRO, super,
descriptors, property, __slots__, dunder protocols, generators, comprehensions,
unpacking, slicing, f-strings, with, decorators, closures, lambda,
default/keyword/*args/**kwargs, and test_grammar / test_scope / test_funcattrs gates).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import ROOT, TMPDIR

sys.path.insert(0, ROOT)

from vela.driver import analyze_source, compile_source  # noqa: E402
from vela.errors import VelaError  # noqa: E402


def _run_diff(name: str, src: str) -> bytes:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    exe_path = os.path.join(TMPDIR, f"v06_{name}.exe")
    res = compile_source(src, f"{name}.vl", boxed=True)
    with open(exe_path, "wb") as f:
        f.write(res.exe)
    nat = subprocess.run([exe_path], capture_output=True, timeout=30)
    py = subprocess.run([sys.executable, "-c", src], capture_output=True,
                        timeout=30, env=env)
    if nat.returncode != 0:
        raise AssertionError(
            f"{name} native failed (exit {nat.returncode}): "
            f"{nat.stderr.decode('utf-8', 'replace')}"
        )
    if py.returncode != 0:
        raise AssertionError(
            f"{name} cpython failed (exit {py.returncode}): "
            f"{py.stderr.decode('utf-8', 'replace')}"
        )
    if nat.stdout != py.stdout:
        raise AssertionError(
            f"{name} output mismatch:\n  native ={nat.stdout!r}\n  cpython={py.stdout!r}"
        )
    return nat.stdout


class V06ClassAndFunctionTests(unittest.TestCase):
    # -- 1. Function semantics: lambda, defaults/kwargs/*args/**kwargs, decorators, closures --
    def test_lambda_expressions(self):
        src = (
            "inc = lambda x: x + 1\n"
            "add = lambda a, b: a + b\n"
            "def apply2(f, x, y):\n"
            "    return f(x, y)\n"
            "print(inc(5), add(3, 4), apply2(lambda u, v: u * v + 1, 6, 7))\n"
        )
        _run_diff("lambda", src)

    def test_default_and_keyword_args(self):
        src = (
            "def calc(a, b=10, c=100):\n"
            "    return a + b * 2 + c * 3\n"
            "print(calc(1), calc(1, 20), calc(1, 20, 30))\n"
            "print(calc(1, c=5), calc(b=3, a=2, c=4), calc(a=7))\n"
        )
        _run_diff("defaults_kw", src)

    def test_varargs_and_kwargs(self):
        src = (
            "def sum_all(head, *rest):\n"
            "    s = head\n"
            "    for x in rest:\n"
            "        s = s + x\n"
            "    return s\n"
            "def cfg(base, **opts):\n"
            '    return base + opts.get("a", 0) * 10 + opts.get("b", 0)\n'
            "vals = [2, 3, 4]\n"
            "print(sum_all(1), sum_all(1, 2, 3, 4), sum_all(10, *vals))\n"
            "print(cfg(5, a=2, b=3), cfg(1, b=9))\n"
        )
        _run_diff("varargs_kwargs", src)

    def test_decorators_and_factories(self):
        src = (
            "def double(fn):\n"
            "    def wrap(x):\n"
            "        return fn(x) * 2\n"
            "    return wrap\n"
            "def plus_one(fn):\n"
            "    def wrap(x):\n"
            "        return fn(x) + 1\n"
            "    return wrap\n"
            "def scale(factor):\n"
            "    def dec(fn):\n"
            "        def wrap(x):\n"
            "            return fn(x) * factor\n"
            "        return wrap\n"
            "    return dec\n"
            "@double\n"
            "def f1(x):\n"
            "    return x + 3\n"
            "@plus_one\n"
            "@double\n"
            "def f2(x):\n"
            "    return x + 3\n"
            "@scale(5)\n"
            "def f3(x):\n"
            "    return x + 2\n"
            "print(f1(4), f2(4), f3(4))\n"
        )
        _run_diff("decorators", src)

    def test_closures_readonly_and_nonlocal(self):
        src = (
            "def make_adder(bias):\n"
            "    def adder(x):\n"
            "        return x + bias\n"
            "    return adder\n"
            "def make_counter(start):\n"
            "    count = start\n"
            "    def step(delta):\n"
            "        nonlocal count\n"
            "        count = count + delta\n"
            "        return count\n"
            "    return step\n"
            "add10 = make_adder(10)\n"
            "add25 = make_adder(25)\n"
            "c1 = make_counter(0)\n"
            "c2 = make_counter(100)\n"
            "print(add10(7), add25(7))\n"
            "print(c1(1), c1(2), c2(5), c1(3), c2(10))\n"
        )
        _run_diff("closures", src)

    # -- 2. f-strings, slicing, unpacking, comprehensions, with ------------
    def test_fstrings(self):
        src = (
            'name = "Vela"\n'
            "ver = 6\n"
            "ok = True\n"
            "pi = 3.5\n"
            'msg = f"lang={name} v0.{ver} ok={ok} pi={pi} sum={ver + 4}"\n'
            "print(msg)\n"
        )
        _run_diff("fstrings", src)

    def test_slicing_str_list_tuple(self):
        src = (
            's = "abcdefgh"\n'
            "print(s[-1], s[-3], s[1:6:2], s[::-1], s[-5:-1])\n"
            "xs = [10, 20, 30, 40, 50, 60]\n"
            "print(xs[-1], xs[-2], xs[1:5:2], xs[::-1], xs[-4:-1])\n"
        )
        _run_diff("slicing", src)

    def test_unpacking_extended(self):
        src = (
            "a = 10\n"
            "b = 20\n"
            "a, b = b, a\n"
            "x, (y, z) = 1, (2, 3)\n"
            "nums = [10, 20, 30, 40, 50]\n"
            "h, *mid, t = nums\n"
            "p1, p2, *tail = nums\n"
            "*init, last = nums\n"
            "print(a, b, x, y, z)\n"
            "print(h, mid, t)\n"
            "print(p1, p2, tail)\n"
            "print(init, last)\n"
        )
        _run_diff("unpacking", src)

    def test_comprehensions(self):
        src = (
            "sq = [x * x for x in range(8) if x % 2 == 0]\n"
            "pairs = [a * 10 + b for a in range(1, 3) for b in range(1, 4) if (a + b) % 2 == 1]\n"
            "st = {x % 4 for x in range(10)}\n"
            "dc = {x: x * 100 for x in range(1, 5) if x != 3}\n"
            "tot = sum(x * 3 for x in range(6))\n"
            "gen_l = list(x + 10 for x in [1, 2, 3])\n"
            "print(sq, pairs, len(st), dc[1], dc[4], 3 in dc, tot, gen_l)\n"
        )
        _run_diff("comprehensions", src)

    def test_with_context_manager(self):
        src = (
            "LOG = 0\n"
            "class Resource:\n"
            "    def __init__(self, tag):\n"
            "        self.tag = tag\n"
            "    def __enter__(self):\n"
            "        global LOG\n"
            "        LOG = LOG * 10 + self.tag\n"
            "        return self.tag * 100\n"
            "    def __exit__(self, et, ev, tb):\n"
            "        global LOG\n"
            "        LOG = LOG * 10 + 9\n"
            "        return 0\n"
            "with Resource(2) as val:\n"
            "    LOG = LOG * 10 + (val // 100)\n"
            "try:\n"
            "    with Resource(5) as v2:\n"
            '        raise ValueError("fail")\n'
            "except ValueError:\n"
            "    LOG = LOG * 10 + 7\n"
            "print(val, LOG)\n"
        )
        _run_diff("with_ctx", src)

    # -- 3. Classes, MRO, super(), property, descriptors, __slots__ --------
    def test_class_inheritance_and_isinstance(self):
        src = (
            "class Animal:\n"
            "    KIND = 1\n"
            "    def __init__(self, base):\n"
            "        self.base = base\n"
            "    def score(self):\n"
            "        return self.base + 10\n"
            "class Dog(Animal):\n"
            "    def __init__(self, base, bonus):\n"
            "        super().__init__(base)\n"
            "        self.bonus = bonus\n"
            "    def score(self):\n"
            "        return super().score() + self.bonus\n"
            "d = Dog(5, 7)\n"
            "a = Animal(3)\n"
            "print(d.score(), a.score(), d.KIND, isinstance(d, Dog), isinstance(d, Animal), isinstance(a, Dog))\n"
        )
        _run_diff("class_inherit", src)

    def test_diamond_mro_and_cooperative_super(self):
        src = (
            "class A:\n"
            "    def trace(self):\n"
            "        return 1\n"
            "class B(A):\n"
            "    def trace(self):\n"
            "        return super().trace() * 10 + 2\n"
            "class C(A):\n"
            "    def trace(self):\n"
            "        return super().trace() * 10 + 3\n"
            "class D(B, C):\n"
            "    def trace(self):\n"
            "        return super().trace() * 10 + 4\n"
            "d = D()\n"
            "print(d.trace())\n"
        )
        _run_diff("diamond_mro", src)

    def test_property_and_descriptors(self):
        src = (
            "class ClampDesc:\n"
            "    def __init__(self, limit):\n"
            "        self.limit = limit\n"
            "    def __get__(self, inst, owner):\n"
            "        return inst._raw\n"
            "    def __set__(self, inst, val):\n"
            "        if val > self.limit:\n"
            "            inst._raw = self.limit\n"
            "        else:\n"
            "            inst._raw = val\n"
            "class Box:\n"
            "    capped = ClampDesc(50)\n"
            "    def __init__(self, v):\n"
            "        self._v = v\n"
            "        self._raw = 0\n"
            "    @property\n"
            "    def value(self):\n"
            "        return self._v * 2\n"
            "    @value.setter\n"
            "    def value(self, nv):\n"
            "        self._v = nv + 1\n"
            "b = Box(10)\n"
            "v0 = b.value\n"
            "b.value = 20\n"
            "v1 = b.value\n"
            "b.capped = 30\n"
            "c0 = b.capped\n"
            "b.capped = 99\n"
            "c1 = b.capped\n"
            "print(v0, v1, c0, c1)\n"
        )
        _run_diff("prop_desc", src)

    def test_slots_enforcement(self):
        src_ok = (
            "class Point:\n"
            '    __slots__ = ("x", "y")\n'
            "    def __init__(self, x, y):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "p = Point(3, 4)\n"
            "p.x = 10\n"
            "print(p.x + p.y)\n"
        )
        _run_diff("slots_ok", src_ok)

        src_bad = (
            "class Point:\n"
            '    __slots__ = ("x", "y")\n'
            "    def __init__(self, x, y):\n"
            "        self.x = x\n"
            "        self.y = y\n"
            "p = Point(3, 4)\n"
            "p.z = 99\n"
        )
        with self.assertRaises(VelaError) as ctx:
            analyze_source(src_bad, "slots_bad.vl", boxed=True)
        self.assertIn("__slots__", str(ctx.exception))

    # -- 4. Dunder protocols -----------------------------------------------
    def test_dunder_arithmetic_compare_str(self):
        src = (
            "class Num:\n"
            "    def __init__(self, v):\n"
            "        self.v = v\n"
            "    def __add__(self, other):\n"
            "        return Num(self.v + other.v)\n"
            "    def __sub__(self, other):\n"
            "        return Num(self.v - other.v)\n"
            "    def __mul__(self, other):\n"
            "        return Num(self.v * other.v)\n"
            "    def __floordiv__(self, other):\n"
            "        return Num(self.v // other.v)\n"
            "    def __mod__(self, other):\n"
            "        return Num(self.v % other.v)\n"
            "    def __neg__(self):\n"
            "        return Num(-self.v)\n"
            "    def __eq__(self, other):\n"
            "        return self.v == other.v\n"
            "    def __lt__(self, other):\n"
            "        return self.v < other.v\n"
            "    def __le__(self, other):\n"
            "        return self.v <= other.v\n"
            "    def __gt__(self, other):\n"
            "        return self.v > other.v\n"
            "    def __ge__(self, other):\n"
            "        return self.v >= other.v\n"
            "    def __str__(self):\n"
            '        return f"Num({self.v})"\n'
            "a = Num(17)\n"
            "b = Num(5)\n"
            "c = a + b\n"
            "d = a - b\n"
            "e = a * b\n"
            "f = a // b\n"
            "g = a % b\n"
            "h = -b\n"
            "print(c, d, e, f, g, h)\n"
            "print(a == Num(17), a != b, b < a, b <= b, a > b, a >= a)\n"
        )
        _run_diff("dunder_arith", src)

    def test_dunder_container_callable_iterable(self):
        src = (
            "class Bag:\n"
            "    def __init__(self):\n"
            "        self.data = [10, 20, 30]\n"
            "    def __len__(self):\n"
            "        return len(self.data)\n"
            "    def __getitem__(self, idx):\n"
            "        return self.data[idx]\n"
            "    def __setitem__(self, idx, val):\n"
            "        self.data[idx] = val\n"
            "    def __contains__(self, item):\n"
            "        return item in self.data\n"
            "    def __call__(self, mul):\n"
            "        return (self.data[0] + self.data[1] + self.data[2]) * mul\n"
            "class RangeIter:\n"
            "    def __init__(self, start, stop):\n"
            "        self.cur = start\n"
            "        self.stop = stop\n"
            "    def __iter__(self):\n"
            "        return self\n"
            "    def __next__(self):\n"
            "        if self.cur >= self.stop:\n"
            '            raise StopIteration("end")\n'
            "        v = self.cur\n"
            "        self.cur = self.cur + 1\n"
            "        return v\n"
            "bag = Bag()\n"
            "bag[1] = 25\n"
            "acc = 0\n"
            "for x in RangeIter(1, 5):\n"
            "    acc = acc * 10 + x\n"
            "print(len(bag), bag[1], 25 in bag, 99 not in bag, bag(2), acc, list(RangeIter(3, 6)), sum(RangeIter(1, 5)))\n"
        )
        _run_diff("dunder_container", src)

    # -- 5. Generators: yield, yield from, send, throw, close --------------
    def test_generator_yield_and_yield_from(self):
        src = (
            "def countdown(n):\n"
            "    while n > 0:\n"
            "        yield n\n"
            "        n = n - 1\n"
            "def combined(a, b):\n"
            "    yield from countdown(a)\n"
            "    yield 99\n"
            "    yield from countdown(b)\n"
            "s = 0\n"
            "for v in countdown(4):\n"
            "    s = s * 10 + v\n"
            "print(s, list(combined(2, 3)), sum(combined(2, 3)))\n"
        )
        _run_diff("generator_basic", src)

    def test_generator_send_throw_close(self):
        src = (
            "CLEANED = 0\n"
            "def coro(seed):\n"
            "    global CLEANED\n"
            "    acc = seed\n"
            "    try:\n"
            "        while True:\n"
            "            dx = yield acc\n"
            "            acc = acc + dx\n"
            "    except ValueError:\n"
            "        yield -999\n"
            "    finally:\n"
            "        CLEANED = CLEANED + 1\n"
            "g = coro(10)\n"
            "v1 = next(g)\n"
            "v2 = g.send(5)\n"
            "v3 = g.send(12)\n"
            'v4 = g.throw(ValueError("stop"))\n'
            "g.close()\n"
            "g2 = coro(1)\n"
            "v5 = next(g2)\n"
            "g2.close()\n"
            "closed_err = 0\n"
            "try:\n"
            "    next(g2)\n"
            "except StopIteration:\n"
            "    closed_err = 1\n"
            "print(v1, v2, v3, v4, v5, CLEANED, closed_err)\n"
        )
        _run_diff("generator_coro", src)

    # -- 6. Milestone Gates: test_grammar, test_scope, test_funcattrs ------
    def test_gate_grammar(self):
        src = (
            "x, *mid, y = [1, 2, 3, 4, 5]\n"
            "comp = [i * 2 for i in mid if i % 2 == 1]\n"
            'fstr = f"x={x},y={y},c={len(comp)}"\n'
            "rev = [10, 20, 30, 40][::-1]\n"
            "print(fstr, comp, rev)\n"
        )
        _run_diff("gate_grammar", src)

    def test_gate_scope(self):
        src = (
            "G = 10\n"
            "def outer(a):\n"
            "    b = a + 1\n"
            "    def mid(c):\n"
            "        nonlocal b\n"
            "        global G\n"
            "        b = b + c\n"
            "        G = G + 1\n"
            "        return b + G\n"
            "    return mid\n"
            "f1 = outer(5)\n"
            "f2 = outer(20)\n"
            "print(f1(2), f1(3), f2(1), G)\n"
        )
        _run_diff("gate_scope", src)

    def test_gate_funcattrs(self):
        src = (
            "def tag(offset):\n"
            "    def dec(fn):\n"
            "        def wrap(x, y=10, *extra, **kw):\n"
            "            s = fn(x, y) + offset\n"
            "            for e in extra:\n"
            "                s = s + e\n"
            '            return s + kw.get("k", 0)\n'
            "        return wrap\n"
            "    return dec\n"
            "@tag(100)\n"
            "def compute(a, b):\n"
            "    return a * b\n"
            "print(compute(3), compute(3, 4), compute(3, 4, 5, 6, k=7))\n"
        )
        _run_diff("gate_funcattrs", src)


if __name__ == "__main__":
    unittest.main()
