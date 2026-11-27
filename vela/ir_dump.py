"""Textual IR dump (--emit-ir)."""
from __future__ import annotations

from .ir import Program


def _fmt(v) -> str:
    return f"%{v.id}:{v.ty}" + (f"({v.name})" if v.name else "") if v is not None else "-"


def dump_program(program: Program) -> str:
    out: list[str] = []
    for fn in program.functions:
        out.append(f"function {fn.name}({', '.join(_fmt(p) for p in fn.params)}) -> {fn.ret_ty}")
        for block in fn.blocks:
            out.append(f"  {block.label}:")
            for ins in block.instrs:
                out.append("    " + _dump_instr(ins))
        out.append("")
    return "\n".join(out)


def _dump_instr(ins) -> str:
    op = ins.op
    dst = _fmt(ins.dst) if ins.dst is not None else ""
    if op == "const":
        return f"{dst} = const {ins.args[0]!r}"
    if op == "str":
        return f"{dst} = string@rdata+{ins.args[0]}"
    if op == "arg":
        return f"{dst} = arg{ins.args[0]}"
    if op == "mov":
        return f"{dst} = mov {_fmt(ins.args[0])}"
    if op == "bin":
        return f"{dst} = {ins.args[0]} {_fmt(ins.args[1])}, {_fmt(ins.args[2])}"
    if op == "un":
        return f"{dst} = {ins.args[0]} {_fmt(ins.args[1])}"
    if op == "cmp":
        return f"{dst} = cmp.{ins.args[0]} {_fmt(ins.args[1])}, {_fmt(ins.args[2])}"
    if op == "jmp":
        return f"jmp {ins.args[0]}"
    if op == "br":
        return f"br {_fmt(ins.args[0])}, {ins.args[1]}, {ins.args[2]}"
    if op == "call":
        args = ", ".join(_fmt(a) for a in ins.args[1])
        return f"{dst + ' = ' if dst else ''}call {ins.args[0]}({args})"
    if op == "ret":
        return f"ret {_fmt(ins.args[0]) if ins.args[0] is not None else ''}".rstrip()
    if op == "print":
        return f"print.{ins.args[0]} {_fmt(ins.args[1]) if ins.args[1] is not None else ''}".rstrip()
    return f"{op} {ins.args}"


def dump_asm(program: Program, labels: dict[str, int] | None = None,
             code_bytes: bytes = b"", allocs: dict | None = None) -> str:
    """Textual x86-64 assembly & symbol offset listing (--emit-asm)."""
    labels = labels or {}
    allocs = allocs or {}
    out: list[str] = ["; Vela x86-64 native assembly & symbol map"]
    for fn in program.functions:
        fn_off = labels.get(fn.name, 0)
        epi_off = labels.get("epi_" + fn.name, fn_off)
        out.append(f"\n; --- {fn.name} (.text+0x{fn_off:04x} .. 0x{epi_off:04x}) ---")
        al = allocs.get(fn.name)
        if al is not None:
            locs = [
                f"%{v.id}({v.name or v.ty})->{al.regs.get(v, f'[rbp-{8 * (al.slots.get(v, 0) + 1)}]')}"
                for v in sorted(al.intervals, key=lambda x: x.id)
                if v in al.regs or v in al.slots
            ]
            if locs:
                out.append("; regs: " + ", ".join(locs))
        out.append(f"{fn.name}:")
        n_blocks = len(fn.blocks)
        for bi, block in enumerate(fn.blocks):
            b_off = labels.get(block.label, fn_off)
            next_lbl = fn.blocks[bi + 1].label if bi + 1 < n_blocks else ("epi_" + fn.name)
            next_off = labels.get(next_lbl, b_off)
            chunk = code_bytes[b_off:next_off].hex(" ") if code_bytes and next_off >= b_off else ""
            out.append(f"  {block.label}: ; .text+0x{b_off:04x}" + (f" [{chunk}]" if chunk else ""))
            for ins in block.instrs:
                out.append("    " + _dump_instr(ins))
    return "\n".join(out) + "\n"
