"""Liveness analysis and linear-scan register allocation.

Virtual registers are allocated to one machine location for their whole live
range.  Only non-volatile registers are used as an allocation pool, so calls
(including runtime helper calls) never need to spill around them:

    GPR pool: rbx rsi rdi r12 r13 r14 r15
    XMM pool: xmm6 .. xmm15

Everything else (rax r10 r11, xmm0-xmm5) is a scratch register used by
instruction selection.  Values that do not fit are spilled to stack slots.
"""
from __future__ import annotations

from .ir import BOOL, FLOAT, GPR_TYPES, INT, NONE, STR, VReg, Function
from .x64 import GPR_POOL, XMM_POOL

ALLOC_GPR = GPR_POOL
ALLOC_XMM = XMM_POOL
GPR_CLASS = set(GPR_TYPES) | {NONE}
XMM_CLASS = {FLOAT}


def _uses(ins) -> tuple:
    op = ins.op
    if op == "bin":
        return (ins.args[1], ins.args[2])
    if op == "cmp":
        return (ins.args[1], ins.args[2])
    if op == "un":
        return (ins.args[1],)
    if op == "mov":
        return (ins.args[0],)
    if op == "br":
        return (ins.args[0],)
    if op == "call":
        return tuple(ins.args[1])
    if op == "print":
        v = ins.args[1]
        return (v,) if v is not None else ()
    if op == "ret":
        v = ins.args[0]
        return (v,) if v is not None else ()
    return ()


class Allocation:
    def __init__(self) -> None:
        self.regs: dict[VReg, str] = {}
        self.slots: dict[VReg, int] = {}
        self.slot_count = 0
        self.intervals: dict[VReg, list] = {}
        self.live_in: dict[str, set[VReg]] = {}
        self.ins_pos: dict[int, int] = {}

    def loc(self, v: VReg):
        if v in self.regs:
            return ("reg", self.regs[v])
        return ("slot", self.slots[v])


def _block_succs(fn: Function) -> dict[str, list[str]]:
    block_labels = {b.label for b in fn.blocks}
    handler_labels = [
        ins.args[0]
        for b in fn.blocks
        for ins in b.instrs
        if ins.op == "addr" and ins.args[0] in block_labels
    ]
    succ: dict[str, list[str]] = {}
    for b in fn.blocks:
        t = b.terminator
        s: list[str] = []
        if t is not None:
            if t.op == "jmp":
                s.append(t.args[0])
            elif t.op == "br":
                s.extend((t.args[1], t.args[2]))
        if handler_labels and any(ins.op == "call" for ins in b.instrs):
            for hl in handler_labels:
                if hl not in s:
                    s.append(hl)
        succ[b.label] = s
    return succ


def analyse_liveness(fn: Function):
    blocks = fn.blocks
    by_label = {b.label: b for b in blocks}
    succ = _block_succs(fn)
    uses: dict[str, set[VReg]] = {}
    defs: dict[str, set[VReg]] = {}
    for b in blocks:
        u: set[VReg] = set()
        d: set[VReg] = set()
        for ins in b.instrs:
            for v in _uses(ins):
                if v is not None and v not in d:
                    u.add(v)
            if ins.dst is not None:
                d.add(ins.dst)
        uses[b.label] = u
        defs[b.label] = d
    live_in = {b.label: set() for b in blocks}
    live_out = {b.label: set() for b in blocks}
    changed = True
    while changed:
        changed = False
        for b in reversed(blocks):
            out: set[VReg] = set()
            for s in succ[b.label]:
                out |= live_in[s]
            inn = uses[b.label] | (out - defs[b.label])
            if out != live_out[b.label] or inn != live_in[b.label]:
                live_out[b.label] = out
                live_in[b.label] = inn
                changed = True
    return live_in, live_out, by_label


def build_intervals(fn: Function, live_in, live_out, by_label):
    pos = {}
    block_first = {}
    block_last = {}
    index = 0
    for b in fn.blocks:
        block_first[b.label] = index
        for ins in b.instrs:
            pos[id(ins)] = index
            index += 1
        block_last[b.label] = max(index - 1, block_first[b.label])

    intervals: dict[VReg, list] = {}

    def touch(v: VReg, p: int) -> None:
        iv = intervals.get(v)
        if iv is None:
            intervals[v] = [p, p]
        else:
            if p < iv[0]:
                iv[0] = p
            if p > iv[1]:
                iv[1] = p

    for b in fn.blocks:
        for ins in b.instrs:
            p = pos[id(ins)]
            if ins.dst is not None:
                touch(ins.dst, p)
            for v in _uses(ins):
                if v is not None:
                    touch(v, p)
    for b in fn.blocks:
        first, last = block_first[b.label], block_last[b.label]
        for v in live_in[b.label]:
            touch(v, first)
        for v in live_out[b.label]:
            touch(v, last)
    return intervals


def _scan(items, pool, alloc: Allocation) -> None:
    """Linear scan.  `items` is [(vreg, (start, end))]."""
    items = sorted(items, key=lambda t: (t[1][0], t[1][1]))
    active: list[tuple[int, VReg]] = []
    free = list(pool)
    for v, (start, end) in items:
        active = [(e, av) for (e, av) in active if e >= start]
        used_regs = {alloc.regs[av] for _, av in active}
        free = [r for r in pool if r not in used_regs]
        if free:
            alloc.regs[v] = free[0]
            active.append((end, v))
            continue
        victim = None
        for cand in active:
            if victim is None or cand[0] > victim[0]:
                victim = cand
        if victim is not None and victim[0] > end:
            spilled = victim[1]
            alloc.regs[v] = alloc.regs.pop(spilled)
            alloc.slots[spilled] = alloc.slot_count
            alloc.slot_count += 1
            active = [(e, av) for (e, av) in active if av is not spilled]
            active.append((end, v))
        else:
            alloc.slots[v] = alloc.slot_count
            alloc.slot_count += 1


def is_imm_operand(ins, v: VReg, const_ints: dict[VReg, int]) -> bool:
    if v not in const_ints:
        return False
    val = const_ints[v]
    if ins.op == "bin":
        op, va, vb = ins.args
        if vb is not v or va is v:
            return False
        if op in ("add", "sub", "mul", "and", "or", "xor"):
            return -2**31 <= val <= 2**31 - 1
        if op in ("shl", "shr"):
            return 0 <= val <= 63
        if op in ("idiv_c", "imod_c"):
            return 2 <= val < (1 << 62)
        return False
    if ins.op == "cmp":
        op, va, vb = ins.args
        if vb is not v or va is v:
            return False
        if va.ty == FLOAT or vb.ty == FLOAT:
            return False
        return -2**31 <= val <= 2**31 - 1
    return False


def find_imm_only_vregs(fn: Function) -> tuple[dict[VReg, int], set[VReg]]:
    defs: dict[VReg, object] = {}
    def_counts: dict[VReg, int] = {}
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.dst is not None:
                def_counts[ins.dst] = def_counts.get(ins.dst, 0) + 1
                defs[ins.dst] = ins
    const_ints: dict[VReg, int] = {
        v: int(ins.args[0])  # type: ignore[attr-defined]
        for v, ins in defs.items()
        if def_counts[v] == 1 and ins.op == "const" and v.ty in (INT, BOOL)  # type: ignore[attr-defined]
    }
    if not const_ints:
        return {}, set()
    used_any: set[VReg] = set()
    non_imm: set[VReg] = set()
    for b in fn.blocks:
        for ins in b.instrs:
            for v in _uses(ins):
                if v is None or v not in const_ints:
                    continue
                used_any.add(v)
                if not is_imm_operand(ins, v, const_ints):
                    non_imm.add(v)
    return const_ints, (used_any - non_imm)


def allocate(fn: Function) -> Allocation:
    live_in, live_out, by_label = analyse_liveness(fn)
    intervals = build_intervals(fn, live_in, live_out, by_label)
    _, imm_only = find_imm_only_vregs(fn)
    alloc = Allocation()
    alloc.intervals = intervals
    alloc.live_in = live_in
    idx = 0
    def_counts: dict[VReg, int] = {}
    block_labels = set(by_label)
    handler_labels = [
        ins.args[0]
        for b in fn.blocks
        for ins in b.instrs
        if ins.op == "addr" and ins.args[0] in block_labels
    ]
    for b in fn.blocks:
        for ins in b.instrs:
            alloc.ins_pos[id(ins)] = idx
            idx += 1
            if ins.dst is not None:
                def_counts[ins.dst] = def_counts.get(ins.dst, 0) + 1
    handler_live: set[VReg] = set()
    for hl in handler_labels:
        handler_live |= live_in.get(hl, set())
    for v in sorted(handler_live, key=lambda x: x.id):
        if v.ty in GPR_CLASS and def_counts.get(v, 0) > 1 and v not in imm_only:
            alloc.slots[v] = alloc.slot_count
            alloc.slot_count += 1
    gpr_items = [
        (v, iv) for v, iv in intervals.items()
        if v.ty in GPR_CLASS and v not in alloc.slots and v not in imm_only
    ]
    xmm_items = [(v, iv) for v, iv in intervals.items() if v.ty in XMM_CLASS]
    _scan(gpr_items, ALLOC_GPR, alloc)
    _scan(xmm_items, ALLOC_XMM, alloc)
    return alloc
