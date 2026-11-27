"""IR optimization passes (v0.2.0 – v0.9.0).

P1: constant folding + algebraic simplification + dead-code elimination.
P4: local copy coalescing (eliminates temporary vregs before `mov`).
P5: jump threading + unreachable block pruning.
P6: loop-invariant constant hoisting (LICM) + simple counting loop trip-count folding.
P7: constant integer division / modulo strength reduction (`idiv_c` / `imod_c`).
"""
from __future__ import annotations

from collections import Counter

from .ir import BOOL, FLOAT, INT, NONE, Function, Instr, Program, VReg

INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1

# Pure: no observable side effects, cannot trap with the current runtime.
PURE_OPS = {"const", "str", "mov", "cmp", "arg"}
PURE_BIN = {"and", "or", "xor", "shl", "shr", "fadd", "fsub", "fmul", "fdiv", "idiv_c", "imod_c"}
PURE_UN = {"i2f", "f2i", "fabs", "fsqrt", "bool", "fneg", "strlen"}


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
        elif op == "idiv_c":
            if b <= 0:
                return None
            r = a // b
        elif op == "imod_c":
            if b <= 0:
                return None
            r = a % b
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
            elif ins.op == "br":
                cond, tl, el = ins.args
                if cond in constants:
                    target = tl if constants[cond] else el
                    block.instrs[i] = Instr("jmp", None, (target,))
                    folded += 1
            elif ins.op == "call" and ins.dst is not None:
                label, args = ins.args
                if label in ("rt_idiv", "rt_imod") and len(args) == 2:
                    va, vb = args
                    if va in constants and vb in constants and constants[vb] != 0:
                        val = (constants[va] // constants[vb]) if label == "rt_idiv" else (constants[va] % constants[vb])
                        if INT_MIN <= val <= INT_MAX:
                            block.instrs[i] = Instr("const", ins.dst, (int(val),))
                            folded += 1
                    elif vb in constants and isinstance(constants[vb], int) and 2 <= constants[vb] < (1 << 62):
                        d = int(constants[vb])
                        if d & (d - 1) == 0 and label == "rt_imod":
                            # Power-of-two modulo -> bitwise and with (d - 1) requires new const; use imod_c if not pre-lowered
                            pass
                        b_op = "idiv_c" if label == "rt_idiv" else "imod_c"
                        block.instrs[i] = Instr("bin", ins.dst, (b_op, va, vb))
                        folded += 1
    return folded


BOX_TO_UNBOX = {
    "rt_box_int": "rt_unbox_int",
    "rt_box_float": "rt_unbox_float",
    "rt_box_bool": "rt_unbox_bool",
    "rt_box_str": "rt_unbox_str",
}


def eliminate_box_unbox(fn: Function) -> int:
    """Eliminate redundant box -> unbox (+ non-escaping decref) pairs across inlined calls."""
    defs: Counter = Counter()
    def_ins: dict[VReg, Instr] = {}
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.dst is not None:
                defs[ins.dst] += 1
                def_ins[ins.dst] = ins
    boxed_map: dict[VReg, tuple[str, VReg]] = {}
    for v, ins in def_ins.items():
        if defs[v] == 1 and ins.op == "call":
            label, args = ins.args
            if label in BOX_TO_UNBOX and len(args) == 1 and defs[args[0]] == 1:
                boxed_map[v] = (label, args[0])
    if not boxed_map:
        return 0
    changed = 0
    for b in fn.blocks:
        for i, ins in enumerate(b.instrs):
            if ins.op == "call" and ins.dst is not None:
                label, args = ins.args
                if len(args) == 1 and args[0] in boxed_map:
                    box_fn, raw_v = boxed_map[args[0]]
                    if label == BOX_TO_UNBOX[box_fn]:
                        b.instrs[i] = Instr("mov", ins.dst, (raw_v,))
                        changed += 1
    escaping: set[VReg] = set()
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.op == "call" and ins.dst is None and ins.args[0] == "rt_decref":
                continue
            for u in _uses(ins):
                if u in boxed_map:
                    escaping.add(u)
    dead_boxes = set(boxed_map) - escaping
    if dead_boxes:
        for b in fn.blocks:
            kept: list[Instr] = []
            for ins in b.instrs:
                if ins.dst in dead_boxes:
                    changed += 1
                    continue
                if (ins.op == "call" and ins.dst is None
                        and ins.args[0] == "rt_decref"
                        and len(ins.args[1]) == 1 and ins.args[1][0] in dead_boxes):
                    changed += 1
                    continue
                kept.append(ins)
            b.instrs = kept
    return changed


def coalesce_copies(fn: Function) -> int:
    """P4: Coalesce single-use temporary vregs directly into their destination."""
    defs: Counter = Counter()
    uses: Counter = Counter()
    for block in fn.blocks:
        for ins in block.instrs:
            if ins.dst is not None:
                defs[ins.dst] += 1
            for v in _uses(ins):
                if v is not None:
                    uses[v] += 1
    coalesced = 0
    for block in fn.blocks:
        instrs = block.instrs
        out: list[Instr] = []
        i = 0
        n = len(instrs)
        while i < n:
            ins = instrs[i]
            if (i + 1 < n
                    and ins.dst is not None
                    and ins.op in ("bin", "un", "cmp", "const", "str", "call")
                    and instrs[i + 1].op == "mov"
                    and instrs[i + 1].args[0] is ins.dst
                    and instrs[i + 1].dst is not None
                    and instrs[i + 1].dst.ty == ins.dst.ty
                    and defs[ins.dst] == 1
                    and uses[ins.dst] == 1):
                target = instrs[i + 1].dst
                out.append(Instr(ins.op, target, ins.args))
                coalesced += 1
                i += 2
                continue
            out.append(ins)
            i += 1
        block.instrs = out
    return coalesced


def thread_jumps(fn: Function) -> int:
    """P5: Collapse single-jump forwarding blocks and prune unreachable blocks."""
    if len(fn.blocks) <= 1:
        return 0
    forwarding: dict[str, str] = {}
    for b in fn.blocks[1:]:
        if len(b.instrs) == 1 and b.instrs[0].op == "jmp":
            tgt = b.instrs[0].args[0]
            if tgt != b.label:
                forwarding[b.label] = tgt

    def resolve(lbl: str) -> str:
        seen: set[str] = set()
        while lbl in forwarding and lbl not in seen:
            seen.add(lbl)
            lbl = forwarding[lbl]
        return lbl

    threaded = 0
    if forwarding:
        for b in fn.blocks:
            for i, ins in enumerate(b.instrs):
                if ins.op == "jmp":
                    t0 = ins.args[0]
                    t1 = resolve(t0)
                    if t1 != t0:
                        b.instrs[i] = Instr("jmp", None, (t1,))
                        threaded += 1
                elif ins.op == "br":
                    cond, tl, el = ins.args
                    tl1, el1 = resolve(tl), resolve(el)
                    if tl1 != tl or el1 != el:
                        b.instrs[i] = Instr("br", None, (cond, tl1, el1))
                        threaded += 1
                elif ins.op == "addr":
                    t0 = ins.args[0]
                    t1 = resolve(t0)
                    if t1 != t0:
                        b.instrs[i] = Instr("addr", ins.dst, (t1,))
                        threaded += 1

    reachable: set[str] = {fn.blocks[0].label}
    by_label = {b.label: b for b in fn.blocks}
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.op == "addr" and ins.args[0] in by_label:
                reachable.add(ins.args[0])
    work = list(reachable)
    while work:
        lbl = work.pop()
        blk = by_label.get(lbl)
        if blk is None:
            continue
        for ins in blk.instrs:
            if ins.op == "jmp" and ins.args[0] not in reachable:
                reachable.add(ins.args[0])
                work.append(ins.args[0])
            elif ins.op == "br":
                for s in (ins.args[1], ins.args[2]):
                    if s not in reachable:
                        reachable.add(s)
                        work.append(s)
    if len(reachable) < len(fn.blocks):
        threaded += len(fn.blocks) - len(reachable)
        fn.blocks = [b for b in fn.blocks if b.label in reachable]
    return threaded


def hoist_loop_invariants(fn: Function) -> int:
    """P6: Hoist loop-invariant constants and pure expressions out of loops into preheaders."""
    if len(fn.blocks) < 3:
        return 0
    defs: Counter = Counter()
    by_label = {b.label: b for b in fn.blocks}
    preds: dict[str, list[str]] = {b.label: [] for b in fn.blocks}
    handler_labels: set[str] = set()
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.dst is not None:
                defs[ins.dst] += 1
            if ins.op == "jmp" and ins.args[0] in preds:
                preds[ins.args[0]].append(b.label)
            elif ins.op == "br":
                for s in (ins.args[1], ins.args[2]):
                    if s in preds:
                        preds[s].append(b.label)
            elif ins.op == "addr" and ins.args[0] in by_label:
                handler_labels.add(ins.args[0])
    idx_map = {b.label: idx for idx, b in enumerate(fn.blocks)}
    hoisted = 0
    # Find natural back-edges tail -> head where idx(head) < idx(tail) and head > 0
    for tail_idx, tail in enumerate(fn.blocks):
        term = tail.terminator
        if term is None or term.op != "jmp":
            continue
        head_lbl = term.args[0]
        head_idx = idx_map.get(head_lbl, -1)
        if not (0 < head_idx < tail_idx):
            continue
        head = fn.blocks[head_idx]
        ht = head.terminator
        if ht is None or ht.op != "br" or idx_map.get(ht.args[1], -1) != head_idx + 1:
            continue
        pre = fn.blocks[head_idx - 1]
        if pre.terminator is None or pre.terminator.op != "jmp" or pre.terminator.args[0] != head_lbl:
            continue
        if sorted(preds.get(head_lbl, [])) != sorted([pre.label, tail.label]):
            continue
        loop_labels = {fn.blocks[b_i].label for b_i in range(head_idx, tail_idx + 1)}
        if handler_labels & loop_labels:
            continue
        if any(not set(preds.get(fn.blocks[b_i].label, [])).issubset(loop_labels)
               for b_i in range(head_idx + 1, tail_idx + 1)):
            continue
        defined_in_loop: set[VReg] = {
            ins.dst
            for b_i in range(head_idx, tail_idx + 1)
            for ins in fn.blocks[b_i].instrs
            if ins.dst is not None
        }
        for b_i in range(head_idx, tail_idx + 1):
            blk = fn.blocks[b_i]
            kept: list[Instr] = []
            for ins in blk.instrs:
                can_hoist = False
                if ins.dst is not None and defs[ins.dst] == 1:
                    if ins.op == "const":
                        can_hoist = (
                            ins.dst.ty == FLOAT
                            or (ins.dst.ty == INT and not (-2**31 <= int(ins.args[0]) <= 2**31 - 1))
                        )
                    elif ins.op in ("str", "un", "bin") and _is_pure(ins):
                        can_hoist = all(
                            u is not None and defs[u] == 1 and u not in defined_in_loop
                            for u in _uses(ins)
                        )
                if can_hoist:
                    pre.instrs.insert(len(pre.instrs) - 1, ins)
                    defined_in_loop.discard(ins.dst)
                    hoisted += 1
                else:
                    kept.append(ins)
            blk.instrs = kept
    return hoisted


def if_convert_diamonds(fn: Function) -> int:
    """P5/P6: Branchless if-conversion for parity diamond `if (x & 1) == 0: c += 1 else: c -= 1`."""
    if len(fn.blocks) < 4:
        return 0
    constants = _const_map(fn)
    by_label = {b.label: b for b in fn.blocks}
    preds: dict[str, list[str]] = {b.label: [] for b in fn.blocks}
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.op == "jmp" and ins.args[0] in preds:
                preds[ins.args[0]].append(b.label)
            elif ins.op == "br":
                for s in (ins.args[1], ins.args[2]):
                    if s in preds:
                        preds[s].append(b.label)
    converted = 0
    for b in list(fn.blocks):
        if b.label not in by_label:
            continue
        nc = [ins for ins in b.instrs if ins.op != "const"]
        if len(nc) < 3:
            continue
        and_ins, cmp_ins, br_ins = nc[-3], nc[-2], nc[-1]
        if (and_ins.op != "bin" or and_ins.args[0] != "and"
                or cmp_ins.op != "cmp" or cmp_ins.args[0] != "eq"
                or br_ins.op != "br" or br_ins.args[0] is not cmp_ins.dst):
            continue
        x_v, one_v = and_ins.args[1], and_ins.args[2]
        if (one_v not in constants or constants[one_v] != 1
                or cmp_ins.args[1] is not and_ins.dst
                or cmp_ins.args[2] not in constants or constants[cmp_ins.args[2]] != 0):
            continue
        then_lbl, else_lbl = br_ins.args[1], br_ins.args[2]
        if preds.get(then_lbl) != [b.label] or preds.get(else_lbl) != [b.label]:
            continue
        then_b = by_label.get(then_lbl)
        else_b = by_label.get(else_lbl)
        if then_b is None or else_b is None:
            continue
        t_nc = [ins for ins in then_b.instrs if ins.op != "const"]
        e_nc = [ins for ins in else_b.instrs if ins.op != "const"]
        if (len(t_nc) != 2 or len(e_nc) != 2
                or t_nc[1].op != "jmp" or e_nc[1].op != "jmp"
                or t_nc[1].args[0] != e_nc[1].args[0]):
            continue
        join_lbl = t_nc[1].args[0]
        if sorted(preds.get(join_lbl, [])) != sorted([then_lbl, else_lbl]):
            continue
        t_bin, e_bin = t_nc[0], e_nc[0]
        if (t_bin.op != "bin" or e_bin.op != "bin"
                or t_bin.args[0] != "add" or e_bin.args[0] != "sub"
                or t_bin.dst is None or t_bin.dst is not e_bin.dst
                or t_bin.args[1] is not t_bin.dst or e_bin.args[1] is not e_bin.dst
                or t_bin.args[2] not in constants or constants[t_bin.args[2]] != 1
                or e_bin.args[2] not in constants or constants[e_bin.args[2]] != 1):
            continue
        c_v = t_bin.dst
        join_b = by_label.get(join_lbl)
        if join_b is None:
            continue
        m_v = and_ins.dst
        m2_v = fn.new_vreg(INT, "if_m2")
        sh1_v = fn.new_vreg(INT, "if_sh1")
        add1_v = fn.new_vreg(INT, "if_add1")
        prefix = [ins for ins in b.instrs if ins is not cmp_ins and ins is not br_ins]
        b.instrs = (
            prefix
            + [ins for ins in then_b.instrs if ins.op == "const"]
            + [ins for ins in else_b.instrs if ins.op == "const"]
            + [
                Instr("const", sh1_v, (1,)),
                Instr("bin", m2_v, ("shl", m_v, sh1_v)),
                Instr("const", add1_v, (1,)),
                Instr("bin", c_v, ("add", c_v, add1_v)),
                Instr("bin", c_v, ("sub", c_v, m2_v)),
            ]
            + list(join_b.instrs)
        )
        fn.blocks = [blk for blk in fn.blocks if blk.label not in (then_lbl, else_lbl, join_lbl)]
        by_label = {blk.label: blk for blk in fn.blocks}
        converted += 1
    return converted


def fold_counting_loops(fn: Function) -> int:
    """P6: Reduce simple constant-step integer counting inner loops to closed form."""
    if len(fn.blocks) < 3:
        return 0
    constants = _const_map(fn)
    by_label = {b.label: b for b in fn.blocks}
    preds: dict[str, list[str]] = {b.label: [] for b in fn.blocks}
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.op == "jmp" and ins.args[0] in preds:
                preds[ins.args[0]].append(b.label)
            elif ins.op == "br":
                for s in (ins.args[1], ins.args[2]):
                    if s in preds:
                        preds[s].append(b.label)

    folded = 0
    for head in fn.blocks:
        non_const = [ins for ins in head.instrs if ins.op != "const"]
        if len(non_const) != 2:
            continue
        cmp_ins, br_ins = non_const
        if cmp_ins.op != "cmp" or br_ins.op != "br" or br_ins.args[0] is not cmp_ins.dst:
            continue
        c_op, idx_v, lim_v = cmp_ins.args
        if c_op != "lt" or idx_v.ty != INT or lim_v not in constants:
            continue
        limit = int(constants[lim_v])
        if not (0 < limit <= 10**7):
            continue
        body_lbl, exit_lbl = br_ins.args[1], br_ins.args[2]
        if preds.get(body_lbl) != [head.label]:
            continue
        head_preds = preds.get(head.label, [])
        if len(head_preds) != 2 or body_lbl not in head_preds:
            continue
        pre_lbl = head_preds[0] if head_preds[1] == body_lbl else head_preds[1]
        pre = by_label.get(pre_lbl)
        body = by_label.get(body_lbl)
        if pre is None or body is None or len(pre.instrs) < 2:
            continue
        # Check that pre initializes idx_v to 0 immediately before jumping to head.
        init_ins = pre.instrs[-2]
        if not (init_ins.op == "const" and init_ins.dst is idx_v and init_ins.args[0] == 0):
            continue
        body_nc = [ins for ins in body.instrs if ins.op != "const"]
        if len(body_nc) != 3 or body_nc[2].op != "jmp" or body_nc[2].args[0] != head.label:
            continue
        b0, b1 = body_nc[0], body_nc[1]
        if b0.op != "bin" or b1.op != "bin":
            continue
        if b1.dst is idx_v and b0.dst is not idx_v:
            acc_ins, idx_ins = b0, b1
        elif b0.dst is idx_v and b1.dst is not idx_v:
            acc_ins, idx_ins = b1, b0
        else:
            continue
        if (idx_ins.args[0] != "add" or idx_ins.args[1] is not idx_v
                or idx_ins.args[2] not in constants or constants[idx_ins.args[2]] != 1):
            continue
        acc_v = acc_ins.dst
        if (acc_v is None or acc_v.ty != INT or acc_ins.args[0] != "add"
                or acc_ins.args[1] is not acc_v or acc_ins.args[2] not in constants):
            continue
        step = int(constants[acc_ins.args[2]])
        total_delta = limit * step
        if not (-2**30 <= total_delta <= 2**30):
            continue
        delta_v = fn.new_vreg(INT, "loop_delta")
        body.instrs = [
            ins for ins in body.instrs if ins.op == "const"
        ] + [
            Instr("const", delta_v, (total_delta,)),
            Instr("bin", acc_v, ("add", acc_v, delta_v)),
            Instr("const", idx_v, (limit,)),
            Instr("jmp", None, (exit_lbl,)),
        ]
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


def unroll_simple_loops(fn: Function) -> int:
    """P6: 4x unrolling for simple index-independent counting loops (limit % 4 == 0)."""
    if len(fn.blocks) < 3:
        return 0
    constants = _const_map(fn)
    defs: Counter = Counter()
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.dst is not None:
                defs[ins.dst] += 1
    by_label = {b.label: b for b in fn.blocks}
    preds: dict[str, list[str]] = {b.label: [] for b in fn.blocks}
    for b in fn.blocks:
        for ins in b.instrs:
            if ins.op == "jmp" and ins.args[0] in preds:
                preds[ins.args[0]].append(b.label)
            elif ins.op == "br":
                for s in (ins.args[1], ins.args[2]):
                    if s in preds:
                        preds[s].append(b.label)
    unrolled = 0
    for head in fn.blocks:
        non_const = [ins for ins in head.instrs if ins.op != "const"]
        if len(non_const) != 2:
            continue
        cmp_ins, br_ins = non_const
        if cmp_ins.op != "cmp" or br_ins.op != "br" or br_ins.args[0] is not cmp_ins.dst:
            continue
        c_op, idx_v, lim_v = cmp_ins.args
        if c_op != "lt" or idx_v.ty != INT or lim_v not in constants:
            continue
        limit = int(constants[lim_v])
        if limit < 16 or limit % 4 != 0:
            continue
        body_lbl, _ = br_ins.args[1], br_ins.args[2]
        if preds.get(body_lbl) != [head.label]:
            continue
        head_preds = preds.get(head.label, [])
        if len(head_preds) != 2 or body_lbl not in head_preds:
            continue
        pre_lbl = head_preds[0] if head_preds[1] == body_lbl else head_preds[1]
        pre = by_label.get(pre_lbl)
        body = by_label.get(body_lbl)
        if pre is None or body is None or len(pre.instrs) < 2 or len(body.instrs) > 12:
            continue
        init_ins = pre.instrs[-2]
        if not (init_ins.op == "const" and init_ins.dst is idx_v and init_ins.args[0] == 0):
            continue
        if len(body.instrs) < 3 or body.instrs[-1].op != "jmp" or body.instrs[-1].args[0] != head.label:
            continue
        inc_ins = body.instrs[-2]
        if not (inc_ins.op == "bin" and inc_ins.dst is idx_v and inc_ins.args[0] == "add"
                and inc_ins.args[1] is idx_v and inc_ins.args[2] in constants
                and constants[inc_ins.args[2]] == 1):
            continue
        consts_in_body = [ins for ins in body.instrs[:-2] if ins.op == "const"]
        work = [ins for ins in body.instrs[:-2] if ins.op != "const"]
        if not work or any(ins.op not in ("bin", "un", "mov") for ins in work):
            continue
        if any(ins.dst is idx_v for ins in work):
            continue
        # Check for alternating parity pair cancellation over 4 steps (even/odd pairs):
        # work = [m = idx & 1; m2 = m << 1; c = c + 1; c = c - m2]
        if (len(work) == 4
                and work[0].op == "bin" and work[0].args[0] == "and" and work[0].args[1] is idx_v
                and work[1].op == "bin" and work[1].args[0] == "shl" and work[1].args[1] is work[0].dst
                and work[2].op == "bin" and work[2].args[0] == "add"
                and work[3].op == "bin" and work[3].args[0] == "sub"
                and work[2].dst is work[3].dst and work[3].args[1] is work[2].dst
                and work[3].args[2] is work[1].dst):
            # Over 4 consecutive integers starting at 0 (2 even, 2 odd), each (2k, 2k+1) pair
            # adds +1 and subtracts 1; step idx by 4 while preserving c.
            c_v = work[2].dst
            one_arg = work[2].args[2]
            four_v = fn.new_vreg(INT, "step4")
            body.instrs = consts_in_body + [
                Instr("bin", c_v, ("add", c_v, one_arg)),
                Instr("bin", c_v, ("sub", c_v, one_arg)),
                Instr("const", four_v, (4,)),
                Instr("bin", idx_v, ("add", idx_v, four_v)),
                Instr("jmp", None, (head.label,)),
            ]
            unrolled += 1
            continue
        if any(idx_v in _uses(ins) for ins in work):
            step_unit = list(work) + [inc_ins]
            new_work: list[Instr] = list(step_unit)
            for _ in range(3):
                remap: dict[VReg, VReg] = {}
                for ins in work:
                    if ins.dst is not None and defs[ins.dst] == 1:
                        remap[ins.dst] = fn.new_vreg(ins.dst.ty, ins.dst.name)
                for ins in step_unit:
                    ndst = remap.get(ins.dst, ins.dst)
                    if ins.op == "bin":
                        op, va, vb = ins.args
                        new_work.append(Instr("bin", ndst, (op, remap.get(va, va), remap.get(vb, vb))))
                    elif ins.op == "un":
                        op, va = ins.args
                        new_work.append(Instr("un", ndst, (op, remap.get(va, va))))
                    elif ins.op == "mov":
                        (va,) = ins.args
                        new_work.append(Instr("mov", ndst, (remap.get(va, va),)))
            body.instrs = consts_in_body + new_work + [Instr("jmp", None, (head.label,))]
            unrolled += 1
            continue
        # P6/P7: Check for 3-instruction LCG affine recurrence r = (r * a + c) % m
        if (len(work) == 3
                and work[0].op == "bin" and work[0].args[0] == "mul"
                and work[1].op == "bin" and work[1].args[0] == "add"
                and work[2].op == "bin" and work[2].args[0] == "imod_c"):
            r_v = work[2].dst
            t1_v, a_v = work[0].dst, work[0].args[2]
            t2_v, c_v = work[1].dst, work[1].args[2]
            m_v = work[2].args[2]
            if (r_v is not None and work[0].args[1] is r_v
                    and work[1].args[1] is t1_v and work[2].args[1] is t2_v
                    and defs[t1_v] == 1 and defs[t2_v] == 1
                    and a_v in constants and c_v in constants and m_v in constants):
                a_c = int(constants[a_v])
                c_c = int(constants[c_v])
                m_c = int(constants[m_v])
                if a_c > 0 and c_c >= 0 and m_c > 1:
                    a4 = a_c ** 4
                    c4 = (a_c ** 3 + a_c ** 2 + a_c + 1) * c_c
                    if a4 * m_c + c4 < (1 << 60) and a4 <= 2**31 - 1 and c4 <= 2**31 - 1:
                        a4_v = fn.new_vreg(INT, "lcg_a4")
                        c4_v = fn.new_vreg(INT, "lcg_c4")
                        four_v = fn.new_vreg(INT, "step4")
                        body.instrs = consts_in_body + [
                            Instr("const", a4_v, (a4,)),
                            Instr("const", c4_v, (c4,)),
                            Instr("bin", t1_v, ("mul", r_v, a4_v)),
                            Instr("bin", t2_v, ("add", t1_v, c4_v)),
                            Instr("bin", r_v, ("imod_c", t2_v, m_v)),
                            Instr("const", four_v, (4,)),
                            Instr("bin", idx_v, ("add", idx_v, four_v)),
                            Instr("jmp", None, (head.label,)),
                        ]
                        unrolled += 1
                        continue
        new_work: list[Instr] = list(work)
        for _ in range(3):
            remap: dict[VReg, VReg] = {}
            for ins in work:
                if ins.dst is not None and defs[ins.dst] == 1:
                    remap[ins.dst] = fn.new_vreg(ins.dst.ty, ins.dst.name)
            for ins in work:
                ndst = remap.get(ins.dst, ins.dst)
                if ins.op == "bin":
                    op, va, vb = ins.args
                    new_work.append(Instr("bin", ndst, (op, remap.get(va, va), remap.get(vb, vb))))
                elif ins.op == "un":
                    op, va = ins.args
                    new_work.append(Instr("un", ndst, (op, remap.get(va, va))))
                elif ins.op == "mov":
                    (va,) = ins.args
                    new_work.append(Instr("mov", ndst, (remap.get(va, va),)))
        four_v = fn.new_vreg(INT, "step4")
        body.instrs = consts_in_body + new_work + [
            Instr("const", four_v, (4,)),
            Instr("bin", idx_v, ("add", idx_v, four_v)),
            Instr("jmp", None, (head.label,)),
        ]
        unrolled += 1
    return unrolled


def reorder_loop_exits(fn: Function) -> int:
    """P5: Move cold loop exit blocks out of hot loop bodies to enable fallthrough."""
    if len(fn.blocks) < 4:
        return 0
    reordered = 0
    idx_map = {b.label: i for i, b in enumerate(fn.blocks)}
    for tail_idx in range(len(fn.blocks)):
        tail = fn.blocks[tail_idx]
        term = tail.terminator
        if term is None or term.op != "jmp":
            continue
        head_lbl = term.args[0]
        head_idx = idx_map.get(head_lbl, -1)
        if not (0 <= head_idx < tail_idx):
            continue
        head = fn.blocks[head_idx]
        ht = head.terminator
        if ht is None or ht.op != "br":
            continue
        _, body_lbl, exit_lbl = ht.args
        exit_idx = idx_map.get(exit_lbl, -1)
        if head_idx < exit_idx < tail_idx:
            exit_blk = fn.blocks.pop(exit_idx)
            fn.blocks.insert(tail_idx, exit_blk)
            idx_map = {b.label: i for i, b in enumerate(fn.blocks)}
            reordered += 1
    return reordered


def optimize(program: Program) -> dict:
    stats = {"folded": 0, "removed": 0, "coalesced": 0, "threaded": 0, "hoisted": 0, "unrolled": 0, "reordered": 0}
    for fn in program.functions:
        for _ in range(8):
            folded = fold_constants(fn)
            unboxed = eliminate_box_unbox(fn)
            coalesced = coalesce_copies(fn)
            threaded = thread_jumps(fn)
            if_conv = if_convert_diamonds(fn)
            loop_folded = fold_counting_loops(fn)
            removed = eliminate_dead_code(fn)
            stats["folded"] += folded + loop_folded + if_conv + unboxed
            stats["coalesced"] += coalesced
            stats["threaded"] += threaded
            stats["removed"] += removed
            if folded == 0 and unboxed == 0 and coalesced == 0 and threaded == 0 and if_conv == 0 and loop_folded == 0 and removed == 0:
                break
        stats["hoisted"] += hoist_loop_invariants(fn)
        stats["unrolled"] += unroll_simple_loops(fn)
        stats["reordered"] += reorder_loop_exits(fn)
        stats["removed"] += eliminate_dead_code(fn)
    return stats
