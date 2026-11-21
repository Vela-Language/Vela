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
from .imports import expand_imports
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
    globals: set[str] = field(default_factory=set)

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
    if isinstance(last, ast.Try):
        return (always_returns(last.body)
                and all(always_returns(h.body) for h in last.handlers))
    if isinstance(last, ast.While) and isinstance(last.test, ast.Constant) and last.test.value is True:
        return True
    return False




class _CallRenamer(ast.NodeTransformer):
    def __init__(self, old: str, new: str, captures: list[str] | None = None) -> None:
        self.old = old
        self.new = new
        self.captures = captures or []

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == self.old:
            node.func.id = self.new
            if self.captures:
                node.args = [ast.Name(id=c, ctx=ast.Load())
                             for c in self.captures] + node.args
        return node


class _CellOuterRewriter(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, str], skip: set[int]) -> None:
        self.mapping = mapping
        self.skip = skip

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        cell = self.mapping.get(node.id)
        if cell is None:
            return node
        ctx = ast.Store() if isinstance(node.ctx, ast.Store) else ast.Load()
        return ast.Subscript(
            value=ast.Name(id=cell, ctx=ast.Load()),
            slice=ast.Constant(value=0),
            ctx=ctx,
        )


class _CellNestedRewriter(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        return None

    def visit_Name(self, node: ast.Name) -> ast.AST:
        cell = self.mapping.get(node.id)
        if cell is None:
            return node
        ctx = ast.Store() if isinstance(node.ctx, ast.Store) else ast.Load()
        return ast.Subscript(
            value=ast.Name(id=cell, ctx=ast.Load()),
            slice=ast.Constant(value=0),
            ctx=ctx,
        )


def _apply_cells(fn: ast.FunctionDef, prefix: str) -> None:
    """Turn ``nonlocal`` variables into one-element list cells.

    The nested function receives the list object, so writes through the cell
    stay visible to the enclosing function.  This is a real capture, not a
    global rewrite: re-entrant calls still get independent cells.
    """
    nested = [s for s in fn.body if isinstance(s, ast.FunctionDef)]
    if not nested:
        return
    outer_vars = {a.arg for a in fn.args.args}
    for stmt in fn.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    outer_vars.add(t.id)
    mutable: dict[str, str] = {}
    for sub in nested:
        for node in ast.walk(sub):
            if isinstance(node, ast.Nonlocal):
                for name in node.names:
                    if name in outer_vars:
                        mutable[name] = f"__cell_{prefix}_{name}"
    if not mutable:
        return
    skip: set[int] = set()
    for name, cell in mutable.items():
        seed = None
        for stmt in fn.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name) \
                    and stmt.targets[0].id == name:
                seed = stmt
                break
        if seed is not None:
            idx = fn.body.index(seed)
            new_seed = ast.Assign(
                targets=[ast.Name(id=cell, ctx=ast.Store())],
                value=ast.List(elts=[seed.value], ctx=ast.Load()),
            )
            fn.body[idx] = new_seed
            skip.add(id(new_seed))
        elif name in {a.arg for a in fn.args.args}:
            new_seed = ast.Assign(
                targets=[ast.Name(id=cell, ctx=ast.Store())],
                value=ast.List(
                    elts=[ast.Name(id=name, ctx=ast.Load())], ctx=ast.Load()),
            )
            fn.body.insert(0, new_seed)
            skip.add(id(new_seed))
        else:
            raise UnsupportedError(
                f"nonlocal '{name}' has no enclosing initializer", fn)
    rewriter = _CellOuterRewriter(mutable, skip)
    for stmt in list(fn.body):
        if id(stmt) in skip:
            continue
        rewriter.visit(stmt)
    for sub in nested:
        _CellNestedRewriter(mutable).visit(sub)
        ast.fix_missing_locations(sub)


def hoist_nested_functions(tree: ast.Module) -> None:
    """Hoist directly nested defs to module level and qualify their calls.

    Captures are not supported: the hoisted function cannot see the outer
    function's locals, so such programs fail with the normal name error.
    """
    added: list[ast.stmt] = []

    def walk(fn: ast.FunctionDef, prefix: str) -> None:
        for stmt in list(fn.body):
            if not isinstance(stmt, ast.FunctionDef):
                continue
            qualified = f"{prefix}__{stmt.name}"
            outer_vars = {a.arg for a in fn.args.args}
            for sub in ast.walk(fn):
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            outer_vars.add(t.id)
            params = {a.arg for a in stmt.args.args}
            assigned = set()
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            assigned.add(t.id)
            used = {n.id for n in ast.walk(stmt)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            captures = sorted(n for n in used
                              if n in outer_vars and n not in params and n not in assigned)
            stmt.args.args.extend(ast.arg(arg=c) for c in captures)
            renamer = _CallRenamer(stmt.name, qualified, captures)
            for inner in fn.body:
                renamer.visit(inner)
            stmt.name = qualified
            added.append(stmt)
            walk(stmt, qualified)

    for node in list(tree.body):
        if isinstance(node, ast.FunctionDef):
            _apply_cells(node, node.name)
            walk(node, node.name)
    tree.body.extend(added)


class Analyzer:
    def __init__(self, tree: ast.Module, filename: str = "<input>", boxed: bool = False,
                 dynamic_int: bool = False) -> None:
        self.tree = tree
        self.filename = filename
        self.boxed = boxed
        self.dynamic_int = dynamic_int
        self.funcs: dict[str, FuncInfo] = {}
        self.main_vars: dict[str, str | None] = {}
        self.has_main_code = False
        self._module_scope = False

    # -- public ------------------------------------------------------------
    def run(self) -> "Analyzer":
        if self.boxed:
            self.tree = expand_imports(self.tree, self.filename)
            hoist_nested_functions(self.tree)
        self.const_globals: dict[str, tuple[str, object]] = {}
        counts: dict[str, int] = {}
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name) \
                    and isinstance(stmt.value, ast.Constant):
                name = stmt.targets[0].id
                counts[name] = counts.get(name, 0) + 1
                v = stmt.value.value
                if isinstance(v, bool):
                    self.const_globals[name] = (BOOL, v)
                elif isinstance(v, int):
                    self.const_globals[name] = (INT, v)
                elif isinstance(v, float):
                    self.const_globals[name] = (FLOAT, v)
                elif isinstance(v, str):
                    self.const_globals[name] = (STR, v)
                elif v is None:
                    self.const_globals[name] = (NONE, v)
        for name, n in counts.items():
            if n != 1:
                self.const_globals.pop(name, None)
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
        mutable: set[str] = set()
        for info in self.funcs.values():
            mutable |= info.globals
        self.mutable_globals = mutable
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
        if isinstance(node, ast.FunctionDef):
            if self.boxed:
                return
            raise UnsupportedError("nested function definitions", node)
        if isinstance(node, ast.Global):
            if info is None:
                raise UnsupportedError("global statement at module level", node)
            for name in node.names:
                if name not in self.const_globals:
                    raise UnsupportedError(f"global '{name}' needs a constant module initializer", node)
                info.globals.add(name)
                env[name] = self.const_globals[name][0]
            return
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                raise UnsupportedError("assignment to anything but a single name", node)
            target = node.targets[0]
            if self.boxed and isinstance(target, ast.Subscript):
                base = self._infer(target.value, env, None)
                if base in ("list", "dict"):
                    kt = self._infer(target.slice, env, INT)
                    if base == "dict" and kt not in (INT, STR):
                        raise VelaTypeError("dict key must be int or str", node)
                    vt = self._infer(node.value, env, INT)
                    if vt not in (INT, BOOL):
                        raise VelaTypeError(f"container element must be int, got {vt}", node)
                    return
            if isinstance(target, ast.Tuple) and self.boxed:
                ty = self._infer(node.value, env, None)
                if ty == "tuple":
                    for elt in target.elts:
                        if not isinstance(elt, ast.Name):
                            raise UnsupportedError("tuple unpacking target", node)
                        self._bind(env, elt.id, INT, elt)
                    return
            if not isinstance(target, ast.Name):
                raise UnsupportedError("assignment to anything but a single name", node)
            want = env.get(target.id)
            ty = self._infer(node.value, env, want)
            if info is not None and target.id in info.globals:
                if want is not None and not assignable(ty, want):
                    raise VelaTypeError(
                        f"cannot assign {type_name(ty)} to global '{target.id}' "
                        f"of type {want}", node)
                return
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
        if isinstance(node, ast.Try) and self.boxed:
            if not node.handlers:
                raise UnsupportedError("try without except/finally", node)
            for stmt in node.body:
                self._stmt(stmt, dict(env), rets, info)
            for stmt in node.orelse:
                self._stmt(stmt, dict(env), rets, info)
            for stmt in node.finalbody:
                self._stmt(stmt, dict(env), rets, info)
            for h in node.handlers:
                if h.type is not None and not isinstance(h.type, ast.Name):
                    raise UnsupportedError("except with a non-name type", node)
                h_env = dict(env)
                if h.name:
                    h_env[h.name] = STR
                for stmt in h.body:
                    self._stmt(stmt, h_env, rets, info)
            return
        if isinstance(node, ast.Assert) and self.boxed:
            self._infer(node.test, env, BOOL)
            if node.msg is not None:
                raise UnsupportedError("assert with a message", node)
            return
        if isinstance(node, ast.Raise) and self.boxed:
            exc = node.exc
            if exc is not None:
                if not (isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                        and len(exc.args) == 1
                        and isinstance(exc.args[0], ast.Constant)
                        and isinstance(exc.args[0].value, str)):
                    raise UnsupportedError("raise with a non-literal exception", node)
            return
        if isinstance(node, ast.For):
            if not isinstance(node.target, ast.Name):
                raise UnsupportedError("for-loop target other than a name", node)
            if self.boxed and isinstance(node.iter, ast.Call) \
                    and isinstance(node.iter.func, ast.Name) \
                    and node.iter.func.id == "range":
                self._range_args(node.iter, env)
                env[node.target.id] = INT
                body_env = dict(env)
                for s in node.body:
                    self._stmt(s, body_env, rets, info)
                self._merge_env(env, env, body_env)
                return
            if self.boxed:
                it_ty = self._infer(node.iter, env, None)
                if it_ty in ("list", "tuple", "set", "dict", "strlist"):
                    env[node.target.id] = STR if it_ty == "strlist" else INT
                    body_env = dict(env)
                    for s in node.body:
                        self._stmt(s, body_env, rets, info)
                    self._merge_env(env, env, body_env)
                    return
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
                    if self.boxed:
                        return "bigint"
                    raise VelaTypeError("integer literal does not fit in int64", node)
                return INT
            if isinstance(v, float):
                return FLOAT
            if isinstance(v, str):
                return STR
            if isinstance(v, bytes) and self.boxed:
                return "bytes"
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
            if node.id in self.const_globals and not self._module_scope:
                return self.const_globals[node.id][0]
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
                if self.boxed and (lt == "bigint" or rt == "bigint") \
                        and isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
                                            ast.Gt, ast.GtE)):
                    left, lt = comp, rt
                    continue
                if self.boxed and isinstance(op, (ast.In, ast.NotIn)) \
                        and rt in ("list", "tuple", "set", "dict", "modules") and lt in (INT, BOOL, STR):
                    continue
                if lt is not None and rt is not None:
                    if self.boxed and lt == STR and rt == STR \
                            and isinstance(op, (ast.Eq, ast.NotEq)):
                        left, lt = comp, rt
                        continue
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
            if self.boxed and isinstance(node.slice, ast.Slice):
                base = self._infer(node.value, env, None)
                if base == "str":
                    if node.slice.lower is not None:
                        self._infer(node.slice.lower, env, INT)
                    if node.slice.upper is not None:
                        self._infer(node.slice.upper, env, INT)
                    return STR
            if self.boxed:
                base = self._infer(node.value, env, None)
                if base == "str":
                    self._infer(node.slice, env, INT)
                    return STR
                if base == "dict":
                    if self._infer(node.slice, env, None) not in (INT, STR):
                        raise VelaTypeError("dict key must be int or str", node)
                    return INT
                if base in ("list", "tuple"):
                    self._infer(node.slice, env, INT)
                    return INT
                if base == "strlist":
                    self._infer(node.slice, env, INT)
                    return STR
            raise UnsupportedError("subscripting (lists, dicts, strings)", node)
        if isinstance(node, ast.Attribute):
            raise UnsupportedError("attribute access / method calls", node)
        if isinstance(node, ast.Dict):
            if self.boxed:
                for k, v in zip(node.keys, node.values):
                    if not (isinstance(k, ast.Constant)
                            and isinstance(k.value, (int, str))
                            and isinstance(v, ast.Constant) and isinstance(v.value, int)):
                        raise UnsupportedError(
                            "dict entries other than int/str keys and int values", node)
                return "dict"
            raise UnsupportedError("container literals", node)
        if isinstance(node, ast.Tuple) and self.boxed:
            for item in node.elts:
                if not isinstance(item, ast.Constant) or not isinstance(item.value, int):
                    raise UnsupportedError("tuple elements other than int constants", node)
            return "tuple"
        if isinstance(node, ast.Set) and self.boxed:
            for item in node.elts:
                if not isinstance(item, ast.Constant) or not isinstance(item.value, int):
                    raise UnsupportedError("set elements other than int constants", node)
            return "set"
        if isinstance(node, (ast.List, ast.Set)):
            if self.boxed and isinstance(node, ast.List):
                if all(isinstance(i, ast.Constant) and isinstance(i.value, int)
                       and not isinstance(i.value, bool) for i in node.elts):
                    return "list"
                if node.elts and all(isinstance(i, ast.Constant)
                                     and isinstance(i.value, str) for i in node.elts):
                    return "strlist"
                raise UnsupportedError("list elements other than int/str constants", node)
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
        if self.boxed and (a == "bigint" or b == "bigint") \
                and isinstance(op, (ast.Add, ast.Sub, ast.Mult)):
            return "bigint"
        if self.boxed and a == "bigint" and b in (INT, BOOL) \
                and isinstance(op, (ast.FloorDiv, ast.Mod)):
            return "bigint"
        if self.boxed and a == "bigint" and b == "bigint" \
                and isinstance(op, (ast.FloorDiv, ast.Mod)):
            return "bigint"
        if self.boxed and a == "bigint" and b == "bigint" \
                and isinstance(op, (ast.FloorDiv, ast.Mod)):
            return "bigint"
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
        if self.boxed and a == STR and b == STR:
            if isinstance(op, ast.Add):
                return STR
            if isinstance(op, (ast.Eq, ast.NotEq)):
                return BOOL
        if isinstance(op, ast.Div):
            if not (is_numeric(a) and is_numeric(b)):
                raise VelaTypeError(f"'/' requires numbers, got {a} and {b}", node)
            return FLOAT
        if self.boxed and a == "set" and b == "set" \
                and isinstance(op, (ast.BitAnd, ast.BitOr)):
            return "set"
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
        if isinstance(node.func, ast.Attribute) and node.func.attr == "endswith" and self.boxed:
            if len(node.args) != 1:
                raise VelaCompileError("endswith() takes one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "str":
                self._infer(node.args[0], env, STR)
                return BOOL
        if isinstance(node.func, ast.Attribute) and node.func.attr == "find" and self.boxed:
            if len(node.args) != 1:
                raise VelaCompileError("find() takes one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "str":
                self._infer(node.args[0], env, STR)
                return INT
        if isinstance(node.func, ast.Attribute) and node.func.attr == "startswith" and self.boxed:
            if len(node.args) != 1:
                raise VelaCompileError("startswith() takes one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "str":
                self._infer(node.args[0], env, STR)
                return BOOL
        if isinstance(node.func, ast.Attribute) and node.func.attr == "lower" and self.boxed:
            if node.args:
                raise VelaCompileError("lower() takes no arguments", node)
            base = self._infer(node.func.value, env, None)
            if base == "str":
                return STR
        if isinstance(node.func, ast.Attribute) and node.func.attr == "upper" and self.boxed:
            if node.args:
                raise VelaCompileError("upper() takes no arguments", node)
            base = self._infer(node.func.value, env, None)
            if base == "str":
                return STR
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("keys", "values", "items") \
                and self.boxed:
            if node.args:
                raise VelaCompileError(f"{node.func.attr}() takes no arguments", node)
            base = self._infer(node.func.value, env, None)
            if base == "dict":
                return "list"
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("count", "index") \
                and self.boxed:
            if len(node.args) != 1:
                raise VelaCompileError(f"{node.func.attr}() takes one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "list":
                self._infer(node.args[0], env, INT)
                return INT
        if isinstance(node.func, ast.Attribute) and node.func.attr == "extend" and self.boxed:
            if len(node.args) != 1:
                raise VelaCompileError("extend() takes one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "list":
                self._infer(node.args[0], env, None)
                return NONE
        if isinstance(node.func, ast.Attribute) and node.func.attr == "pop" and self.boxed:
            if len(node.args) > 1:
                raise VelaCompileError("pop() takes at most one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "list":
                if node.args:
                    self._infer(node.args[0], env, INT)
                return INT
        if isinstance(node.func, ast.Attribute) and node.func.attr == "add" and self.boxed:
            if len(node.args) != 1:
                raise VelaCompileError("add() takes one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "set":
                vt = self._infer(node.args[0], env, INT)
                if vt not in (INT, BOOL):
                    raise VelaTypeError(f"set element must be int, got {vt}", node)
                return NONE
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and self.boxed:
            if len(node.args) != 2:
                raise VelaCompileError("get() takes two arguments", node)
            base = self._infer(node.func.value, env, None)
            if base == "dict":
                self._infer(node.args[0], env, INT)
                self._infer(node.args[1], env, INT)
                return INT
        if isinstance(node.func, ast.Attribute) and node.func.attr == "append" and self.boxed:
            if len(node.args) != 1:
                raise VelaCompileError("append() takes exactly one argument", node)
            base = self._infer(node.func.value, env, None)
            if base == "list":
                vt = self._infer(node.args[0], env, INT)
                if vt not in (INT, BOOL):
                    raise VelaTypeError(f"append() requires int, got {vt}", node)
                return NONE
        if not isinstance(node.func, ast.Name):
            raise UnsupportedError("calling an attribute or expression", node)
        name = node.func.id
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise UnsupportedError("starred call arguments", node)
        for a in node.args:
            if isinstance(a, ast.Lambda):
                raise UnsupportedError("lambda expressions", a)
        if name == "__modules_registry":
            for a in node.args:
                self._infer(a, env, None)
            return "modules"
        if name == "__module_new":
            for a in node.args:
                self._infer(a, env, None)
            return "module"
        if name == "__module_get":
            self._infer(node.args[0], env, None)
            return getattr(node, "_vela_mod_ty", "int")
        if name == "print":
            for a in node.args:
                t = self._infer(a, env, None)
                allowed = (INT, FLOAT, BOOL, STR, NONE)
                if self.boxed and t in ("list", "tuple", "set", "dict", "bytes", "bigint", "strlist"):
                    allowed = allowed + (t,)
                if t is not None and t not in allowed:
                    raise VelaTypeError(f"print() cannot print a value of type {t}", a)
            return NONE
        if name == "len":
            if len(node.args) != 1:
                raise VelaCompileError("len() takes exactly one argument", node)
            t = self._infer(node.args[0], env, None)
            if self.boxed and t in ("list", "tuple", "set", "dict", "bigint", "strlist", "modules"):
                return INT
            if t is not None and t != STR:
                raise VelaTypeError(f"len() requires str or list, got {t}", node)
            return INT
        if name in ("abs", "min", "max"):
            if name == "abs":
                if len(node.args) != 1:
                    raise VelaCompileError("abs() takes exactly one argument", node)
                t = self._infer(node.args[0], env, hint)
                if t is None:
                    return None
                if self.boxed and t == "bigint":
                    return "bigint"
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
            if self.boxed and (ta == "bigint" or tb == "bigint") \
                    and ta in (INT, "bigint") and tb in (INT, "bigint"):
                return "bigint"
            if not (is_numeric(ta) and is_numeric(tb)):
                raise VelaTypeError(f"{name}() requires numbers, got {ta} and {tb}", node)
            return join(norm_numeric(ta), norm_numeric(tb))
        if name == "str":
            if len(node.args) != 1:
                raise VelaCompileError("str() takes exactly one argument", node)
            t = self._infer(node.args[0], env, None)
            if t == STR:
                return STR
            if self.boxed and t in (INT, "bigint", BOOL):
                return STR
            raise VelaTypeError(f"str() does not support {t}", node)
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
                    if self.boxed and info.param_ann[i] is None \
                            and want == INT and t == STR:
                        info.param_types[i] = STR
                        self._infer_func(info, commit=True)
                        continue
                    if self.boxed and info.param_ann[i] is None \
                            and want == INT and t == "bigint":
                        info.param_types[i] = "bigint"
                        self._infer_func(info, commit=True)
                        continue
                    raise VelaTypeError(
                        f"argument {i + 1} of {name}() is {type_name(t)} but the "
                        f"parameter is {want}", arg)
            return info.ret_type
        raise UnsupportedError(f"call to unknown function '{name}'", node)
