"""IR optimization passes (v0.2.0).

P1: constant folding + algebraic simplification + dead-code elimination.

Folding is deliberately conservative:

* only operands whose vreg has exactly one definition, and that definition is a
  ``const``, are folded (avoids unsound propagation through multi-assignment
  variables across control flow);
* int add/sub/mul/neg are folded only when the result fits in int64, because an
  overflow must still trap at runtime;
* float division by zero and shift counts outside 0..63 are not folded.

DCE only removes instructions that cannot trap.  That excludes int add/sub/mul
and neg while the runtime traps on overflow.
"""
from __future__ import annotations

from collections import Counter

from .ir import BOOL, FLOAT, INT, NONE, Function, Instr, Program

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1

# Pure: no observable side effects, cannot trap with the current runtime.
PURE_OPS = {"const", "str", "mov", "cmp", "arg"}
PURE_BIN = {"and", "or", "xor", "shl", "shr", "fadd", "fsub", "fmul", "fdiv"}
PURE_UN = {"i2f", "f2i", "fabs", "bool", "fneg", "strlen"}


def _is_pure(ins: Instr) -> bool:
    if ins.op in PURE_OPS:
        return True
    if ins.op == "bin":
        return ins.args[0] in PURE_BIN
    if ins.op == "un":
        return ins.args[0] in PURE_UN
    return False


def _uses(ins: Instr):
    op = ins.op
    if op == "bin" or op == "cmp":
        return (ins.args[1], ins.args[2])
    if op == "un":
        return (ins.args[1],)
    if op == "mov":
        return (ins.args[0],)
    if op == "br":
        return (ins.args[0],)
    if op == "call":
        return tuple(ins.args[1])
    if op == "print" or op == "ret":
        v = ins.args[1] if op == "print" else ins.args[0]
        return (v,) if v is not None else ()
    return ()


def _const_map(fn: Function) -> dict:
    """vreg -> constant value, only for single-definition constant vregs."""
    defs: dict = {}
    counts: Counter = Counter()
    for block in fn.blocks:
        for ins in block.instrs:
            if ins.dst is not None:
                counts[ins.dst] += 1
                defs[ins.dst] = ins
    return {v: defs[v].args[0] for v, n in counts.items()
            if n == 1 and defs[v].op == "const"}


def _eval_bin(op: str, a, b, dst_ty: str):
    try:
        if op == "add":
            r = a + b
        elif op == "sub":
            r = a - b
        elif op == "mul":
            r = a * b
        elif op == "and":
            r = a & b
        elif op == "or":
            r = a | b
        elif op == "xor":
            r = a ^ b
        elif op == "shl":
            if not (0 <= b <= 63):
                return None
            r = a << b
        elif op == "shr":
            if not (0 <= b <= 63):
                return None
            r = a >> b
        elif op == "fadd":
            r = a + b
        elif op == "fsub":
            r = a - b
        elif op == "fmul":
            r = a * b
        elif op == "fdiv":
            if b == 0.0:
                return None
            r = a / b
        else:
            return None
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    if dst_ty == INT:
        if not (INT_MIN <= r <= INT_MAX):
            return None
        return int(r)
    if dst_ty == FLOAT:
        return float(r)
    return None


def _eval_un(op: str, a, dst_ty: str):
    if op == "neg":
        if a == INT_MIN:
            return None
        return -a
    if op == "fneg":
        return -a
    if op == "i2f":
        return float(a)
    if op == "f2i":
        f = float(a)
        if f != f or f < float(INT_MIN) or f > float(INT_MAX):
            return None
        return int(f)
    if op == "fabs":
        return abs(a)
    if op == "bool":
        return 0 if a == 0 else 1
    return None


def fold_constants(fn: Function) -> int:
    constants = _const_map(fn)
    if not constants:
        return 0
    folded = 0
    for block in fn.blocks:
        for i, ins in enumerate(block.instrs):
            if ins.op == "bin":
                op, va, vb = ins.args
                if va in constants and vb in constants:
                    value = _eval_bin(op, constants[va], constants[vb], ins.dst.ty)
                    if value is not None:
                        block.instrs[i] = Instr("const", ins.dst, (value,))
                        folded += 1
            elif ins.op == "un":
                op, va = ins.args
                if va in constants:
                    value = _eval_un(op, constants[va], ins.dst.ty)
                    if value is not None:
                        block.instrs[i] = Instr("const", ins.dst, (value,))
                        folded += 1
            elif ins.op == "cmp":
                op, va, vb = ins.args
                if va in constants and vb in constants:
                    a, b = constants[va], constants[vb]
                    table = {
                        "eq": a == b, "ne": a != b, "lt": a < b,
                        "le": a <= b, "gt": a > b, "ge": a >= b,
                    }
                    block.instrs[i] = Instr("const", ins.dst, (1 if table[op] else 0,))
                    folded += 1
    return folded


def eliminate_dead_code(fn: Function) -> int:
    removed = 0
    changed = True
    while changed:
        changed = False
        uses: Counter = Counter()
        for block in fn.blocks:
            for ins in block.instrs:
                for v in _uses(ins):
                    if v is not None:
                        uses[v] += 1
        for block in fn.blocks:
            kept = []
            for ins in block.instrs:
                if ins.op == "mov" and ins.dst is ins.args[0]:
                    removed += 1
                    changed = True
                    continue
                if _is_pure(ins) and (ins.dst is None or uses[ins.dst] == 0):
                    removed += 1
                    changed = True
                    continue
                kept.append(ins)
            block.instrs = kept
    return removed


def optimize(program: Program) -> dict:
    stats = {"folded": 0, "removed": 0}
    for fn in program.functions:
        for _ in range(8):
            folded = fold_constants(fn)
            removed = eliminate_dead_code(fn)
            stats["folded"] += folded
            stats["removed"] += removed
            if folded == 0 and removed == 0:
                break
    return stats
