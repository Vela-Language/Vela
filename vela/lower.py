"""AST -> Vela IR lowering.

The lowering is deliberately simple and explicit: each source variable becomes
one virtual register, expressions become typed instructions, and Python's
short-circuit / conditional expressions are turned into ordinary control flow.
"""
from __future__ import annotations

import ast

from .analyze import Analyzer, FuncInfo, is_numeric, norm_numeric
from .errors import UnsupportedError, VelaCompileError, VelaTypeError
from .ir import (BOOL, FLOAT, GPR_TYPES, INT, NONE, Program, STR, UNKNOWN,
                 Function, VReg)
from .pe import DataBuilder

PY_NONE = None


class Lowerer:
    def __init__(self, analyzer: Analyzer, rdata: DataBuilder,
                 filename: str = "<input>") -> None:
        self.an = analyzer
        self.rdata = rdata
        self.filename = filename
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
        fn = Function(info.label, info.ret_type or NONE)
        b = FuncBuilder(self, fn, info.name, info.var_types, info.ret_type or NONE)
        for i, (pname, pty) in enumerate(zip(info.params, info.param_types)):
            v = fn.new_vreg(pty, pname)
            b.locals[pname] = v
            b.assigned.add(pname)
            fn.params.append(v)
            fn.entry.add("arg", v, i)
        for stmt in info.node.body:
            b.stmt(stmt)
        b.finish()
        return fn

    def lower_main(self) -> Function:
        fn = Function("fn___vela_main__", NONE)
        b = FuncBuilder(self, fn, "__main__", self.an.main_vars, NONE)
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
        raise VelaTypeError(f"cannot convert {v.ty} to {want}")

    def var(self, name: str, node: ast.AST) -> VReg:
        v = self.locals.get(name)
        if v is None or name not in self.assigned:
            raise VelaCompileError(
                f"local variable '{name}' is used before assignment", node)
        return v

    def bind(self, name: str, value: VReg | None, node: ast.AST) -> None:
        declared = self.var_types.get(name)
        if declared is None:
            declared = value.ty if value is not None else NONE
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

    def finish(self) -> None:
        # Only the block that is still "open" at the end of lowering is a real
        # fall-through continuation; terminate it with `ret`.  Other blocks
        # without a terminator are unreachable (e.g. after an early return) and
        # get a harmless forward jump so the IR stays well formed.
        for i, block in enumerate(self.fn.blocks):
            if block.terminator is not None:
                continue
            if block is self.cur:
                if self.ret_ty != NONE:
                    raise VelaTypeError(
                        f"{self.name}() may fall off the end without returning a value")
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
            value = self.expr(node.value)
            self.bind(node.targets[0].id, value, node)
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
        if isinstance(node, ast.For):
            self.for_stmt(node)
            return
        if isinstance(node, ast.Return):
            if node.value is None:
                if self.ret_ty != NONE:
                    raise VelaTypeError(
                        f"{self.name}() must return a value of type {self.ret_ty}")
                self.cur.add("ret", None, None)
            else:
                v = self.coerce(self.expr(node.value), self.ret_ty)
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

    def for_stmt(self, node: ast.For) -> None:
        if node.orelse:
            raise UnsupportedError("for/else", node)
        args = node.iter.args
        one = len(args) == 1
        start_node = None if one else args[0]
        stop_node = args[0] if one else args[1]
        step_node = None if len(args) < 3 else args[2]
        start = self.const_int(0) if start_node is None else self.coerce(self.expr(start_node), INT)
        stop = self.coerce(self.expr(stop_node), INT)
        step = self.const_int(1) if step_node is None else self.coerce(self.expr(step_node), INT)
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
                return self.const_int(v)
            if isinstance(v, float):
                return self.const_float(v)
            if isinstance(v, str):
                r = self.fn.new_vreg(STR)
                self.cur.add("str", r, self.lw.string_offset(v))
                return r
            raise UnsupportedError(f"constant of type {type(v).__name__}", node)
        if isinstance(node, ast.Name):
            return self.var(node.id, node)
        if isinstance(node, ast.BinOp):
            return self.binop(node)
        if isinstance(node, ast.UnaryOp):
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

    def binop(self, node: ast.BinOp) -> VReg:
        left = self.expr(node.left)
        right = self.expr(node.right)
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
        name = node.func.id
        if name == "print":
            self.emit_print(node)
            return None
        if name == "len":
            v = self.expr(node.args[0])
            r = self.fn.new_vreg(INT)
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
        args: list[VReg | None] = []
        for i, a in enumerate(node.args):
            v = self.expr(a)
            args.append(self.coerce(v, info.param_types[i]))
        ret = info.ret_type or NONE
        dst = self.fn.new_vreg(ret) if ret != NONE else None
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
            v = self.expr(arg)
            if v is None or v.ty == NONE:
                self.cur.add("print", None, "none", None)
            elif v.ty == FLOAT:
                self.cur.add("print", None, "float", v)
            elif v.ty == BOOL:
                self.cur.add("print", None, "bool", v)
            elif v.ty == STR:
                self.cur.add("print", None, "str", v)
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
