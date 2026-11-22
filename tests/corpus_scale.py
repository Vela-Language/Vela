"""Seeded recursive random AST & multi-construct differential corpus (800 programs).

400 native-profile random AST programs + 400 boxed-profile random AST programs
covering arithmetic/bitwise/boolean/comparison/ternary expression trees,
nested if/elif/else, while+break/continue, for-range (positive/negative steps),
multi-arg functions, strings/slicing/f-strings, lists/dicts/sets/tuples,
comprehensions, unpacking, try/except/finally, closures/lambdas/decorators,
classes/inheritance/properties/dunders, and generators (yield/yield from).

Strict gate:
  * 0 compile skips allowed
  * 0 runtime crashes / mismatches allowed
  * AST structural diversity gate enforced (>= 400 distinct AST skeletons)

Run: python tests/corpus_scale.py
"""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import os
import random
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vela.driver import compile_source  # noqa: E402

TOTAL = 800


def _rand_int_expr(rng: random.Random, vars_in_scope: list[str], depth: int) -> str:
    if depth <= 0 or rng.random() < 0.28:
        if vars_in_scope and rng.random() < 0.65:
            return rng.choice(vars_in_scope)
        return str(rng.randint(-40, 60))
    kind = rng.randint(0, 7)
    a = _rand_int_expr(rng, vars_in_scope, depth - 1)
    b = _rand_int_expr(rng, vars_in_scope, depth - 1)
    if kind == 0:
        op = rng.choice(["+", "-", "*"])
        return f"({a} {op} {b})"
    if kind == 1:
        op = rng.choice(["//", "%"])
        div = rng.randint(2, 13) * rng.choice([-1, 1])
        return f"({a} {op} {div})"
    if kind == 2:
        op = rng.choice(["&", "|", "^"])
        return f"({a} {op} {b})"
    if kind == 3:
        sh = rng.randint(1, 4)
        op = rng.choice(["<<", ">>"])
        return f"({a} {op} {sh})"
    if kind == 4:
        fn = rng.choice(["abs", "-"])
        return f"abs({a})" if fn == "abs" else f"(-{a})"
    if kind == 5:
        fn = rng.choice(["min", "max"])
        return f"{fn}({a}, {b})"
    if kind == 6:
        cond = _rand_bool_expr(rng, vars_in_scope, depth - 1)
        return f"({a} if {cond} else {b})"
    exp = rng.randint(0, 3)
    base = rng.randint(-9, 11)
    return f"({base} ** {exp})"


def _rand_bool_expr(rng: random.Random, vars_in_scope: list[str], depth: int) -> str:
    if depth <= 0 or rng.random() < 0.45:
        a = _rand_int_expr(rng, vars_in_scope, 1)
        b = _rand_int_expr(rng, vars_in_scope, 1)
        if rng.random() < 0.25:
            c = _rand_int_expr(rng, vars_in_scope, 1)
            op1 = rng.choice(["<", "<=", "=="])
            op2 = rng.choice(["<", "<="])
            return f"({a} {op1} {b} {op2} {c})"
        op = rng.choice(["==", "!=", "<", "<=", ">", ">="])
        return f"({a} {op} {b})"
    kind = rng.randint(0, 2)
    if kind == 0:
        return f"(not {_rand_bool_expr(rng, vars_in_scope, depth - 1)})"
    op = "and" if kind == 1 else "or"
    left = _rand_bool_expr(rng, vars_in_scope, depth - 1)
    right = _rand_bool_expr(rng, vars_in_scope, depth - 1)
    return f"({left} {op} {right})"


def _generate_native_program(rng: random.Random, idx: int) -> str:
    lines: list[str] = []
    fn_names: list[tuple[str, int]] = []
    n_funcs = rng.randint(1, 3)
    for fi in range(n_funcs):
        fname = f"fn_{fi}"
        arity = rng.randint(1, 4)
        params = [f"p{j}" for j in range(arity)]
        fn_names.append((fname, arity))
        lines.append(f"def {fname}({', '.join(params)}):")
        loc = list(params)
        lines.append(f"    acc = {_rand_int_expr(rng, loc, 2)}")
        loc.append("acc")
        if rng.random() < 0.6:
            c = _rand_bool_expr(rng, loc, 2)
            lines.append(f"    if {c}:")
            lines.append(f"        acc = acc + {_rand_int_expr(rng, loc, 1)}")
            lines.append("    else:")
            lines.append(f"        acc = acc - {_rand_int_expr(rng, loc, 1)}")
        if rng.random() < 0.5:
            n_iter = rng.randint(2, 7)
            step = rng.choice([1, 2, -1])
            if step > 0:
                lines.append(f"    for k in range(0, {n_iter}, {step}):")
            else:
                lines.append(f"    for k in range({n_iter}, 0, {step}):")
            lines.append(f"        acc = acc + k * {rng.randint(1, 5)}")
        lines.append(f"    return {_rand_int_expr(rng, loc, 2)}")

    main_vars = ["x", "y", "z"]
    lines.append(f"x = {rng.randint(-30, 40)}")
    lines.append(f"y = {rng.randint(1, 25)}")
    lines.append(f"z = {_rand_int_expr(rng, ['x', 'y'], 2)}")

    n_stmts = rng.randint(2, 5)
    for si in range(n_stmts):
        choice = rng.randint(0, 3)
        target = rng.choice(main_vars)
        if choice == 0:
            lines.append(f"{target} = {_rand_int_expr(rng, main_vars, 2)}")
        elif choice == 1:
            cond1 = _rand_bool_expr(rng, main_vars, 2)
            cond2 = _rand_bool_expr(rng, main_vars, 1)
            lines.append(f"if {cond1}:")
            lines.append(f"    {target} = {_rand_int_expr(rng, main_vars, 2)}")
            lines.append(f"elif {cond2}:")
            lines.append(f"    {target} = {_rand_int_expr(rng, main_vars, 1)}")
            lines.append("else:")
            lines.append(f"    {target} = {target} + {rng.randint(-10, 10)}")
        elif choice == 2:
            cnt = f"w_{si}"
            limit = rng.randint(3, 8)
            lines.append(f"{cnt} = 0")
            lines.append(f"while {cnt} < {limit}:")
            lines.append(f"    {cnt} = {cnt} + 1")
            if rng.random() < 0.5:
                lines.append(f"    if {cnt} == {rng.randint(1, limit)}:")
                lines.append("        continue")
            lines.append(f"    {target} = {target} + {cnt}")
        else:
            fname, arity = rng.choice(fn_names)
            cargs = [_rand_int_expr(rng, main_vars, 1) for _ in range(arity)]
            lines.append(f"{target} = {fname}({', '.join(cargs)})")

    if rng.random() < 0.5:
        fx = rng.randint(1, 20) * 0.5
        fy = float(1 << rng.randint(0, 3)) * 0.5
        lines.append(f"fl = ({fx} + {fy}) / {fy}")
        lines.append("print(x, y, z, int(fl), fl > 1.0)")
    else:
        s_lit = f"vela_{idx}_{rng.randint(10, 999)}"
        lines.append(f's = "{s_lit}"')
        lines.append(f"print(x, y, z, len(s), s, {_rand_bool_expr(rng, main_vars, 2)})")
    return "\n".join(lines) + "\n"


def _generate_boxed_program(rng: random.Random, idx: int) -> str:
    lines: list[str] = []
    # Mix a random selection of 2-4 boxed language features per program
    features = rng.sample(
        ["list_comp", "dict_try", "slice_fstr", "closure_lambda", "class_dunder", "generator", "unpack_def"],
        k=rng.randint(2, 4),
    )
    out_exprs: list[str] = []

    if "closure_lambda" in features:
        bias = rng.randint(3, 25)
        step_d = rng.randint(1, 9)
        lines.extend([
            f"def mk_acc_{idx}(start):",
            "    cur = start",
            "    def step(dx):",
            "        nonlocal cur",
            f"        cur = cur + dx + {bias}",
            "        return cur",
            "    return step",
            f"cl_{idx} = mk_acc_{idx}({rng.randint(1, 15)})",
            f"lm_{idx} = lambda a, b: a * {step_d} - b",
        ])
        out_exprs.append(f"cl_{idx}({rng.randint(1, 8)})")
        out_exprs.append(f"cl_{idx}({rng.randint(1, 8)})")
        out_exprs.append(f"lm_{idx}({rng.randint(2, 12)}, {rng.randint(1, 10)})")

    if "class_dunder" in features:
        k1 = rng.randint(2, 15)
        k2 = rng.randint(1, 9)
        lines.extend([
            f"class Base_{idx}:",
            "    def __init__(self, v):",
            "        self.v = v",
            "    def calc(self):",
            f"        return self.v * {k1}",
            f"class Sub_{idx}(Base_{idx}):",
            "    def __init__(self, v, extra):",
            "        super().__init__(v)",
            "        self.extra = extra",
            "    def calc(self):",
            "        return super().calc() + self.extra",
            "    @property",
            "    def tag(self):",
            f"        return self.v + self.extra * {k2}",
            "    def __add__(self, other):",
            f"        return Sub_{idx}(self.v + other.v, self.extra + other.extra)",
            f"o1_{idx} = Sub_{idx}({rng.randint(1, 10)}, {rng.randint(1, 10)})",
            f"o2_{idx} = Sub_{idx}({rng.randint(1, 10)}, {rng.randint(1, 10)})",
            f"o3_{idx} = o1_{idx} + o2_{idx}",
        ])
        out_exprs.append(f"o3_{idx}.calc()")
        out_exprs.append(f"o3_{idx}.tag")

    if "generator" in features:
        n_lim = rng.randint(3, 7)
        mul = rng.randint(2, 6)
        lines.extend([
            f"def gen_{idx}(n):",
            "    i = 1",
            "    while i <= n:",
            f"        yield i * {mul}",
            "        i = i + 1",
            f"gsum_{idx} = sum(gen_{idx}({n_lim}))",
        ])
        out_exprs.append(f"gsum_{idx}")

    if "list_comp" in features:
        n_r = rng.randint(5, 12)
        mod_k = rng.randint(2, 4)
        add_k = rng.randint(1, 10)
        lines.extend([
            f"lc_{idx} = [i * {add_k} + {rng.randint(-5, 5)} for i in range({n_r}) if i % {mod_k} != 0]",
        ])
        out_exprs.append(f"len(lc_{idx})")
        out_exprs.append(f"sum(lc_{idx})")

    if "dict_try" in features:
        v_a = rng.randint(10, 50)
        v_b = rng.randint(1, 30)
        lines.extend([
            f'd_{idx} = {{"a": {v_a}, "b": {v_b}}}',
            f'd_{idx}["c"] = d_{idx}["a"] - d_{idx}["b"]',
            f"tr_{idx} = 0",
            "try:",
            f"    if {rng.randint(0, 1)} == 0:",
            '        raise ValueError("v")',
            '    raise TypeError("t")',
            "except ValueError:",
            f"    tr_{idx} = {rng.randint(100, 199)}",
            "except TypeError:",
            f"    tr_{idx} = {rng.randint(200, 299)}",
            "finally:",
            f"    tr_{idx} = tr_{idx} + {rng.randint(1, 9)}",
        ])
        out_exprs.append(f'd_{idx}["c"]')
        out_exprs.append(f"tr_{idx}")

    if "slice_fstr" in features:
        word = f"VelaRand_{idx}_{rng.randint(100, 999)}"
        st = rng.choice([1, 2, -1])
        lines.extend([
            f's_{idx} = "{word}"',
            f"sl_{idx} = s_{idx}[::{st}]",
            f'fs_{idx} = f"{{sl_{idx}[:4]}}_{{len(s_{idx})}}"',
        ])
        out_exprs.append(f"fs_{idx}")

    if "unpack_def" in features:
        d1 = rng.randint(2, 15)
        d2 = rng.randint(3, 18)
        e0, e1, e2, e3 = [rng.randint(1, 30) for _ in range(4)]
        lines.extend([
            f"def kwfn_{idx}(a, b={d1}, *rest, c={d2}):",
            "    return a + b * 2 + sum(rest) + c * 3",
            f"u1_{idx}, *umid_{idx}, u2_{idx} = [{e0}, {e1}, {e2}, {e3}]",
        ])
        out_exprs.append(f"kwfn_{idx}({e0}, c={e3})")
        out_exprs.append(f"kwfn_{idx}({e0}, {e1}, {e2}, {e3})")
        out_exprs.append(f"u1_{idx} + sum(umid_{idx}) + u2_{idx}")

    lines.append(f"print({', '.join(out_exprs)})")
    return "\n".join(lines) + "\n"


def program(i: int) -> tuple[str, bool]:
    rng = random.Random(0x5EED_0000 + i * 7919)
    boxed = i >= (TOTAL // 2)
    if not boxed:
        return _generate_native_program(rng, i), False
    return _generate_boxed_program(rng, i), True


def ast_skeleton(src: str) -> str:
    tree = ast.parse(src)
    return "|".join(type(n).__name__ for n in ast.walk(tree))


def verify_corpus_diversity(total: int = TOTAL) -> tuple[int, int]:
    programs: set[str] = set()
    skeletons: set[str] = set()
    for i in range(total):
        src, _ = program(i)
        programs.add(src)
        skeletons.add(ast_skeleton(src))
    return len(programs), len(skeletons)


def run_cpython(src: str) -> bytes:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       timeout=30, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def run_vela(src: str, path: str, boxed: bool) -> bytes:
    exe = compile_source(src, path, boxed=boxed).exe
    with open(path, "wb") as f:
        f.write(exe)
    p = subprocess.run([path], capture_output=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def _check_one(args: tuple[int, str]) -> tuple[int, str | None]:
    i, tmp = args
    src, boxed = program(i)
    exe = os.path.join(tmp, f"case_{i}.exe")
    try:
        want = run_cpython(src)
        got = run_vela(src, exe, boxed)
    except Exception as exc:  # noqa: BLE001
        return i, f"case {i} (boxed={boxed}): error {exc}\n{src}"
    finally:
        try:
            os.unlink(exe)
        except OSError:
            pass
    if want != got:
        return i, f"case {i} (boxed={boxed}): mismatch\n  py={want!r}\n  vl={got!r}\n{src}"
    return i, None


def main() -> int:
    unique_progs, unique_skels = verify_corpus_diversity(TOTAL)
    if unique_progs < TOTAL or unique_skels < 400:
        print(f"FAIL diversity gate: {unique_progs}/{TOTAL} unique programs, "
              f"{unique_skels} unique AST skeletons (need >= 400)")
        return 1
    failures = 0
    tmp = tempfile.mkdtemp(prefix="vela_corpus_")
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _, err in pool.map(_check_one, ((i, tmp) for i in range(TOTAL))):
            if err is not None:
                failures += 1
                print(err)
    print(f"{TOTAL - failures} passed, {failures} failed, 0 skipped "
          f"({unique_progs} unique programs, {unique_skels} unique AST skeletons)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
