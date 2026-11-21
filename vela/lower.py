"""AST -> Vela IR lowering.

The lowering is deliberately simple and explicit: each source variable becomes
one virtual register, expressions become typed instructions, and Python's
short-circuit / conditional expressions are turned into ordinary control flow.
"""
from __future__ import annotations

import ast

from .analyze import Analyzer, FuncInfo, is_numeric, norm_numeric
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

    # -- public ------------------------------------------------------------
    def run(self) -> Program:
        prog = Program(strings=self.rdata)
        for info in self.an.funcs.values():
            prog.functions.append(self.lower_function(info))
        prog.main = self.lower_main()
        prog.functions.append(prog.main)
        return prog

    def lower_function(self, info: FuncInfo) -> Function:
        native_ret = info.ret_type or NONE
        boxed_ret = self.boxed and (
            native_ret in (INT, FLOAT, BOOL, STR)
            or (self.dynamic_int and native_ret == INT))
        fn = Function(info.label, OBJ if boxed_ret else native_ret)
        b = FuncBuilder(self, fn, info.name, info.var_types, info.ret_type or NONE)
        b.globals = set(info.globals)
        b.param_names = set(info.params)
        for i, (pname, pty) in enumerate(zip(info.params, info.param_types)):
            if self.boxed and pty in (INT, FLOAT, BOOL, STR):
                v = fn.new_vreg(OBJ, pname + "_obj")
                fn.params.append(v)
                fn.entry.add("arg", v, i)
            elif self.boxed and pty in ("list", "dict", "tuple", "set"):
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
        if self.boxed:
            zero = fn.new_vreg(INT, "zero_slot")
            fn.entry.add("const", zero, 0)
            for name, ty in info.var_types.items():
                if ty in (INT, FLOAT, BOOL, STR) and name not in b.locals:
                    slot = fn.new_vreg(OBJ, name + "_obj")
                    fn.entry.add("mov", slot, zero)
                    b.locals[name] = slot
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
        if want in ("list", "dict", "tuple", "set", "bigint") and v.ty == OBJ:
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

    def box_scalar(self, v: VReg) -> VReg:
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
        if self.lw.boxed and declared in ("list", "dict", "tuple", "set", "bigint") and v.ty == OBJ:
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
        if self.lw.boxed and declared in ("list", "dict", "tuple", "set", "bigint"):
            v = self.locals.get(name)
            if v is None or v.ty != OBJ:
                v = self.fn.new_vreg(OBJ, name + "_list")
                self.locals[name] = v
            if value is None:
                raise VelaTypeError(f"cannot assign None to list '{name}'", node)
            self.cur.add("mov", v, value)
            self.assigned.add(name)
            return
        if self.lw.boxed and declared in (INT, FLOAT, BOOL, STR):
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

    def boxed_cleanup(self) -> None:
        if not self.lw.boxed:
            return
        for name, v in list(self.locals.items()):
            if name in self.param_names:
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
                    boxed = self.fn.new_vreg(OBJ, "boxed_set")
                    self.cur.add("call", boxed, "rt_box_int", (native,))
                    self.cur.add("call", None, "rt_list_set", (obj, key, boxed))
                return
            value = self.expr(node.value)
            if isinstance(target, ast.Name):
                cv = self.const_int_value(node.value)
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
                    self.boxed_cleanup()
                    if self.lw.boxed:
                        self.cur.add("call", None, "rt_leave", ())
                    self.cur.add("ret", None, o)
                else:
                    self.emit_finally()
                    self.boxed_cleanup()
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
        types = {"ValueError": 1, "TypeError": 2, "Exception": 0,
                 "BaseException": 0}
        for i, h in enumerate(node.handlers):
            self.use(dispatch[i])
            if h.type is None:
                self.cur.add("jmp", None, hblocks[i].block.label)
            else:
                expected = types.get(getattr(h.type, "id", ""), 1)
                if expected == 0:
                    self.cur.add("jmp", None, hblocks[i].block.label)
                else:
                    code = self.call_rt("rt_exc_code", [], INT)
                    want = self.const_int(expected)
                    cond = self.fn.new_vreg(BOOL)
                    self.cur.add("cmp", cond, "eq", code, want)
                    self.cur.add("br", None, cond, hblocks[i].block.label,
                                 dispatch[i + 1].block.label)
        self.use(dispatch[n])
        self.cur.add("call", None, "rt_reraise", ())
        self.cur.add("jmp", None, done.block.label)
        for i, h in enumerate(node.handlers):
            self.use(hblocks[i])
            if h.name:
                self.var_types[h.name] = STR
                desc = self.call_rt("rt_exc_desc", [], STR)
                self.bind(h.name, desc, node)
            self.finally_stack.append(list(node.finalbody))
            for stmt in h.body:
                self.stmt(stmt)
            self.finally_stack.pop()
            for stmt in node.finalbody:
                self.stmt(stmt)
            if self.cur.terminator is None:
                self.cur.add("jmp", None, done.block.label)
        self.use(done)

    def emit_finally(self) -> None:
        for block in reversed(self.finally_stack):
            for stmt in block:
                self.stmt(stmt)

    def raise_stmt(self, node: ast.Raise) -> None:
        desc = self.const_int(0)
        code_val = 1
        if node.exc is not None:
            desc = self.expr(node.exc.args[0])
            code_val = {"ValueError": 1, "TypeError": 2}.get(node.exc.func.id, 1)
        cause = self.const_int(0)
        if node.cause is not None:
            c = node.cause
            if not (isinstance(c, ast.Call) and len(c.args) == 1):
                raise UnsupportedError("raise ... from with a non-call cause", node)
            cause = self.coerce(self.expr(c.args[0]), STR)
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
            return self.var(node.id, node)
        if isinstance(node, ast.Dict):
            obj = self.call_rt("rt_dict_new", [], OBJ)
            for k, v in zip(node.keys, node.values):
                if isinstance(k.value, str):
                    ko = self.box_scalar(self.expr(k))
                else:
                    ko = self.box_scalar(self.const_int(k.value))
                vo = self.fn.new_vreg(OBJ, "dict_val")
                vn = self.const_int(v.value)
                self.cur.add("call", vo, "rt_box_int", (vn,))
                self.cur.add("call", None, "rt_dict_set", (obj, ko, vo))
            return obj
        if isinstance(node, ast.Set):
            n = self.const_int(len(node.elts))
            obj = self.call_rt("rt_list_new", [n], OBJ)
            for i, item in enumerate(node.elts):
                native = self.expr(item)
                boxed = self.fn.new_vreg(OBJ, "set_elem")
                self.cur.add("call", boxed, "rt_box_int", (native,))
                idx = self.const_int(i)
                self.cur.add("call", None, "rt_list_set", (obj, idx, boxed))
            self.cur.add("call", None, "rt_set_type", (obj,))
            return obj
        if isinstance(node, ast.Tuple):
            n = self.const_int(len(node.elts))
            obj = self.call_rt("rt_list_new", [n], OBJ)
            for i, item in enumerate(node.elts):
                native = self.expr(item)
                boxed = self.fn.new_vreg(OBJ, "tuple_elem")
                self.cur.add("call", boxed, "rt_box_int", (native,))
                idx = self.const_int(i)
                self.cur.add("call", None, "rt_list_set", (obj, idx, boxed))
            self.cur.add("call", None, "rt_tuple_type", (obj,))
            return obj
        if isinstance(node, ast.List):
            n = self.const_int(len(node.elts))
            obj = self.call_rt("rt_list_new", [n], OBJ)
            for i, item in enumerate(node.elts):
                native = self.expr(item)
                boxed = self.fn.new_vreg(OBJ, "list_elem")
                self.cur.add("call", boxed, "rt_box_int", (native,))
                idx = self.const_int(i)
                self.cur.add("call", None, "rt_list_set", (obj, idx, boxed))
            return obj
        if isinstance(node, ast.Subscript) and (
                isinstance(node.value, ast.List)
                or getattr(node.value, "_vela_ty", None) in ("list", "tuple")):
            obj = self.expr(node.value)
            idx = self.expr(node.slice)
            elem = self.call_rt("rt_list_get", [obj, idx], OBJ)
            native = self.fn.new_vreg(INT)
            self.cur.add("call", native, "rt_unbox_int", (elem,))
            return native
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) \
                and getattr(node.value, "_vela_ty", None) == "str":
            desc = self.expr(node.value)
            start = self.coerce(self.expr(node.slice.lower), INT) if node.slice.lower else self.const_int(0)
            stop = self.coerce(self.expr(node.slice.upper), INT) if node.slice.upper else self.call_rt("rt_str_len", [desc], INT)
            return self.call_rt("rt_str_slice", [desc, start, stop], STR)
        if isinstance(node, ast.Subscript) and getattr(node.value, "_vela_ty", None) == "str":
            desc = self.expr(node.value)
            idx = self.coerce(self.expr(node.slice), INT)
            return self.call_rt("rt_str_index", [desc, idx], STR)
        if isinstance(node, ast.Subscript) and getattr(node.value, "_vela_ty", None) == "dict":
            obj = self.expr(node.value)
            keyo = self.box_scalar(self.expr(node.slice))
            val = self.call_rt("rt_dict_get", [obj, keyo], OBJ)
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
        return None

    def binop(self, node: ast.BinOp) -> VReg:
        if self.dyn and isinstance(node.op, (ast.FloorDiv, ast.Mod)):
            left = self.expr(node.left)
            right = self.expr(node.right)
            a = self.as_num(left)
            b = self.as_num(right)
            label = "rt_num_floordiv" if isinstance(node.op, ast.FloorDiv) else "rt_num_mod"
            out = self.fn.new_vreg(OBJ, "num_div")
            self.cur.add("call", out, label, (a, b))
            return out
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
            raise UnsupportedError(
                "bigint division requires compile-time constant operands", node)
        left = self.expr(node.left)
        right = self.expr(node.right)
        if self.lw.boxed and "bigint" in (
                getattr(node, "_vela_ty", None),
                getattr(node.left, "_vela_ty", None),
                getattr(node.right, "_vela_ty", None)):
            a = self.as_bigint(left)
            b = self.as_bigint(right)
            if isinstance(node.op, (ast.FloorDiv, ast.Mod)):
                if right.ty != INT:
                    raise UnsupportedError("bigint division needs an int divisor", node)
                if isinstance(node.op, ast.FloorDiv):
                    out = self.fn.new_vreg(OBJ, "big_div")
                    self.cur.add("call", out, "rt_bigint_div_small", (a, right))
                    return out
                res = self.fn.new_vreg(INT, "big_mod")
                self.cur.add("call", res, "rt_bigint_mod_small", (a, right))
                return res
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
            vo = self.box_scalar(self.expr(node.left))
            label = "rt_dict_contains" if getattr(node.comparators[0], "_vela_ty", None) == "dict" else "rt_contains"
            res = self.call_rt(label, [right, vo], BOOL)
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

    def call(self, node: ast.Call) -> VReg | None:
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
            native = self.coerce(self.expr(node.args[0]), INT)
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
            if self.lw.boxed and t == "dict":
                self.cur.add("call", r, "rt_dict_len", (v,))
            elif self.lw.boxed and t in ("list", "tuple", "set"):
                self.cur.add("call", r, "rt_list_len", (v,))
            elif self.lw.boxed and t == "bigint":
                self.cur.add("call", r, "rt_bigint_len", (v,))
            else:
                self.cur.add("un", r, "strlen", v)
            return r
        if name == "abs":
            v = self.expr(node.args[0])
            return self.emit_abs(v)
        if name in ("min", "max"):
            a = self.expr(node.args[0])
            b = self.expr(node.args[1])
            return self.emit_minmax(name == "min", a, b, node)
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
        info = self.lw.an.funcs.get(name)
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
        dst_ty = OBJ if (self.dyn and ret == INT) else ret
        dst = self.fn.new_vreg(dst_ty) if ret != NONE else None
        self.cur.add("call", dst, info.label, tuple(args))
        return dst

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
                    or getattr(arg, "_vela_ty", None) in ("list", "tuple", "set", "dict")):
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
