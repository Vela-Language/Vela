"""Vela x86-64 machine-code encoder.

This module is deliberately dependency-free: it encodes the exact subset of
x86-64 instructions the Vela backend needs, into a flat byte buffer, with a
tiny label/fixup system so that control flow and data references can be
resolved after layout.

Only the System V ... no: only the Microsoft x64 (Win64) ABI is targeted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# registers
# ---------------------------------------------------------------------------

REGS = {
    "rax": 0, "rcx": 1, "rdx": 2, "rbx": 3,
    "rsp": 4, "rbp": 5, "rsi": 6, "rdi": 7,
    "r8": 8, "r9": 9, "r10": 10, "r11": 11,
    "r12": 12, "r13": 13, "r14": 14, "r15": 15,
}

XMMS = {f"xmm{i}": i for i in range(16)}

# condition codes for Jcc / SETcc
CC = {
    "o": 0x0, "no": 0x1,
    "b": 0x2, "c": 0x2, "nae": 0x2,
    "ae": 0x3, "nb": 0x3, "nc": 0x3,
    "e": 0x4, "z": 0x4,
    "ne": 0x5, "nz": 0x5,
    "be": 0x6, "na": 0x6,
    "a": 0x7, "nbe": 0x7,
    "s": 0x8, "ns": 0x9,
    "p": 0xA, "pe": 0xA, "np": 0xB, "po": 0xB,
    "l": 0xC, "nge": 0xC,
    "ge": 0xD, "nl": 0xD,
    "le": 0xE, "ng": 0xE,
    "g": 0xF, "nle": 0xF,
}

GPR_POOL = ["rbx", "rsi", "rdi", "r12", "r13", "r14", "r15"]
GPR_SCRATCH = ["rax", "r10", "r11"]
XMM_POOL = [f"xmm{i}" for i in range(6, 16)]
XMM_SCRATCH = [f"xmm{i}" for i in range(0, 6)]


def r(name: str | int) -> int:
    if isinstance(name, int):
        assert 0 <= name <= 15
        return name
    return REGS[name]


def x(name: str | int) -> int:
    if isinstance(name, int):
        assert 0 <= name <= 15
        return name
    return XMMS[name]


@dataclass
class Fixup:
    kind: str           # "rel32" | "abs64"
    pos: int            # offset of the field to patch
    target: object      # label name (rel32) or ("data", offset) (abs64)


@dataclass
class Asm:
    """A single code section with labels, plus pseudo-instructions."""

    b: bytearray = field(default_factory=bytearray)
    labels: dict[str, int] = field(default_factory=dict)
    fixups: list[Fixup] = field(default_factory=list)

    # -- low level ---------------------------------------------------------
    def here(self) -> int:
        return len(self.b)

    def byte(self, *vals: int) -> None:
        self.b.extend(vals)

    def u32(self, v: int) -> None:
        self.b.extend((v & 0xFFFFFFFF).to_bytes(4, "little"))

    def i32(self, v: int) -> None:
        self.b.extend((v & 0xFFFFFFFF).to_bytes(4, "little"))

    def u64(self, v: int) -> None:
        self.b.extend((v & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little"))

    def bind(self, name: str) -> None:
        assert name not in self.labels, f"duplicate label {name}"
        self.labels[name] = len(self.b)

    def align(self, n: int = 16) -> None:
        while len(self.b) % n:
            self.b.append(0xCC)

    def _rex(self, w: int = 0, rr: int = 0, xx: int = 0, bb: int = 0,
             force: bool = False) -> None:
        v = 0x40 | (w << 3) | (((rr >> 3) & 1) << 2) | (((xx >> 3) & 1) << 1) | ((bb >> 3) & 1)
        if v != 0x40 or force:
            self.b.append(v)

    def _modrm(self, mod: int, reg: int, rm: int) -> None:
        self.b.append(((mod & 3) << 6) | ((reg & 7) << 3) | (rm & 7))

    def _sib(self, scale: int, index: int, base: int) -> None:
        self.b.append(((scale & 3) << 6) | ((index & 7) << 3) | (base & 7))

    def _mem(self, reg: int, base: int, disp: int = 0,
             index: int | None = None, scale: int = 0) -> None:
        """Encode a ModRM (+SIB +disp) memory operand for [base+index*scale+disp]."""
        b = base & 7
        if index is None:
            if b == 4:  # rsp/r12 needs a SIB byte
                self._modrm(0b00 if disp == 0 else (0b01 if -128 <= disp <= 127 else 0b10), reg, 4)
                self._sib(0, 4, b)
                if disp == 0:
                    pass
                elif -128 <= disp <= 127:
                    self.b.append(disp & 0xFF)
                else:
                    self.i32(disp)
                return
            if b == 5 and disp == 0:
                # rbp/r13 cannot use mod=00 without disp
                self._modrm(0b01, reg, b)
                self.b.append(0)
                return
        if disp == 0:
            mod = 0b00
        elif -128 <= disp <= 127:
            mod = 0b01
        else:
            mod = 0b10
        if index is None:
            self._modrm(mod, reg, b)
        else:
            self._modrm(mod, reg, 4)
            self._sib(scale, index, b)
        if mod == 0b01:
            self.b.append(disp & 0xFF)
        elif mod == 0b10:
            self.i32(disp)

    def _mem_rip(self, reg: int, disp: int = 0) -> None:
        self._modrm(0b00, reg, 5)
        self.i32(disp)

    # -- labels / fixups ---------------------------------------------------
    def rel32(self, target: str) -> None:
        pos = len(self.b)
        self.u32(0)
        self.fixups.append(Fixup("rel32", pos, target))

    def abs64(self, target: tuple[str, int]) -> None:
        pos = len(self.b)
        self.u64(0)
        self.fixups.append(Fixup("abs64", pos, target))

    def resolve(self, image_base: int, rvas: dict[str, int]) -> None:
        for f in self.fixups:
            if f.kind == "rel32":
                if f.target not in self.labels:
                    raise KeyError(f"undefined label {f.target!r}")
                rel = self.labels[f.target] - (f.pos + 4)
                if not -2**31 <= rel < 2**31:
                    raise OverflowError(f"rel32 out of range for {f.target!r}: {rel}")
                self.b[f.pos:f.pos + 4] = (rel & 0xFFFFFFFF).to_bytes(4, "little")
            elif f.kind == "abs64":
                sec, off = f.target  # type: ignore[misc]
                if sec == "text":
                    addr = image_base + rvas["text"] + off
                else:
                    addr = image_base + rvas[sec] + off
                self.b[f.pos:f.pos + 8] = (addr & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
            else:  # pragma: no cover
                raise ValueError(f.kind)
        self.fixups.clear()

    # -- data movement -----------------------------------------------------
    def mov_rr(self, dst: str | int, src: str | int) -> None:
        d, s_ = r(dst), r(src)
        self._rex(1, s_, 0, d)
        self.byte(0x89)
        self._modrm(0b11, s_, d)

    def mov_ri(self, dst: str | int, imm: int) -> None:
        d = r(dst)
        imm &= 0xFFFFFFFFFFFFFFFF
        signed = imm - (1 << 64) if imm >= (1 << 63) else imm
        if -2**31 <= signed <= 2**31 - 1:
            self._rex(1, 0, 0, d)
            self.byte(0xC7)
            self._modrm(0b11, 0, d)
            self.i32(signed)
        else:
            self._rex(1, 0, 0, d)
            self.byte(0xB8 + (d & 7))
            self.u64(imm)

    def mov_abs64(self, dst: str | int, target: tuple[str, int]) -> None:
        """mov dst, <absolute address of target>; patched by resolve()."""
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xB8 + (d & 7))
        self.abs64(target)

    def mov_rm(self, dst: str | int, base: str | int, disp: int = 0) -> None:
        d, bb = r(dst), r(base)
        self._rex(1, d, 0, bb)
        self.byte(0x8B)
        self._mem(d, bb, disp)

    def mov_mr(self, base: str | int, disp: int, src: str | int) -> None:
        bb, s_ = r(base), r(src)
        self._rex(1, s_, 0, bb)
        self.byte(0x89)
        self._mem(s_, bb, disp)

    def mov_mi(self, base: str | int, disp: int, imm: int) -> None:
        bb = r(base)
        self._rex(1, 0, 0, bb)
        self.byte(0xC7)
        self._mem(0, bb, disp)
        self.i32(imm)

    def mov_mi8(self, base: str | int, disp: int, imm: int) -> None:
        bb = r(base)
        self._rex(0, 0, 0, bb, force=True)
        self.byte(0xC6)
        self._mem(0, bb, disp)
        self.byte(imm & 0xFF)

    def mov_mr8(self, base: str | int, src: str | int) -> None:
        """mov byte ptr [base], src8  (base register, no displacement)"""
        bb, s_ = r(base), r(src)
        self._rex(0, s_, 0, bb, force=True)
        self.byte(0x88)
        self._modrm(0b00, s_, bb)

    def mov_mr_ind(self, base: str | int, src: str | int) -> None:
        """mov qword ptr [base], src"""
        bb, s_ = r(base), r(src)
        self._rex(1, s_, 0, bb)
        self.byte(0x89)
        self._modrm(0b00, s_, bb)

    def mov_mr_xmm(self, base: str | int, disp: int, src: str | int) -> None:
        bb, s_ = r(base), x(src)
        self.byte(0xF2)
        self._rex(0, s_, 0, bb)
        self.byte(0x0F, 0x11)
        self._mem(s_, bb, disp)

    def mov_rm_xmm(self, dst: str | int, base: str | int, disp: int = 0) -> None:
        d, bb = x(dst), r(base)
        self.byte(0xF2)
        self._rex(0, d, 0, bb)
        self.byte(0x0F, 0x10)
        self._mem(d, bb, disp)

    def movsd_rr(self, dst: str | int, src: str | int) -> None:
        d, s_ = x(dst), x(src)
        self.byte(0xF2)
        self._rex(0, d, 0, s_)
        self.byte(0x0F, 0x10)
        self._modrm(0b11, d, s_)

    def movq_xmm_r(self, dst: str | int, src: str | int) -> None:
        """movq xmm, r64  (low 64 bits)"""
        d, s_ = x(dst), r(src)
        self.byte(0x66)
        self._rex(1, d, 0, s_)
        self.byte(0x0F, 0x6E)
        self._modrm(0b11, d, s_)

    def movq_r_xmm(self, dst: str | int, src: str | int) -> None:
        """movq r64, xmm"""
        d, s_ = r(dst), x(src)
        self.byte(0x66)
        self._rex(1, s_, 0, d)
        self.byte(0x0F, 0x7E)
        self._modrm(0b11, s_, d)

    def lea(self, dst: str | int, base: str | int, disp: int = 0) -> None:
        d, bb = r(dst), r(base)
        self._rex(1, d, 0, bb)
        self.byte(0x8D)
        self._mem(d, bb, disp)

    def movzx8(self, dst: str | int, src: str | int) -> None:
        d, s_ = r(dst), r(src)
        self._rex(1, d, 0, s_)
        self.byte(0x0F, 0xB6)
        self._modrm(0b11, d, s_)

    def movzx8_rm(self, dst: str | int, base: str | int, disp: int = 0) -> None:
        d, bb = r(dst), r(base)
        self._rex(1, d, 0, bb)
        self.byte(0x0F, 0xB6)
        self._mem(d, bb, disp)

    # -- integer arithmetic ------------------------------------------------
    def _alu_rr(self, op: int, dst: str | int, src: str | int) -> None:
        d, s_ = r(dst), r(src)
        self._rex(1, s_, 0, d)
        self.byte(op)
        self._modrm(0b11, s_, d)

    def _alu_ri(self, ext: int, dst: str | int, imm: int) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0x81)
        self._modrm(0b11, ext, d)
        self.i32(imm)

    def add_rr(self, dst, src): self._alu_rr(0x01, dst, src)
    def or_rr(self, dst, src): self._alu_rr(0x09, dst, src)
    def and_rr(self, dst, src): self._alu_rr(0x21, dst, src)
    def sub_rr(self, dst, src): self._alu_rr(0x29, dst, src)
    def xor_rr(self, dst, src): self._alu_rr(0x31, dst, src)
    def cmp_rr(self, a, b): self._alu_rr(0x39, a, b)
    def test_rr(self, a, b): self._alu_rr(0x85, a, b)

    def add_ri(self, dst, imm): self._alu_ri(0, dst, imm)
    def or_ri(self, dst, imm): self._alu_ri(1, dst, imm)
    def and_ri(self, dst, imm): self._alu_ri(4, dst, imm)
    def sub_ri(self, dst, imm): self._alu_ri(5, dst, imm)
    def xor_ri(self, dst, imm): self._alu_ri(6, dst, imm)
    def cmp_ri(self, a, imm): self._alu_ri(7, a, imm)

    def imul_rr(self, dst, src) -> None:
        d, s_ = r(dst), r(src)
        self._rex(1, d, 0, s_)
        self.byte(0x0F, 0xAF)
        self._modrm(0b11, d, s_)

    def imul_rri(self, dst, src, imm: int) -> None:
        d, s_ = r(dst), r(src)
        self._rex(1, d, 0, s_)
        self.byte(0x69)
        self._modrm(0b11, d, s_)
        self.i32(imm)

    def idiv_r(self, src) -> None:
        s_ = r(src)
        self._rex(1, 0, 0, s_)
        self.byte(0xF7)
        self._modrm(0b11, 7, s_)

    def div_r(self, src) -> None:
        s_ = r(src)
        self._rex(1, 0, 0, s_)
        self.byte(0xF7)
        self._modrm(0b11, 6, s_)

    def neg_r(self, dst) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xF7)
        self._modrm(0b11, 3, d)

    def cqo(self) -> None:
        self.byte(0x48, 0x99)

    def shl_cl(self, dst) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xD3)
        self._modrm(0b11, 4, d)

    def sar_cl(self, dst) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xD3)
        self._modrm(0b11, 7, d)

    def shr_cl(self, dst) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xD3)
        self._modrm(0b11, 5, d)

    def shl_imm(self, dst, imm: int) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xC1)
        self._modrm(0b11, 4, d)
        self.byte(imm & 0xFF)

    def sar_imm(self, dst, imm: int) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xC1)
        self._modrm(0b11, 7, d)
        self.byte(imm & 0xFF)

    def shr_imm(self, dst, imm: int) -> None:
        d = r(dst)
        self._rex(1, 0, 0, d)
        self.byte(0xC1)
        self._modrm(0b11, 5, d)
        self.byte(imm & 0xFF)

    # -- control flow ------------------------------------------------------
    def jmp(self, label: str) -> None:
        self.byte(0xE9)
        self.rel32(label)

    def jcc(self, cc: str, label: str) -> None:
        self.byte(0x0F, 0x80 | CC[cc])
        self.rel32(label)

    def call(self, label: str) -> None:
        self.byte(0xE8)
        self.rel32(label)

    def call_r(self, reg) -> None:
        d = r(reg)
        self._rex(0, 0, 0, d)
        self.byte(0xFF)
        self._modrm(0b11, 2, d)

    def call_ptr(self, base: str | int) -> None:
        """call qword ptr [base]"""
        d = r(base)
        self._rex(0, 0, 0, d)
        self.byte(0xFF)
        self._modrm(0b00, 2, d)

    def call_iat(self, target: tuple[str, int]) -> None:
        """call qword ptr [absolute IAT slot] via a scratch register."""
        self.mov_abs64("r11", target)
        self.call_ptr("r11")

    def jmp_r(self, reg) -> None:
        d = r(reg)
        self._rex(0, 0, 0, d)
        self.byte(0xFF)
        self._modrm(0b11, 4, d)

    def ret(self) -> None:
        self.byte(0xC3)

    def setcc(self, cc: str, dst) -> None:
        d = r(dst)
        self._rex(0, 0, 0, d, force=(d >= 4 and d <= 7))
        self.byte(0x0F, 0x90 | CC[cc])
        self._modrm(0b11, 0, d)

    def push_r(self, reg) -> None:
        d = r(reg)
        if d >= 8:
            self.byte(0x41)
        self.byte(0x50 + (d & 7))

    def pop_r(self, reg) -> None:
        d = r(reg)
        if d >= 8:
            self.byte(0x41)
        self.byte(0x58 + (d & 7))

    def sub_rsp(self, imm: int) -> None:
        self._rex(1, 0, 0, 4)
        self.byte(0x81)
        self._modrm(0b11, 5, 4)
        self.i32(imm)

    def add_rsp(self, imm: int) -> None:
        self._rex(1, 0, 0, 4)
        self.byte(0x81)
        self._modrm(0b11, 0, 4)
        self.i32(imm)

    def and_rsp(self, imm: int) -> None:
        self._rex(1, 0, 0, 4)
        self.byte(0x81)
        self._modrm(0b11, 4, 4)
        self.i32(imm)

    # -- SSE2 scalar double ------------------------------------------------
    def _sse_rr(self, prefix: int, opcode: int, dst, src) -> None:
        # Mandatory prefix, then REX, then the 0F escape: F2 REX 0F 58 /r
        d, s_ = x(dst), x(src)
        self.byte(prefix)
        self._rex(0, d, 0, s_)
        self.byte(0x0F, opcode)
        self._modrm(0b11, d, s_)

    def addsd(self, dst, src): self._sse_rr(0xF2, 0x58, dst, src)
    def subsd(self, dst, src): self._sse_rr(0xF2, 0x5C, dst, src)
    def mulsd(self, dst, src): self._sse_rr(0xF2, 0x59, dst, src)
    def divsd(self, dst, src): self._sse_rr(0xF2, 0x5E, dst, src)
    def sqrtsd(self, dst, src): self._sse_rr(0xF2, 0x51, dst, src)
    def ucomisd(self, a, b): self._sse_rr(0x66, 0x2E, a, b)
    def xorpd(self, dst, src): self._sse_rr(0x66, 0x57, dst, src)

    def cmpsd(self, dst, src, imm: int) -> None:
        """cmpsd dst, src, imm8  (0=eq 1=lt 2=le 3=unord 4=neq 5=nlt 6=nle 7=ord)"""
        d, s_ = x(dst), x(src)
        self.b.extend(b"\xF2")
        self._rex(0, d, 0, s_)
        self.byte(0x0F, 0xC2)
        self._modrm(0b11, d, s_)
        self.byte(imm & 0xFF)

    def cvtsi2sd(self, dst_xmm, src_gpr) -> None:
        d, s_ = x(dst_xmm), r(src_gpr)
        self.byte(0xF2)
        self._rex(1, d, 0, s_)
        self.byte(0x0F, 0x2A)
        self._modrm(0b11, d, s_)

    def cvttsd2si(self, dst_gpr, src_xmm) -> None:
        d, s_ = r(dst_gpr), x(src_xmm)
        self.byte(0xF2)
        self._rex(1, d, 0, s_)
        self.byte(0x0F, 0x2C)
        self._modrm(0b11, d, s_)

    def pxor(self, dst, src) -> None:
        self._sse_rr(0x66, 0xEF, dst, src)
