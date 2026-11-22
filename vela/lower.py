"""AST -> Vela IR lowering.

The lowering is deliberately simple and explicit: each source variable becomes
one virtual register, expressions become typed instructions, and Python's
short-circuit / conditional expressions are turned into ordinary control flow.
"""
from __future__ import annotations

import ast

from .analyze import Analyzer, FuncInfo, is_func_type, is_numeric, norm_numeric
from .errors import UnsupportedError, VelaCompileError, VelaTypeError
from .ir import (BOOL, FLOAT, GPR_TYPES, INT, NONE, OBJ, Program, STR, UNKNOWN,
                 Function, VReg)
from .pe import DataBuilder

PY_NONE = None


class Lowerer:
    def __init__(self, analyzer: Analyzer, rdata: DataBuilder,
                 filename: str = "<input>", boxed: bool = False,
                 dynamic_int: bool = False) -> None:
        self.an = analyzer
        self.rdata = rdata
        self.filename = filename
        self.boxed = boxed
        self.dynamic_int = dynamic_int
        self.mutable_globals = set(getattr(analyzer, "mutable_globals", set()))
        self.global_slots = {name: i for i, name in enumerate(sorted(self.mutable_globals))}
        self.strings: dict[str, int] = {}
        self.wrapped_funcs: set[str] = set()

    # -- public ------------------------------------------------------------
    def run(self) -> Program:
        prog = Program(strings=self.rdata)
        by_name: dict[str, Function] = {}
        for info in self.an.funcs.values():
            fn = self.lower_function(info)
            by_name[info.name] = fn
            prog.functions.append(fn)
        prog.main = self.lower_main()
        for qual in sorted(self.wrapped_funcs):
            if qual in by_name:
                by_name[qual].no_inline = True
                prog.functions.append(self.lower_wrapper(self.an.funcs[qual]))
        prog.functions.append(prog.main)
        return prog

    def lower_wrapper(self, info: FuncInfo) -> Function:
        fn = Function("fn_wrap_" + info.name, OBJ)
        fn.no_inline = True
        argv = fn.new_vreg(OBJ, "argv")
        fn.params.append(argv)
        fn.entry.add("arg", argv, 0)
        call_args: list[VReg] = []
        for i in range(len(info.params)):
            idx = fn.new_vreg(INT, f"idx_{i}")
            fn.entry.add("const", idx, i)
            elem = fn.new_vreg(OBJ, f"arg_{i}")
            fn.entry.add("call", elem, "rt_list_get", (argv, idx))
            call_args.append(elem)
        ret = info.ret_type or NONE
        if ret == NONE:
            fn.entry.add("call", None, info.label, tuple(call_args))
            out = fn.new_vreg(OBJ, "none_ret")
            fn.entry.add("call", out, "rt_box_none", ())
            fn.entry.add("ret", None, out)
        else:
            out = fn.new_vreg(OBJ, "wrap_ret")
            fn.entry.add("call", out, info.label, tuple(call_args))
            fn.entry.add("ret", None, out)
        return fn

    def lower_function(self, info: FuncInfo) -> Function:
        native_ret = info.ret_type or NONE
        if (native_ret in ("list", "dict", "tuple", "set", "bigint", "bytes", "strlist", "module", "modules")
                or is_func_type(native_ret)):
            native_ret = OBJ
        boxed_ret = self.boxed and (
            native_ret in (INT, FLOAT, BOOL, STR, "bigint")
            or (self.dynamic_int and native_ret == INT))
        fn = Function(info.label, OBJ if boxed_ret else native_ret)
        if any(is_func_type(t) for t in info.param_types) or is_func_type(info.ret_type):
            fn.no_inline = True
        b = FuncBuilder(self, fn, info.name, info.var_types, info.ret_type or NONE)
        b.globals = set(info.globals)
        b.param_names = set(info.params)
        for i, (pname, pty) in enumerate(zip(info.params, info.param_types)):
            if self.boxed and pty in (INT, FLOAT, BOOL, STR):
                v = fn.new_vreg(OBJ, pname + "_obj")
                fn.params.append(v)
                fn.entry.add("arg", v, i)
            elif self.boxed and (pty in ("list", "dict", "tuple", "set", "bigint", "bytes", "strlist", "module", "modules")
                                 or is_func_type(pty)):
                v = fn.new_vreg(OBJ, pname + "_obj")
                fn.params.append(v)
                fn.entry.add("arg", v, i)
            else:
                v = fn.new_vreg(pty, pname)
                fn.params.append(v)
                fn.entry.add("arg", v, i)
            b.locals[pname] = v
            b.assigned.add(pname)
        if self.boxed:
            fname = fn.new_vreg(STR, "func_name")
            fn.entry.add("str", fname, self.string_offset(info.name))
            fn.entry.add("call", None, "rt_enter", (fname,))
        for stmt in info.node.body:
            b.stmt(stmt)
        b.finish()
        return fn

    def lower_main(self) -> Function:
        fn = Function("fn___vela_main__", NONE)
        b = FuncBuilder(self, fn, "__main__", self.an.main_vars, NONE)
        b.globals = set(self.mutable_globals)
        if self.boxed:
            mname = fn.new_vreg(STR, "func_name")
            fn.entry.add("str", mname, self.string_offset("__main__"))
            fn.entry.add("call", None, "rt_enter", (mname,))
            fdesc = fn.new_vreg(STR, "file_name")
            fn.entry.add("str", fdesc, self.string_offset(self.filename))
            fn.entry.add("call", None, "rt_set_file", (fdesc,))
        for stmt in self.an.tree.body:
            if isinstance(stmt, ast.FunctionDef):
                continue
            b.stmt(stmt)
        b.finish()
        return fn

    def string_offset(self, s: str) -> int:
        off = self.strings.get(s)
        if off is None:
            off = self.rdata.add_str_descriptor(s)
            self.strings[s] = off
        return off


class FuncBuilder:
    def __init__(self, lowerer: Lowerer, fn: Function, name: str,
                 var_types: dict[str, str], ret_ty: str) -> None:
        self.lw = lowerer
        self.fn = fn
        self.name = name
        self.var_types = dict(var_types)
        self.ret_ty = ret_ty
        self.globals: set[str] = set()
        self.finally_stack: list[list[ast.stmt]] = []
        self.param_names: set[str] = set()
        self.big_consts: dict[str, int] = {}
        self.dyn = self.lw.dynamic_int
        self.cur = fn.entry
        self.locals: dict[str, VReg] = {}
        self.assigned: set[str] = set()
        self.loops: list[tuple[str, str]] = []

    # -- infrastructure ----------------------------------------------------
    def new_block(self, hint: str = "") -> "BlockRef":
        return BlockRef(self, self.fn.new_block(hint))

    def use(self, blk: "BlockRef") -> None:
        self.cur = blk.block

    def const_int(self, v: int) -> VReg:
        r = self.fn.new_vreg(INT)
        self.cur.add("const", r, v)
        return r

    def const_bool(self, v: bool) -> VReg:
        r = self.fn.new_vreg(BOOL)
        self.cur.add("const", r, 1 if v else 0)
        return r

    def const_float(self, v: float) -> VReg:
        r = self.fn.new_vreg(FLOAT)
        self.cur.add("const", r, v)
        return r

    def ty(self, node: ast.expr) -> str:
        t = getattr(node, "_vela_ty", None)
        return t or INT

    def coerce(self, v: VReg | None, want: str) -> VReg | None:
        if v is None:
            return None
        if v.ty == want:
            return v
        if v.ty == BOOL and want == INT:
            return v
        if v.ty in (INT, BOOL) and want == FLOAT:
            r = self.fn.new_vreg(FLOAT)
            self.cur.add("un", r, "i2f", v)
            return r
        if v.ty == INT and want == BOOL:
            r = self.fn.new_vreg(BOOL)
            self.cur.add("un", r, "bool", v)
            return r
        if self.dyn and want == INT and v.ty == OBJ:
            return v
        if self.dyn and want == BOOL and v.ty == OBJ:
            r = self.fn.new_vreg(BOOL)
            self.cur.add("call", r, "rt_is_true", (v,))
            return r
        if (want in ("list", "dict", "tuple", "set", "bigint", "bytes", "strlist", "module", "modules")
                or is_func_type(want)) and v.ty == OBJ:
            return v
        raise VelaTypeError(f"cannot convert {v.ty} to {want}")

    def dict_items_to_list(self, obj: VReg) -> VReg:
        dst = self.call_rt("rt_list_new", [self.const_int(0)], OBJ)
        length = self.fn.new_vreg(INT, "dict_len")
        self.cur.add("call", length, "rt_dict_len", (obj,))
        idx = self.fn.new_vreg(INT, "dict_i")
        self.cur.add("mov", idx, self.const_int(0))
        cond = self.new_block("di_cond")
        body = self.new_block("di_body")
        exit_b = self.new_block("di_exit")
        self.cur.add("jmp", None, cond.block.label)
        self.use(cond)
        c = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c, "lt", idx, length)
        self.cur.add("br", None, c, body.block.label, exit_b.block.label)
        self.use(body)
        item = self.call_rt("rt_list_new", [self.const_int(2)], OBJ)
        key = self.call_rt("rt_dict_key_at", [obj, idx], OBJ)
        self.cur.add("call", None, "rt_list_set", (item, self.const_int(0), key))
        val = self.call_rt("rt_dict_val_at", [obj, idx], OBJ)
        self.cur.add("call", None, "rt_list_set", (item, self.const_int(1), val))
        self.cur.add("call", None, "rt_tuple_type", (item,))
        self.cur.add("call", None, "rt_list_append", (dst, item))
        one = self.const_int(1)
        ni = self.fn.new_vreg(INT)
        self.cur.add("bin", ni, "add", idx, one)
        self.cur.add("mov", idx, ni)
        self.cur.add("jmp", None, cond.block.label)
        self.use(exit_b)
        return dst

    def dict_to_list(self, obj: VReg, values: bool) -> VReg:
        dst = self.call_rt("rt_list_new", [self.const_int(0)], OBJ)
        length = self.fn.new_vreg(INT, "dict_len")
        self.cur.add("call", length, "rt_dict_len", (obj,))
        idx = self.fn.new_vreg(INT, "dict_i")
        self.cur.add("mov", idx, self.const_int(0))
        cond = self.new_block("dl_cond")
        body = self.new_block("dl_body")
        exit_b = self.new_block("dl_exit")
        self.cur.add("jmp", None, cond.block.label)
        self.use(cond)
        c = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c, "lt", idx, length)
        self.cur.add("br", None, c, body.block.label, exit_b.block.label)
        self.use(body)
        label = "rt_dict_val_at" if values else "rt_dict_key_at"
        elem = self.call_rt(label, [obj, idx], OBJ)
        self.cur.add("call", None, "rt_list_append", (dst, elem))
        one = self.const_int(1)
        ni = self.fn.new_vreg(INT)
        self.cur.add("bin", ni, "add", idx, one)
        self.cur.add("mov", idx, ni)
        self.cur.add("jmp", None, cond.block.label)
        self.use(exit_b)
        return dst

    def set_merge(self, dst: VReg, src: VReg, filter_src: VReg | None) -> None:
        length = self.fn.new_vreg(INT, "set_len")
        self.cur.add("call", length, "rt_list_len", (src,))
        idx = self.fn.new_vreg(INT, "set_i")
        self.cur.add("mov", idx, self.const_int(0))
        cond = self.new_block("set_cond")
        body = self.new_block("set_body")
        addb = self.new_block("set_add")
        nxt = self.new_block("set_next")
        exit_b = self.new_block("set_exit")
        self.cur.add("jmp", None, cond.block.label)
        self.use(cond)
        c = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c, "lt", idx, length)
        self.cur.add("br", None, c, body.block.label, exit_b.block.label)
        self.use(body)
        elem = self.call_rt("rt_list_get", [src, idx], OBJ)
        if filter_src is not None:
            inc = self.call_rt("rt_contains", [filter_src, elem], BOOL)
            self.cur.add("br", None, inc, addb.block.label, nxt.block.label)
            self.use(addb)
        self.cur.add("call", None, "rt_set_add", (dst, elem))
        self.cur.add("jmp", None, nxt.block.label)
        self.use(nxt)
        one = self.const_int(1)
        ni = self.fn.new_vreg(INT)
        self.cur.add("bin", ni, "add", idx, one)
        self.cur.add("mov", idx, ni)
        self.cur.add("jmp", None, cond.block.label)
        self.use(exit_b)

    def bigint_divmod_ir(self, a: VReg, b: VReg, want_quotient: bool | None):
        self.cur.add("call", None, "rt_bigint_require",
                     (a, self.const_int(1)))
        self.cur.add("call", None, "rt_bigint_require",
                     (b, self.const_int(0)))
        zero = self.const_int(0)
        one = self.const_int(1)
        ten = self.call_rt("rt_box_int", [self.const_int(10)], OBJ)
        zobj = self.call_rt("rt_box_int", [zero], OBJ)
        q = self.fn.new_vreg(OBJ, "dvq")
        rem = self.fn.new_vreg(OBJ, "dvrem")
        self.cur.add("mov", q, zobj)
        self.cur.add("mov", rem, zobj)
        la = self.call_rt("rt_bigint_len", [a], INT)
        i = self.fn.new_vreg(INT, "dvi")
        self.cur.add("bin", i, "sub", la, one)
        outer = self.new_block("dv_outer")
        body = self.new_block("dv_body")
        check = self.new_block("dv_check")
        hit = self.new_block("dv_hit")
        miss = self.new_block("dv_miss")
        dec = self.new_block("dv_dec")
        app = self.new_block("dv_app")
        done = self.new_block("dv_done")
        self.cur.add("jmp", None, outer.block.label)
        self.use(outer)
        c = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c, "ge", i, zero)
        self.cur.add("br", None, c, body.block.label, done.block.label)
        self.use(body)
        d = self.call_rt("rt_bigint_digit_at", [a, i], INT)
        do = self.call_rt("rt_box_int", [d], OBJ)
        t1 = self.call_rt("rt_num_mul", [rem, ten], OBJ)
        t2 = self.call_rt("rt_num_add", [t1, do], OBJ)
        self.cur.add("mov", rem, t2)
        k = self.fn.new_vreg(INT, "dvk")
        qd = self.fn.new_vreg(INT, "dvqd")
        self.cur.add("mov", k, self.const_int(9))
        self.cur.add("mov", qd, zero)
        self.cur.add("jmp", None, check.block.label)
        self.use(check)
        ck = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", ck, "ge", k, zero)
        self.cur.add("br", None, ck, hit.block.label, app.block.label)
        self.use(hit)
        ko = self.call_rt("rt_box_int", [k], OBJ)
        bk = self.call_rt("rt_num_mul", [b, ko], OBJ)
        cv = self.call_rt("rt_num_cmp", [rem, bk], INT)
        ge = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", ge, "ge", cv, zero)
        self.cur.add("br", None, ge, miss.block.label, dec.block.label)
        self.use(miss)
        t3 = self.call_rt("rt_num_sub", [rem, bk], OBJ)
        self.cur.add("mov", rem, t3)
        self.cur.add("mov", qd, k)
        self.cur.add("jmp", None, app.block.label)
        self.use(dec)
        dk = self.fn.new_vreg(INT)
        self.cur.add("bin", dk, "sub", k, one)
        self.cur.add("mov", k, dk)
        self.cur.add("jmp", None, check.block.label)
        self.use(app)
        t4 = self.call_rt("rt_num_mul", [q, ten], OBJ)
        qo = self.call_rt("rt_box_int", [qd], OBJ)
        t5 = self.call_rt("rt_num_add", [t4, qo], OBJ)
        self.cur.add("mov", q, t5)
        ni = self.fn.new_vreg(INT)
        self.cur.add("bin", ni, "sub", i, one)
        self.cur.add("mov", i, ni)
        self.cur.add("jmp", None, outer.block.label)
        self.use(done)
        if want_quotient is None:
            return (q, rem)
        return q if want_quotient else rem

    def bigint_floor_divmod(self, a: VReg, b: VReg):
        zero = self.call_rt("rt_box_int", [self.const_int(0)], OBJ)
        one = self.call_rt("rt_box_int", [self.const_int(1)], OBJ)
        aa = self.call_rt("rt_num_abs", [a], OBJ)
        bb = self.call_rt("rt_num_abs", [b], OBJ)
        qmag, rmag = self.bigint_divmod_ir(aa, bb, None)
        ca = self.call_rt("rt_num_cmp", [a, zero], INT)
        cb = self.call_rt("rt_num_cmp", [b, zero], INT)
        na = self.fn.new_vreg(BOOL, "div_na")
        nb = self.fn.new_vreg(BOOL, "div_nb")
        self.cur.add("cmp", na, "lt", ca, self.const_int(0))
        self.cur.add("cmp", nb, "lt", cb, self.const_int(0))
        same = self.fn.new_vreg(BOOL, "div_same")
        self.cur.add("cmp", same, "eq", na, nb)
        qout = self.fn.new_vreg(OBJ, "div_q")
        rout = self.fn.new_vreg(OBJ, "div_r")
        same_b = self.new_block("div_same")
        diff_b = self.new_block("div_diff")
        rpos_b = self.new_block("div_rpos")
        rneg_b = self.new_block("div_rneg")
        qminus_b = self.new_block("div_qminus")
        qzero_b = self.new_block("div_qzero")
        bpos_b = self.new_block("div_bpos")
        bneg_b = self.new_block("div_bneg")
        final_b = self.new_block("div_final")
        self.cur.add("br", None, same, same_b.block.label, diff_b.block.label)

        self.use(same_b)
        self.cur.add("mov", qout, qmag)
        self.cur.add("br", None, nb, rneg_b.block.label, rpos_b.block.label)
        self.use(rpos_b)
        self.cur.add("mov", rout, rmag)
        self.cur.add("jmp", None, final_b.block.label)
        self.use(rneg_b)
        rn = self.call_rt("rt_num_neg", [rmag], OBJ)
        self.cur.add("mov", rout, rn)
        self.cur.add("jmp", None, final_b.block.label)

        self.use(diff_b)
        qn = self.call_rt("rt_num_neg", [qmag], OBJ)
        rz = self.call_rt("rt_num_cmp", [rmag, zero], INT)
        nz = self.fn.new_vreg(BOOL, "div_nz")
        self.cur.add("cmp", nz, "ne", rz, self.const_int(0))
        self.cur.add("br", None, nz, qminus_b.block.label, qzero_b.block.label)
        self.use(qminus_b)
        q1 = self.call_rt("rt_num_sub", [qn, one], OBJ)
        self.cur.add("mov", qout, q1)
        base = self.call_rt("rt_num_sub", [bb, rmag], OBJ)
        self.cur.add("br", None, nb, bneg_b.block.label, bpos_b.block.label)
        self.use(bpos_b)
        self.cur.add("mov", rout, base)
        self.cur.add("jmp", None, final_b.block.label)
        self.use(bneg_b)
        rn2 = self.call_rt("rt_num_neg", [base], OBJ)
        self.cur.add("mov", rout, rn2)
        self.cur.add("jmp", None, final_b.block.label)
        self.use(qzero_b)
        self.cur.add("mov", qout, qn)
        self.cur.add("mov", rout, rmag)
        self.cur.add("jmp", None, final_b.block.label)
        self.use(final_b)
        return qout, rout

    def const_bigint(self, value: int) -> VReg:
        d = self.fn.new_vreg(STR, "big_desc")
        self.cur.add("str", d, self.lw.string_offset(str(value)))
        o = self.fn.new_vreg(OBJ, "big_obj")
        self.cur.add("call", o, "rt_bigint_from_decimal", (d,))
        return o

    def as_bigint(self, v: VReg) -> VReg:
        if v.ty == OBJ:
            return v
        if v.ty == INT:
            out = self.fn.new_vreg(OBJ, "big_arg")
            self.cur.add("call", out, "rt_bigint_from_int", (v,))
            return out
        raise VelaTypeError(f"cannot use {v.ty} in bigint arithmetic")

    def unbox_num(self, v: VReg) -> VReg:
        if self.dyn and v.ty == OBJ:
            r = self.fn.new_vreg(INT, "native")
            self.cur.add("call", r, "rt_unbox_int", (v,))
            return r
        return self.coerce(v, INT)

    def as_num(self, v: VReg) -> VReg:
        if v.ty == OBJ:
            return v
        if v.ty == INT:
            o = self.fn.new_vreg(OBJ, "num")
            self.cur.add("call", o, "rt_box_int", (v,))
            return o
        raise VelaTypeError(f"cannot use {v.ty} as integer")

    def box_scalar(self, v: VReg | None) -> VReg:
        if v is None:
            o = self.fn.new_vreg(OBJ, "boxed_none")
            self.cur.add("call", o, "rt_box_none", ())
            return o
        if v.ty == OBJ:
            return v
        o = self.fn.new_vreg(OBJ, "boxed")
        label = {INT: "rt_box_int", FLOAT: "rt_box_float",
                 BOOL: "rt_box_bool", STR: "rt_box_str"}[v.ty]
        self.cur.add("call", o, label, (v,))
        return o

    def var(self, name: str, node: ast.AST) -> VReg:
        if name in self.globals:
            ty = self.var_types.get(name)
            if ty is None:
                ty = self.lw.an.const_globals.get(name, (INT, 0))[0]
            idx = self.const_int(self.lw.global_slots[name])
            obj = self.fn.new_vreg(OBJ, name + "_g")
            self.cur.add("call", obj, "rt_global_get", (idx,))
            r = self.fn.new_vreg(ty, name + "_v")
            unbox = {INT: "rt_unbox_int", FLOAT: "rt_unbox_float",
                     BOOL: "rt_unbox_bool", STR: "rt_unbox_str"}[ty]
            self.cur.add("call", r, unbox, (obj,))
            return r
        v = self.locals.get(name)
        if self.dyn and self.var_types.get(name) == INT and v is not None \
                and v.ty == OBJ and name in self.assigned:
            return v
        if v is None or name not in self.assigned:
            raise VelaCompileError(
                f"local variable '{name}' is used before assignment", node)
        declared = self.var_types.get(name)
        if self.lw.boxed and (declared in ("list", "dict", "tuple", "set", "bigint", "bytes", "strlist", "module", "modules")
                              or is_func_type(declared)) and v.ty == OBJ:
            return v
        if self.lw.boxed and v.ty == OBJ and declared in (INT, FLOAT, BOOL, STR):
            r = self.fn.new_vreg(declared, name + "_v")
            unbox = {INT: "rt_unbox_int", FLOAT: "rt_unbox_float",
                     BOOL: "rt_unbox_bool", STR: "rt_unbox_str"}[declared]
            self.cur.add("call", r, unbox, (v,))
            return r
        return v

    def bind(self, name: str, value: VReg | None, node: ast.AST) -> None:
        if name in self.globals:
            declared = self.var_types.get(name)
            if declared is None:
                declared = self.lw.an.const_globals.get(name, (INT, 0))[0]
            if value is None:
                raise VelaTypeError(f"cannot assign None to global '{name}'", node)
            native = self.coerce(value, declared)
            boxed = self.fn.new_vreg(OBJ, name + "_gb")
            label = {INT: "rt_box_int", FLOAT: "rt_box_float",
                     BOOL: "rt_box_bool", STR: "rt_box_str"}[declared]
            self.cur.add("call", boxed, label, (native,))
            idx = self.const_int(self.lw.global_slots[name])
            self.cur.add("call", None, "rt_global_set", (idx, boxed))
            self.assigned.add(name)
            return
        declared = self.var_types.get(name)
        if declared is None:
            declared = value.ty if value is not None else NONE
        if self.dyn and declared == INT:
            v = self.locals.get(name)
            if v is None or v.ty != OBJ:
                v = self.fn.new_vreg(OBJ, name + "_num")
                self.locals[name] = v
            if value is None:
                raise VelaTypeError(f"cannot assign None to '{name}'", node)
            self.cur.add("mov", v, value)
            self.assigned.add(name)
            return
        if self.lw.boxed and (declared in ("list", "dict", "tuple", "set", "bigint", "bytes", "strlist", "module", "modules")
                              or is_func_type(declared)):
            v = self.locals.get(name)
            if v is None or v.ty != OBJ:
                v = self.fn.new_vreg(OBJ, name + "_list")
                self.locals[name] = v
            if value is None:
                raise VelaTypeError(f"cannot assign None to list '{name}'", node)
            if value in self.locals.values() and value is not v:
                self.cur.add("call", None, "rt_incref", (value,))
            self.cur.add("mov", v, value)
            self.assigned.add(name)
            return
        if self.lw.boxed and name in self.param_names and declared in (INT, FLOAT, BOOL, STR):
            v = self.locals.get(name)
            if v is None or v.ty != OBJ:
                v = self.fn.new_vreg(OBJ, name + "_obj")
                self.locals[name] = v
            if value is None:
                raise VelaTypeError(
                    f"cannot assign None to variable '{name}' of type {declared}", node)
            native = self.coerce(value, declared)
            if name in self.assigned:
                self.cur.add("call", None, "rt_decref", (v,))
            boxed = self.fn.new_vreg(OBJ, name + "_box")
            label = {INT: "rt_box_int", FLOAT: "rt_box_float",
                     BOOL: "rt_box_bool", STR: "rt_box_str"}[declared]
            self.cur.add("call", boxed, label, (native,))
            self.cur.add("mov", v, boxed)
            self.assigned.add(name)
            return
        v = self.locals.get(name)
        if v is None:
            v = self.fn.new_vreg(declared, name)
            self.locals[name] = v
        if value is None:
            if v.ty != NONE:
                raise VelaTypeError(
                    f"cannot assign None to variable '{name}' of type {v.ty}", node)
            zero = self.const_int(0)
            self.cur.add("mov", v, zero)
        else:
            value = self.coerce(value, v.ty)
            self.cur.add("mov", v, value)
        self.assigned.add(name)

    def boxed_cleanup(self, keep: VReg | None = None) -> None:
        if not self.lw.boxed:
            return
        for name, v in list(self.locals.items()):
            if name in self.param_names or v is keep:
                continue
            if v.ty == OBJ and name in self.assigned:
                self.cur.add("call", None, "rt_decref", (v,))

    def finish(self) -> None:
        # Only the block that is still "open" at the end of lowering is a real
        # fall-through continuation; terminate it with `ret`.  Other blocks
        # without a terminator are unreachable (e.g. after an early return) and
        # get a harmless forward jump so the IR stays well formed.
        for i, block in enumerate(self.fn.blocks):
            if block.terminator is not None:
                continue
            if block is self.cur:
                self.boxed_cleanup()
                if self.lw.boxed:
                    block.add("call", None, "rt_leave", ())
                if self.ret_ty != NONE:
                    # The analyzer already proved every reachable path returns;
                    # this continuation is unreachable, but the IR still needs a
                    # well-formed terminator.
                    zero = self.fn.new_vreg(self.ret_ty, "unreachable_ret")
                    block.add("const", zero, 0)
                    block.add("ret", None, zero)
                else:
                    block.add("ret", None, None)
            elif i + 1 < len(self.fn.blocks):
                block.add("jmp", None, self.fn.blocks[i + 1].label)
            else:
                block.add("ret", None, None)

    # -- statements --------------------------------------------------------
    def stmt(self, node: ast.stmt) -> None:
        if self.cur.terminator is not None:
            self.cur = self.fn.new_block("unreachable")
        if isinstance(node, ast.Assign):
            target = node.targets[0]
            if isinstance(target, ast.Tuple):
                obj = self.expr(node.value)
                for i, elt in enumerate(target.elts):
                    idx = self.const_int(i)
                    elem = self.call_rt("rt_list_get", [obj, idx], OBJ)
                    native = self.fn.new_vreg(INT)
                    self.cur.add("call", native, "rt_unbox_int", (elem,))
                    self.bind(elt.id, native, node)
                return
            if isinstance(target, ast.Subscript):
                obj = self.expr(target.value)
                if getattr(target.value, "_vela_ty", None) == "dict":
                    keyo = self.box_scalar(self.expr(target.slice))
                    native = self.coerce(self.expr(node.value), INT)
                    boxed = self.box_scalar(native)
                    self.cur.add("call", None, "rt_dict_set", (obj, keyo, boxed))
                else:
                    key = self.coerce(self.expr(target.slice), INT)
                    native = self.coerce(self.expr(node.value), INT)
                    old = self.call_rt("rt_list_get", [obj, key], OBJ)
                    boxed = self.fn.new_vreg(OBJ, "boxed_set")
                    self.cur.add("call", boxed, "rt_box_int", (native,))
                    self.cur.add("call", None, "rt_list_set", (obj, key, boxed))
                    self.cur.add("call", None, "rt_decref", (old,))
                return
            value = self.expr(node.value)
            if isinstance(target, ast.Name):
                cv = self.const_value(node.value)
                if cv is not None and not -(2 ** 63) <= cv < 2 ** 63:
                    self.big_consts[target.id] = cv
                else:
                    self.big_consts.pop(target.id, None)
            self.bind(target.id, value, node)
            return
        if isinstance(node, ast.AnnAssign):
            self.bind(node.target.id, self.expr(node.value), node)
            return
        if isinstance(node, ast.AugAssign):
            self.augassign(node)
            return
        if isinstance(node, ast.Expr):
            if isinstance(node.value, ast.Constant):
                return
            self.expr(node.value)
            return
        if isinstance(node, ast.If):
            self.if_stmt(node)
            return
        if isinstance(node, ast.While):
            self.while_stmt(node)
            return
        if isinstance(node, (ast.Global, ast.FunctionDef)):
            return
        if isinstance(node, ast.Assert):
            test = self.coerce(self.expr(node.test), BOOL)
            ok = self.new_block("assert_ok")
            bad = self.new_block("assert_bad")
            self.cur.add("br", None, test, ok.block.label, bad.block.label)
            self.use(bad)
            d = self.fn.new_vreg(STR, "assert_msg")
            self.cur.add("str", d, self.lw.string_offset("assertion failed"))
            code = self.const_int(1)
            self.cur.add("call", None, "rt_raise", (code, d))
            dead = self.new_block("after_assert")
            self.cur.add("jmp", None, dead.block.label)
            self.use(ok)
            return
        if isinstance(node, ast.Try):
            self.try_stmt(node)
            return
        if isinstance(node, ast.Raise):
            self.raise_stmt(node)
            return
        if isinstance(node, ast.For):
            self.for_stmt(node)
            return
        if isinstance(node, ast.Return):
            if node.value is None:
                if self.ret_ty != NONE:
                    raise VelaTypeError(
                        f"{self.name}() must return a value of type {self.ret_ty}")
                self.emit_finally()
                self.boxed_cleanup()
                if self.lw.boxed:
                    self.cur.add("call", None, "rt_leave", ())
                self.cur.add("ret", None, None)
            else:
                v = self.coerce(self.expr(node.value), self.ret_ty)
                if self.lw.boxed and v.ty in (INT, FLOAT, BOOL, STR) \
                        and not (self.dyn and self.ret_ty == INT):
                    o = self.fn.new_vreg(OBJ, "boxed_ret")
                    label = {INT: "rt_box_int", FLOAT: "rt_box_float",
                             BOOL: "rt_box_bool", STR: "rt_box_str"}[v.ty]
                    self.emit_finally()
                    self.cur.add("call", o, label, (v,))
                    self.boxed_cleanup(o)
                    if self.lw.boxed:
                        self.cur.add("call", None, "rt_leave", ())
                    self.cur.add("ret", None, o)
                else:
                    self.emit_finally()
                    self.boxed_cleanup(v)
                    if self.lw.boxed:
                        self.cur.add("call", None, "rt_leave", ())
                    self.cur.add("ret", None, v)
            return
        if isinstance(node, ast.Pass):
            return
        if isinstance(node, ast.Break):
            if not self.loops:
                raise VelaCompileError("'break' outside a loop", node)
            self.cur.add("jmp", None, self.loops[-1][1])
            return
        if isinstance(node, ast.Continue):
            if not self.loops:
                raise VelaCompileError("'continue' outside a loop", node)
            self.cur.add("jmp", None, self.loops[-1][0])
            return
        raise UnsupportedError(ast.dump(node).split("(")[0], node)

    def augassign(self, node: ast.AugAssign) -> None:
        name = node.target.id
        cur = self.var(name, node)
        rhs = self.expr(node.value)
        op = node.op
        if isinstance(op, ast.Add):
            value = self.arith("add", cur, rhs, node, inplace=True)
        elif isinstance(op, ast.Sub):
            value = self.arith("sub", cur, rhs, node, inplace=True)
        elif isinstance(op, ast.Mult):
            value = self.arith("mul", cur, rhs, node, inplace=True)
        elif isinstance(op, ast.Div):
            value = self.arith("fdiv", cur, rhs, node, inplace=True)
        elif isinstance(op, ast.FloorDiv):
            value = self.int_divmod("floordiv", cur, rhs, node,
                                    self.const_int_value(node.value))
        elif isinstance(op, ast.Mod):
            value = self.int_divmod("mod", cur, rhs, node,
                                    self.const_int_value(node.value))
        elif isinstance(op, ast.Pow):
            value = self.call_rt("rt_ipow", [cur, rhs], INT)
        elif isinstance(op, ast.BitAnd):
            value = self.arith("and", cur, rhs, node, inplace=True)
        elif isinstance(op, ast.BitOr):
            value = self.arith("or", cur, rhs, node, inplace=True)
        elif isinstance(op, ast.BitXor):
            value = self.arith("xor", cur, rhs, node, inplace=True)
        elif isinstance(op, ast.LShift):
            value = self.shift("shl", cur, rhs, node)
        elif isinstance(op, ast.RShift):
            value = self.shift("shr", cur, rhs, node)
        else:
            raise UnsupportedError(f"augmented operator {type(op).__name__}", node)
        self.bind(name, value, node)

    def if_stmt(self, node: ast.If) -> None:
        cond = self.expr(node.test)
        then_b = self.new_block("then")
        else_b = self.new_block("else")
        join_b = self.new_block("join")
        self.cur.add("br", None, cond, then_b.block.label, else_b.block.label)
        self.use(then_b)
        saved = set(self.assigned)
        for s in node.body:
            self.stmt(s)
        then_assigned = set(self.assigned)
        then_open = self.cur.terminator is None
        if then_open:
            self.cur.add("jmp", None, join_b.block.label)
        self.assigned = set(saved)
        self.use(else_b)
        for s in node.orelse:
            self.stmt(s)
        else_assigned = set(self.assigned) if node.orelse else set(saved)
        if self.cur.terminator is None:
            self.cur.add("jmp", None, join_b.block.label)
        self.assigned = then_assigned & else_assigned
        self.use(join_b)

    def for_list(self, node: ast.For) -> None:
        obj = self.expr(node.iter)
        length = self.fn.new_vreg(INT, "list_len")
        self.cur.add("call", length, "rt_list_len", (obj,))
        idx = self.fn.new_vreg(INT, "list_i")
        zero = self.const_int(0)
        self.cur.add("mov", idx, zero)
        cond = self.new_block("for_list_cond")
        body = self.new_block("for_list_body")
        exit_b = self.new_block("for_list_exit")
        self.cur.add("jmp", None, cond.block.label)
        self.use(cond)
        c = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c, "lt", idx, length)
        self.cur.add("br", None, c, body.block.label, exit_b.block.label)
        self.use(body)
        elem = self.call_rt("rt_list_get", [obj, idx], OBJ)
        iter_ty = getattr(node.iter, "_vela_ty", None)
        if iter_ty == "strlist":
            native = self.fn.new_vreg(STR)
            self.cur.add("call", native, "rt_unbox_str", (elem,))
            self.bind(node.target.id, native, node)
        elif self.dyn and iter_ty == "list":
            self.bind(node.target.id, elem, node)
        else:
            native = self.fn.new_vreg(INT)
            self.cur.add("call", native, "rt_unbox_int", (elem,))
            self.bind(node.target.id, native, node)
        for stmt in node.body:
            self.stmt(stmt)
        one = self.const_int(1)
        nxt = self.fn.new_vreg(INT)
        self.cur.add("bin", nxt, "add", idx, one)
        self.cur.add("mov", idx, nxt)
        self.cur.add("jmp", None, cond.block.label)
        self.use(exit_b)

    def for_dict(self, node: ast.For) -> None:
        obj = self.expr(node.iter)
        length = self.fn.new_vreg(INT, "dict_len")
        self.cur.add("call", length, "rt_dict_len", (obj,))
        idx = self.fn.new_vreg(INT, "dict_i")
        zero = self.const_int(0)
        self.cur.add("mov", idx, zero)
        cond = self.new_block("for_dict_cond")
        body = self.new_block("for_dict_body")
        exit_b = self.new_block("for_dict_exit")
        self.cur.add("jmp", None, cond.block.label)
        self.use(cond)
        c = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c, "lt", idx, length)
        self.cur.add("br", None, c, body.block.label, exit_b.block.label)
        self.use(body)
        keyo = self.call_rt("rt_dict_key_at", [obj, idx], OBJ)
        if self.dyn:
            self.bind(node.target.id, keyo, node)
        else:
            native = self.fn.new_vreg(INT)
            self.cur.add("call", native, "rt_unbox_int", (keyo,))
            self.bind(node.target.id, native, node)
        for stmt in node.body:
            self.stmt(stmt)
        one = self.const_int(1)
        nxt = self.fn.new_vreg(INT)
        self.cur.add("bin", nxt, "add", idx, one)
        self.cur.add("mov", idx, nxt)
        self.cur.add("jmp", None, cond.block.label)
        self.use(exit_b)

    def while_stmt(self, node: ast.While) -> None:
        if node.orelse:
            raise UnsupportedError("while/else", node)
        header = self.new_block("while_head")
        body_b = self.new_block("while_body")
        exit_b = self.new_block("while_exit")
        self.cur.add("jmp", None, header.block.label)
        self.use(header)
        cond = self.expr(node.test)
        self.cur.add("br", None, cond, body_b.block.label, exit_b.block.label)
        self.use(body_b)
        saved = set(self.assigned)
        self.loops.append((header.block.label, exit_b.block.label))
        for s in node.body:
            self.stmt(s)
        self.loops.pop()
        if self.cur.terminator is None:
            self.cur.add("jmp", None, header.block.label)
        self.assigned = saved
        self.use(exit_b)

    def try_stmt(self, node: ast.Try) -> None:
        n = len(node.handlers)
        is_star = getattr(node, "_vela_trystar", False)
        handled = self.fn.new_vreg(INT, "eg_handled") if is_star else None
        if handled is not None:
            self.cur.add("const", handled, 0)
        body = self.new_block("try_body")
        dispatch = [self.new_block(f"try_dispatch_{i}") for i in range(n + 1)]
        hblocks = [self.new_block(f"try_except_{i}") for i in range(n)]
        done = self.new_block("try_done")
        addr = self.fn.new_vreg(INT, "handler_addr")
        self.cur.add("addr", addr, dispatch[0].block.label)
        self.cur.add("call", None, "rt_try_push", (addr,))
        self.cur.add("jmp", None, body.block.label)
        self.use(body)
        self.finally_stack.append(list(node.finalbody))
        for stmt in node.body:
            self.stmt(stmt)
        self.finally_stack.pop()
        self.cur.add("call", None, "rt_try_pop", ())
        for stmt in node.orelse:
            self.stmt(stmt)
        for stmt in node.finalbody:
            self.stmt(stmt)
        self.cur.add("jmp", None, done.block.label)
        types = {"ValueError": 1, "TypeError": 2, "StopIteration": 3,
                 "AttributeError": 4, "KeyError": 5, "IndexError": 6,
                 "Exception": 0, "BaseException": 0}
        for i, h in enumerate(node.handlers):
            self.use(dispatch[i])
            if h.type is None:
                self.cur.add("jmp", None, hblocks[i].block.label)
            else:
                expected = types.get(getattr(h.type, "id", ""), 1)
                if expected == 0:
                    self.cur.add("jmp", None, hblocks[i].block.label)
                elif is_star:
                    code = self.call_rt("rt_exc_code", [], INT)
                    want = self.const_int(expected)
                    c_eq = self.fn.new_vreg(BOOL)
                    self.cur.add("cmp", c_eq, "eq", code, want)
                    mask_bit = self.const_int(1 << expected)
                    masked = self.fn.new_vreg(INT)
                    self.cur.add("bin", masked, "and", code, mask_bit)
                    c_mask = self.fn.new_vreg(BOOL)
                    self.cur.add("cmp", c_mask, "ne", masked, self.const_int(0))
                    is_eg = self.fn.new_vreg(INT)
                    self.cur.add("bin", is_eg, "and", code, self.const_int(0x100))
                    c_eg = self.fn.new_vreg(BOOL)
                    self.cur.add("cmp", c_eg, "ne", is_eg, self.const_int(0))
                    c_both = self.fn.new_vreg(INT)
                    self.cur.add("bin", c_both, "and", c_mask, c_eg)
                    c_any = self.fn.new_vreg(INT)
                    self.cur.add("bin", c_any, "or", c_eq, c_both)
                    self.cur.add("br", None, c_any, hblocks[i].block.label,
                                 dispatch[i + 1].block.label)
                else:
                    code = self.call_rt("rt_exc_code", [], INT)
                    want = self.const_int(expected)
                    cond = self.fn.new_vreg(BOOL)
                    self.cur.add("cmp", cond, "eq", code, want)
                    self.cur.add("br", None, cond, hblocks[i].block.label,
                                 dispatch[i + 1].block.label)
        self.use(dispatch[n])
        if is_star and handled is not None:
            c_h = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c_h, "ne", handled, self.const_int(0))
            fin_b = self.new_block("trystar_fin")
            reraise_b = self.new_block("trystar_reraise")
            self.cur.add("br", None, c_h, fin_b.block.label, reraise_b.block.label)
            self.use(fin_b)
            for stmt in node.finalbody:
                self.stmt(stmt)
            self.cur.add("jmp", None, done.block.label)
            self.use(reraise_b)
        for stmt in node.finalbody:
            self.stmt(stmt)
        self.cur.add("call", None, "rt_reraise", ())
        self.cur.add("jmp", None, done.block.label)
        for i, h in enumerate(node.handlers):
            self.use(hblocks[i])
            if is_star and handled is not None:
                self.cur.add("const", handled, 1)
            if h.name:
                self.var_types[h.name] = STR
                desc = self.call_rt("rt_exc_desc", [], STR)
                self.bind(h.name, desc, node)
            self.finally_stack.append(list(node.finalbody))
            for stmt in h.body:
                self.stmt(stmt)
            self.finally_stack.pop()
            if not is_star:
                for stmt in node.finalbody:
                    self.stmt(stmt)
            if self.cur.terminator is None:
                self.cur.add("jmp", None,
                             dispatch[i + 1].block.label if is_star else done.block.label)
        self.use(done)

    def emit_finally(self) -> None:
        for block in reversed(self.finally_stack):
            for stmt in block:
                self.stmt(stmt)

    def raise_stmt(self, node: ast.Raise) -> None:
        desc = self.const_int(0)
        code_val = 1
        exc_types = {"ValueError": 1, "TypeError": 2, "StopIteration": 3,
                     "AttributeError": 4, "KeyError": 5, "IndexError": 6}
        if node.exc is not None:
            if isinstance(node.exc, ast.Call):
                if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ExceptionGroup" \
                        and len(node.exc.args) == 2 and isinstance(node.exc.args[1], ast.List):
                    desc = self.expr(node.exc.args[0])
                    code_val = 0x100
                    for elt in node.exc.args[1].elts:
                        if isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name):
                            bit = exc_types.get(elt.func.id, 1)
                            code_val |= (1 << bit)
                else:
                    if node.exc.args:
                        desc = self.expr(node.exc.args[0])
                    code_val = exc_types.get(node.exc.func.id, 1)
            elif isinstance(node.exc, ast.Name):
                code_val = exc_types.get(node.exc.id, 1)
        cause = self.const_int(0)
        if node.cause is not None:
            c = node.cause
            if isinstance(c, ast.Name):
                cause = self.coerce(self.expr(c), STR)
            elif isinstance(c, ast.Call) and len(c.args) == 1:
                cause = self.coerce(self.expr(c.args[0]), STR)
            else:
                raise UnsupportedError("raise ... from with a non-call cause", node)
        self.cur.add("call", None, "rt_set_cause", (cause,))
        code = self.const_int(code_val)
        line = self.const_int(node.lineno)
        self.cur.add("call", None, "rt_set_line", (line,))
        self.cur.add("call", None, "rt_raise", (code, desc))
        dead = self.new_block("after_raise")
        self.cur.add("jmp", None, dead.block.label)
        self.use(dead)

    def for_stmt(self, node: ast.For) -> None:
        if node.orelse:
            raise UnsupportedError("for/else", node)
        iter_ty = getattr(node.iter, "_vela_ty", None)
        if self.lw.boxed and (
                isinstance(node.iter, (ast.List, ast.Tuple, ast.Set))
                or iter_ty in ("list", "tuple", "set", "dict")):
            if iter_ty == "dict":
                self.for_dict(node)
            else:
                self.for_list(node)
            return
        args = node.iter.args
        one = len(args) == 1
        start_node = None if one else args[0]
        stop_node = args[0] if one else args[1]
        step_node = None if len(args) < 3 else args[2]
        start = self.const_int(0) if start_node is None else self.unbox_num(self.expr(start_node))
        stop = self.unbox_num(self.expr(stop_node))
        step = self.const_int(1) if step_node is None else self.unbox_num(self.expr(step_node))
        step_const = self.const_int_value(step_node) if step_node is not None else 1
        if step_const == 0:
            raise VelaCompileError("range() step must not be zero", node)
        zero = self.const_int(0)
        zero_cond = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", zero_cond, "eq", step, zero)
        ok_b = self.new_block("for_ok")
        err_b = self.new_block("for_bad_step")
        self.cur.add("br", None, zero_cond, err_b.block.label, ok_b.block.label)
        self.use(err_b)
        self.cur.add("call", None, "rt_range_error", ())
        self.cur.add("jmp", None, ok_b.block.label)
        self.use(ok_b)

        index = self.fn.new_vreg(INT, "$i")
        self.cur.add("mov", index, start)
        header = self.new_block("for_head")
        body_b = self.new_block("for_body")
        inc_b = self.new_block("for_inc")
        exit_b = self.new_block("for_exit")
        self.cur.add("jmp", None, header.block.label)
        self.use(header)
        if step_const is not None and step_const > 0:
            cond = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", cond, "lt", index, stop)
            self.cur.add("br", None, cond, body_b.block.label, exit_b.block.label)
        elif step_const is not None and step_const < 0:
            cond = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", cond, "gt", index, stop)
            self.cur.add("br", None, cond, body_b.block.label, exit_b.block.label)
        else:
            neg = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", neg, "lt", step, zero)
            neg_b = self.new_block("for_neg")
            pos_b = self.new_block("for_pos")
            join_b = self.new_block("for_cond")
            self.cur.add("br", None, neg, neg_b.block.label, pos_b.block.label)
            self.use(neg_b)
            c1 = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c1, "gt", index, stop)
            self.cur.add("mov", neg, c1)
            self.cur.add("jmp", None, join_b.block.label)
            self.use(pos_b)
            c2 = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c2, "lt", index, stop)
            self.cur.add("mov", neg, c2)
            self.cur.add("jmp", None, join_b.block.label)
            self.use(join_b)
            self.cur.add("br", None, neg, body_b.block.label, exit_b.block.label)
        self.use(body_b)
        saved = set(self.assigned)
        if self.dyn:
            idx_obj = self.fn.new_vreg(OBJ, "loop_num")
            self.cur.add("call", idx_obj, "rt_box_int", (index,))
            self.bind(node.target.id, idx_obj, node)
        else:
            self.bind(node.target.id, index, node)
        self.loops.append((inc_b.block.label, exit_b.block.label))
        for s in node.body:
            self.stmt(s)
        self.loops.pop()
        if self.cur.terminator is None:
            self.cur.add("jmp", None, inc_b.block.label)
        self.use(inc_b)
        new_i = self.fn.new_vreg(INT)
        self.cur.add("bin", new_i, "add", index, step)
        self.cur.add("mov", index, new_i)
        self.cur.add("jmp", None, header.block.label)
        self.assigned = saved
        self.use(exit_b)

    def const_int_value(self, node: ast.expr | None) -> int | None:
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            v = self.const_int_value(node.operand)
            return -v if v is not None else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
            return self.const_int_value(node.operand)
        return None

    # -- expressions -------------------------------------------------------
    def expr(self, node: ast.expr) -> VReg | None:
        ty = self.ty(node)
        if isinstance(node, ast.Constant):
            v = node.value
            if v is None:
                return None
            if isinstance(v, bool):
                return self.const_bool(v)
            if isinstance(v, int):
                if self.dyn:
                    if not -(2 ** 63) <= v < 2 ** 63:
                        return self.const_bigint(v)
                    return self.as_num(self.const_int(v))
                if self.lw.boxed and not -(2 ** 63) <= v < 2 ** 63:
                    d = self.fn.new_vreg(STR, "big_desc")
                    self.cur.add("str", d, self.lw.string_offset(str(v)))
                    o = self.fn.new_vreg(OBJ, "big_obj")
                    self.cur.add("call", o, "rt_bigint_from_decimal", (d,))
                    return o
                return self.const_int(v)
            if isinstance(v, float):
                return self.const_float(v)
            if isinstance(v, str):
                r = self.fn.new_vreg(STR)
                self.cur.add("str", r, self.lw.string_offset(v))
                return r
            raise UnsupportedError(f"constant of type {type(v).__name__}", node)
        if isinstance(node, ast.Name):
            if node.id not in self.locals and node.id not in self.globals \
                    and node.id in self.lw.an.const_globals:
                ty, val = self.lw.an.const_globals[node.id]
                v = self.fn.new_vreg(ty, node.id)
                if ty == STR:
                    self.cur.add("str", v, self.lw.string_offset(val))
                elif ty == NONE:
                    self.cur.add("const", v, 0)
                elif ty == BOOL:
                    self.cur.add("const", v, 1 if val else 0)
                else:
                    self.cur.add("const", v, val)
                return v
            if self.lw.boxed and node.id not in self.locals \
                    and node.id not in self.globals \
                    and node.id in self.lw.an.funcs:
                return self.emit_func_value(node.id, node)
            return self.var(node.id, node)
        if isinstance(node, ast.Dict):
            obj = self.call_rt("rt_dict_new", [], OBJ)
            for k, v in zip(node.keys, node.values):
                ko = self.box_scalar(self.expr(k))
                vo = self.box_scalar(self.expr(v))
                self.cur.add("call", None, "rt_dict_set", (obj, ko, vo))
            return obj
        if isinstance(node, ast.Set):
            n = self.const_int(len(node.elts))
            obj = self.call_rt("rt_list_new", [n], OBJ)
            for i, item in enumerate(node.elts):
                native = self.expr(item)
                boxed = self.box_scalar(native)
                idx = self.const_int(i)
                self.cur.add("call", None, "rt_list_set", (obj, idx, boxed))
            self.cur.add("call", None, "rt_set_type", (obj,))
            return obj
        if isinstance(node, ast.Tuple):
            n = self.const_int(len(node.elts))
            obj = self.call_rt("rt_list_new", [n], OBJ)
            for i, item in enumerate(node.elts):
                native = self.expr(item)
                boxed = self.box_scalar(native)
                idx = self.const_int(i)
                self.cur.add("call", None, "rt_list_set", (obj, idx, boxed))
            self.cur.add("call", None, "rt_tuple_type", (obj,))
            return obj
        if isinstance(node, ast.List):
            n = self.const_int(len(node.elts))
            obj = self.call_rt("rt_list_new", [n], OBJ)
            for i, item in enumerate(node.elts):
                native = self.expr(item)
                if native is not None and native.ty == OBJ:
                    self.cur.add("call", None, "rt_incref", (native,))
                boxed = self.box_scalar(native)
                idx = self.const_int(i)
                self.cur.add("call", None, "rt_list_set", (obj, idx, boxed))
            return obj
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) \
                and getattr(node.value, "_vela_ty", None) in ("str", "list", "tuple", "strlist"):
            return self.emit_slice(node)
        if isinstance(node, ast.Subscript) and (
                isinstance(node.value, ast.List)
                or getattr(node.value, "_vela_ty", None) in ("list", "tuple", "strlist")):
            obj = self.expr(node.value)
            idx = self.expr(node.slice)
            elem = self.call_rt("rt_list_get", [obj, idx], OBJ)
            if getattr(node.value, "_vela_ty", None) == "strlist":
                native = self.fn.new_vreg(STR)
                self.cur.add("call", native, "rt_unbox_str", (elem,))
                return native
            native = self.fn.new_vreg(INT)
            self.cur.add("call", native, "rt_unbox_int", (elem,))
            return native
        if isinstance(node, ast.Subscript) and getattr(node.value, "_vela_ty", None) == "str":
            desc = self.expr(node.value)
            idx = self.coerce(self.expr(node.slice), INT)
            return self.call_rt("rt_str_index", [desc, idx], STR)
        if isinstance(node, ast.Subscript) and getattr(node.value, "_vela_ty", None) == "dict":
            obj = self.expr(node.value)
            key_expr = self.expr(node.slice)
            keyo = self.box_scalar(key_expr)
            val = self.call_rt("rt_dict_get", [obj, keyo], OBJ)
            if key_expr.ty != OBJ:
                self.cur.add("call", None, "rt_decref", (keyo,))
            native = self.fn.new_vreg(INT)
            self.cur.add("call", native, "rt_unbox_int", (val,))
            return native
        if isinstance(node, ast.BinOp):
            return self.binop(node)
        if isinstance(node, ast.UnaryOp):
            if self.dyn and isinstance(node.op, (ast.USub, ast.UAdd)) \
                    and getattr(node.operand, "_vela_ty", None) == INT:
                v = self.expr(node.operand)
                if isinstance(node.op, ast.UAdd):
                    return v
                if v.ty == OBJ:
                    out = self.fn.new_vreg(OBJ, "num_neg")
                    self.cur.add("call", out, "rt_num_neg", (v,))
                    return out
                r = self.fn.new_vreg(INT)
                self.cur.add("un", r, "neg", v)
                return r
            if isinstance(node.op, ast.Not):
                v = self.expr(node.operand)
                r = self.fn.new_vreg(BOOL)
                zero = self.const_int(0)
                self.cur.add("cmp", r, "eq", v, zero)
                return r
            if isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
                    and isinstance(node.operand.value, int) \
                    and not isinstance(node.operand.value, bool):
                # Fold -literal so that -2**63 does not go through neg
                # (which would raise a spurious overflow).
                return self.const_int(-node.operand.value)
            v = self.expr(node.operand)
            if isinstance(node.op, ast.UAdd):
                return v
            if isinstance(node.op, ast.USub):
                if v.ty == FLOAT:
                    r = self.fn.new_vreg(FLOAT)
                    self.cur.add("un", r, "fneg", v)
                    return r
                r = self.fn.new_vreg(INT)
                self.cur.add("un", r, "neg", v)
                return r
            raise UnsupportedError(f"unary operator {type(node.op).__name__}", node)
        if isinstance(node, ast.BoolOp):
            return self.boolop(node)
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.IfExp):
            return self.ifexp(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        raise UnsupportedError(ast.dump(node).split("(")[0], node)

    def const_value(self, node: ast.expr) -> int | None:
        v = self.const_int_value(node)
        if v is not None:
            return v
        if isinstance(node, ast.Name):
            return self.big_consts.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = self.const_value(node.operand)
            return -inner if inner is not None else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
            return self.const_value(node.operand)
        if isinstance(node, ast.BinOp):
            a = self.const_value(node.left)
            b = self.const_value(node.right)
            if a is None or b is None:
                return None
            try:
                if isinstance(node.op, ast.Add):
                    return a + b
                if isinstance(node.op, ast.Sub):
                    return a - b
                if isinstance(node.op, ast.Mult):
                    return a * b
                if isinstance(node.op, ast.FloorDiv) and b:
                    return a // b
                if isinstance(node.op, ast.Mod) and b:
                    return a % b
            except ArithmeticError:
                return None
        return None

    def binop(self, node: ast.BinOp) -> VReg:
        if self.dyn and isinstance(node.op, (ast.FloorDiv, ast.Mod)):
            left = self.expr(node.left)
            right = self.expr(node.right)
            a = self.as_num(left)
            b = self.as_num(right)
            aa = self.call_rt("rt_num_to_bigint", [a], OBJ)
            bb = self.call_rt("rt_num_to_bigint", [b], OBJ)
            qv, rv = self.bigint_floor_divmod(aa, bb)
            return qv if isinstance(node.op, ast.FloorDiv) else rv
        if self.dyn and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            left = self.expr(node.left)
            right = self.expr(node.right)
            a = self.as_num(left)
            b = self.as_num(right)
            label = {ast.Add: "rt_num_add", ast.Sub: "rt_num_sub",
                     ast.Mult: "rt_num_mul"}[type(node.op)]
            out = self.fn.new_vreg(OBJ, "num_res")
            self.cur.add("call", out, label, (a, b))
            return out
        if self.lw.boxed and isinstance(node.op, (ast.FloorDiv, ast.Mod)) \
                and getattr(node.left, "_vela_ty", None) == "bigint" \
                and getattr(node.right, "_vela_ty", None) == "bigint":
            a = self.const_value(node.left)
            b = self.const_value(node.right)
            if a is not None and b is not None:
                if b == 0:
                    raise VelaCompileError("division by zero", node)
                value = a // b if isinstance(node.op, ast.FloorDiv) else a % b
                return self.const_bigint(value)
            la_expr = self.expr(node.left)
            lb_expr = self.expr(node.right)
            qv, rv = self.bigint_floor_divmod(la_expr, lb_expr)
            return qv if isinstance(node.op, ast.FloorDiv) else rv
        left = self.expr(node.left)
        right = self.expr(node.right)
        if self.lw.boxed and "bigint" in (
                getattr(node, "_vela_ty", None),
                getattr(node.left, "_vela_ty", None),
                getattr(node.right, "_vela_ty", None)):
            a = self.as_bigint(left)
            b = self.as_bigint(right)
            if isinstance(node.op, (ast.FloorDiv, ast.Mod)):
                qv, rv = self.bigint_floor_divmod(a, b)
                return qv if isinstance(node.op, ast.FloorDiv) else rv
            label = {ast.Add: "rt_bigint_add", ast.Sub: "rt_bigint_sub",
                     ast.Mult: "rt_bigint_mul"}.get(type(node.op))
            if label is None:
                raise UnsupportedError("bigint operator", node)
            out = self.fn.new_vreg(OBJ, "big_res")
            self.cur.add("call", out, label, (a, b))
            return out
        if self.lw.boxed and isinstance(node.op, (ast.BitOr, ast.BitAnd)) \
                and getattr(node.left, "_vela_ty", None) == "set" \
                and getattr(node.right, "_vela_ty", None) == "set":
            dst = self.call_rt("rt_list_new", [self.const_int(0)], OBJ)
            self.cur.add("call", None, "rt_set_type", (dst,))
            self.set_merge(dst, left, right if isinstance(node.op, ast.BitAnd) else None)
            if isinstance(node.op, ast.BitOr):
                self.set_merge(dst, right, None)
            return dst
        if self.lw.boxed and left.ty == STR and right.ty == STR:
            if isinstance(node.op, ast.Add):
                return self.call_rt("rt_str_concat", [left, right], STR)
            if isinstance(node.op, (ast.Eq, ast.NotEq)):
                eq = self.call_rt("rt_str_eq", [left, right], BOOL)
                if isinstance(node.op, ast.Eq):
                    return eq
                z = self.const_int(0)
                inv = self.fn.new_vreg(BOOL)
                self.cur.add("cmp", inv, "eq", eq, z)
                return inv
        ty = self.ty(node)
        op = node.op
        if isinstance(op, ast.Div):
            a = self.coerce(left, FLOAT)
            b = self.coerce(right, FLOAT)
            r = self.fn.new_vreg(FLOAT)
            self.cur.add("bin", r, "fdiv", a, b)
            return r
        if isinstance(op, ast.Pow):
            a = self.coerce(left, INT)
            b = self.coerce(right, INT)
            return self.call_rt("rt_ipow", [a, b], INT)
        if isinstance(op, ast.FloorDiv):
            return self.int_divmod("floordiv", left, right, node,
                                   self.const_int_value(node.right))
        if isinstance(op, ast.Mod):
            return self.int_divmod("mod", left, right, node,
                                   self.const_int_value(node.right))
        if isinstance(op, ast.Add):
            return self.arith("add", left, right, node)
        if isinstance(op, ast.Sub):
            return self.arith("sub", left, right, node)
        if isinstance(op, ast.Mult):
            return self.arith("mul", left, right, node)
        if isinstance(op, ast.BitAnd):
            return self.arith("and", left, right, node)
        if isinstance(op, ast.BitOr):
            return self.arith("or", left, right, node)
        if isinstance(op, ast.BitXor):
            return self.arith("xor", left, right, node)
        if isinstance(op, ast.LShift):
            return self.shift("shl", left, right, node)
        if isinstance(op, ast.RShift):
            return self.shift("shr", left, right, node)
        raise UnsupportedError(f"operator {type(op).__name__}", node)

    def arith(self, op: str, left: VReg | None, right: VReg | None,
              node: ast.AST, inplace: bool = False) -> VReg:
        if left is None or right is None:
            raise UnsupportedError("arithmetic on None", node)
        float_op = left.ty == FLOAT or right.ty == FLOAT
        if float_op:
            if op not in ("add", "sub", "mul"):
                raise UnsupportedError(f"'{op}' on floats", node)
            a = self.coerce(left, FLOAT)
            b = self.coerce(right, FLOAT)
            r = self.fn.new_vreg(FLOAT)
            self.cur.add("bin", r, "f" + op, a, b)
            return r
        a = self.coerce(left, INT)
        b = self.coerce(right, INT)
        r = self.fn.new_vreg(INT, op)
        self.cur.add("bin", r, op, a, b)
        return r

    def int_divmod(self, op: str, left: VReg | None, right: VReg | None,
                   node: ast.AST, const: int | None = None) -> VReg:
        if left is None or right is None:
            raise UnsupportedError("division on None", node)
        if left.ty == FLOAT or right.ty == FLOAT:
            raise UnsupportedError("float floor division / modulo", node)
        a = self.coerce(left, INT)
        if const is not None and const > 0 and const & (const - 1) == 0:
            # Strength reduction for positive power-of-two divisors.
            # Python floor division / modulo agree with sar / mask here, also
            # for negative dividends (-3 // 2 == -2, -3 % 2 == 1).
            shift = const.bit_length() - 1
            if op == "floordiv":
                if shift == 0:
                    return a
                r = self.fn.new_vreg(INT, "floordiv")
                self.cur.add("bin", r, "shr", a, self.const_int(shift))
                return r
            if shift == 0:
                return self.const_int(0)
            r = self.fn.new_vreg(INT, "mod")
            self.cur.add("bin", r, "and", a, self.const_int(const - 1))
            return r
        b = self.coerce(right, INT)
        return self.call_rt("rt_idiv" if op == "floordiv" else "rt_imod", [a, b], INT)

    def shift(self, op: str, left: VReg | None, right: VReg | None,
              node: ast.AST) -> VReg:
        if left is None or right is None:
            raise UnsupportedError("shift on None", node)
        a = self.coerce(left, INT)
        b = self.coerce(right, INT)
        r = self.fn.new_vreg(INT, op)
        self.cur.add("bin", r, op, a, b)
        return r

    def boolop(self, node: ast.BoolOp) -> VReg:
        result = self.fn.new_vreg(BOOL)
        end_b = self.new_block("bool_end")
        short_b = self.new_block("bool_short")
        is_and = isinstance(node.op, ast.And)
        for i, value_node in enumerate(node.values):
            value = self.expr(value_node)
            if i == len(node.values) - 1:
                self.cur.add("mov", result, self.coerce(value, BOOL))
                self.cur.add("jmp", None, end_b.block.label)
            else:
                nxt = self.new_block("bool_next")
                if is_and:
                    self.cur.add("br", None, value, nxt.block.label, short_b.block.label)
                else:
                    self.cur.add("br", None, value, short_b.block.label, nxt.block.label)
                self.use(nxt)
        self.use(short_b)
        self.cur.add("const", result, 0 if is_and else 1)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(end_b)
        return result

    def compare(self, node: ast.Compare) -> VReg:
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            right = self.expr(node.comparators[0])
            left_expr = self.expr(node.left)
            vo = self.box_scalar(left_expr)
            label = "rt_dict_contains" if getattr(node.comparators[0], "_vela_ty", None) in ("dict", "modules") else "rt_contains"
            res = self.call_rt(label, [right, vo], BOOL)
            if left_expr.ty != OBJ:
                self.cur.add("call", None, "rt_decref", (vo,))
            if isinstance(node.ops[0], ast.In):
                return res
            z = self.const_int(0)
            inv = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", inv, "eq", res, z)
            return inv
        if len(node.ops) == 1:
            left = self.expr(node.left)
            right = self.expr(node.comparators[0])
            return self.cmp_value(node.ops[0], left, right, node)
        result = self.fn.new_vreg(BOOL)
        end_b = self.new_block("cmp_end")
        false_b = self.new_block("cmp_false")
        left = self.expr(node.left)
        for i, (op, comp) in enumerate(zip(node.ops, node.comparators)):
            right = self.expr(comp)
            c = self.cmp_value(op, left, right, node)
            if i == len(node.ops) - 1:
                self.cur.add("mov", result, c)
                self.cur.add("jmp", None, end_b.block.label)
            else:
                nxt = self.new_block("cmp_next")
                self.cur.add("br", None, c, nxt.block.label, false_b.block.label)
                self.use(nxt)
            left = right
        self.use(false_b)
        self.cur.add("const", result, 0)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(end_b)
        return result

    def cmp_value(self, op: ast.cmpop, left: VReg | None, right: VReg | None,
                  node: ast.AST) -> VReg:
        if left is None or right is None:
            raise UnsupportedError("comparison with None", node)
        if self.lw.boxed and left.ty == OBJ and right.ty == OBJ \
                and isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)) \
                and isinstance(node, ast.Compare) \
                and is_func_type(getattr(node.left, "_vela_ty", None)):
            r = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", r, "eq" if isinstance(op, (ast.Eq, ast.Is)) else "ne",
                         left, right)
            return r
        if self.dyn and (left.ty in (INT, OBJ) and right.ty in (INT, OBJ)):
            a = self.as_num(left)
            b = self.as_num(right)
            cv = self.fn.new_vreg(INT, "num_cmp")
            self.cur.add("call", cv, "rt_num_cmp", (a, b))
            z = self.const_int(0)
            name = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt",
                    ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}.get(type(op))
            if name is None:
                raise UnsupportedError("dynamic int comparison", node)
            r = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", r, name, cv, z)
            return r
        if self.lw.boxed and (left.ty == OBJ or right.ty == OBJ):
            a = self.as_bigint(left)
            b = self.as_bigint(right)
            cv = self.fn.new_vreg(INT, "big_cmp")
            self.cur.add("call", cv, "rt_bigint_cmp", (a, b))
            z = self.const_int(0)
            name = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt",
                    ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}.get(type(op))
            if name is None:
                raise UnsupportedError("bigint comparison", node)
            r = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", r, name, cv, z)
            return r
        if left.ty == STR and right.ty == STR and isinstance(op, (ast.Eq, ast.NotEq)):
            res = self.call_rt("rt_str_eq", [left, right], BOOL)
            if isinstance(op, ast.Eq):
                return res
            z = self.const_int(0)
            inv = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", inv, "eq", res, z)
            return inv
        if left.ty == STR and right.ty == STR and isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            cv = self.call_rt("rt_str_cmp", [left, right], INT)
            z = self.const_int(0)
            cname = {ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}[type(op)]
            r = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", r, cname, cv, z)
            return r
        if left.ty == FLOAT or right.ty == FLOAT:
            a = self.coerce(left, FLOAT)
            b = self.coerce(right, FLOAT)
        else:
            a = self.coerce(left, INT)
            b = self.coerce(right, INT)
        if isinstance(op, ast.Eq):
            name = "eq"
        elif isinstance(op, ast.NotEq):
            name = "ne"
        elif isinstance(op, ast.Lt):
            name = "lt"
        elif isinstance(op, ast.LtE):
            name = "le"
        elif isinstance(op, ast.Gt):
            name = "gt"
        elif isinstance(op, ast.GtE):
            name = "ge"
        else:
            raise UnsupportedError(f"comparison {type(op).__name__}", node)
        r = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", r, name, a, b)
        return r

    def ifexp(self, node: ast.IfExp) -> VReg | None:
        ty = self.ty(node)
        cond = self.expr(node.test)
        then_b = self.new_block("ifexp_then")
        else_b = self.new_block("ifexp_else")
        end_b = self.new_block("ifexp_end")
        self.cur.add("br", None, cond, then_b.block.label, else_b.block.label)
        self.use(then_b)
        v = self.coerce(self.expr(node.body), ty) if ty != NONE else None
        body_none = ty == NONE
        if v is not None:
            result = self.fn.new_vreg(ty)
            self.cur.add("mov", result, v)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(else_b)
        v = self.coerce(self.expr(node.orelse), ty) if ty != NONE else None
        if v is not None:
            self.cur.add("mov", result, v)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(end_b)
        return None if body_none else result

    def call_rt(self, label: str, args: list[VReg | None], ret_ty: str) -> VReg:
        dst = self.fn.new_vreg(ret_ty) if ret_ty != NONE else None
        self.cur.add("call", dst, label, tuple(args))
        return dst  # type: ignore[return-value]

    def emit_slice(self, node: ast.Subscript) -> VReg:
        assert isinstance(node.slice, ast.Slice)
        base_ty = getattr(node.value, "_vela_ty", "list")
        seq = self.expr(node.value)
        is_str = base_ty == "str"
        length = self.call_rt("rt_str_len" if is_str else "rt_list_len", [seq], INT)
        step = self.coerce(self.expr(node.slice.step), INT) if node.slice.step is not None else self.const_int(1)
        zero = self.const_int(0)
        one = self.const_int(1)
        neg_one = self.const_int(-1)
        len_m1 = self.fn.new_vreg(INT, "len_m1")
        self.cur.add("bin", len_m1, "sub", length, one)

        step_neg = self.fn.new_vreg(BOOL, "step_neg")
        self.cur.add("cmp", step_neg, "lt", step, zero)

        start = self.fn.new_vreg(INT, "sl_start")
        if node.slice.lower is None:
            s_pos = self.new_block("sl_s_pos")
            s_neg = self.new_block("sl_s_neg")
            s_join = self.new_block("sl_s_join")
            self.cur.add("br", None, step_neg, s_neg.block.label, s_pos.block.label)
            self.use(s_pos)
            self.cur.add("mov", start, zero)
            self.cur.add("jmp", None, s_join.block.label)
            self.use(s_neg)
            self.cur.add("mov", start, len_m1)
            self.cur.add("jmp", None, s_join.block.label)
            self.use(s_join)
        else:
            raw_s = self.coerce(self.expr(node.slice.lower), INT)
            self.cur.add("mov", start, raw_s)
            c_neg = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c_neg, "lt", start, zero)
            b_add = self.new_block("sl_s_add")
            b_clamp = self.new_block("sl_s_clamp")
            self.cur.add("br", None, c_neg, b_add.block.label, b_clamp.block.label)
            self.use(b_add)
            tmp_s = self.fn.new_vreg(INT)
            self.cur.add("bin", tmp_s, "add", start, length)
            self.cur.add("mov", start, tmp_s)
            self.cur.add("jmp", None, b_clamp.block.label)
            self.use(b_clamp)
            c_low = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c_low, "lt", start, zero)
            b_low = self.new_block("sl_s_low")
            b_hi_chk = self.new_block("sl_s_hichk")
            self.cur.add("br", None, c_low, b_low.block.label, b_hi_chk.block.label)
            self.use(b_low)
            self.cur.add("mov", start, zero)
            self.cur.add("jmp", None, b_hi_chk.block.label)
            self.use(b_hi_chk)
            c_hi = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c_hi, "gt", start, length)
            b_hi = self.new_block("sl_s_hi")
            b_sdone = self.new_block("sl_s_done")
            self.cur.add("br", None, c_hi, b_hi.block.label, b_sdone.block.label)
            self.use(b_hi)
            self.cur.add("mov", start, length)
            self.cur.add("jmp", None, b_sdone.block.label)
            self.use(b_sdone)

        stop = self.fn.new_vreg(INT, "sl_stop")
        if node.slice.upper is None:
            e_pos = self.new_block("sl_e_pos")
            e_neg = self.new_block("sl_e_neg")
            e_join = self.new_block("sl_e_join")
            self.cur.add("br", None, step_neg, e_neg.block.label, e_pos.block.label)
            self.use(e_pos)
            self.cur.add("mov", stop, length)
            self.cur.add("jmp", None, e_join.block.label)
            self.use(e_neg)
            self.cur.add("mov", stop, neg_one)
            self.cur.add("jmp", None, e_join.block.label)
            self.use(e_join)
        else:
            raw_e = self.coerce(self.expr(node.slice.upper), INT)
            self.cur.add("mov", stop, raw_e)
            c_neg = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c_neg, "lt", stop, zero)
            b_add = self.new_block("sl_e_add")
            b_clamp = self.new_block("sl_e_clamp")
            self.cur.add("br", None, c_neg, b_add.block.label, b_clamp.block.label)
            self.use(b_add)
            tmp_e = self.fn.new_vreg(INT)
            self.cur.add("bin", tmp_e, "add", stop, length)
            self.cur.add("mov", stop, tmp_e)
            self.cur.add("jmp", None, b_clamp.block.label)
            self.use(b_clamp)
            c_low = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c_low, "lt", stop, zero)
            b_low = self.new_block("sl_e_low")
            b_hi_chk = self.new_block("sl_e_hichk")
            self.cur.add("br", None, c_low, b_low.block.label, b_hi_chk.block.label)
            self.use(b_low)
            self.cur.add("mov", stop, zero)
            self.cur.add("jmp", None, b_hi_chk.block.label)
            self.use(b_hi_chk)
            c_hi = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c_hi, "gt", stop, length)
            b_hi = self.new_block("sl_e_hi")
            b_edone = self.new_block("sl_e_done")
            self.cur.add("br", None, c_hi, b_hi.block.label, b_edone.block.label)
            self.use(b_hi)
            self.cur.add("mov", stop, length)
            self.cur.add("jmp", None, b_edone.block.label)
            self.use(b_edone)

        if is_str:
            out = self.fn.new_vreg(STR, "sl_str")
            self.cur.add("str", out, self.lw.string_offset(""))
        else:
            out = self.call_rt("rt_list_new", [zero], OBJ)

        idx = self.fn.new_vreg(INT, "sl_i")
        self.cur.add("mov", idx, start)
        head = self.new_block("sl_head")
        pos_chk = self.new_block("sl_pos_chk")
        neg_chk = self.new_block("sl_neg_chk")
        body = self.new_block("sl_body")
        done = self.new_block("sl_done")
        self.cur.add("jmp", None, head.block.label)
        self.use(head)
        self.cur.add("br", None, step_neg, neg_chk.block.label, pos_chk.block.label)
        self.use(pos_chk)
        c1 = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c1, "lt", idx, stop)
        self.cur.add("br", None, c1, body.block.label, done.block.label)
        self.use(neg_chk)
        c2 = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c2, "gt", idx, stop)
        self.cur.add("br", None, c2, body.block.label, done.block.label)
        self.use(body)
        if is_str:
            ch = self.call_rt("rt_str_index", [seq, idx], STR)
            nxt_s = self.call_rt("rt_str_concat", [out, ch], STR)
            self.cur.add("mov", out, nxt_s)
        else:
            elem = self.call_rt("rt_list_get", [seq, idx], OBJ)
            self.cur.add("call", None, "rt_list_append", (out, elem))
        nxt_i = self.fn.new_vreg(INT)
        self.cur.add("bin", nxt_i, "add", idx, step)
        self.cur.add("mov", idx, nxt_i)
        self.cur.add("jmp", None, head.block.label)
        self.use(done)
        return out

    def emit_func_value(self, qual: str, node: ast.AST) -> VReg:
        info = self.lw.an.funcs.get(qual)
        if info is None:
            raise UnsupportedError(f"unknown function value '{qual}'", node)
        self.lw.wrapped_funcs.add(qual)
        addr = self.fn.new_vreg(INT, "func_addr")
        self.cur.add("addr", addr, "fn_wrap_" + info.name)
        code = self.fn.new_vreg(OBJ, "func_value")
        self.cur.add("call", code, "rt_code_new",
                     (addr, self.const_int(len(info.params))))
        return code

    def call(self, node: ast.Call) -> VReg | None:
        if isinstance(node.func, ast.Name) and node.func.id == "__closure_new":
            qual = node.args[0].value
            info = self.lw.an.funcs.get(qual)
            if info is None:
                raise UnsupportedError(f"unknown closure function '{qual}'", node)
            self.lw.wrapped_funcs.add(qual)
            arity = self.coerce(self.expr(node.args[1]), INT)
            env_list = self.expr(node.args[2])
            addr = self.fn.new_vreg(INT, "closure_addr")
            self.cur.add("addr", addr, "fn_wrap_" + info.name)
            code = self.fn.new_vreg(OBJ, "closure_val")
            self.cur.add("call", code, "rt_closure_new", (addr, arity, env_list))
            return code
        if isinstance(node.func, ast.Name) and node.func.id == "__attr_set":
            obj = self.expr(node.args[0])
            ko = self.box_scalar(self.expr(node.args[1]))
            vo = self.box_scalar(self.expr(node.args[2]))
            self.cur.add("call", None, "rt_dict_set", (obj, ko, vo))
            return None
        if isinstance(node.func, ast.Name) and node.func.id == "__attr_get":
            obj = self.expr(node.args[0])
            ko = self.box_scalar(self.expr(node.args[1]))
            if len(node.args) > 2:
                do = self.box_scalar(self.expr(node.args[2]))
                val = self.call_rt("rt_dict_get_def", [obj, ko, do], OBJ)
            else:
                val = self.call_rt("rt_dict_get", [obj, ko], OBJ)
            self.cur.add("call", None, "rt_decref", (ko,))
            ty = getattr(node, "_vela_attr_ty", INT)
            if ty in (INT, FLOAT, BOOL, STR):
                unbox = {INT: "rt_unbox_int", FLOAT: "rt_unbox_float",
                         BOOL: "rt_unbox_bool", STR: "rt_unbox_str"}[ty]
                out = self.fn.new_vreg(ty, "attr_val")
                self.cur.add("call", out, unbox, (val,))
                return out
            return val
        if isinstance(node.func, ast.Name) and node.func.id == "__global_obj_set":
            slot = self.coerce(self.expr(node.args[0]), INT)
            val = self.box_scalar(self.expr(node.args[1]))
            self.cur.add("call", None, "rt_global_set", (slot, val))
            return None
        if isinstance(node.func, ast.Name) and node.func.id == "__global_obj_get":
            slot = self.coerce(self.expr(node.args[0]), INT)
            return self.call_rt("rt_global_get", [slot], OBJ)
        if isinstance(node.func, ast.Name) and node.func.id == "__math_sqrt":
            v = self.coerce(self.expr(node.args[0]), FLOAT)
            r = self.fn.new_vreg(FLOAT, "sqrt_res")
            self.cur.add("un", r, "fsqrt", v)
            return r
        if isinstance(node.func, ast.Name) and node.func.id == "__gc_collect":
            self.cur.add("call", None, "rt_gc_collect", ())
            return None
        if isinstance(node.func, ast.Name) and node.func.id == "__gc_live":
            return self.call_rt("rt_gc_live", [], INT)
        if isinstance(node.func, ast.Name) and node.func.id == "__dump_frames":
            self.cur.add("call", None, "rt_dump_frames", ())
            return None
        if isinstance(node.func, ast.Name) and node.func.id == "__sys_getrefcount":
            v = self.box_scalar(self.expr(node.args[0]))
            rc = self.call_rt("rt_refcount", [v], INT)
            out = self.fn.new_vreg(INT, "refcnt")
            self.cur.add("bin", out, "add", rc, self.const_int(1))
            return out
        if isinstance(node.func, ast.Name) and node.func.id in ("__sys_settrace", "__sys_setprofile"):
            slot = self.const_int(60 if node.func.id == "__sys_settrace" else 61)
            val = self.expr(node.args[0]) if node.args else None
            flag = self.const_int(0 if val is None else 1)
            self.cur.add("call", None, "rt_global_set", (slot, flag))
            return None
        if isinstance(node.func, ast.Name) and node.func.id in ("__sys_gettrace", "__sys_getprofile"):
            slot = self.const_int(60 if node.func.id == "__sys_gettrace" else 61)
            raw = self.call_rt("rt_global_get", [slot], INT)
            r = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", r, "ne", raw, self.const_int(0))
            return r
        if isinstance(node.func, ast.Name) and node.func.id == "__set_finalizer":
            obj = self.expr(node.args[0])
            a1 = node.args[1]
            fn_label: str | None = None
            if isinstance(a1, ast.Constant) and isinstance(a1.value, str):
                fn_label = "fn_" + a1.value
            else:
                ty1 = getattr(a1, "_vela_ty", None)
                if isinstance(ty1, str) and ty1.startswith("func:"):
                    fn_label = "fn_" + ty1[5:]
            if fn_label is not None:
                addr = self.fn.new_vreg(INT, "fin_addr")
                self.cur.add("addr", addr, fn_label)
                self.cur.add("call", None, "rt_set_finalizer", (obj, addr))
            return None
        if isinstance(node.func, ast.Name) and node.func.id == "__del_var":
            v = self.expr(node.args[0])
            if v is not None and v.ty == FLOAT:
                return self.const_float(0.0)
            if v is not None and v.ty == OBJ:
                z = self.fn.new_vreg(OBJ, "del_null")
                self.cur.add("const", z, 0)
                return z
            return self.const_int(0)
        if isinstance(node.func, ast.Name) and node.func.id == "__ptr_to_int":
            v = self.expr(node.args[0])
            r = self.fn.new_vreg(INT, "raw_ptr")
            self.cur.add("mov", r, v)
            return r
        if isinstance(node.func, ast.Name) and node.func.id == "__gc_is_alive":
            p = self.coerce(self.expr(node.args[0]), INT)
            return self.call_rt("rt_gc_is_alive", [p], BOOL)
        if isinstance(node.func, ast.Name) and node.func.id == "__exc_desc":
            return self.call_rt("rt_exc_desc", [], STR)
        if isinstance(node.func, ast.Name) and node.func.id == "__module_func":
            return self.emit_func_value(node.args[0].value, node)
        if isinstance(node.func, ast.Name) and node.func.id == "__modules_registry":
            obj = self.call_rt("rt_dict_new", [self.const_int(0)], OBJ)
            for i in range(0, len(node.args), 2):
                key = self.box_scalar(self.expr(node.args[i]))
                val = self.expr(node.args[i + 1])
                if val is None:
                    val = self.const_int(0)
                self.cur.add("call", None, "rt_dict_set", (obj, key, val))
            return obj
        if isinstance(node.func, ast.Name) and node.func.id == "__module_new":
            n = self.const_int(len(node.args))
            obj = self.call_rt("rt_list_new", [n], OBJ)
            for i, a in enumerate(node.args):
                native = self.expr(a)
                boxed = self.box_scalar(native)
                idx = self.const_int(i)
                self.cur.add("call", None, "rt_list_set", (obj, idx, boxed))
            return obj
        if isinstance(node.func, ast.Name) and node.func.id == "__module_get":
            obj = self.expr(node.args[0])
            idx = self.expr(node.args[1])
            elem = self.call_rt("rt_list_get", [obj, idx], OBJ)
            ty = getattr(node, "_vela_mod_ty", "bigint")
            if ty == "bigint":
                return elem
            out = self.fn.new_vreg({"str": STR, "int": INT, "bool": BOOL,
                                    "float": FLOAT}.get(ty, OBJ))
            fn = {"str": "rt_unbox_str", "int": "rt_unbox_int",
                  "bool": "rt_unbox_bool", "float": "rt_unbox_float"}.get(ty)
            if fn is None:
                return elem
            self.cur.add("call", out, fn, (elem,))
            return out
        if isinstance(node.func, ast.Attribute) and node.func.attr == "endswith" \
                and self.lw.boxed:
            desc = self.expr(node.func.value)
            suff = self.expr(node.args[0])
            return self.call_rt("rt_str_ends", [desc, suff], BOOL)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "find" \
                and self.lw.boxed:
            desc = self.expr(node.func.value)
            sub = self.expr(node.args[0])
            return self.call_rt("rt_str_find", [desc, sub], INT)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "startswith" \
                and self.lw.boxed:
            desc = self.expr(node.func.value)
            pref = self.expr(node.args[0])
            return self.call_rt("rt_str_starts", [desc, pref], BOOL)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "lower" \
                and self.lw.boxed:
            desc = self.expr(node.func.value)
            return self.call_rt("rt_str_lower", [desc], STR)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "upper" \
                and self.lw.boxed:
            desc = self.expr(node.func.value)
            return self.call_rt("rt_str_upper", [desc], STR)
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("keys", "values", "items") \
                and self.lw.boxed:
            obj = self.expr(node.func.value)
            if node.func.attr == "items":
                return self.dict_items_to_list(obj)
            return self.dict_to_list(obj, node.func.attr == "values")
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("count", "index") \
                and self.lw.boxed:
            obj = self.expr(node.func.value)
            vo = self.box_scalar(self.coerce(self.expr(node.args[0]), INT))
            label = "rt_list_count" if node.func.attr == "count" else "rt_list_index"
            return self.call_rt(label, [obj, vo], INT)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "extend" \
                and self.lw.boxed:
            dst = self.expr(node.func.value)
            src = self.expr(node.args[0])
            self.cur.add("call", None, "rt_list_extend", (dst, src))
            return None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "pop" \
                and self.lw.boxed:
            obj = self.expr(node.func.value)
            if node.args:
                idx = self.coerce(self.expr(node.args[0]), INT)
                val = self.call_rt("rt_list_pop_at", [obj, idx], OBJ)
            else:
                val = self.call_rt("rt_list_pop", [obj], OBJ)
            native = self.fn.new_vreg(INT)
            self.cur.add("call", native, "rt_unbox_int", (val,))
            return native
        if isinstance(node.func, ast.Attribute) and node.func.attr == "add" \
                and self.lw.boxed:
            obj = self.expr(node.func.value)
            native = self.coerce(self.expr(node.args[0]), INT)
            boxed = self.fn.new_vreg(OBJ, "set_val")
            self.cur.add("call", boxed, "rt_box_int", (native,))
            self.cur.add("call", None, "rt_set_add", (obj, boxed))
            return None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and self.lw.boxed:
            obj = self.expr(node.func.value)
            ko = self.box_scalar(self.expr(node.args[0]))
            dflt = self.coerce(self.expr(node.args[1]), INT)
            do = self.fn.new_vreg(OBJ, "get_def")
            self.cur.add("call", do, "rt_box_int", (dflt,))
            val = self.call_rt("rt_dict_get_def", [obj, ko, do], OBJ)
            native = self.fn.new_vreg(INT)
            self.cur.add("call", native, "rt_unbox_int", (val,))
            return native
        if isinstance(node.func, ast.Attribute) and node.func.attr == "append" \
                and self.lw.boxed:
            obj = self.expr(node.func.value)
            val_expr = self.expr(node.args[0])
            if val_expr is not None and val_expr.ty == STR:
                boxed = self.box_scalar(val_expr)
            else:
                native = self.coerce(val_expr, INT)
                boxed = self.fn.new_vreg(OBJ, "append_val")
                self.cur.add("call", boxed, "rt_box_int", (native,))
            self.cur.add("call", None, "rt_list_append", (obj, boxed))
            return None
        name = node.func.id
        if name == "print":
            self.emit_print(node)
            return None
        if name == "len":
            v = self.expr(node.args[0])
            r = self.fn.new_vreg(INT)
            t = getattr(node.args[0], "_vela_ty", None)
            if self.lw.boxed and t in ("dict", "modules"):
                self.cur.add("call", r, "rt_dict_len", (v,))
            elif self.lw.boxed and t in ("list", "tuple", "set", "strlist"):
                self.cur.add("call", r, "rt_list_len", (v,))
            elif self.lw.boxed and t == "bigint":
                self.cur.add("call", r, "rt_bigint_len", (v,))
            else:
                self.cur.add("un", r, "strlen", v)
            return r
        if name == "abs":
            v = self.expr(node.args[0])
            if v.ty == OBJ:
                out = self.fn.new_vreg(OBJ, "abs_num")
                self.cur.add("call", out, "rt_num_abs", (v,))
                return out
            return self.emit_abs(v)
        if name in ("min", "max"):
            a = self.expr(node.args[0])
            b = self.expr(node.args[1])
            if a.ty == OBJ or b.ty == OBJ:
                return self.num_minmax(name == "min", a, b)
            return self.emit_minmax(name == "min", a, b, node)
        if name == "str":
            v = self.expr(node.args[0])
            if v is None:
                r = self.fn.new_vreg(STR)
                self.cur.add("str", r, self.lw.string_offset("None"))
                return r
            if v.ty == STR:
                return v
            if v.ty == OBJ:
                return self.call_rt("rt_num_to_str", [v], STR)
            if self.lw.boxed and v.ty == INT:
                big = self.call_rt("rt_bigint_from_int", [v], OBJ)
                return self.call_rt("rt_num_to_str", [big], STR)
            if self.lw.boxed and v.ty == FLOAT:
                return self.call_rt("rt_float_to_str", [v], STR)
            if self.lw.boxed and v.ty == BOOL:
                out = self.fn.new_vreg(STR)
                tb = self.new_block("str_t")
                fb = self.new_block("str_f")
                jb = self.new_block("str_j")
                self.cur.add("br", None, v, tb.block.label, fb.block.label)
                self.use(tb)
                self.cur.add("str", out, self.lw.string_offset("True"))
                self.cur.add("jmp", None, jb.block.label)
                self.use(fb)
                self.cur.add("str", out, self.lw.string_offset("False"))
                self.cur.add("jmp", None, jb.block.label)
                self.use(jb)
                return out
            raise UnsupportedError("str() on native int in this profile", node)
        if self.lw.boxed and name == "sum":
            seq = self.expr(node.args[0])
            acc = self.fn.new_vreg(INT, "sum_acc")
            init_v = self.coerce(self.expr(node.args[1]), INT) if len(node.args) == 2 else self.const_int(0)
            self.cur.add("mov", acc, init_v)
            length = self.call_rt("rt_list_len", [seq], INT)
            idx = self.fn.new_vreg(INT, "sum_i")
            self.cur.add("mov", idx, self.const_int(0))
            cond_b = self.new_block("sum_cond")
            body_b = self.new_block("sum_body")
            exit_b = self.new_block("sum_exit")
            self.cur.add("jmp", None, cond_b.block.label)
            self.use(cond_b)
            c = self.fn.new_vreg(BOOL)
            self.cur.add("cmp", c, "lt", idx, length)
            self.cur.add("br", None, c, body_b.block.label, exit_b.block.label)
            self.use(body_b)
            elem = self.call_rt("rt_list_get", [seq, idx], OBJ)
            ev = self.call_rt("rt_unbox_int", [elem], INT)
            nxt_acc = self.fn.new_vreg(INT)
            self.cur.add("bin", nxt_acc, "add", acc, ev)
            self.cur.add("mov", acc, nxt_acc)
            nxt_i = self.fn.new_vreg(INT)
            self.cur.add("bin", nxt_i, "add", idx, self.const_int(1))
            self.cur.add("mov", idx, nxt_i)
            self.cur.add("jmp", None, cond_b.block.label)
            self.use(exit_b)
            return acc
        if self.lw.boxed and name in ("list", "tuple", "set"):
            out = self.call_rt("rt_list_new", [self.const_int(0)], OBJ)
            if node.args:
                src = self.expr(node.args[0])
                self.cur.add("call", None, "rt_list_extend", (out, src))
            if name == "tuple":
                self.cur.add("call", None, "rt_tuple_type", (out))
            elif name == "set":
                self.cur.add("call", None, "rt_set_type", (out,))
            return out
        if name in ("ValueError", "TypeError", "StopIteration",
                    "AttributeError", "KeyError", "IndexError", "Exception"):
            for a in node.args:
                self.expr(a)
            code_val = {"ValueError": 1, "TypeError": 2, "StopIteration": 3,
                        "AttributeError": 4, "KeyError": 5, "IndexError": 6}.get(name, 1)
            return self.const_int(code_val)
        if name == "int":
            v = self.expr(node.args[0])
            if v.ty == FLOAT:
                r = self.fn.new_vreg(INT)
                self.cur.add("un", r, "f2i", v)
                return r
            if v.ty == BOOL:
                r = self.fn.new_vreg(INT)
                self.cur.add("mov", r, v)
                return r
            return self.coerce(v, INT)
        if name == "float":
            v = self.expr(node.args[0])
            return self.coerce(v, FLOAT)
        if name == "bool":
            v = self.expr(node.args[0])
            r = self.fn.new_vreg(BOOL)
            self.cur.add("un", r, "bool", v)
            return r
        target = name
        ft = self.var_types.get(name)
        if ft == "func" or (isinstance(ft, str) and ft.startswith("func*")):
            code = self.expr(node.func)
            argv = self.call_rt("rt_list_new", [self.const_int(len(node.args))], OBJ)
            for idx_i, a in enumerate(node.args):
                boxed_a = self.box_scalar(self.expr(a))
                self.cur.add("call", None, "rt_list_set",
                             (argv, self.const_int(idx_i), boxed_a))
            res = self.fn.new_vreg(OBJ, "dyn_call")
            self.cur.add("call", res, "rt_code_call_argv", (code, argv))
            ret_ty = ft[5:] if isinstance(ft, str) and ft.startswith("func*") else INT
            if ret_ty == NONE:
                return None
            if ret_ty in (INT, FLOAT, BOOL, STR) and not (self.dyn and ret_ty == INT):
                native = self.fn.new_vreg(ret_ty, "dyn_ret")
                unbox = {INT: "rt_unbox_int", FLOAT: "rt_unbox_float",
                         BOOL: "rt_unbox_bool", STR: "rt_unbox_str"}[ret_ty]
                self.cur.add("call", native, unbox, (res,))
                return native
            return res
        if isinstance(ft, str) and ft.startswith("func:"):
            target = ft[5:]
        info = self.lw.an.funcs.get(target)
        if info is None:
            raise UnsupportedError(f"call to unknown function '{name}'", node)
        if self.lw.boxed:
            line = self.const_int(node.lineno)
            self.cur.add("call", None, "rt_set_line", (line,))
        args: list[VReg | None] = []
        for i, a in enumerate(node.args):
            v = self.expr(a)
            v = self.coerce(v, info.param_types[i])
            if self.lw.boxed and v.ty in (INT, FLOAT, BOOL, STR):
                boxed = self.fn.new_vreg(OBJ, "boxed_arg")
                label = {INT: "rt_box_int", FLOAT: "rt_box_float",
                         BOOL: "rt_box_bool", STR: "rt_box_str"}[v.ty]
                self.cur.add("call", boxed, label, (v,))
                v = boxed
            args.append(v)
        ret = info.ret_type or NONE
        if self.lw.boxed and ret in (INT, FLOAT, BOOL, STR) \
                and not (self.dyn and ret == INT):
            obj = self.fn.new_vreg(OBJ, "boxed_call")
            self.cur.add("call", obj, info.label, tuple(args))
            native = self.fn.new_vreg(ret, "unboxed")
            unbox = {INT: "rt_unbox_int", FLOAT: "rt_unbox_float",
                     BOOL: "rt_unbox_bool", STR: "rt_unbox_str"}[ret]
            self.cur.add("call", native, unbox, (obj,))
            self.cur.add("call", None, "rt_decref", (obj,))
            return native
        dst_ty = OBJ if ((self.dyn and ret == INT)
                         or ret in ("bigint", "list", "dict", "tuple", "set", "bytes", "strlist", "module", "modules")
                         or is_func_type(ret)) else ret
        dst = self.fn.new_vreg(dst_ty) if ret != NONE else None
        self.cur.add("call", dst, info.label, tuple(args))
        return dst

    def num_minmax(self, want_min: bool, a: VReg, b: VReg) -> VReg:
        aa = self.as_num(a)
        bb = self.as_num(b)
        cv = self.fn.new_vreg(INT, "mm_cmp")
        self.cur.add("call", cv, "rt_num_cmp", (aa, bb))
        z = self.const_int(0)
        cond = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", cond, "lt" if want_min else "gt", cv, z)
        pick_a = self.new_block("mm_a")
        pick_b = self.new_block("mm_b")
        join = self.new_block("mm_join")
        out = self.fn.new_vreg(OBJ, "mm_out")
        self.cur.add("br", None, cond, pick_a.block.label, pick_b.block.label)
        self.use(pick_a)
        self.cur.add("mov", out, aa)
        self.cur.add("jmp", None, join.block.label)
        self.use(pick_b)
        self.cur.add("mov", out, bb)
        self.cur.add("jmp", None, join.block.label)
        self.use(join)
        return out

    def emit_abs(self, v: VReg) -> VReg:
        if v.ty == FLOAT:
            r = self.fn.new_vreg(FLOAT)
            self.cur.add("un", r, "fabs", v)
            return r
        src = self.coerce(v, INT)
        r = self.fn.new_vreg(INT)
        zero = self.const_int(0)
        cond = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", cond, "lt", src, zero)
        neg_b = self.new_block("abs_neg")
        pos_b = self.new_block("abs_pos")
        end_b = self.new_block("abs_end")
        self.cur.add("br", None, cond, neg_b.block.label, pos_b.block.label)
        self.use(neg_b)
        n = self.fn.new_vreg(INT)
        self.cur.add("un", n, "neg", src)
        self.cur.add("mov", r, n)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(pos_b)
        self.cur.add("mov", r, src)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(end_b)
        return r

    def emit_minmax(self, is_min: bool, a: VReg, b: VReg, node: ast.AST) -> VReg:
        ty = FLOAT if FLOAT in (a.ty, b.ty) else INT
        x = self.coerce(a, ty)
        y = self.coerce(b, ty)
        r = self.fn.new_vreg(ty)
        c = self.fn.new_vreg(BOOL)
        self.cur.add("cmp", c, "lt" if is_min else "gt", x, y)
        take_b = self.new_block("mm_b")
        take_a = self.new_block("mm_a")
        end_b = self.new_block("mm_end")
        self.cur.add("br", None, c, take_a.block.label, take_b.block.label)
        self.use(take_a)
        self.cur.add("mov", r, x)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(take_b)
        self.cur.add("mov", r, y)
        self.cur.add("jmp", None, end_b.block.label)
        self.use(end_b)
        return r

    def emit_print(self, node: ast.Call) -> None:
        for i, arg in enumerate(node.args):
            if i:
                self.cur.add("print", None, "space", None)
            if self.lw.boxed and isinstance(arg, ast.Constant) \
                    and isinstance(arg.value, int) and not isinstance(arg.value, bool) \
                    and not (-2**63 <= arg.value < 2**63):
                d = self.fn.new_vreg(STR, "big_desc")
                self.cur.add("str", d, self.lw.string_offset(str(arg.value)))
                o = self.fn.new_vreg(OBJ, "big_obj")
                self.cur.add("call", o, "rt_bigint_from_decimal", (d,))
                self.cur.add("call", None, "rt_print_obj", (o,))
                self.cur.add("call", None, "rt_decref", (o,))
                continue
            if self.lw.boxed and isinstance(arg, ast.Constant) \
                    and isinstance(arg.value, bytes):
                try:
                    text = arg.value.decode("ascii")
                except UnicodeDecodeError:
                    raise UnsupportedError("non-ASCII bytes literal", arg)
                d = self.fn.new_vreg(STR, "bytes_desc")
                self.cur.add("str", d, self.lw.string_offset(text))
                o = self.fn.new_vreg(OBJ, "bytes_obj")
                self.cur.add("call", o, "rt_box_bytes", (d,))
                self.cur.add("call", None, "rt_print_obj", (o,))
                self.cur.add("call", None, "rt_decref", (o,))
                continue
            if self.dyn and getattr(arg, "_vela_ty", None) == INT:
                v = self.expr(arg)
                if v.ty == OBJ:
                    self.cur.add("call", None, "rt_print_obj", (v,))
                    continue
            if self.lw.boxed and getattr(arg, "_vela_ty", None) == "bigint":
                v = self.expr(arg)
                self.cur.add("call", None, "rt_print_obj", (v,))
                continue
            if self.lw.boxed and (
                    isinstance(arg, (ast.List, ast.Tuple, ast.Set, ast.Dict))
                    or getattr(arg, "_vela_ty", None) in ("list", "tuple", "set", "dict", "strlist")):
                v = self.expr(arg)
                self.cur.add("call", None, "rt_print_obj", (v,))
                continue
            v = self.expr(arg)
            if self.lw.boxed and (v is None or v.ty in (NONE, INT, FLOAT, BOOL, STR)):
                o = self.fn.new_vreg(OBJ, "boxed")
                if v is None or v.ty == NONE:
                    self.cur.add("call", o, "rt_box_none", ())
                elif v.ty == INT:
                    self.cur.add("call", o, "rt_box_int", (v,))
                elif v.ty == FLOAT:
                    self.cur.add("call", o, "rt_box_float", (v,))
                elif v.ty == STR:
                    self.cur.add("call", o, "rt_box_str", (v,))
                else:
                    self.cur.add("call", o, "rt_box_bool", (v,))
                self.cur.add("print", None, "obj", o)
                self.cur.add("call", None, "rt_decref", (o,))
                continue
            if v is None or v.ty == NONE:
                self.cur.add("print", None, "none", None)
            elif v.ty == FLOAT:
                self.cur.add("print", None, "float", v)
            elif v.ty == BOOL:
                self.cur.add("print", None, "bool", v)
            elif v.ty == STR:
                self.cur.add("print", None, "str", v)
            else:
                if self.lw.boxed:
                    o = self.fn.new_vreg(OBJ, "boxed")
                    self.cur.add("call", o, "rt_box_int", (v,))
                    self.cur.add("print", None, "obj", o)
                else:
                    self.cur.add("print", None, "int", v)
        self.cur.add("print", None, "nl", None)


class BlockRef:
    __slots__ = ("builder", "block")

    def __init__(self, builder: FuncBuilder, block) -> None:
        self.builder = builder
        self.block = block

    @property
    def label(self) -> str:
        return self.block.label
