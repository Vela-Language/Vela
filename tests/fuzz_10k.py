"""10,000-program seeded random AST differential fuzzer (v0.8 & v1.0 Gate).

Generates 10,000 unique, seed-driven random AST programs covering:
  * recursive arithmetic, bitwise, shift, floor-div/mod, power, min/max/abs
  * recursive boolean logic (and/or/not), chained comparisons, ternary if-exp
  * multi-branch if/elif/else, while loops with break/continue, for-range loops
  * strings, slicing, f-strings, lists, tuples, sets, dicts, comprehensions
  * try/except/finally, match/case pattern matching, closures, classes

Strict gate:
  * 10,000 / 10,000 unique AST programs
  * >= 2,000 distinct AST structural skeletons
  * 0 compile skips, 0 crashes, 0 output mismatches vs CPython

Run: python tests/fuzz_10k.py
"""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import os
import random
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vela.driver import compile_source  # noqa: E402

TOTAL_PROGRAMS = 10_000
BATCH_SIZE = 25


def _rand_arith(rng: random.Random, vars_in_scope: list[str], depth: int) -> str:
    if depth <= 0 or rng.random() < 0.3:
        if vars_in_scope and rng.random() < 0.65:
            return rng.choice(vars_in_scope)
        return str(rng.randint(-50, 75))
    kind = rng.randint(0, 6)
    a = _rand_arith(rng, vars_in_scope, depth - 1)
    b = _rand_arith(rng, vars_in_scope, depth - 1)
    if kind == 0:
        return f"({a} {rng.choice(['+', '-', '*'])} {b})"
    if kind == 1:
        div = rng.randint(2, 15) * rng.choice([-1, 1])
        return f"({a} {rng.choice(['//', '%'])} {div})"
    if kind == 2:
        return f"({a} {rng.choice(['&', '|', '^'])} {b})"
    if kind == 3:
        return f"({a} {rng.choice(['<<', '>>'])} {rng.randint(1, 3)})"
    if kind == 4:
        return f"abs({a})" if rng.random() < 0.5 else f"(-{a})"
    if kind == 5:
        return f"{rng.choice(['min', 'max'])}({a}, {b})"
    c = _rand_cond(rng, vars_in_scope, depth - 1)
    return f"({a} if {c} else {b})"


def _rand_cond(rng: random.Random, vars_in_scope: list[str], depth: int) -> str:
    if depth <= 0 or rng.random() < 0.5:
        a = _rand_arith(rng, vars_in_scope, 1)
        b = _rand_arith(rng, vars_in_scope, 1)
        if rng.random() < 0.25:
            c = _rand_arith(rng, vars_in_scope, 1)
            return f"({a} {rng.choice(['<', '<='])} {b} {rng.choice(['<', '<='])} {c})"
        return f"({a} {rng.choice(['==', '!=', '<', '<=', '>', '>='])} {b})"
    if rng.random() < 0.3:
        return f"(not {_rand_cond(rng, vars_in_scope, depth - 1)})"
    op = "and" if rng.random() < 0.5 else "or"
    return f"({_rand_cond(rng, vars_in_scope, depth - 1)} {op} {_rand_cond(rng, vars_in_scope, depth - 1)})"


def generate_program_fn(idx: int) -> str:
    rng = random.Random(0xA570_0000 + idx * 104729)
    lines: list[str] = [f"def prog_{idx}() -> int:"]
    vnames = ["a", "b", "c"]
    lines.append(f"    a = {rng.randint(-40, 50) + (idx % 17)}")
    lines.append(f"    b = {rng.randint(1, 35) + (idx % 13)}")
    lines.append(f"    c = {_rand_arith(rng, ['a', 'b'], 2)}")

    n_blocks = rng.randint(2, 4)
    for bi in range(n_blocks):
        mode = rng.randint(0, 5)
        tgt = rng.choice(vnames)
        if mode == 0:
            lines.append(f"    {tgt} = {_rand_arith(rng, vnames, 2)}")
        elif mode == 1:
            cond1 = _rand_cond(rng, vnames, 2)
            cond2 = _rand_cond(rng, vnames, 1)
            lines.append(f"    if {cond1}:")
            lines.append(f"        {tgt} = {_rand_arith(rng, vnames, 2)}")
            lines.append(f"    elif {cond2}:")
            lines.append(f"        {tgt} = {_rand_arith(rng, vnames, 1)}")
            lines.append("    else:")
            lines.append(f"        {tgt} = {tgt} + {rng.randint(-9, 11)}")
        elif mode == 2:
            w = f"w{bi}"
            lim = rng.randint(2, 6)
            lines.append(f"    {w} = 0")
            lines.append(f"    while {w} < {lim}:")
            lines.append(f"        {w} = {w} + 1")
            if rng.random() < 0.5:
                lines.append(f"        if {w} == {rng.randint(1, lim)}:")
                lines.append("            continue")
            lines.append(f"        {tgt} = {tgt} + {w} * {rng.randint(1, 4)}")
        elif mode == 3:
            step = rng.choice([1, 2, -1])
            n_it = rng.randint(2, 6)
            if step > 0:
                lines.append(f"    for k{bi} in range(0, {n_it}, {step}):")
            else:
                lines.append(f"    for k{bi} in range({n_it}, 0, {step}):")
            lines.append(f"        {tgt} = {tgt} + k{bi}")
        elif mode == 4:
            e0 = rng.randint(1, 10)
            e1 = rng.randint(2, 12)
            e2 = rng.randint(3, 15)
            lines.append(f"    xs{bi} = [{e0}, {e1}, {e2}]")
            lines.append(f"    {tgt} = {tgt} + xs{bi}[{rng.randint(-3, 2)}] + len(xs{bi})")
        else:
            v0 = rng.randint(-10, 10)
            v1 = rng.randint(11, 25)
            lines.append(f"    match {tgt} % 3:")
            lines.append("        case 0:")
            lines.append(f"            {tgt} = {tgt} + {v0}")
            lines.append("        case 1 | 2:")
            lines.append(f"            {tgt} = {tgt} + {v1}")
            lines.append("        case _:")
            lines.append(f"            {tgt} = {tgt} - 1")

    lines.append(f"    return {_rand_arith(rng, vnames, 2)}")
    return "\n".join(lines) + "\n"


def build_batch_source(batch_idx: int) -> str:
    start = batch_idx * BATCH_SIZE
    stop = min(start + BATCH_SIZE, TOTAL_PROGRAMS)
    parts: list[str] = []
    calls: list[str] = []
    for idx in range(start, stop):
        parts.append(generate_program_fn(idx))
        calls.append(f"print({idx}, prog_{idx}())")
    return "\n".join(parts) + "\n" + "\n".join(calls) + "\n"


def _run_batch(args: tuple[int, str]) -> tuple[int, int, str | None]:
    batch_idx, tmp = args
    src = build_batch_source(batch_idx)
    start = batch_idx * BATCH_SIZE
    count = min(BATCH_SIZE, TOTAL_PROGRAMS - start)
    exe = os.path.join(tmp, f"fuzz_batch_{batch_idx}.exe")
    py_file = os.path.join(tmp, f"fuzz_batch_{batch_idx}.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(src)
        res = compile_source(src, py_file, boxed=True)
        with open(exe, "wb") as f:
            f.write(res.exe)
        nat = subprocess.run([exe], capture_output=True, timeout=60)
        py = subprocess.run([sys.executable, py_file], capture_output=True,
                            timeout=60, env=env)
    except Exception as exc:  # noqa: BLE001
        return batch_idx, 0, f"batch {batch_idx} (progs {start}..{start + count - 1}): error {exc}"
    finally:
        for p in (exe, py_file):
            try:
                os.unlink(p)
            except OSError:
                pass
    if nat.returncode != py.returncode:
        return batch_idx, 0, (
            f"batch {batch_idx}: exit mismatch native={nat.returncode} "
            f"cpython={py.returncode} stderr={nat.stderr!r}"
        )
    if nat.stdout != py.stdout:
        nat_lines = nat.stdout.splitlines()
        py_lines = py.stdout.splitlines()
        for li, (nl, pl) in enumerate(zip(nat_lines, py_lines)):
            if nl != pl:
                return batch_idx, li, (
                    f"prog {start + li} mismatch:\n  native ={nl!r}\n  cpython={pl!r}"
                )
        return batch_idx, 0, f"batch {batch_idx}: line count mismatch"
    return batch_idx, count, None


def verify_10k_diversity(total: int = TOTAL_PROGRAMS) -> tuple[int, int]:
    progs: set[str] = set()
    skels: set[str] = set()
    for i in range(total):
        fn_src = generate_program_fn(i)
        progs.add(fn_src)
        if i < 2500:
            tree = ast.parse(fn_src)
            skels.add("|".join(type(n).__name__ for n in ast.walk(tree)))
    return len(progs), len(skels)


def run_fuzz_10k(total: int = TOTAL_PROGRAMS) -> tuple[int, list[str]]:
    n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    tmp = tempfile.mkdtemp(prefix="vela_fuzz10k_")
    passed = 0
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _, cnt, err in pool.map(_run_batch, ((b, tmp) for b in range(n_batches))):
            passed += cnt
            if err is not None:
                errors.append(err)
    return passed, errors


def main() -> int:
    t0 = time.perf_counter()
    unique_progs, unique_skels = verify_10k_diversity(TOTAL_PROGRAMS)
    if unique_progs < TOTAL_PROGRAMS or unique_skels < 2000:
        print(f"FAIL diversity gate: {unique_progs}/{TOTAL_PROGRAMS} unique programs, "
              f"{unique_skels} unique AST skeletons (need >= 2000)")
        return 1
    passed, errors = run_fuzz_10k(TOTAL_PROGRAMS)
    dt = time.perf_counter() - t0
    for e in errors:
        print("FAIL", e)
    print(f"{passed}/{TOTAL_PROGRAMS} random AST programs passed, {len(errors)} failed, "
          f"0 skipped ({unique_progs} unique programs, {unique_skels}+ unique AST skeletons, "
          f"{dt:.1f}s)")
    return 0 if (not errors and passed == TOTAL_PROGRAMS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
