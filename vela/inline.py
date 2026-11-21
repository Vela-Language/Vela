"""P2: inline small functions.

Eligibility:

* at most 4 parameters and at most 8 basic blocks;
* no ``call`` instructions in the body, which also excludes every recursive or
  mutually recursive function (a recursive body must contain a call);
* at most 48 body instructions (jumps included), so code growth stays bounded.

The inliner clones the callee CFG into the caller: the call site is split into
a continuation block, parameters are copied into fresh vregs, and every ``ret``
becomes ``mov dst, value; jmp continuation``.  CFG-based callees (branches and
loops) are therefore supported; only recursive callees are rejected, because
unbounded inlining of recursion cannot terminate.
"""
from __future__ import annotations

from .ir import NONE, Function, Instr, Program, VReg

MAX_PARAMS = 4
MAX_BLOCKS = 16
MAX_BODY = 48             # acyclic callees
MAX_BODY_LOOP = 32        # loop-containing callees
MAX_GROWTH = 120          # cloned instructions per caller
MAX_SITES_ACYCLIC = 3     # per callee, per caller
MAX_SITES_LOOP = 1        # duplicating a loop is rarely profitable


def _body_size(fn: Function) -> int:
    return sum(1 for block in fn.blocks for ins in block.instrs if ins.op != "arg")


def _has_loop(fn: Function) -> bool:
    order = {block.label: i for i, block in enumerate(fn.blocks)}
    for i, block in enumerate(fn.blocks):
        term = block.terminator
        if term is None:
            continue
        if term.op == "jmp":
            targets = (term.args[0],)
        elif term.op == "br":
            targets = (term.args[1], term.args[2])
        else:
            continue
        for label in targets:
            if order.get(label, 1 << 30) <= i:
                return True
    return False


def _eligible(fn: Function) -> bool:
    if len(fn.params) > MAX_PARAMS or len(fn.blocks) > MAX_BLOCKS:
        return False
    limit = MAX_BODY_LOOP if _has_loop(fn) else MAX_BODY
    if _body_size(fn) > limit:
        return False
    for block in fn.blocks:
        for ins in block.instrs:
            # Runtime helpers (rt_*) are fine; user calls would mean recursion
            # or a nested function we are not inlining in this pass.
            if ins.op == "call" and not ins.args[0].startswith("rt_"):
                return False
    return True


def _inline_call(caller: Function, callee: Function, block, index: int) -> int:
    call = block.instrs[index]
    pre = block.instrs[:index]
    post = block.instrs[index + 1:]
    cont = caller.new_block("cont")

    mapping: dict[VReg, VReg] = {}
    blocks = {old.label: caller.new_block("inl") for old in callee.blocks}
    entry_label = blocks[callee.blocks[0].label].label

    def mapped(v: VReg | None) -> VReg | None:
        if v is None:
            return None
        nv = mapping.get(v)
        if nv is None:
            nv = caller.new_vreg(v.ty, v.name)
            mapping[v] = nv
        return nv

    new_pre: list[Instr] = list(pre)
    for pv, arg in zip(callee.params, call.args[1]):
        nv = caller.new_vreg(pv.ty, pv.name)
        mapping[pv] = nv
        new_pre.append(Instr("mov", nv, (arg,)))
    new_pre.append(Instr("jmp", None, (entry_label,)))
    block.instrs = new_pre
    cont.instrs = list(post)

    for old in callee.blocks:
        target = blocks[old.label]
        for ins in old.instrs:
            op = ins.op
            if op == "arg":
                continue
            if op == "ret":
                val = ins.args[0]
                if call.dst is not None and val is not None:
                    target.instrs.append(Instr("mov", call.dst, (mapped(val),)))
                target.instrs.append(Instr("jmp", None, (cont.label,)))
            elif op == "jmp":
                target.instrs.append(Instr("jmp", None, (blocks[ins.args[0]].label,)))
            elif op == "br":
                cond, t, f = ins.args
                target.instrs.append(Instr("br", None, (mapped(cond),
                                                        blocks[t].label,
                                                        blocks[f].label)))
            elif op in ("const", "str"):
                target.instrs.append(Instr(op, mapped(ins.dst), ins.args))
            elif op == "mov":
                target.instrs.append(Instr("mov", mapped(ins.dst), (mapped(ins.args[0]),)))
            elif op in ("bin", "cmp"):
                o, a, b = ins.args
                target.instrs.append(Instr(op, mapped(ins.dst), (o, mapped(a), mapped(b))))
            elif op == "un":
                o, a = ins.args
                target.instrs.append(Instr("un", mapped(ins.dst), (o, mapped(a))))
            elif op == "print":
                kind, v = ins.args
                target.instrs.append(Instr("print", None, (kind, mapped(v))))
            elif op == "call":
                label, args = ins.args
                target.instrs.append(Instr("call", mapped(ins.dst),
                                           (label, tuple(mapped(a) for a in args))))
            else:  # pragma: no cover - guarded by _eligible
                raise AssertionError(f"unexpected op in inlinable body: {op}")
    return _body_size(callee)


def _find_call_in(caller: Function, eligible: dict[str, Function]):
    for block in caller.blocks:
        for i, ins in enumerate(block.instrs):
            if ins.op == "call" and ins.args[0] in eligible:
                return block, i
    return None


def _find_call(caller: Function, eligible: dict[str, Function]):
    return _find_call_in(caller, eligible)


def optimize(program: Program) -> int:
    """Bottom-up fixpoint: inline callees first, then newly call-free callers.

    A function becomes inlineable only after all of its user calls have been
    inlined (runtime ``rt_*`` calls do not count).  Recomputing eligibility
    each pass therefore handles call chains of arbitrary depth, while
    recursive functions never become call-free and are never inlined.
    """
    funcs = program.functions
    growth = {fn.name: 0 for fn in funcs}
    sites: dict[tuple[str, str], int] = {}
    inlined = 0
    for _ in range(12):
        eligible = {fn.name: fn for fn in funcs if _eligible(fn)}
        changed = False
        for caller in funcs:
            for callee in eligible.values():
                if callee is caller:
                    continue
                key = (caller.name, callee.name)
                limit = MAX_SITES_LOOP if _has_loop(callee) else MAX_SITES_ACYCLIC
                while sites.get(key, 0) < limit and growth[caller.name] < MAX_GROWTH:
                    site = _find_call_in(caller, {callee.name: callee})
                    if site is None:
                        break
                    block, index = site
                    growth[caller.name] += _inline_call(caller, callee, block, index)
                    sites[key] = sites.get(key, 0) + 1
                    inlined += 1
                    changed = True
        if not changed:
            break
    return inlined
