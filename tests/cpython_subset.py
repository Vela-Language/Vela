"""200 CPython pure-Python subset programs for output + exit-code differential testing.

Run: python tests/cpython_subset.py
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vela.driver import compile_source  # noqa: E402


def build_cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []

    # 1. test_int (20 cases)
    for k in range(20):
        a = (k + 3) * 17 - 19
        b = (k % 7) + 2
        c = -((k % 5) + 1)
        src = (
            f"a = {a}\nb = {b}\nc = {c}\n"
            "print(a + b, a - b, a * b, a // b, a % b)\n"
            "print(a // c, a % c, (-a) // b, (-a) % b)\n"
            f"print(abs(a), min(a, b), max(a, c), (b ** {(k % 4) + 1}))\n"
        )
        cases.append((f"test_int_{k:02d}", src))

    # 2. test_bigint (20 cases)
    for k in range(20):
        big_a = (10 ** 22) + (k + 1) * 987654321012345
        big_b = (10 ** 11) + (k + 3) * 12345
        src = (
            f"a = {big_a}\nb = {big_b}\n"
            "print(a + b, a - b, a * b)\n"
            "print(a // b, a % b, a > b, a == b, len(str(a)))\n"
        )
        cases.append((f"test_bigint_{k:02d}", src))

    # 3. test_float (20 cases)
    for k in range(20):
        x = (k + 1) * 1.5
        y = float(1 << (k % 4)) * 0.5
        src = (
            f"x = {x}\ny = {y}\n"
            "print(x + y, x - y, x * y, x / y)\n"
            "print(-x, abs(-x), int(x), float(int(x)), x > y, x == y)\n"
        )
        cases.append((f"test_float_{k:02d}", src))

    # 4. test_bool_compare (20 cases)
    for k in range(20):
        v = k - 8
        src = (
            f"v = {v}\n"
            "c1 = -5 <= v < 5\n"
            "c2 = (v > 0 and v < 10) or (v == -3)\n"
            "r = 100 if c1 else -100\n"
            "print(c1, c2, not c1, bool(v), int(c2), r)\n"
        )
        cases.append((f"test_bool_compare_{k:02d}", src))

    # 5. test_controlflow (20 cases)
    for k in range(20):
        n = (k % 9) + 4
        step = (k % 3) + 1
        src = (
            "acc = 0\n"
            f"for i in range(1, {n * 2}, {step}):\n"
            "    if i % 5 == 0:\n"
            "        continue\n"
            "    acc = acc + i\n"
            "w = 0\n"
            f"j = {n}\n"
            "while j > 0:\n"
            "    w = w + j\n"
            "    if w > 30:\n"
            "        break\n"
            "    j = j - 1\n"
            "print(acc, w, j)\n"
        )
        cases.append((f"test_controlflow_{k:02d}", src))

    # 6. test_functions_closures (20 cases)
    for k in range(20):
        base = k + 2
        src = (
            f"G = {base}\n"
            "def bump(dx: int) -> int:\n"
            "    global G\n"
            "    G = G + dx\n"
            "    return G\n"
            "def outer(seed: int) -> int:\n"
            f"    acc = {base}\n"
            "    def step(v: int) -> int:\n"
            "        nonlocal acc\n"
            "        acc = acc + v\n"
            "        return acc\n"
            "    a = step(seed)\n"
            "    b = step(4)\n"
            "    return a + b\n"
            f"print(bump(5), outer({base}))\n"
        )
        cases.append((f"test_functions_closures_{k:02d}", src))

    # 7. test_str_bytes (20 cases)
    for k in range(20):
        token = f"VelaPy{k:02d}"
        src = (
            f's = "{token}_test"\n'
            'p = s + "!"\n'
            'print(len(p), p[0], p[2:8], p.upper(), p.lower())\n'
            'print(p.startswith("Vela"), p.endswith("!"), p.find("_test"), p == s)\n'
        )
        cases.append((f"test_str_bytes_{k:02d}", src))

    # 8. test_list_tuple_set (20 cases)
    for k in range(20):
        a, b, c = k + 1, k + 2, k + 3
        src = (
            f"xs = [{a}, {b}, {c}]\n"
            f"xs.append({a + 10})\n"
            f"xs.extend([{b + 10}, {a}])\n"
            "last = xs.pop()\n"
            "s = 0\n"
            "for item in xs:\n"
            "    s = s + item\n"
            f"t = ({a}, {b})\n"
            "u, v = t\n"
            f"st1 = {{{a}, {b}, {c}}}\n"
            f"st2 = {{{b}, {c}, {c + 1}}}\n"
            f"print(len(xs), s, last, xs.count({a}), xs.index({b}), {b} in xs, u + v, len(st1 | st2), len(st1 & st2))\n"
        )
        cases.append((f"test_list_tuple_set_{k:02d}", src))

    # 9. test_dict_exceptions (20 cases)
    for k in range(20):
        v1 = k + 10
        v2 = k * 2 + 5
        src = (
            f'd = {{"a": {v1}, "b": {v2}}}\n'
            f'd["c"] = {v1 + v2}\n'
            'd["a"] = d["a"] + 1\n'
            "caught = 0\n"
            "try:\n"
            f"    if {k % 2} == 0:\n"
            '        raise ValueError("even")\n'
            '    raise TypeError("odd")\n'
            "except ValueError:\n"
            "    caught = 1\n"
            "except TypeError:\n"
            "    caught = 2\n"
            "finally:\n"
            "    caught = caught + 10\n"
            'print(len(d), d["a"], d["c"], "b" in d, d.get("z", 99), caught)\n'
        )
        cases.append((f"test_dict_exceptions_{k:02d}", src))

    # 10. test_funcobj (20 cases)
    for k in range(20):
        x = k + 3
        src = (
            "def add5(a, b, c, d, e):\n"
            "    return a + b + c + d + e\n"
            "def mul_add5(a, b, c, d, e):\n"
            "    return a * b + c + d + e\n"
            "def call5(fn, a, b, c, d, e):\n"
            "    return fn(a, b, c, d, e)\n"
            "def choose(sel: int):\n"
            "    if sel == 0:\n"
            "        return add5\n"
            "    return mul_add5\n"
            f"f0 = choose({k % 2})\n"
            f"f1 = choose({(k + 1) % 2})\n"
            f"print(call5(f0, {x}, 2, 3, 4, 5), call5(f1, {x}, 2, 3, 4, 5), add5 is choose(0))\n"
        )
        cases.append((f"test_funcobj_{k:02d}", src))

    assert len(cases) == 200
    return cases


def _run_case(args: tuple[str, str, str]) -> tuple[str, str | None]:
    name, src, tmp = args
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    exe = os.path.join(tmp, f"{name}.exe")
    try:
        res = compile_source(src, f"{name}.vl", boxed=True)
        with open(exe, "wb") as f:
            f.write(res.exe)
        nat = subprocess.run([exe], capture_output=True, timeout=30)
        py = subprocess.run([sys.executable, "-c", src], capture_output=True,
                            timeout=30, env=env)
    except Exception as exc:  # noqa: BLE001
        return name, f"{name}: error: {exc}"
    finally:
        try:
            os.unlink(exe)
        except OSError:
            pass
    if nat.returncode != py.returncode:
        return name, (f"{name}: exit mismatch native={nat.returncode} "
                      f"cpython={py.returncode} stderr={nat.stderr!r}")
    if nat.stdout != py.stdout:
        return name, f"{name}: stdout mismatch\n  native={nat.stdout!r}\n  cpython={py.stdout!r}"
    return name, None


def run_subset(cases: list[tuple[str, str]] | None = None) -> tuple[int, list[str]]:
    cases = cases if cases is not None else build_cases()
    tmp = tempfile.mkdtemp(prefix="vela_cpysubset_")
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _, err in pool.map(_run_case, ((n, s, tmp) for n, s in cases)):
            if err is not None:
                errors.append(err)
    return len(cases) - len(errors), errors


def main() -> int:
    passed, errors = run_subset()
    for e in errors:
        print("FAIL", e)
    print(f"{passed} passed, {len(errors)} failed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
