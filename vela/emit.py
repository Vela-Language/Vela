"""IR + register allocation -> x86-64 machine code.

Instruction selection is intentionally straightforward (load/operate/store),
but it respects the Win64 ABI and the allocation invariants from regalloc.py:
allocated registers are always non-volatile, so calls need no save/reload.
"""
from __future__ import annotations

import struct

from .ir import BOOL, FLOAT, INT, NONE, STR, VReg
from .pe import align_up
from .regalloc import _uses
from .x64 import Asm, GPR_POOL, XMM_POOL

GP_ARGS = ("rcx", "rdx", "r8", "r9")
XMM_ARGS = ("xmm0", "xmm1", "xmm2", "xmm3")
SCRATCH_GPR = ("rax", "r10", "r11")
SCRATCH_XMM = ("xmm0", "xmm1", "xmm2", "xmm3", "xmm4", "xmm5")

CMP_CC = {"eq": "e", "ne": "ne", "lt": "l", "le": "le", "gt": "g", "ge": "ge"}
FCMP_IMM = {"eq": 0, "ne": 4, "lt": 1, "le": 2, "gt": 1, "ge": 2}


def f64_bits(value: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", value))[0]


class FuncEmitter:
    def __init__(self, asm: Asm, fn, alloc, rt, counter) -> None:
        self.a = asm
        self.fn = fn
        self.alloc = alloc
        self.rt = rt
        self.counter = counter
        self.epi = "epi_" + fn.name
        self.gpr_save: dict[str, int] = {}
        self.xmm_save: dict[str, int] = {}
        self.slot_disp: dict[int, int] = {}
        self.frame = 0
        self.has_calls = any(ins.op in ("call", "print")
                             for block in fn.blocks for ins in block.instrs)
        self.use_count: dict[VReg, int] = {}
        for block in fn.blocks:
            for ins in block.instrs:
                for v in _uses(ins):
                    if v is not None:
                        self.use_count[v] = self.use_count.get(v, 0) + 1
        self._layout_frame()

    # -- frame -------------------------------------------------------------
    def _layout_frame(self) -> None:
        used_gpr = [r for r in GPR_POOL if r in set(self.alloc.regs.values())]
        used_xmm = [r for r in XMM_POOL if r in set(self.alloc.regs.values())]
        self.used_gpr = used_gpr
        self.used_xmm = used_xmm
        # A function with no calls, no spills and no XMM callee-saves needs no
        # frame pointer and no shadow space: push/pop the used GPRs directly.
        self.leaf_fast = (not self.has_calls
                          and self.alloc.slot_count == 0
                          and not used_xmm)
        if self.leaf_fast:
            self.frame = 0
            return
        off = 0
        for reg in used_gpr:
            off += 8
            self.gpr_save[reg] = -off
        for reg in used_xmm:
            off += 8
            self.xmm_save[reg] = -off
        for i in range(self.alloc.slot_count):
            off += 8
            self.slot_disp[i] = -off
        shadow = 32 if self.has_calls else 0
        self.frame = align_up(off + shadow, 16)

    def _disp(self, v: VReg) -> int:
        return self.slot_disp[self.alloc.slots[v]]

    def _reg(self, v: VReg) -> str:
        return self.alloc.regs[v]

    def _is_reg(self, v: VReg) -> bool:
        return v in self.alloc.regs

    # -- operand access ----------------------------------------------------
    def _read_gpr(self, v: VReg, avoid: set[str]) -> str:
        if v in self.alloc.regs:
            return self.alloc.regs[v]
        for cand in SCRATCH_GPR:
            if cand not in avoid:
                self.a.mov_rm(cand, "rbp", self._disp(v))
                return cand
        raise AssertionError("out of GPR scratch registers")

    def _write_gpr(self, v: VReg, reg: str) -> None:
        if v in self.alloc.regs:
            dst = self.alloc.regs[v]
            if dst != reg:
                self.a.mov_rr(dst, reg)
        else:
            self.a.mov_mr("rbp", self._disp(v), reg)

    def _read_xmm(self, v: VReg, avoid: set[str]) -> str:
        if v in self.alloc.regs:
            return self.alloc.regs[v]
        for cand in SCRATCH_XMM:
            if cand not in avoid:
                self.a.mov_rm_xmm(cand, "rbp", self._disp(v))
                return cand
        raise AssertionError("out of XMM scratch registers")

    def _write_xmm(self, v: VReg, reg: str) -> None:
        if v in self.alloc.regs:
            dst = self.alloc.regs[v]
            if dst != reg:
                self.a.movsd_rr(dst, reg)
        else:
            self.a.mov_mr_xmm("rbp", self._disp(v), reg)

    def _next_label(self, hint: str) -> str:
        n = next(self.counter)
        return f"{hint}_{n}"

    def _overflow_check(self) -> None:
        ok = self._next_label("ovf")
        self.a.jcc("no", ok)
        self.a.jmp(self.rt.OVERFLOW)
        self.a.bind(ok)

    # -- function ----------------------------------------------------------
    def _alloc_stack(self) -> None:
        """sub rsp, frame; probe every 4 KiB page when the frame is large."""
        a = self.a
        if self.frame <= 4096:
            a.sub_rsp(self.frame)
            return
        a.mov_ri("r11", self.frame)
        loop = self._next_label("probe")
        a.bind(loop)
        a.sub_rsp(4096)
        a.mov_rm("rax", "rsp", 0)       # touch the new page
        a.sub_ri("r11", 4096)
        a.cmp_ri("r11", 4096)
        a.jcc("a", loop)
        a.sub_rr("rsp", "r11")
        a.mov_rm("rax", "rsp", 0)
        a.bind(loop + "_done")

    def emit(self) -> None:
        a = self.a
        a.bind(self.fn.name)
        if self.leaf_fast:
            for reg in self.used_gpr:
                a.push_r(reg)
        else:
            a.push_r("rbp")
            a.mov_rr("rbp", "rsp")
            self._alloc_stack()
            for reg, disp in self.gpr_save.items():
                a.mov_mr("rbp", disp, reg)
            for reg, disp in self.xmm_save.items():
                a.mov_mr_xmm("rbp", disp, reg)
        for block in self.fn.blocks:
            a.bind(block.label)
            instrs = block.instrs
            i = 0
            while i < len(instrs):
                ins = instrs[i]
                # Peephole: `%c = cmp ...; br %c` -> cmp + jcc directly.  This
                # is the hottest pattern in conditions and loop headers.
                if (ins.op == "cmp" and i + 1 < len(instrs)
                        and instrs[i + 1].op == "br"
                        and instrs[i + 1].args[0] is ins.dst
                        and self.use_count.get(ins.dst, 0) == 1
                        and ins.args[1].ty != FLOAT and ins.args[2].ty != FLOAT):
                    self.emit_cmp_branch(ins, instrs[i + 1])
                    i += 2
                    continue
                self.emit_instr(ins)
                i += 1
        a.bind(self.epi)
        if self.leaf_fast:
            for reg in reversed(self.used_gpr):
                a.pop_r(reg)
            a.ret()
        else:
            for reg, disp in self.xmm_save.items():
                a.mov_rm_xmm(reg, "rbp", disp)
            for reg, disp in self.gpr_save.items():
                a.mov_rm(reg, "rbp", disp)
            a.mov_rr("rsp", "rbp")
            a.pop_r("rbp")
            a.ret()

    def emit_instr(self, ins) -> None:
        op = ins.op
        if op == "arg":
            self.emit_arg(ins)
        elif op == "const":
            self.emit_const(ins)
        elif op == "str":
            self.emit_str(ins)
        elif op == "mov":
            self.emit_mov(ins)
        elif op == "bin":
            self.emit_bin(ins)
        elif op == "un":
            self.emit_un(ins)
        elif op == "cmp":
            self.emit_cmp(ins)
        elif op == "jmp":
            self.a.jmp(ins.args[0])
        elif op == "br":
            self.emit_br(ins)
        elif op == "call":
            self.emit_call(ins)
        elif op == "ret":
            self.emit_ret(ins)
        elif op == "print":
            self.emit_print(ins)
        else:  # pragma: no cover
            raise AssertionError(f"unknown IR op {op}")

    # -- individual instructions ------------------------------------------
    def emit_arg(self, ins) -> None:
        v = ins.dst
        idx = ins.args[0]
        if v.ty == FLOAT:
            src = XMM_ARGS[idx]
            self._write_xmm(v, src)
        else:
            self._write_gpr(v, GP_ARGS[idx])

    def emit_const(self, ins) -> None:
        v = ins.dst
        value = ins.args[0]
        a = self.a
        if v.ty == FLOAT:
            bits = f64_bits(float(value))
            a.mov_ri("rax", bits)
            if self._is_reg(v):
                a.movq_xmm_r(self._reg(v), "rax")
            else:
                a.mov_mr("rbp", self._disp(v), "rax")
        else:
            a.mov_ri("rax", int(value))
            self._write_gpr(v, "rax")

    def emit_str(self, ins) -> None:
        v = ins.dst
        off = ins.args[0]
        if self._is_reg(v):
            self.a.mov_abs64(self._reg(v), ("rdata", off))
        else:
            self.a.mov_abs64("rax", ("rdata", off))
            self._write_gpr(v, "rax")

    def emit_mov(self, ins) -> None:
        dst = ins.dst
        src = ins.args[0]
        if dst is src:
            return
        if dst.ty == FLOAT:
            src_reg = self._read_xmm(src, set())
            self._write_xmm(dst, src_reg)
        else:
            src_reg = self._read_gpr(src, {self._reg(dst)} if self._is_reg(dst) else set())
            self._write_gpr(dst, src_reg)

    def emit_bin(self, ins) -> None:
        op = ins.args[0]
        a = ins.args[1]
        b = ins.args[2]
        if op.startswith("f"):
            self.emit_bin_float(op, ins.dst, a, b)
        elif op in ("shl", "shr"):
            self.emit_shift(op, ins.dst, a, b)
        else:
            self.emit_bin_int(op, ins.dst, a, b)

    def emit_bin_int(self, op: str, dst: VReg, va: VReg, vb: VReg) -> None:
        a = self.a
        used: set[str] = set()
        if self._is_reg(dst):
            R = self._reg(dst)
            used.add(R)
        else:
            R = next(r for r in SCRATCH_GPR if r not in used)
            used.add(R)
        A = self._read_gpr(va, used)
        used.add(A)
        B = self._read_gpr(vb, used)
        used.add(B)
        if R != A:
            if R == B:
                T = next(r for r in SCRATCH_GPR if r not in used)
                a.mov_rr(T, A)
                self._alu(op, T, B)
                a.mov_rr(R, T)
            else:
                a.mov_rr(R, A)
                self._alu(op, R, B)
        else:
            self._alu(op, R, B)
        if not self._is_reg(dst):
            self._write_gpr(dst, R)

    def _alu(self, op: str, dst: str, src: str) -> None:
        a = self.a
        if op == "add":
            a.add_rr(dst, src)
            self._overflow_check()
        elif op == "sub":
            a.sub_rr(dst, src)
            self._overflow_check()
        elif op == "mul":
            a.imul_rr(dst, src)
            self._overflow_check()
        elif op == "and":
            a.and_rr(dst, src)
        elif op == "or":
            a.or_rr(dst, src)
        elif op == "xor":
            a.xor_rr(dst, src)
        else:  # pragma: no cover
            raise AssertionError(op)

    def emit_shift(self, op: str, dst: VReg, va: VReg, vb: VReg) -> None:
        a = self.a
        used: set[str] = set()
        if self._is_reg(dst):
            R = self._reg(dst)
            used.add(R)
        else:
            R = next(r for r in SCRATCH_GPR if r not in used)
            used.add(R)
        A = self._read_gpr(va, used)
        used.add(A)
        if R != A:
            a.mov_rr(R, A)
        B = self._read_gpr(vb, used | {R})
        if B != "rcx":
            a.mov_rr("rcx", B)
        if op == "shl":
            a.shl_cl(R)
        else:
            a.sar_cl(R)
        if not self._is_reg(dst):
            self._write_gpr(dst, R)

    def emit_bin_float(self, op: str, dst: VReg, va: VReg, vb: VReg) -> None:
        a = self.a
        used: set[str] = set()
        if self._is_reg(dst):
            R = self._reg(dst)
            used.add(R)
        else:
            R = next(r for r in SCRATCH_XMM if r not in used)
            used.add(R)
        A = self._read_xmm(va, used)
        used.add(A)
        B = self._read_xmm(vb, used)
        if R != A:
            a.movsd_rr(R, A)
        if op == "fadd":
            a.addsd(R, B)
        elif op == "fsub":
            a.subsd(R, B)
        elif op == "fmul":
            a.mulsd(R, B)
        elif op == "fdiv":
            a.divsd(R, B)
        else:  # pragma: no cover
            raise AssertionError(op)
        if not self._is_reg(dst):
            self._write_xmm(dst, R)

    def emit_un(self, ins) -> None:
        op = ins.args[0]
        src = ins.args[1]
        dst = ins.dst
        a = self.a
        if op == "neg":
            used: set[str] = set()
            if self._is_reg(dst):
                R = self._reg(dst)
                used.add(R)
            else:
                R = next(r for r in SCRATCH_GPR if r not in used)
                used.add(R)
            A = self._read_gpr(src, used)
            if R != A:
                a.mov_rr(R, A)
            a.neg_r(R)
            self._overflow_check()
            if not self._is_reg(dst):
                self._write_gpr(dst, R)
        elif op == "i2f":
            A = self._read_gpr(src, set())
            if self._is_reg(dst):
                a.cvtsi2sd(self._reg(dst), A)
            else:
                a.cvtsi2sd("xmm0", A)
                self._write_xmm(dst, "xmm0")
        elif op == "f2i":
            A = self._read_xmm(src, set())
            if self._is_reg(dst):
                a.cvttsd2si(self._reg(dst), A)
            else:
                a.cvttsd2si("rax", A)
                self._write_gpr(dst, "rax")
        elif op == "fneg":
            used: set[str] = {self._reg(dst)} if self._is_reg(dst) else set()
            R = self._reg(dst) if self._is_reg(dst) else next(r for r in SCRATCH_XMM if r not in used)
            used.add(R)
            A = self._read_xmm(src, used)
            if R != A:
                a.movsd_rr(R, A)
            T = next(r for r in SCRATCH_XMM if r not in used)
            a.mov_ri("rax", 0x8000000000000000)
            a.movq_xmm_r(T, "rax")
            a.xorpd(R, T)
            if not self._is_reg(dst):
                self._write_xmm(dst, R)
        elif op == "fabs":
            used: set[str] = {self._reg(dst)} if self._is_reg(dst) else set()
            R = self._reg(dst) if self._is_reg(dst) else next(r for r in SCRATCH_XMM if r not in used)
            A = self._read_xmm(src, {R})
            a.movq_r_xmm("rax", A)
            a.mov_ri("r11", 0x7FFFFFFFFFFFFFFF)
            a.and_rr("rax", "r11")
            a.movq_xmm_r(R, "rax")
            if not self._is_reg(dst):
                self._write_xmm(dst, R)
        elif op == "strlen":
            # Python's len(str) counts code points; count UTF-8 lead bytes.
            A = self._read_gpr(src, set())
            a.mov_rm("r10", A, 0)
            a.lea("r11", A, 8)
            a.mov_ri("rax", 0)
            loop = self._next_label("strlen")
            done = self._next_label("strlen_done")
            skip = self._next_label("strlen_skip")
            a.bind(loop)
            a.test_rr("r10", "r10")
            a.jcc("e", done)
            a.movzx8_rm("rdx", "r11")
            a.mov_rr("r8", "rdx")
            a.and_ri("r8", 0xC0)
            a.cmp_ri("r8", 0x80)
            a.jcc("e", skip)
            a.add_ri("rax", 1)
            a.bind(skip)
            a.add_ri("r11", 1)
            a.sub_ri("r10", 1)
            a.jmp(loop)
            a.bind(done)
            self._write_gpr(dst, "rax")
        elif op == "bool":
            if src.ty == FLOAT:
                A = self._read_xmm(src, {"xmm4", "xmm5"})
                a.pxor("xmm4", "xmm4")
                a.cmpsd("xmm5", A, 4)
                a.movq_r_xmm("rax", "xmm5")
            else:
                A = self._read_gpr(src, set())
                a.test_rr(A, A)
                a.setcc("ne", "rax")
                a.movzx8("rax", "rax")
            a.and_ri("rax", 1)
            self._write_gpr(dst, "rax")
        else:  # pragma: no cover
            raise AssertionError(op)

    def emit_cmp(self, ins) -> None:
        op, a, b = ins.args
        dst = ins.dst
        asm = self.a
        if a.ty == FLOAT or b.ty == FLOAT:
            if op in ("gt", "ge"):
                a, b = b, a
                op = "lt" if op == "gt" else "le"
            A = self._read_xmm(a, set())
            B = self._read_xmm(b, {A, "xmm4", "xmm5"})
            asm.movsd_rr("xmm4", A)
            asm.cmpsd("xmm4", B, FCMP_IMM[op])
            asm.movq_r_xmm("rax", "xmm4")
            asm.and_ri("rax", 1)
            self._write_gpr(dst, "rax")
            return
        A = self._read_gpr(a, set())
        B = self._read_gpr(b, {A})
        asm.cmp_rr(A, B)
        asm.setcc(CMP_CC[op], "rax")
        asm.movzx8("rax", "rax")
        self._write_gpr(dst, "rax")

    def emit_cmp_branch(self, ins, br) -> None:
        op, va, vb = ins.args
        A = self._read_gpr(va, set())
        B = self._read_gpr(vb, {A})
        self.a.cmp_rr(A, B)
        self.a.jcc(CMP_CC[op], br.args[1])
        self.a.jmp(br.args[2])

    def emit_br(self, ins) -> None:
        cond, then_l, else_l = ins.args
        R = self._read_gpr(cond, set())
        self.a.test_rr(R, R)
        self.a.jcc("ne", then_l)
        self.a.jmp(else_l)

    def emit_call(self, ins) -> None:
        label = ins.args[0]
        args = ins.args[1]
        a = self.a
        gp = 0
        xp = 0
        for v in args:
            if v.ty == FLOAT:
                dst = XMM_ARGS[xp]
                xp += 1
                src = self._read_xmm(v, set(XMM_ARGS))
                if src != dst:
                    a.movsd_rr(dst, src)
            else:
                dst = GP_ARGS[gp]
                gp += 1
                src = self._read_gpr(v, set(GP_ARGS))
                if src != dst:
                    a.mov_rr(dst, src)
        a.call(label)
        dst = ins.dst
        if dst is None:
            return
        if dst.ty == FLOAT:
            self._write_xmm(dst, "xmm0")
        else:
            self._write_gpr(dst, "rax")

    def emit_ret(self, ins) -> None:
        val = ins.args[0]
        if val is not None:
            if val.ty == FLOAT:
                src = self._read_xmm(val, {"xmm0"})
                if src != "xmm0":
                    self.a.movsd_rr("xmm0", src)
            else:
                src = self._read_gpr(val, {"rax"})
                if src != "rax":
                    self.a.mov_rr("rax", src)
        self.a.jmp(self.epi)

    def emit_print(self, ins) -> None:
        kind, v = ins.args
        a = self.a
        if kind == "int":
            src = self._read_gpr(v, {"rcx"})
            if src != "rcx":
                a.mov_rr("rcx", src)
            a.call(self.rt.PRINT_INT)
        elif kind == "bool":
            src = self._read_gpr(v, {"rcx"})
            if src != "rcx":
                a.mov_rr("rcx", src)
            a.call(self.rt.PRINT_BOOL)
        elif kind == "str":
            src = self._read_gpr(v, {"rcx"})
            if src != "rcx":
                a.mov_rr("rcx", src)
            a.call(self.rt.PRINT_STR)
        elif kind == "float":
            src = self._read_xmm(v, {"xmm0"})
            if src != "xmm0":
                a.movsd_rr("xmm0", src)
            a.call(self.rt.PRINT_FLOAT)
        elif kind == "none":
            a.call(self.rt.PRINT_NONE)
        elif kind == "nl":
            a.call(self.rt.PRINT_NL)
        elif kind == "space":
            a.call(self.rt.PRINT_SPACE)
        else:  # pragma: no cover
            raise AssertionError(kind)


def emit_program(asm: Asm, functions, allocs: dict, rt, counter) -> None:
    for fn in functions:
        FuncEmitter(asm, fn, allocs[fn.name], rt, counter).emit()
