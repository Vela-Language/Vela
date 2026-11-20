"""Static analysis for the Vela native profile.

Two jobs:

1. Coverage: reject everything the native profile (v0.1) cannot compile, with
   precise source locations.
2. Monomorphic type inference: every source variable and function parameter
   gets exactly one type (int, float, bool, str or None).  The result drives
   lowering (int64 vs float64 operations, coercions) and the register class
   assignment.

The inference is a small fixpoint over function signatures.  It is deliberately
conservative: `int` widens to `float` at assignments/arguments/returns, never
the other way around.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .errors import UnsupportedError, VelaCompileError, VelaTypeError
from .ir import BOOL, FLOAT, INT, NONE, STR, UNKNOWN

ANNOTATIONS = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR, "None": NONE}
NUMERIC_RANK = {BOOL: 0, INT: 1, FLOAT: 2}


def type_name(ty: str | None) -> str:
    return ty or "unknown"


def annotation_type(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name) and node.id in ANNOTATIONS:
        return ANNOTATIONS[node.id]
    if isinstance(node, ast.Constant) and node.value is None:
        return NONE
    raise UnsupportedError(f"type annotation {ast.unparse(node)!r}", node)


def is_numeric(ty: str | None) -> bool:
    return ty in (INT, FLOAT, BOOL)


def norm_numeric(ty: str) -> str:
    return INT if ty == BOOL else ty


def assignable(src: str | None, dst: str | None) -> bool:
    if src is None or dst is None:
        return True
    if src == dst:
        return True
    if src == BOOL and dst == INT:
        return True
    if src == INT and dst == FLOAT:
        return True
    return False


def join(a: str | None, b: str | None) -> str | None:
    """Join two types for a value that can be either (symmetric)."""
    if a is None:
        return b
    if b is None:
        return a
    if a == b:
        return a
    if a in NUMERIC_RANK and b in NUMERIC_RANK:
        return FLOAT if FLOAT in (a, b) else INT
    raise VelaTypeError(f"cannot mix {a} and {b} in one expression")


@dataclass
class FuncInfo:
    node: ast.FunctionDef
    name: str
    params: list[str] = field(default_factory=list)
    param_ann: list[str | None] = field(default_factory=list)
    ret_ann: str | None = None
    param_types: list[str | None] = field(default_factory=list)
    ret_type: str | None = None
    var_types: dict[str, str | None] = field(default_factory=dict)
    returns: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "fn_" + self.name


def always_returns(stmts: list[ast.stmt]) -> bool:
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, ast.Return):
        return True
    if isinstance(last, ast.If):
        return always_returns(last.body) and always_returns(last.orelse)
    if isinstance(last, ast.While) and isinstance(last.test, ast.Constant) and last.test.value is True:
        return True
    return False


class Analyzer:
    def __init__(self, tree: ast.Module, filename: str = "<input>") -> None:
        self.tree = tree
        self.filename = filename
        self.funcs: dict[str, FuncInfo] = {}
        self.main_vars: dict[str, str | None] = {}
        self.has_main_code = False
        self._module_scope = False

    # -- public ------------------------------------------------------------
    def run(self) -> "Analyzer":
        self._collect_functions()
        for _ in range(10):
            changed = False
            for info in self.funcs.values():
                changed |= self._infer_func(info)
            if not changed:
                break
        for info in self.funcs.values():
            self._infer_func(info, commit=True)
            self._finalize(info)
        self._infer_main()
        return self

    # -- collection --------------------------------------------------------
    def _collect_functions(self) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef):
                if node.decorator_list:
                    raise UnsupportedError("decorators", node)
                if getattr(node, "type_params", None):
                    raise UnsupportedError("generic type parameters (PEP 695)", node)
                a = node.args
                if a.vararg or a.kwarg or a.kwonlyargs:
                    raise UnsupportedError("varargs / keyword-only arguments", node)
                if a.defaults:
                    raise UnsupportedError("default argument values", node)
                if node.name in self.funcs:
                    raise VelaCompileError(f"duplicate function definition '{node.name}'", node)
                all_args = list(a.posonlyargs) + list(a.args)
                if len(all_args) > 4:
                    raise UnsupportedError("more than 4 parameters (Win64 ABI v0.1 limit)", node)
                info = FuncInfo(node=node, name=node.name)
                for arg in all_args:
                    info.params.append(arg.arg)
                    info.param_ann.append(annotation_type(arg.annotation))
                info.ret_ann = annotation_type(node.returns)
                info.param_types = list(info.param_ann)
                info.ret_type = info.ret_ann
                self.funcs[node.name] = info

    # -- inference ---------------------------------------------------------
    def _infer_func(self, info: FuncInfo, commit: bool = False) -> bool:
        old_params = list(info.param_types)
        old_ret = info.ret_type
        env: dict[str, str | None] = {}
        for i, p in enumerate(info.params):
            env[p] = info.param_ann[i] or info.param_types[i]
        rets: list[str] = []
        for stmt in info.node.body:
            self._stmt(stmt, env, rets, info)
        for i, p in enumerate(info.params):
            if info.param_ann[i] is not None:
                continue
            new = env.get(p)
            if new is not None and new != info.param_types[i]:
                if not assignable(info.param_types[i], new):
                    raise VelaTypeError(
                        f"parameter '{p}' of {info.name}() is inferred as "
                        f"{type_name(info.param_types[i])} but used as {new}", info.node)
                info.param_types[i] = new
        rt: str | None = None
        for t in rets:
            rt = join(rt, t)
        if info.ret_ann is not None:
            for t in rets:
                if not assignable(t, info.ret_ann):
                    raise VelaTypeError(
                        f"{info.name}() is declared to return {info.ret_ann} "
                        f"but returns {t}", info.node)
            rt = info.ret_ann
        elif rt is not None and rt != info.ret_type:
            info.ret_type = rt
        if info.ret_ann is None and rets and not always_returns(info.node.body):
            raise VelaTypeError(
                f"{info.name}() returns a value on some paths but falls off the "
                f"end of the function; add an explicit return", info.node)
        if info.ret_ann is not None and info.ret_ann != NONE and not always_returns(info.node.body):
            raise VelaTypeError(
                f"{info.name}() is declared to return {info.ret_ann} but may fall "
                f"off the end without returning a value", info.node)
        if commit:
            if info.ret_ann is None and info.ret_type is None:
                info.ret_type = INT if any(t is not None for t in rets) else NONE
            info.var_types = dict(env)
            info.returns = rets
        return old_params != info.param_types or old_ret != info.ret_type

    def _finalize(self, info: FuncInfo) -> None:
        info.param_types = [t if t is not None else INT for t in info.param_types]
        if info.ret_type is None:
            info.ret_type = INT if any(t is not None for t in info.returns) else NONE
        info.var_types = {k: (v if v is not None else INT) for k, v in info.var_types.items()}
        for p in info.params:
            info.var_types.setdefault(p, INT)

    def _infer_main(self) -> None:
        env: dict[str, str | None] = {}
        rets: list[str] = []
        self._module_scope = True
        try:
            for stmt in self.tree.body:
                if isinstance(stmt, ast.FunctionDef):
                    continue
                self.has_main_code = True
                self._stmt(stmt, env, rets, None)
        finally:
            self._module_scope = False
        if rets:
            raise UnsupportedError("return statement at module level", self.tree)
        self.main_vars = {k: (v if v is not None else INT) for k, v in env.items()}

    # -- statements --------------------------------------------------------
    def _stmt(self, node: ast.stmt, env: dict[str, str | None],
              rets: list[str], info: FuncInfo | None) -> None:
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise UnsupportedError("assignment to anything but a single name", node)
            target = node.targets[0]
            want = env.get(target.id)
            ty = self._infer(node.value, env, want)
            self._bind(env, target.id, ty, target)
            node._vela_ty = ty
            return
        if isinstance(node, ast.AnnAssign):
            if node.value is None or not isinstance(node.target, ast.Name):
                raise UnsupportedError("annotation without a value", node)
            declared = annotation_type(node.annotation)
            ty = self._infer(node.value, env, declared)
            if not assignable(ty, declared):
                raise VelaTypeError(
                    f"cannot assign {type_name(ty)} to variable '{node.target.id}' "
                    f"declared as {declared}", node)
            env[node.target.id] = declared
            return
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name):
                raise UnsupportedError("augmented assignment to anything but a name", node)
            cur = env.get(node.target.id)
            if cur is None:
                raise VelaTypeError(
                    f"variable '{node.target.id}' is used before assignment", node)
            ty = self._infer(node.value, env, cur)
            res = self._binop_type(node.op, cur, ty, node)
            self._bind(env, node.target.id, join(cur, res), node)
            return
        if isinstance(node, ast.Expr):
            if isinstance(node.value, ast.Constant):
                return                      # docstring / no-op literal
            self._infer(node.value, env, None)
            return
        if isinstance(node, ast.If):
            self._condition(node.test, env)
            then_env = dict(env)
            for s in node.body:
                self._stmt(s, then_env, rets, info)
            else_env = dict(env)
            for s in node.orelse:
                self._stmt(s, else_env, rets, info)
            self._merge_env(env, then_env, else_env)
            return
        if isinstance(node, ast.While):
            self._condition(node.test, env)
            body_env = dict(env)
            for s in node.body:
                self._stmt(s, body_env, rets, info)
            self._merge_env(env, env, body_env)
            return
        if isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name):
                raise UnsupportedError("for-loop target other than a name", node)
            self._range_args(node.iter, env)
            env[node.target.id] = INT
            body_env = dict(env)
            for s in node.body:
                self._stmt(s, body_env, rets, info)
            self._merge_env(env, env, body_env)
            return
        if isinstance(node, ast.Return):
            if info is None:
                raise UnsupportedError("return statement at module level", node)
            hint = info.ret_ann
            ty = NONE
            if node.value is not None:
                # May stay unknown (e.g. a recursive call); do not force int
                # here or mutually recursive bool/float functions get poisoned.
                ty = self._infer(node.value, env, hint)
            rets.append(ty)
            return
        if isinstance(node, ast.Pass):
            return
        if isinstance(node, (ast.Break, ast.Continue)):
            return
        if isinstance(node, ast.Assert):
            raise UnsupportedError("assert statements", node)
        if isinstance(node, ast.FunctionDef):
            raise UnsupportedError("nested function definitions", node)
        raise UnsupportedError(ast.dump(node).split("(")[0], node)

    def _bind(self, env: dict[str, str | None], name: str, ty: str | None,
              node: ast.AST) -> None:
        old = env.get(name)
        if old is None:
            env[name] = ty
            return
        if ty is None:
            return
        if not assignable(ty, old):
            if assignable(old, ty):
                env[name] = ty
                return
            raise VelaTypeError(
                f"variable '{name}' is {old} but is assigned {ty}; the native "
                f"profile requires one static type per variable", node)
        env[name] = old

    def _merge_env(self, env: dict[str, str | None], a: dict[str, str | None],
                   b: dict[str, str | None]) -> None:
        for k in set(a) | set(b):
            t = join(a.get(k), b.get(k))
            if t is None:
                continue
            old = env.get(k)
            env[k] = t if old is None else join(old, t)

    def _condition(self, node: ast.expr, env: dict[str, str | None]) -> None:
        ty = self._infer(node, env, BOOL)
        if ty is None:
            ty = INT
        if ty not in (BOOL, INT):
            raise VelaTypeError(f"condition must be bool or int, got {ty}", node)

    def _range_args(self, node: ast.expr, env: dict[str, str | None]) -> tuple[int, int]:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) \
                or node.func.id != "range":
            raise UnsupportedError("for-loop over anything but range()", node)
        if node.keywords:
            raise UnsupportedError("range() keyword arguments", node)
        if not 1 <= len(node.args) <= 3:
            raise VelaCompileError("range() takes 1 to 3 arguments", node)
        for a in node.args:
            t = self._infer(a, env, INT)
            if t is not None and not assignable(t, INT):
                raise VelaTypeError(f"range() arguments must be int, got {t}", a)
        return len(node.args), 0

    # -- expressions -------------------------------------------------------
    def _infer(self, node: ast.expr, env: dict[str, str | None],
               hint: str | None = None) -> str | None:
        ty = self._infer_inner(node, env, hint)
        node._vela_ty = ty
        return ty

    def _infer_inner(self, node: ast.expr, env: dict[str, str | None],
                     hint: str | None) -> str | None:
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return BOOL
            if isinstance(v, int):
                if not -(2 ** 63) <= v < 2 ** 63:
                    raise VelaTypeError("integer literal does not fit in int64", node)
                return INT
            if isinstance(v, float):
                return FLOAT
            if isinstance(v, str):
                return STR
            if v is None:
                return NONE
            raise UnsupportedError(f"constant of type {type(v).__name__}", node)

        if isinstance(node, ast.Name):
            if node.id in env:
                t = env[node.id]
                if t is None and hint is not None:
                    env[node.id] = hint
                    return hint
                return t
            if node.id in self.funcs:
                raise UnsupportedError("function object as a value", node)
            if self._module_scope:
                raise VelaCompileError(
                    f"variable '{node.id}' is used before assignment", node)
            raise UnsupportedError(
                f"global variable '{node.id}' (the native profile only supports "
                f"function-local names)", node)

        if isinstance(node, ast.BinOp):
            return self._binop(node, env, hint)

        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
                    and isinstance(node.operand.value, int) \
                    and not isinstance(node.operand.value, bool) \
                    and node.operand.value == 2 ** 63:
                # -9223372036854775808 is valid int64 even though the positive
                # literal alone is out of range; lowering folds the negation.
                return INT
            if isinstance(node.op, ast.Not):
                t = self._infer(node.operand, env, BOOL)
                if t is None:
                    t = INT
                if t not in (BOOL, INT):
                    raise VelaTypeError(f"'not' requires bool/int, got {t}", node)
                return BOOL
            t = self._infer(node.operand, env, hint)
            if t is None:
                t = INT
            if t not in (INT, FLOAT, BOOL):
                raise VelaTypeError(f"unary +/- requires a number, got {t}", node)
            return norm_numeric(t)

        if isinstance(node, ast.BoolOp):
            is_and = isinstance(node.op, ast.And)
            for v in node.values:
                t = self._infer(v, env, BOOL if hint in (None, BOOL) else hint)
                if t is not None and t not in (BOOL, INT):
                    raise VelaTypeError(f"'{'and' if is_and else 'or'}' requires bool/int, got {t}", v)
            return BOOL

        if isinstance(node, ast.Compare):
            left = node.left
            lt = self._infer(left, env, None)
            for op, comp in zip(node.ops, node.comparators):
                rt = self._infer(comp, env, lt)
                if lt is None and rt is not None:
                    lt = rt
                    self._retro(left, rt, env)
                if rt is None and lt is not None:
                    rt = lt
                    self._retro(comp, lt, env)
                if lt is not None and rt is not None:
                    if not (is_numeric(lt) and is_numeric(rt)):
                        raise VelaTypeError(
                            f"cannot compare {type_name(lt)} with {type_name(rt)}", node)
                if isinstance(op, (ast.In, ast.NotIn, ast.Is, ast.IsNot)):
                    raise UnsupportedError(f"operator {type(op).__name__}", node)
                left, lt = comp, rt
            return BOOL

        if isinstance(node, ast.IfExp):
            self._condition(node.test, env)
            a = self._infer(node.body, env, hint)
            b = self._infer(node.orelse, env, hint if hint is not None else a)
            if a is None:
                a = b
            if b is None:
                b = a
            if a is None:
                raise VelaTypeError("conditional expression has no usable type", node)
            if not (assignable(a, b) or assignable(b, a)):
                raise VelaTypeError(
                    f"conditional expression mixes {a} and {b}", node)
            return join(a, b)

        if isinstance(node, ast.Call):
            return self._call(node, env, hint)

        if isinstance(node, ast.Subscript):
            raise UnsupportedError("subscripting (lists, dicts, strings)", node)
        if isinstance(node, ast.Attribute):
            raise UnsupportedError("attribute access / method calls", node)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            raise UnsupportedError("container literals", node)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            raise UnsupportedError("comprehensions", node)
        if isinstance(node, ast.Lambda):
            raise UnsupportedError("lambda expressions", node)
        if isinstance(node, ast.JoinedStr):
            raise UnsupportedError("f-strings", node)
        if isinstance(node, ast.NamedExpr):
            raise UnsupportedError("assignment expressions (:=)", node)
        raise UnsupportedError(ast.dump(node).split("(")[0], node)

    def _retro(self, node: ast.expr, ty: str, env: dict[str, str | None]) -> None:
        """Constrain an as-yet-untyped name to `ty` after seeing its peer."""
        if isinstance(node, ast.Name) and env.get(node.id) is None:
            env[node.id] = ty

    def _binop(self, node: ast.BinOp, env: dict[str, str | None],
               hint: str | None) -> str | None:
        ta = self._infer(node.left, env, hint)
        tb = self._infer(node.right, env, hint if hint is not None else ta)
        if ta is None and tb is not None:
            ta = tb
            self._retro(node.left, tb, env)
        if tb is None and ta is not None:
            tb = ta
            self._retro(node.right, ta, env)
        return self._binop_type(node.op, ta, tb, node)

    def _binop_type(self, op: ast.operator, a: str | None, b: str | None,
                    node: ast.AST) -> str:
        if a is None and b is None:
            # Both sides are still unknown (e.g. a recursive call whose return
            # type is being inferred): assume int; a later fixpoint iteration
            # will revisit this if the signature becomes concrete.
            return INT
        if a is None:
            a = b
        if b is None:
            b = a
        assert a is not None and b is not None
        if isinstance(op, ast.Div):
            if not (is_numeric(a) and is_numeric(b)):
                raise VelaTypeError(f"'/' requires numbers, got {a} and {b}", node)
            return FLOAT
        if isinstance(op, (ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)):
            if a not in (INT, BOOL) or b not in (INT, BOOL):
                raise VelaTypeError(f"bitwise/shift operators require int, got {a} and {b}", node)
            return INT
        if isinstance(op, (ast.FloorDiv, ast.Mod)) and FLOAT in (a, b):
            raise UnsupportedError("float floor division / modulo", node)
        if not (is_numeric(a) and is_numeric(b)):
            raise VelaTypeError(
                f"operator {type(op).__name__} requires numbers, got {a} and {b}", node)
        if isinstance(op, ast.Pow):
            if norm_numeric(a) == FLOAT or norm_numeric(b) == FLOAT:
                raise UnsupportedError("float exponentiation ('**')", node)
            return INT
        return FLOAT if FLOAT in (a, b) else INT

    def _call(self, node: ast.Call, env: dict[str, str | None],
              hint: str | None) -> str | None:
        if node.keywords:
            raise UnsupportedError("keyword arguments", node)
        if not isinstance(node.func, ast.Name):
            raise UnsupportedError("calling an attribute or expression", node)
        name = node.func.id
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise UnsupportedError("starred call arguments", node)
        for a in node.args:
            if isinstance(a, ast.Lambda):
                raise UnsupportedError("lambda expressions", a)
        if name == "print":
            for a in node.args:
                t = self._infer(a, env, None)
                if t is not None and t not in (INT, FLOAT, BOOL, STR, NONE):
                    raise VelaTypeError(f"print() cannot print a value of type {t}", a)
            return NONE
        if name == "len":
            if len(node.args) != 1:
                raise VelaCompileError("len() takes exactly one argument", node)
            t = self._infer(node.args[0], env, STR)
            if t is not None and t != STR:
                raise VelaTypeError(f"len() requires str, got {t}", node)
            return INT
        if name in ("abs", "min", "max"):
            if name == "abs":
                if len(node.args) != 1:
                    raise VelaCompileError("abs() takes exactly one argument", node)
                t = self._infer(node.args[0], env, hint)
                if t is None:
                    return None
                if not is_numeric(t):
                    raise VelaTypeError(f"abs() requires a number, got {t}", node)
                return norm_numeric(t)
            if len(node.args) != 2:
                raise UnsupportedError(f"{name}() with other than 2 arguments", node)
            ta = self._infer(node.args[0], env, None)
            tb = self._infer(node.args[1], env, ta)
            if ta is None:
                ta = tb
            if tb is None:
                tb = ta
            if ta is None or tb is None:
                return None
            if not (is_numeric(ta) and is_numeric(tb)):
                raise VelaTypeError(f"{name}() requires numbers, got {ta} and {tb}", node)
            return join(norm_numeric(ta), norm_numeric(tb))
        if name in ("int", "float", "bool"):
            if len(node.args) != 1:
                raise VelaCompileError(f"{name}() takes exactly one argument", node)
            t = self._infer(node.args[0], env, None)
            if t is None:
                return None
            if name == "int":
                if t not in (INT, FLOAT, BOOL):
                    raise VelaTypeError(f"int() cannot convert {t}", node)
                return INT
            if name == "float":
                if t not in (INT, FLOAT, BOOL):
                    raise VelaTypeError(f"float() cannot convert {t}", node)
                return FLOAT
            if t not in (INT, FLOAT, BOOL):
                raise VelaTypeError(f"bool() cannot convert {t}", node)
            return BOOL
        if name == "range":
            raise UnsupportedError("range() outside a for-loop", node)
        if name in self.funcs:
            info = self.funcs[name]
            if len(node.args) != len(info.params):
                raise VelaCompileError(
                    f"{name}() takes {len(info.params)} arguments but "
                    f"{len(node.args)} were given", node)
            for i, arg in enumerate(node.args):
                want = info.param_ann[i] or info.param_types[i]
                t = self._infer(arg, env, want)
                if want is None and t is not None:
                    info.param_types[i] = t
                elif want is not None and not assignable(t, want):
                    raise VelaTypeError(
                        f"argument {i + 1} of {name}() is {type_name(t)} but the "
                        f"parameter is {want}", arg)
            return info.ret_type
        raise UnsupportedError(f"call to unknown function '{name}'", node)
