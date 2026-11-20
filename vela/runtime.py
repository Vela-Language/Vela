"""Vela runtime library.

Hand-written x86-64 code (emitted through our own encoder) implementing the
small set of primitives the native profile needs:

  * print int / float / bool / None / str / newline
  * Python-semantics floor division and modulo for int64
  * integer exponentiation with overflow detection
  * runtime diagnostics (stderr + ExitProcess)

All routines obey the Microsoft x64 ABI and preserve the non-volatile
registers used by the register allocator (rbx, rsi, rdi, r12-r15, xmm6-xmm15);
they only ever touch volatile registers.
"""
from __future__ import annotations

from .pe import DataBuilder, ImportTable
from .x64 import Asm

KERNEL32 = "kernel32.dll"
MSVCRT = "msvcrt.dll"


def make_imports() -> ImportTable:
    return ImportTable({
        KERNEL32: ["GetStdHandle", "WriteFile", "ExitProcess"],
        MSVCRT: ["sprintf"],
    })


class Runtime:
    RT_INIT = "rt_init"
    PRINT_INT = "rt_print_int"
    PRINT_FLOAT = "rt_print_float"
    PRINT_STR = "rt_print_str"
    PRINT_BOOL = "rt_print_bool"
    PRINT_NONE = "rt_print_none"
    PRINT_NL = "rt_print_nl"
    PRINT_SPACE = "rt_print_space"
    IDIV = "rt_idiv"
    IMOD = "rt_imod"
    IPOW = "rt_ipow"
    FAIL = "rt_fail"
    OVERFLOW = "rt_overflow"
    RANGE_ERROR = "rt_range_error"

    def __init__(self, imports: ImportTable, rdata: DataBuilder, data: DataBuilder) -> None:
        self.imp = imports
        self.rdata = rdata
        self.data = data

        self.handle_off = data.add(b"\x00" * 8, 8)
        self.err_handle_off = data.add(b"\x00" * 8, 8)
        self.fmt_g_off = rdata.add(b"%.15g\x00", 1)
        self.true_off = rdata.add_str_descriptor("True")
        self.false_off = rdata.add_str_descriptor("False")
        self.none_off = rdata.add_str_descriptor("None")
        # CPython's sys.stdout is a text stream: print() writes os.linesep, so
        # on Windows a newline is CRLF.  Match that byte-for-byte.
        self.nl_off = rdata.add_str_descriptor("\r\n")
        self.space_off = rdata.add_str_descriptor(" ")
        self.msg_divzero = rdata.add_str_descriptor(
            "Vela runtime error: integer division by zero\n")
        self.msg_overflow = rdata.add_str_descriptor(
            "Vela runtime error: integer overflow (int64 range exceeded)\n")
        self.msg_negexp = rdata.add_str_descriptor(
            "Vela runtime error: negative integer exponent is not supported "
            "by the native int profile\n")
        self.msg_stepzero = rdata.add_str_descriptor(
            "Vela runtime error: range() step must not be zero\n")

    # -- helpers -----------------------------------------------------------
    def _call(self, a: Asm, dll: str, func: str) -> None:
        a.call_iat(("idata", self.imp.slot(dll, func)))

    def _load_handle(self, a: Asm, reg: str, err: bool = False) -> None:
        off = self.err_handle_off if err else self.handle_off
        a.mov_abs64("r11", ("data", off))
        a.mov_rm(reg, "r11")

    def _fail(self, a: Asm, msg_off: int) -> None:
        a.mov_abs64("rcx", ("rdata", msg_off))
        a.jmp(self.FAIL)

    # -- code emission -----------------------------------------------------
    def emit(self, a: Asm) -> None:
        self._emit_init(a)
        self._emit_print_int(a)
        self._emit_print_str(a)
        self._emit_print_float(a)
        self._emit_print_const(a)
        self._emit_idiv(a)
        self._emit_imod(a)
        self._emit_ipow(a)
        self._emit_fail(a)
        self._emit_stubs(a)

    def _emit_init(self, a: Asm) -> None:
        a.bind(self.RT_INIT)
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(32)
        a.mov_ri("rcx", -11)          # STD_OUTPUT_HANDLE
        self._call(a, KERNEL32, "GetStdHandle")
        a.mov_abs64("r11", ("data", self.handle_off))
        a.mov_mr_ind("r11", "rax")
        a.mov_ri("rcx", -12)          # STD_ERROR_HANDLE
        self._call(a, KERNEL32, "GetStdHandle")
        a.mov_abs64("r11", ("data", self.err_handle_off))
        a.mov_mr_ind("r11", "rax")
        a.mov_rr("rsp", "rbp")
        a.pop_r("rbp")
        a.ret()

    def _emit_print_int(self, a: Asm) -> None:
        a.bind(self.PRINT_INT)
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(80)
        a.mov_rr("rax", "rcx")
        a.mov_rr("r8", "rbp")
        a.mov_rr("r9", "rbp")
        a.xor_rr("r10", "r10")
        a.test_rr("rax", "rax")
        a.jcc("ns", "pi_pos")
        a.mov_ri("r10", 1)
        a.neg_r("rax")
        a.bind("pi_pos")
        a.mov_ri("r11", 10)
        a.bind("pi_loop")
        a.xor_rr("rdx", "rdx")
        a.div_r("r11")
        a.add_ri("rdx", 0x30)
        a.sub_ri("r8", 1)
        a.mov_mr8("r8", "rdx")
        a.test_rr("rax", "rax")
        a.jcc("ne", "pi_loop")
        a.test_rr("r10", "r10")
        a.jcc("e", "pi_nosign")
        a.sub_ri("r8", 1)
        a.mov_mi8("r8", 0, 0x2D)
        a.bind("pi_nosign")
        a.mov_rr("rdx", "r8")
        a.mov_rr("r8", "r9")
        a.sub_rr("r8", "rdx")
        self._load_handle(a, "rcx")
        a.lea("r9", "rbp", -32)
        a.mov_mi("rsp", 32, 0)
        self._call(a, KERNEL32, "WriteFile")
        a.mov_rr("rsp", "rbp")
        a.pop_r("rbp")
        a.ret()

    def _emit_print_str(self, a: Asm) -> None:
        a.bind(self.PRINT_STR)
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(64)
        a.mov_rr("r10", "rcx")
        a.add_ri("r10", 8)
        a.mov_rm("r8", "rcx", 0)
        self._load_handle(a, "rcx")
        a.mov_rr("rdx", "r10")
        a.lea("r9", "rbp", -8)
        a.mov_mi("rsp", 32, 0)
        self._call(a, KERNEL32, "WriteFile")
        a.mov_rr("rsp", "rbp")
        a.pop_r("rbp")
        a.ret()

    def _emit_print_float(self, a: Asm) -> None:
        a.bind(self.PRINT_FLOAT)
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(128)
        a.lea("rcx", "rbp", -64)
        a.mov_abs64("rdx", ("rdata", self.fmt_g_off))
        a.movq_r_xmm("r8", "xmm0")
        a.movsd_rr("xmm2", "xmm0")
        self._call(a, MSVCRT, "sprintf")
        a.mov_rr("r8", "rax")
        a.lea("rdx", "rbp", -64)
        # Python prints integral floats as "7.0", C's %g prints "7": append
        # ".0" when the formatted value has no '.', exponent, inf or nan.
        a.mov_rr("r10", "rdx")
        a.bind("pf_scan")
        a.movzx8_rm("r11", "r10")
        a.test_rr("r11", "r11")
        a.jcc("e", "pf_append")
        for ch in (0x2E, 0x65, 0x45, 0x6E, 0x69):
            a.cmp_ri("r11", ch)
            a.jcc("e", "pf_done")
        a.add_ri("r10", 1)
        a.jmp("pf_scan")
        a.bind("pf_append")
        a.mov_mi8("r10", 0, 0x2E)
        a.mov_mi8("r10", 1, 0x30)
        a.add_ri("r8", 2)
        a.bind("pf_done")
        self._load_handle(a, "rcx")
        a.lea("r9", "rbp", -72)
        a.mov_mi("rsp", 32, 0)
        self._call(a, KERNEL32, "WriteFile")
        a.mov_rr("rsp", "rbp")
        a.pop_r("rbp")
        a.ret()

    def _emit_print_const(self, a: Asm) -> None:
        for label, off in (
            (self.PRINT_BOOL, None),
            (self.PRINT_NONE, self.none_off),
            (self.PRINT_NL, self.nl_off),
            (self.PRINT_SPACE, self.space_off),
        ):
            a.bind(label)
            if label == self.PRINT_BOOL:
                a.test_rr("rcx", "rcx")
                a.jcc("e", "pb_false")
                a.mov_abs64("rcx", ("rdata", self.true_off))
                a.jmp(self.PRINT_STR)
                a.bind("pb_false")
                a.mov_abs64("rcx", ("rdata", self.false_off))
                a.jmp(self.PRINT_STR)
            else:
                a.mov_abs64("rcx", ("rdata", off))
                a.jmp(self.PRINT_STR)

    def _emit_idiv(self, a: Asm) -> None:
        a.bind(self.IDIV)
        a.mov_rr("r8", "rdx")
        a.test_rr("r8", "r8")
        a.jcc("e", "id_zero")
        a.mov_ri("r11", -(2 ** 63))
        a.cmp_rr("rcx", "r11")
        a.jcc("ne", "id_ok")
        a.cmp_ri("r8", -1)
        a.jcc("e", "id_ovf")
        a.bind("id_ok")
        a.mov_rr("rax", "rcx")
        a.cqo()
        a.idiv_r("r8")
        a.test_rr("rdx", "rdx")
        a.jcc("e", "id_done")
        a.mov_rr("r11", "rdx")
        a.xor_rr("r11", "r8")
        a.jcc("ns", "id_done")
        a.sub_ri("rax", 1)
        a.bind("id_done")
        a.ret()
        a.bind("id_zero")
        self._fail(a, self.msg_divzero)
        a.bind("id_ovf")
        self._fail(a, self.msg_overflow)

    def _emit_imod(self, a: Asm) -> None:
        a.bind(self.IMOD)
        a.mov_rr("r8", "rdx")
        a.test_rr("r8", "r8")
        a.jcc("e", "im_zero")
        a.mov_ri("r11", -(2 ** 63))
        a.cmp_rr("rcx", "r11")
        a.jcc("ne", "im_ok")
        a.cmp_ri("r8", -1)
        a.jcc("e", "im_ovf")
        a.bind("im_ok")
        a.mov_rr("rax", "rcx")
        a.cqo()
        a.idiv_r("r8")
        a.test_rr("rdx", "rdx")
        a.jcc("e", "im_done")
        a.mov_rr("r11", "rdx")
        a.xor_rr("r11", "r8")
        a.jcc("ns", "im_done")
        a.add_rr("rdx", "r8")
        a.bind("im_done")
        a.mov_rr("rax", "rdx")
        a.ret()
        a.bind("im_zero")
        self._fail(a, self.msg_divzero)
        a.bind("im_ovf")
        self._fail(a, self.msg_overflow)

    def _emit_ipow(self, a: Asm) -> None:
        a.bind(self.IPOW)
        a.mov_rr("r8", "rcx")
        a.test_rr("rdx", "rdx")
        a.jcc("s", "ip_neg")
        a.mov_rr("r9", "rdx")
        a.mov_ri("rax", 1)
        a.bind("ip_loop")
        a.test_rr("r9", "r9")
        a.jcc("e", "ip_done")
        a.mov_rr("r10", "r9")
        a.and_ri("r10", 1)
        a.jcc("e", "ip_sq")
        a.imul_rr("rax", "r8")
        a.jcc("o", "ip_ovf")
        a.bind("ip_sq")
        a.shr_imm("r9", 1)
        a.jcc("e", "ip_done")
        a.imul_rr("r8", "r8")
        a.jcc("o", "ip_ovf")
        a.jmp("ip_loop")
        a.bind("ip_done")
        a.ret()
        a.bind("ip_neg")
        self._fail(a, self.msg_negexp)
        a.bind("ip_ovf")
        self._fail(a, self.msg_overflow)

    def _emit_fail(self, a: Asm) -> None:
        a.bind(self.FAIL)
        a.push_r("rbp")
        a.mov_rr("rbp", "rsp")
        a.sub_rsp(64)
        a.mov_rr("r10", "rcx")
        a.add_ri("r10", 8)
        a.mov_rm("r8", "rcx", 0)
        self._load_handle(a, "rcx", err=True)
        a.mov_rr("rdx", "r10")
        a.lea("r9", "rbp", -8)
        a.mov_mi("rsp", 32, 0)
        self._call(a, KERNEL32, "WriteFile")
        a.mov_ri("rcx", 1)
        self._call(a, KERNEL32, "ExitProcess")
        a.bind("rt_fail_hang")
        a.jmp("rt_fail_hang")

    def _emit_stubs(self, a: Asm) -> None:
        a.bind(self.OVERFLOW)
        self._fail(a, self.msg_overflow)
        a.bind(self.RANGE_ERROR)
        self._fail(a, self.msg_stepzero)
