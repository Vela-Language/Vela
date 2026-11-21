"""P2a: inline small straight-line functions.

Scope (deliberately narrow for the first inlining step):

* callee has exactly one basic block, no branches, no calls, <= 24 body
  instructions and <= 4 parameters;
* parameters are copied into fresh vregs, so reassigning a parameter cannot
  clobber the caller's value;
* the callee's `ret` becomes a `mov` into the call's destination.

Recursive and looping callees are excluded.  Because eligible callees contain
no calls, a single pass is enough; the caller is never restructured.
"""
from __future__ import annotations

from .ir import BOOL, FLOAT, INT, NONE, STR, Function, Instr, Program, VReg

MAX_BODY = 24


def _eligible(fn: Function) -> bool:
    if len(fn.params) > 4 or len(fn.blocks) != 1:
        return False
    block = fn.blocks[0]
    if block.terminator is None or block.terminator.op != "ret":
        return False
    body = [i for i in block.instrs if i.op not in ("arg", "ret")]
    if len(body) > MAX_BODY:
        return False
    return all(i.op not in ("call", "jmp", "br", "ret") for i in body)


def _clone(caller: Function, callee: Function, call: Instr) -> list[Instr]:
    mapping: dict[VReg, VReg] = {}
    out: list[Instr] = []

    def mapped(v: VReg | None) -> VReg | None:
        if v is None:
            return None
        nv = mapping.get(v)
        if nv is None:
            nv = caller.new_vreg(v.ty, v.name)
            mapping[v] = nv
        return nv

    call_args = call.args[1]
    for pv, arg in zip(callee.params, call_args):
        nv = caller.new_vreg(pv.ty, pv.name)
        mapping[pv] = nv
        out.append(Instr("mov", nv, (arg,)))

    for ins in callee.blocks[0].instrs:
        op = ins.op
        if op == "arg":
            continue
        if op == "ret":
            val = ins.args[0]
            if call.dst is not None and val is not None:
                out.append(Instr("mov", call.dst, (mapped(val),)))
            continue
        if op in ("const", "str"):
            out.append(Instr(op, mapped(ins.dst), ins.args))
        elif op == "mov":
            out.append(Instr("mov", mapped(ins.dst), (mapped(ins.args[0]),)))
        elif op in ("bin", "cmp"):
            o, a, b = ins.args
            out.append(Instr(op, mapped(ins.dst), (o, mapped(a), mapped(b))))
        elif op == "un":
            o, a = ins.args
            out.append(Instr("un", mapped(ins.dst), (o, mapped(a))))
        elif op == "print":
            kind, v = ins.args
            out.append(Instr("print", None, (kind, mapped(v))))
        else:  # pragma: no cover - guarded by _eligible
            raise AssertionError(f"unexpected op in inlinable body: {op}")
    return out


def optimize(program: Program) -> int:
    by_label = {fn.name: fn for fn in program.functions}
    eligible = {name: fn for name, fn in by_label.items() if _eligible(fn)}
    if not eligible:
        return 0
    inlined = 0
    for caller in program.functions:
        for block in caller.blocks:
            if not any(i.op == "call" and i.args[0] in eligible for i in block.instrs):
                continue
            new: list[Instr] = []
            for ins in block.instrs:
                if ins.op == "call" and ins.args[0] in eligible:
                    new.extend(_clone(caller, eligible[ins.args[0]], ins))
                    inlined += 1
                else:
                    new.append(ins)
            block.instrs = new
    return inlined
