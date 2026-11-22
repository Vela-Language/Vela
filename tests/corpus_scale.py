"""Generate an 800-case differential corpus and compare Vela with CPython.

Run: python tests/corpus_scale.py
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vela.driver import compile_source

TOTAL = 800


def program(i: int) -> str:
    k = i % 8
    if k == 0:
        return (f"a = {i} * 3 + 1\nb = {i} % 7 + 1\n"
                f"print(a + b, a - b, a * b, a // b, a % b, a ** 2)\n")
    if k == 1:
        return (f"x = {i}\nprint(x > 3 and x < 100, not (x == 5), x % 2 == 0)\n")
    if k == 2:
        return (f"s = 0\nfor j in range({i} % 10 + 1):\n    s = s + j\nprint(s)\n")
    if k == 3:
        return (f"def f(x):\n    return x * x + 2 * x + 1\nprint(f({i} % 9))\n")
    if k == 4:
        return ("def fib(n):\n    if n < 2:\n        return n\n"
                "    return fib(n - 1) + fib(n - 2)\n"
                f"print(fib({i} % 12))\n")
    if k == 5:
        return (f"a = {i} + 5\nb = ({i} % 11) + 1\n"
                "print((a & b), (a | b), (a ^ b), (a << 2), (a >> 1))\n")
    if k == 6:
        return (f"x = {i} - 250\n"
                "y = x if x >= 0 else -x\n"
                "print(abs(x), min(x, y), max(x, y))\n")
    return f's = "ab_{i % 17}"\nprint(len(s), s)\n'


def run_cpython(src: str) -> bytes:
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def run_vela(src: str, path: str) -> bytes:
    exe = compile_source(src, path).exe
    with open(path, "wb") as f:
        f.write(exe)
    p = subprocess.run([path], capture_output=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def _check_one(args: tuple[int, str]) -> tuple[int, str | None]:
    i, tmp = args
    src = program(i)
    exe = os.path.join(tmp, f"case_{i}.exe")
    try:
        want = run_cpython(src)
        got = run_vela(src, exe)
    except Exception as exc:  # noqa: BLE001
        return i, f"case {i}: error {exc}"
    finally:
        try:
            os.unlink(exe)
        except OSError:
            pass
    if want != got:
        return i, f"case {i}: mismatch\n  py={want!r}\n  vl={got!r}\n{src}"
    return i, None


def main() -> int:
    failures = 0
    tmp = tempfile.mkdtemp(prefix="vela_corpus_")
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _, err in pool.map(_check_one, ((i, tmp) for i in range(TOTAL))):
            if err is not None:
                failures += 1
                print(err)
    print(f"{TOTAL - failures} passed, {failures} failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
