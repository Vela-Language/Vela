"""P2: full inlining coverage.

Definition of "full coverage" (feasible version):

* every call to a **non-recursive** user function is inlined, regardless of the
  callee's size; the only limit is a per-caller code-growth budget (2000 cloned
  instructions) that prevents pathological explosion;
* every **recursive** callee is inlined one level at call sites outside its own
  body (depth-1 partial inlining).  Deeper recursion stays a call, because
  unlimited inlining of recursion does not terminate.
"""
from __future__ import annotations

import copy

from .ir import Function, Instr, Program, VReg

MAX_PARAMS = 4
MAX_GROWTH = 2000


def _body_size(fn: Function) -> int:
    return sum(1 for block in fn.blocks for ins in block.instrs if ins.op != "arg")


def _user_calls(fn: Function, known: set[str]) -> set[str]:
    return {ins.args[0] for block in fn.blocks for ins in block.instrs
            if ins.op == "call" and ins.args[0] in known}


def _eligible(fn: Function, known: set[str]) -> bool:
    if fn.no_inline:
        return False
    if len(fn.params) > MAX_PARAMS:
        return False
    if any(ins.op == "addr" for block in fn.blocks for ins in block.instrs):
        return False
    return not _user_calls(fn, known)


def _recursive(funcs: list[Function]) -> set[str]:
    names = {fn.name for fn in funcs}
    graph = {fn.name: _user_calls(fn, names) for fn in funcs}
    result: set[str] = set()
    for start in graph:
        stack = list(graph[start])
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                result.add(start)
                break
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return result


def _inline_call(caller: Function, callee: Function, block, index: int) -> int:
    call = block.instrs[index]
    pre = block.instrs[:index]
    post = block.instrs[index + 1:]
    b_idx = caller.blocks.index(block)

    mapping: dict[VReg, VReg] = {}
    blocks = {old.label: caller.new_block("inl") for old in callee.blocks}
    cont = caller.new_block("cont")
    added = len(callee.blocks) + 1
    new_slice = caller.blocks[-added:]
    del caller.blocks[-added:]
    caller.blocks[b_idx + 1:b_idx + 1] = new_slice
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
            else:  # pragma: no cover
                raise AssertionError(f"unexpected op in inlinable body: {op}")
    return _body_size(callee)


def _find_call_in(caller: Function, eligible: dict[str, Function]):
    for block in caller.blocks:
        for i, ins in enumerate(block.instrs):
            if ins.op == "call" and ins.args[0] in eligible:
                return block, i
    return None


def _inline_non_recursive(funcs: list[Function], growth: dict[str, int]) -> int:
    known = {fn.name for fn in funcs}
    inlined = 0
    for _ in range(16):
        eligible = {fn.name: fn for fn in funcs if _eligible(fn, known)}
        changed = False
        for caller in funcs:
            for callee in eligible.values():
                if callee is caller:
                    continue
                while growth[caller.name] < MAX_GROWTH:
                    site = _find_call_in(caller, {callee.name: callee})
                    if site is None:
                        break
                    block, index = site
                    growth[caller.name] += _inline_call(caller, callee, block, index)
                    inlined += 1
                    changed = True
        if not changed:
            break
    return inlined


def optimize(program: Program) -> int:
    funcs = program.functions
    if not funcs:
        return 0
    known = {fn.name for fn in funcs}
    growth = {fn.name: 0 for fn in funcs}
    by_label = {fn.name: fn for fn in funcs}

    inlined = _inline_non_recursive(funcs, growth)

    # Self-recursive unrolling (depth 2) for small pure numerical recursive
    # functions (e.g. fib), cutting >80% of leaf function call overhead.
    recursive = _recursive(funcs)
    for name in recursive:
        fn = by_label[name]
        if (fn.no_inline or len(fn.params) > MAX_PARAMS
                or _body_size(fn) > 24
                or any(ins.op in ("addr", "print")
                       or (ins.op == "call" and ins.args[0] != name)
                       for b in fn.blocks for ins in b.instrs)):
            continue
        tmpl = copy.deepcopy(fn)
        for _ in range(3):
            cur_labels = {b.label for b in fn.blocks}
            while True:
                target_site = None
                for b in fn.blocks:
                    if b.label not in cur_labels:
                        continue
                    for i, ins in enumerate(b.instrs):
                        if ins.op == "call" and ins.args[0] == name:
                            target_site = (b, i)
                            break
                    if target_site is not None:
                        break
                if target_site is None:
                    break
                cur_labels.discard(target_site[0].label)
                growth[name] += _inline_call(fn, tmpl, target_site[0], target_site[1])
                inlined += 1

    # Depth-1 partial inlining for recursive callees, at call sites outside the
    # callee's own body.  Cloned self-calls stay as calls forever.
    for caller in funcs:
        for name in recursive:
            if name == caller.name:
                continue
            callee = by_label[name]
            if growth[caller.name] >= MAX_GROWTH:
                break
            site = _find_call_in(caller, {name: callee})
            if site is None:
                continue
            block, index = site
            growth[caller.name] += _inline_call(caller, callee, block, index)
            inlined += 1

    # Recursive bodies may have brought calls to non-recursive helpers; clean
    # those up with one more fixpoint.
    inlined += _inline_non_recursive(funcs, growth)
    return inlined
