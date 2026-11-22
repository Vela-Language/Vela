"""AST desugaring pass for Vela v0.6 (classes, MRO, super, descriptors, property,
__slots__, dunder protocols, generators, comprehensions, unpacking, slicing,
f-strings, with, decorators, closures, lambda, default/keyword/*args/**kwargs).
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field

from .errors import UnsupportedError, VelaCompileError, VelaTypeError


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def next(self, prefix: str = "__tmp") -> str:
        self.n += 1
        return f"{prefix}_{self.n}"


# ---------------------------------------------------------------------------
# 1. f-strings -> str concatenation
# ---------------------------------------------------------------------------

class _FStringDesugarer(ast.NodeTransformer):
    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        self.generic_visit(node)
        parts: list[ast.expr] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                if v.value:
                    parts.append(v)
            elif isinstance(v, ast.FormattedValue):
                s_call = ast.Call(
                    func=ast.Name(id="str", ctx=ast.Load()),
                    args=[v.value],
                    keywords=[],
                )
                parts.append(ast.copy_location(s_call, v))
            else:
                s_call = ast.Call(
                    func=ast.Name(id="str", ctx=ast.Load()),
                    args=[v],
                    keywords=[],
                )
                parts.append(ast.copy_location(s_call, v))
        if not parts:
            return ast.copy_location(ast.Constant(value=""), node)
        acc = parts[0]
        for p in parts[1:]:
            acc = ast.copy_location(ast.BinOp(left=acc, op=ast.Add(), right=p), node)
        return acc


# ---------------------------------------------------------------------------
# 2. lambda -> synthetic FunctionDef
# ---------------------------------------------------------------------------

class _LambdaDesugarer(ast.NodeTransformer):
    def __init__(self, counter: _Counter) -> None:
        self.counter = counter
        self.scope_stack: list[list[ast.stmt]] = []

    def _process_block(self, stmts: list[ast.stmt]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for s in stmts:
            lifted: list[ast.stmt] = []
            self.scope_stack.append(lifted)
            s_new = self.visit(s)
            self.scope_stack.pop()
            out.extend(lifted)
            if isinstance(s_new, list):
                out.extend(s_new)
            elif s_new is not None:
                out.append(s_new)
        return out

    def visit_Module(self, node: ast.Module) -> ast.AST:
        node.body = self._process_block(node.body)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.args.defaults = [self.visit(d) for d in node.args.defaults]
        node.args.kw_defaults = [
            self.visit(d) if d is not None else None for d in node.args.kw_defaults
        ]
        node.body = self._process_block(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.body = self._process_block(node.body)
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        node.test = self.visit(node.test)
        node.body = self._process_block(node.body)
        node.orelse = self._process_block(node.orelse)
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        node.test = self.visit(node.test)
        node.body = self._process_block(node.body)
        node.orelse = self._process_block(node.orelse)
        return node

    def visit_For(self, node: ast.For) -> ast.AST:
        node.iter = self.visit(node.iter)
        node.body = self._process_block(node.body)
        node.orelse = self._process_block(node.orelse)
        return node

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        self.generic_visit(node)
        fn_name = self.counter.next("__lambda")
        fn_def = ast.FunctionDef(
            name=fn_name,
            args=node.args,
            body=[ast.copy_location(ast.Return(value=node.body), node)],
            decorator_list=[],
            returns=None,
        )
        ast.fix_missing_locations(fn_def)
        if self.scope_stack:
            self.scope_stack[-1].append(fn_def)
        return ast.copy_location(ast.Name(id=fn_name, ctx=ast.Load()), node)


# ---------------------------------------------------------------------------
# 3. Generators (yield, yield from, send, throw, close) -> state-machine class
# ---------------------------------------------------------------------------

def _has_yield(fn: ast.FunctionDef) -> bool:
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(child):
            if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                continue
            if isinstance(sub, (ast.Yield, ast.YieldFrom)):
                return True
    return False


class _GenSelfRewriter(ast.NodeTransformer):
    def __init__(self, local_vars: set[str]) -> None:
        self.local_vars = local_vars

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.local_vars:
            return ast.copy_location(
                ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()),
                    attr=node.id,
                    ctx=node.ctx,
                ),
                node,
            )
        return node


def _normalize_gen_stmts(stmts: list[ast.stmt], counter: _Counter) -> list[ast.stmt]:
    """Convert `for` loops and `yield from` inside a generator into `while` loops."""
    out: list[ast.stmt] = []
    for s in stmts:
        if isinstance(s, ast.For) and isinstance(s.target, ast.Name):
            tname = s.target.id
            if isinstance(s.iter, ast.Call) and isinstance(s.iter.func, ast.Name) \
                    and s.iter.func.id == "range":
                rargs = s.iter.args
                start_e = ast.Constant(value=0) if len(rargs) == 1 else rargs[0]
                stop_e = rargs[0] if len(rargs) == 1 else rargs[1]
                step_e = ast.Constant(value=1) if len(rargs) < 3 else rargs[2]
                idx_v = counter.next("_gi")
                stop_v = counter.next("_gstop")
                step_v = counter.next("_gstep")
                out.append(ast.Assign(targets=[ast.Name(id=idx_v, ctx=ast.Store())], value=start_e))
                out.append(ast.Assign(targets=[ast.Name(id=stop_v, ctx=ast.Store())], value=stop_e))
                out.append(ast.Assign(targets=[ast.Name(id=step_v, ctx=ast.Store())], value=step_e))
                body_norm = [
                    ast.Assign(
                        targets=[ast.Name(id=tname, ctx=ast.Store())],
                        value=ast.Name(id=idx_v, ctx=ast.Load()),
                    )
                ] + _normalize_gen_stmts(s.body, counter) + [
                    ast.Assign(
                        targets=[ast.Name(id=idx_v, ctx=ast.Store())],
                        value=ast.BinOp(
                            left=ast.Name(id=idx_v, ctx=ast.Load()),
                            op=ast.Add(),
                            right=ast.Name(id=step_v, ctx=ast.Load()),
                        ),
                    )
                ]
                out.append(ast.While(
                    test=ast.Compare(
                        left=ast.Name(id=idx_v, ctx=ast.Load()),
                        ops=[ast.Lt()],
                        comparators=[ast.Name(id=stop_v, ctx=ast.Load())],
                    ),
                    body=body_norm,
                    orelse=[],
                ))
                continue
            else:
                seq_v = counter.next("_gseq")
                idx_v = counter.next("_gi")
                out.append(ast.Assign(targets=[ast.Name(id=seq_v, ctx=ast.Store())],
                                      value=ast.Call(func=ast.Name(id="list", ctx=ast.Load()),
                                                     args=[s.iter], keywords=[])))
                out.append(ast.Assign(targets=[ast.Name(id=idx_v, ctx=ast.Store())],
                                      value=ast.Constant(value=0)))
                body_norm = [
                    ast.Assign(
                        targets=[ast.Name(id=tname, ctx=ast.Store())],
                        value=ast.Subscript(
                            value=ast.Name(id=seq_v, ctx=ast.Load()),
                            slice=ast.Name(id=idx_v, ctx=ast.Load()),
                            ctx=ast.Load(),
                        ),
                    )
                ] + _normalize_gen_stmts(s.body, counter) + [
                    ast.Assign(
                        targets=[ast.Name(id=idx_v, ctx=ast.Store())],
                        value=ast.BinOp(
                            left=ast.Name(id=idx_v, ctx=ast.Load()),
                            op=ast.Add(),
                            right=ast.Constant(value=1),
                        ),
                    )
                ]
                out.append(ast.While(
                    test=ast.Compare(
                        left=ast.Name(id=idx_v, ctx=ast.Load()),
                        ops=[ast.Lt()],
                        comparators=[ast.Call(func=ast.Name(id="len", ctx=ast.Load()),
                                              args=[ast.Name(id=seq_v, ctx=ast.Load())],
                                              keywords=[])],
                    ),
                    body=body_norm,
                    orelse=[],
                ))
                continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.YieldFrom):
            seq_v = counter.next("_gyf_seq")
            idx_v = counter.next("_gyf_i")
            out.append(ast.Assign(
                targets=[ast.Name(id=seq_v, ctx=ast.Store())],
                value=ast.Call(func=ast.Name(id="list", ctx=ast.Load()),
                               args=[s.value.value], keywords=[]),
            ))
            out.append(ast.Assign(
                targets=[ast.Name(id=idx_v, ctx=ast.Store())],
                value=ast.Constant(value=0),
            ))
            out.append(ast.While(
                test=ast.Compare(
                    left=ast.Name(id=idx_v, ctx=ast.Load()),
                    ops=[ast.Lt()],
                    comparators=[ast.Call(func=ast.Name(id="len", ctx=ast.Load()),
                                          args=[ast.Name(id=seq_v, ctx=ast.Load())],
                                          keywords=[])],
                ),
                body=[
                    ast.Expr(value=ast.Yield(
                        value=ast.Subscript(
                            value=ast.Name(id=seq_v, ctx=ast.Load()),
                            slice=ast.Name(id=idx_v, ctx=ast.Load()),
                            ctx=ast.Load(),
                        )
                    )),
                    ast.Assign(
                        targets=[ast.Name(id=idx_v, ctx=ast.Store())],
                        value=ast.BinOp(
                            left=ast.Name(id=idx_v, ctx=ast.Load()),
                            op=ast.Add(),
                            right=ast.Constant(value=1),
                        ),
                    ),
                ],
                orelse=[],
            ))
            continue
        if isinstance(s, ast.If):
            s.body = _normalize_gen_stmts(s.body, counter)
            s.orelse = _normalize_gen_stmts(s.orelse, counter)
            out.append(s)
            continue
        if isinstance(s, ast.While):
            s.body = _normalize_gen_stmts(s.body, counter)
            out.append(s)
            continue
        if isinstance(s, ast.Try):
            s.body = _normalize_gen_stmts(s.body, counter)
            for h in s.handlers:
                h.body = _normalize_gen_stmts(h.body, counter)
            s.finalbody = _normalize_gen_stmts(s.finalbody, counter)
            out.append(s)
            continue
        out.append(s)
    return out


def _compile_generator(fn: ast.FunctionDef, counter: _Counter) -> list[ast.stmt]:
    norm_body = _normalize_gen_stmts(fn.body, counter)
    param_names = [a.arg for a in fn.args.args]
    global_vars: set[str] = set()
    local_vars = set(param_names)
    for node in ast.walk(ast.Module(body=norm_body, type_ignores=[])):
        if isinstance(node, ast.Global):
            global_vars.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local_vars.add(node.id)
    local_vars -= global_vars

    states: dict[int, list[ast.stmt]] = {0: []}
    cur_id = [0]
    next_id = [1]
    finally_cleanups: list[ast.stmt] = []

    def alloc_state() -> int:
        sid = next_id[0]
        next_id[0] += 1
        states[sid] = []
        return sid

    def set_attr(name: str, val: ast.expr) -> ast.stmt:
        return ast.Assign(
            targets=[ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()),
                                   attr=name, ctx=ast.Store())],
            value=val,
        )

    def get_attr(name: str) -> ast.expr:
        return ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()),
                             attr=name, ctx=ast.Load())

    def emit_stmts(stmts: list[ast.stmt], loop_stack: list[tuple[int, int]]) -> None:
        for s in stmts:
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Yield):
                yval = s.value.value or ast.Constant(value=0)
                resume = alloc_state()
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=resume)))
                states[cur_id[0]].append(ast.Return(value=yval))
                cur_id[0] = resume
                continue
            if isinstance(s, ast.Assign) and len(s.targets) == 1 \
                    and isinstance(s.targets[0], ast.Name) \
                    and isinstance(s.value, ast.Yield):
                tname = s.targets[0].id
                yval = s.value.value or ast.Constant(value=0)
                resume = alloc_state()
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=resume)))
                states[cur_id[0]].append(ast.Return(value=yval))
                cur_id[0] = resume
                states[cur_id[0]].append(
                    ast.Assign(
                        targets=[ast.Name(id=tname, ctx=ast.Store())],
                        value=get_attr("_sent"),
                    )
                )
                continue
            if isinstance(s, ast.Return):
                states[cur_id[0]].append(set_attr("_closed", ast.Constant(value=1)))
                states[cur_id[0]].append(
                    ast.Raise(
                        exc=ast.Call(func=ast.Name(id="StopIteration", ctx=ast.Load()),
                                     args=[ast.Constant(value="done")], keywords=[]),
                        cause=None,
                    )
                )
                cur_id[0] = alloc_state()
                continue
            if isinstance(s, ast.Break) and loop_stack:
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=loop_stack[-1][1])))
                states[cur_id[0]].append(ast.Continue())
                cur_id[0] = alloc_state()
                continue
            if isinstance(s, ast.Continue) and loop_stack:
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=loop_stack[-1][0])))
                states[cur_id[0]].append(ast.Continue())
                cur_id[0] = alloc_state()
                continue
            if isinstance(s, ast.If):
                then_s = alloc_state()
                else_s = alloc_state()
                join_s = alloc_state()
                states[cur_id[0]].append(
                    ast.If(
                        test=s.test,
                        body=[set_attr("_state", ast.Constant(value=then_s)), ast.Continue()],
                        orelse=[set_attr("_state", ast.Constant(value=else_s)), ast.Continue()],
                    )
                )
                cur_id[0] = then_s
                emit_stmts(s.body, loop_stack)
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=join_s)))
                states[cur_id[0]].append(ast.Continue())
                cur_id[0] = else_s
                emit_stmts(s.orelse, loop_stack)
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=join_s)))
                states[cur_id[0]].append(ast.Continue())
                cur_id[0] = join_s
                continue
            if isinstance(s, ast.While):
                head_s = alloc_state()
                body_s = alloc_state()
                exit_s = alloc_state()
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=head_s)))
                states[cur_id[0]].append(ast.Continue())
                cur_id[0] = head_s
                states[cur_id[0]].append(
                    ast.If(
                        test=s.test,
                        body=[set_attr("_state", ast.Constant(value=body_s)), ast.Continue()],
                        orelse=[set_attr("_state", ast.Constant(value=exit_s)), ast.Continue()],
                    )
                )
                cur_id[0] = body_s
                emit_stmts(s.body, loop_stack + [(head_s, exit_s)])
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=head_s)))
                states[cur_id[0]].append(ast.Continue())
                cur_id[0] = exit_s
                continue
            if isinstance(s, ast.Try):
                handler_s = alloc_state() if s.handlers else 0
                after_s = alloc_state()
                if handler_s:
                    states[cur_id[0]].append(set_attr("_exc_state", ast.Constant(value=handler_s)))
                if s.finalbody:
                    states[cur_id[0]].append(set_attr("_has_fin", ast.Constant(value=1)))
                    finally_cleanups.extend(copy.deepcopy(s.finalbody))
                emit_stmts(s.body, loop_stack)
                if handler_s:
                    states[cur_id[0]].append(set_attr("_exc_state", ast.Constant(value=0)))
                if s.finalbody:
                    states[cur_id[0]].append(set_attr("_has_fin", ast.Constant(value=0)))
                    emit_stmts(copy.deepcopy(s.finalbody), loop_stack)
                states[cur_id[0]].append(set_attr("_state", ast.Constant(value=after_s)))
                states[cur_id[0]].append(ast.Continue())
                if handler_s:
                    cur_id[0] = handler_s
                    states[cur_id[0]].append(set_attr("_exc_state", ast.Constant(value=0)))
                    for h in s.handlers:
                        emit_stmts(h.body, loop_stack)
                    if s.finalbody:
                        states[cur_id[0]].append(set_attr("_has_fin", ast.Constant(value=0)))
                        emit_stmts(copy.deepcopy(s.finalbody), loop_stack)
                    states[cur_id[0]].append(set_attr("_state", ast.Constant(value=after_s)))
                    states[cur_id[0]].append(ast.Continue())
                cur_id[0] = after_s
                continue
            states[cur_id[0]].append(s)

    emit_stmts(norm_body, [])
    end_id = cur_id[0]
    states[end_id].append(set_attr("_closed", ast.Constant(value=1)))
    states[end_id].append(
        ast.Raise(
            exc=ast.Call(func=ast.Name(id="StopIteration", ctx=ast.Load()),
                         args=[ast.Constant(value="done")], keywords=[]),
            cause=None,
        )
    )

    rewriter = _GenSelfRewriter(local_vars)
    for sid in list(states):
        states[sid] = [rewriter.visit(st) for st in states[sid]]
    finally_cleanups = [rewriter.visit(st) for st in finally_cleanups]

    cls_name = f"__Gen_{fn.name}"
    init_body: list[ast.stmt] = [
        set_attr("_state", ast.Constant(value=0)),
        set_attr("_closed", ast.Constant(value=0)),
        set_attr("_exc_state", ast.Constant(value=0)),
        set_attr("_has_fin", ast.Constant(value=0)),
        set_attr("_sent", ast.Constant(value=0)),
    ]
    for p in param_names:
        init_body.append(set_attr(p, ast.Name(id=p, ctx=ast.Load())))
    for lv in sorted(local_vars - set(param_names)):
        if lv.startswith(("_gseq", "_gyf_seq")):
            init_body.append(set_attr(lv, ast.List(elts=[], ctx=ast.Load())))
        else:
            init_body.append(set_attr(lv, ast.Constant(value=0)))

    dispatch_ifs: list[ast.stmt] = [
        ast.If(
            test=ast.Compare(left=get_attr("_closed"), ops=[ast.NotEq()],
                             comparators=[ast.Constant(value=0)]),
            body=[
                ast.Raise(
                    exc=ast.Call(func=ast.Name(id="StopIteration", ctx=ast.Load()),
                                 args=[ast.Constant(value="closed")], keywords=[]),
                    cause=None,
                )
            ],
            orelse=[],
        ),
        set_attr("_sent", ast.Name(id="_sent", ctx=ast.Load())),
    ]
    loop_body: list[ast.stmt] = []
    for sid in sorted(states):
        if not states[sid]:
            continue
        loop_body.append(
            ast.If(
                test=ast.Compare(left=get_attr("_state"), ops=[ast.Eq()],
                                 comparators=[ast.Constant(value=sid)]),
                body=states[sid],
                orelse=[],
            )
        )
    loop_body.append(
        ast.Raise(
            exc=ast.Call(func=ast.Name(id="StopIteration", ctx=ast.Load()),
                         args=[ast.Constant(value="done")], keywords=[]),
            cause=None,
        )
    )
    dispatch_ifs.append(ast.While(test=ast.Constant(value=True), body=loop_body, orelse=[]))
    dispatch_ifs.append(ast.Return(value=ast.Constant(value=0)))
    if global_vars:
        dispatch_ifs.insert(0, ast.Global(names=sorted(global_vars)))

    close_body: list[ast.stmt] = []
    if global_vars:
        close_body.append(ast.Global(names=sorted(global_vars)))
    if finally_cleanups:
        close_body.append(
            ast.If(
                test=ast.BoolOp(
                    op=ast.And(),
                    values=[
                        ast.Compare(left=get_attr("_closed"), ops=[ast.Eq()],
                                    comparators=[ast.Constant(value=0)]),
                        ast.Compare(left=get_attr("_has_fin"), ops=[ast.NotEq()],
                                    comparators=[ast.Constant(value=0)]),
                    ],
                ),
                body=[set_attr("_has_fin", ast.Constant(value=0))] + finally_cleanups,
                orelse=[],
            )
        )
    close_body.append(set_attr("_closed", ast.Constant(value=1)))
    close_body.append(ast.Return(value=ast.Constant(value=0)))

    throw_body: list[ast.stmt] = [
        ast.If(
            test=ast.BoolOp(
                op=ast.And(),
                values=[
                    ast.Compare(left=get_attr("_closed"), ops=[ast.Eq()],
                                comparators=[ast.Constant(value=0)]),
                    ast.Compare(left=get_attr("_exc_state"), ops=[ast.NotEq()],
                                comparators=[ast.Constant(value=0)]),
                ],
            ),
            body=[
                set_attr("_state", get_attr("_exc_state")),
                set_attr("_exc_state", ast.Constant(value=0)),
                ast.Return(
                    value=ast.Call(
                        func=ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()),
                                           attr="send", ctx=ast.Load()),
                        args=[ast.Constant(value=0)],
                        keywords=[],
                    )
                ),
            ],
            orelse=[],
        ),
        ast.Expr(
            value=ast.Call(
                func=ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()),
                                   attr="close", ctx=ast.Load()),
                args=[],
                keywords=[],
            )
        ),
        ast.Raise(
            exc=ast.Call(func=ast.Name(id="ValueError", ctx=ast.Load()),
                         args=[ast.Constant(value="generator throw")], keywords=[]),
            cause=None,
        ),
        ast.Return(value=ast.Constant(value=0)),
    ]

    cls_def = ast.ClassDef(
        name=cls_name,
        bases=[],
        keywords=[],
        body=[
            ast.FunctionDef(
                name="__init__",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self")] + [ast.arg(arg=p) for p in param_names],
                    kwonlyargs=[],
                    kw_defaults=[],
                    defaults=[],
                ),
                body=init_body,
                decorator_list=[],
            ),
            ast.FunctionDef(
                name="__iter__",
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                                   kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=[ast.Return(value=ast.Name(id="self", ctx=ast.Load()))],
                decorator_list=[],
            ),
            ast.FunctionDef(
                name="__next__",
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                                   kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=[
                    ast.Return(
                        value=ast.Call(
                            func=ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()),
                                               attr="send", ctx=ast.Load()),
                            args=[ast.Constant(value=0)],
                            keywords=[],
                        )
                    )
                ],
                decorator_list=[],
            ),
            ast.FunctionDef(
                name="send",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self"), ast.arg(arg="_sent")],
                    kwonlyargs=[],
                    kw_defaults=[],
                    defaults=[],
                ),
                body=dispatch_ifs,
                decorator_list=[],
            ),
            ast.FunctionDef(
                name="close",
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                                   kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=close_body,
                decorator_list=[],
            ),
            ast.FunctionDef(
                name="throw",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self"), ast.arg(arg="_exc")],
                    kwonlyargs=[],
                    kw_defaults=[],
                    defaults=[ast.Constant(value=0)],
                ),
                body=throw_body,
                decorator_list=[],
            ),
        ],
        decorator_list=[],
    )
    factory_fn = ast.FunctionDef(
        name=fn.name,
        args=fn.args,
        body=[
            ast.Return(
                value=ast.Call(
                    func=ast.Name(id=cls_name, ctx=ast.Load()),
                    args=[ast.Name(id=p, ctx=ast.Load()) for p in param_names],
                    keywords=[],
                )
            )
        ],
        decorator_list=fn.decorator_list,
    )
    ast.fix_missing_locations(cls_def)
    ast.fix_missing_locations(factory_fn)
    return [cls_def, factory_fn]


def _desugar_generators(tree: ast.Module, counter: _Counter) -> None:
    new_body: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and _has_yield(stmt):
            new_body.extend(_compile_generator(stmt, counter))
        else:
            new_body.append(stmt)
    tree.body = new_body


# ---------------------------------------------------------------------------
# 4. Comprehensions, `with` statements, and Unpacking
# ---------------------------------------------------------------------------

def _build_comp_loops(generators: list[ast.comprehension], inner_stmt: ast.stmt) -> ast.stmt:
    cur = inner_stmt
    for gen in reversed(generators):
        for cond in reversed(gen.ifs):
            cur = ast.If(test=cond, body=[cur], orelse=[])
        cur = ast.For(target=gen.target, iter=gen.iter, body=[cur], orelse=[])
    return cur


class _CompExtractor(ast.NodeTransformer):
    def __init__(self, counter: _Counter) -> None:
        self.counter = counter
        self.pre_stmts: list[ast.stmt] = []

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if len(node.args) == 1 and isinstance(node.args[0], ast.GeneratorExp) \
                and isinstance(node.func, ast.Name) and node.func.id in ("list", "tuple", "set", "sum"):
            gexp = node.args[0]
            comp = ast.ListComp(elt=gexp.elt, generators=gexp.generators)
            replaced = self.visit_ListComp(comp)
            if node.func.id == "list":
                return replaced
            node.args = [replaced]
            return node
        return self.generic_visit(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> ast.AST:
        return self.visit_ListComp(ast.ListComp(elt=node.elt, generators=node.generators))

    def visit_ListComp(self, node: ast.ListComp) -> ast.AST:
        self.generic_visit(node)
        tmp = self.counter.next("__lcomp")
        init = ast.Assign(
            targets=[ast.Name(id=tmp, ctx=ast.Store())],
            value=ast.List(elts=[], ctx=ast.Load()),
        )
        append_stmt = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(value=ast.Name(id=tmp, ctx=ast.Load()),
                                   attr="append", ctx=ast.Load()),
                args=[node.elt],
                keywords=[],
            )
        )
        loop = _build_comp_loops(node.generators, append_stmt)
        self.pre_stmts.extend([init, loop])
        return ast.copy_location(ast.Name(id=tmp, ctx=ast.Load()), node)

    def visit_SetComp(self, node: ast.SetComp) -> ast.AST:
        self.generic_visit(node)
        tmp = self.counter.next("__scomp")
        init = ast.Assign(
            targets=[ast.Name(id=tmp, ctx=ast.Store())],
            value=ast.Set(elts=[]),
        )
        add_stmt = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(value=ast.Name(id=tmp, ctx=ast.Load()),
                                   attr="add", ctx=ast.Load()),
                args=[node.elt],
                keywords=[],
            )
        )
        loop = _build_comp_loops(node.generators, add_stmt)
        self.pre_stmts.extend([init, loop])
        return ast.copy_location(ast.Name(id=tmp, ctx=ast.Load()), node)

    def visit_DictComp(self, node: ast.DictComp) -> ast.AST:
        self.generic_visit(node)
        tmp = self.counter.next("__dcomp")
        init = ast.Assign(
            targets=[ast.Name(id=tmp, ctx=ast.Store())],
            value=ast.Dict(keys=[], values=[]),
        )
        set_stmt = ast.Assign(
            targets=[
                ast.Subscript(
                    value=ast.Name(id=tmp, ctx=ast.Load()),
                    slice=node.key,
                    ctx=ast.Store(),
                )
            ],
            value=node.value,
        )
        loop = _build_comp_loops(node.generators, set_stmt)
        self.pre_stmts.extend([init, loop])
        return ast.copy_location(ast.Name(id=tmp, ctx=ast.Load()), node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> ast.AST:
        self.generic_visit(node)
        self.pre_stmts.append(
            ast.Assign(
                targets=[ast.Name(id=node.target.id, ctx=ast.Store())],
                value=node.value,
            )
        )
        return ast.copy_location(ast.Name(id=node.target.id, ctx=ast.Load()), node)


def _unpack_assign(target: ast.AST, value: ast.expr, counter: _Counter) -> list[ast.stmt]:
    if not isinstance(target, (ast.Tuple, ast.List)):
        return [ast.Assign(targets=[target], value=value)]
    elts = target.elts
    has_star = any(isinstance(e, ast.Starred) for e in elts)
    if not has_star and isinstance(value, (ast.Tuple, ast.List)) \
            and len(value.elts) == len(elts) \
            and not any(isinstance(e, ast.Starred) for e in value.elts):
        tmps = [counter.next("__upk") for _ in elts]
        stmts: list[ast.stmt] = [
            ast.Assign(targets=[ast.Name(id=t, ctx=ast.Store())], value=v)
            for t, v in zip(tmps, value.elts)
        ]
        for tgt, t in zip(elts, tmps):
            stmts.extend(_unpack_assign(tgt, ast.Name(id=t, ctx=ast.Load()), counter))
        return stmts
    if isinstance(value, ast.Name):
        seq_name = value.id
        stmts = []
    else:
        seq_name = counter.next("__useq")
        stmts = [ast.Assign(targets=[ast.Name(id=seq_name, ctx=ast.Store())], value=value)]
    star_idx = next((i for i, e in enumerate(elts) if isinstance(e, ast.Starred)), None)
    if star_idx is None:
        for i, tgt in enumerate(elts):
            sub = ast.Subscript(
                value=ast.Name(id=seq_name, ctx=ast.Load()),
                slice=ast.Constant(value=i),
                ctx=ast.Load(),
            )
            stmts.extend(_unpack_assign(tgt, sub, counter))
        return stmts
    for i in range(star_idx):
        sub = ast.Subscript(
            value=ast.Name(id=seq_name, ctx=ast.Load()),
            slice=ast.Constant(value=i),
            ctx=ast.Load(),
        )
        stmts.extend(_unpack_assign(elts[i], sub, counter))
    suffix_count = len(elts) - star_idx - 1
    star_target = elts[star_idx].value  # type: ignore[attr-defined]
    upper_expr: ast.expr | None = None
    if suffix_count > 0:
        upper_expr = ast.BinOp(
            left=ast.Call(func=ast.Name(id="len", ctx=ast.Load()),
                          args=[ast.Name(id=seq_name, ctx=ast.Load())], keywords=[]),
            op=ast.Sub(),
            right=ast.Constant(value=suffix_count),
        )
    stmts.append(
        ast.Assign(
            targets=[star_target],
            value=ast.Subscript(
                value=ast.Name(id=seq_name, ctx=ast.Load()),
                slice=ast.Slice(lower=ast.Constant(value=star_idx), upper=upper_expr, step=None),
                ctx=ast.Load(),
            ),
        )
    )
    for j in range(suffix_count):
        idx_expr = ast.BinOp(
            left=ast.Call(func=ast.Name(id="len", ctx=ast.Load()),
                          args=[ast.Name(id=seq_name, ctx=ast.Load())], keywords=[]),
            op=ast.Sub(),
            right=ast.Constant(value=suffix_count - j),
        )
        sub = ast.Subscript(
            value=ast.Name(id=seq_name, ctx=ast.Load()),
            slice=idx_expr,
            ctx=ast.Load(),
        )
        stmts.extend(_unpack_assign(elts[star_idx + 1 + j], sub, counter))
    return stmts


def _compile_pattern(
    pat: ast.pattern, subj: ast.expr, class_fields: dict[str, list[str]]
) -> tuple[ast.expr, list[ast.stmt]]:
    if isinstance(pat, ast.MatchValue):
        return (
            ast.Compare(left=copy.deepcopy(subj), ops=[ast.Eq()], comparators=[pat.value]),
            [],
        )
    if isinstance(pat, ast.MatchSingleton):
        if pat.value is None:
            return (
                ast.Compare(left=copy.deepcopy(subj), ops=[ast.Eq()], comparators=[ast.Constant(value=0)]),
                [],
            )
        return (
            ast.Compare(
                left=copy.deepcopy(subj),
                ops=[ast.Eq()],
                comparators=[ast.Constant(value=pat.value)],
            ),
            [],
        )
    if isinstance(pat, ast.MatchAs):
        if pat.pattern is None:
            binds: list[ast.stmt] = []
            if pat.name is not None:
                binds.append(
                    ast.Assign(
                        targets=[ast.Name(id=pat.name, ctx=ast.Store())],
                        value=copy.deepcopy(subj),
                    )
                )
            return ast.Constant(value=True), binds
        cond, binds = _compile_pattern(pat.pattern, subj, class_fields)
        if pat.name is not None:
            binds.append(
                ast.Assign(
                    targets=[ast.Name(id=pat.name, ctx=ast.Store())],
                    value=copy.deepcopy(subj),
                )
            )
        return cond, binds
    if isinstance(pat, ast.MatchOr):
        sub_conds: list[ast.expr] = []
        for sub_p in pat.patterns:
            c, _ = _compile_pattern(sub_p, subj, class_fields)
            sub_conds.append(c)
        return ast.BoolOp(op=ast.Or(), values=sub_conds), []
    if isinstance(pat, ast.MatchSequence):
        pats = pat.patterns
        star_idx = next((i for i, p in enumerate(pats) if isinstance(p, ast.MatchStar)), None)
        len_call = ast.Call(
            func=ast.Name(id="len", ctx=ast.Load()),
            args=[copy.deepcopy(subj)],
            keywords=[],
        )
        conds: list[ast.expr] = []
        binds = []
        if star_idx is None:
            conds.append(
                ast.Compare(left=len_call, ops=[ast.Eq()], comparators=[ast.Constant(value=len(pats))])
            )
            for i, sp in enumerate(pats):
                elem_e = ast.Subscript(
                    value=copy.deepcopy(subj), slice=ast.Constant(value=i), ctx=ast.Load()
                )
                c, b = _compile_pattern(sp, elem_e, class_fields)
                if not (isinstance(c, ast.Constant) and c.value is True):
                    conds.append(c)
                binds.extend(b)
        else:
            conds.append(
                ast.Compare(
                    left=len_call, ops=[ast.GtE()], comparators=[ast.Constant(value=len(pats) - 1)]
                )
            )
            for i in range(star_idx):
                elem_e = ast.Subscript(
                    value=copy.deepcopy(subj), slice=ast.Constant(value=i), ctx=ast.Load()
                )
                c, b = _compile_pattern(pats[i], elem_e, class_fields)
                if not (isinstance(c, ast.Constant) and c.value is True):
                    conds.append(c)
                binds.extend(b)
            suffix_n = len(pats) - star_idx - 1
            star_pat = pats[star_idx]
            assert isinstance(star_pat, ast.MatchStar)
            if star_pat.name is not None:
                upper_e: ast.expr | None = None
                if suffix_n > 0:
                    upper_e = ast.BinOp(
                        left=copy.deepcopy(len_call),
                        op=ast.Sub(),
                        right=ast.Constant(value=suffix_n),
                    )
                binds.append(
                    ast.Assign(
                        targets=[ast.Name(id=star_pat.name, ctx=ast.Store())],
                        value=ast.Subscript(
                            value=copy.deepcopy(subj),
                            slice=ast.Slice(lower=ast.Constant(value=star_idx), upper=upper_e, step=None),
                            ctx=ast.Load(),
                        ),
                    )
                )
            for j in range(suffix_n):
                elem_e = ast.Subscript(
                    value=copy.deepcopy(subj),
                    slice=ast.Constant(value=-(suffix_n - j)),
                    ctx=ast.Load(),
                )
                c, b = _compile_pattern(pats[star_idx + 1 + j], elem_e, class_fields)
                if not (isinstance(c, ast.Constant) and c.value is True):
                    conds.append(c)
                binds.extend(b)
        full_cond = conds[0] if len(conds) == 1 else ast.BoolOp(op=ast.And(), values=conds)
        return full_cond, binds
    if isinstance(pat, ast.MatchMapping):
        conds = []
        binds = []
        for k_expr, v_pat in zip(pat.keys, pat.patterns):
            conds.append(
                ast.Compare(left=copy.deepcopy(k_expr), ops=[ast.In()], comparators=[copy.deepcopy(subj)])
            )
            val_e = ast.Subscript(
                value=copy.deepcopy(subj), slice=copy.deepcopy(k_expr), ctx=ast.Load()
            )
            c, b = _compile_pattern(v_pat, val_e, class_fields)
            if not (isinstance(c, ast.Constant) and c.value is True):
                conds.append(c)
            binds.extend(b)
        if not conds:
            return ast.Constant(value=True), binds
        full_cond = conds[0] if len(conds) == 1 else ast.BoolOp(op=ast.And(), values=conds)
        return full_cond, binds
    if isinstance(pat, ast.MatchClass):
        cname = pat.cls.id if isinstance(pat.cls, ast.Name) else ""
        conds = [
            ast.Call(
                func=ast.Name(id="isinstance", ctx=ast.Load()),
                args=[copy.deepcopy(subj), copy.deepcopy(pat.cls)],
                keywords=[],
            )
        ]
        binds = []
        pos_attrs = class_fields.get(cname, [])
        for i, pos_p in enumerate(pat.patterns):
            attr_name = pos_attrs[i] if i < len(pos_attrs) else f"_{i}"
            attr_e = ast.Attribute(value=copy.deepcopy(subj), attr=attr_name, ctx=ast.Load())
            c, b = _compile_pattern(pos_p, attr_e, class_fields)
            if not (isinstance(c, ast.Constant) and c.value is True):
                conds.append(c)
            binds.extend(b)
        for attr_name, kw_p in zip(pat.kwd_attrs, pat.kwd_patterns):
            attr_e = ast.Attribute(value=copy.deepcopy(subj), attr=attr_name, ctx=ast.Load())
            c, b = _compile_pattern(kw_p, attr_e, class_fields)
            if not (isinstance(c, ast.Constant) and c.value is True):
                conds.append(c)
            binds.extend(b)
        full_cond = conds[0] if len(conds) == 1 else ast.BoolOp(op=ast.And(), values=conds)
        return full_cond, binds
    return ast.Constant(value=True), []


def _desugar_stmt_list(
    stmts: list[ast.stmt], counter: _Counter, class_fields: dict[str, list[str]] | None = None
) -> list[ast.stmt]:
    cfields = class_fields or {}
    out: list[ast.stmt] = []
    for s in stmts:
        if isinstance(s, ast.FunctionDef):
            s.body = _desugar_stmt_list(s.body, counter, cfields)
            out.append(s)
            continue
        if isinstance(s, ast.ClassDef):
            s.body = _desugar_stmt_list(s.body, counter, cfields)
            out.append(s)
            continue
        if isinstance(s, ast.Match):
            subj_var = counter.next("__msubj")
            done_var = counter.next("__mdone")
            match_stmts: list[ast.stmt] = [
                ast.Assign(targets=[ast.Name(id=subj_var, ctx=ast.Store())], value=s.subject),
                ast.Assign(targets=[ast.Name(id=done_var, ctx=ast.Store())], value=ast.Constant(value=0)),
            ]
            subj_ref = ast.Name(id=subj_var, ctx=ast.Load())
            for case in s.cases:
                pcond, pbinds = _compile_pattern(case.pattern, subj_ref, cfields)
                not_done = ast.Compare(
                    left=ast.Name(id=done_var, ctx=ast.Load()),
                    ops=[ast.Eq()],
                    comparators=[ast.Constant(value=0)],
                )
                if isinstance(pcond, ast.Constant) and pcond.value is True:
                    outer_test: ast.expr = not_done
                else:
                    outer_test = ast.BoolOp(op=ast.And(), values=[not_done, pcond])
                mark_done = ast.Assign(
                    targets=[ast.Name(id=done_var, ctx=ast.Store())],
                    value=ast.Constant(value=1),
                )
                case_exec = [mark_done] + list(case.body)
                if case.guard is not None:
                    inner_body = pbinds + [ast.If(test=case.guard, body=case_exec, orelse=[])]
                else:
                    inner_body = pbinds + case_exec
                match_stmts.append(ast.If(test=outer_test, body=inner_body, orelse=[]))
            if s.cases and s.cases[-1].body and isinstance(s.cases[-1].body[-1], ast.Return):
                match_stmts.append(ast.Return(value=ast.Constant(value=0)))
            out.extend(_desugar_stmt_list(match_stmts, counter, cfields))
            continue
        if isinstance(s, ast.With):
            cur_body = _desugar_stmt_list(s.body, counter)
            for item in reversed(s.items):
                ctx_name = counter.next("__ctx")
                enter_call = ast.Call(
                    func=ast.Attribute(value=ast.Name(id=ctx_name, ctx=ast.Load()),
                                       attr="__enter__", ctx=ast.Load()),
                    args=[],
                    keywords=[],
                )
                enter_stmt: ast.stmt
                if item.optional_vars is not None:
                    enter_stmt = ast.Assign(targets=[item.optional_vars], value=enter_call)
                else:
                    enter_stmt = ast.Expr(value=enter_call)
                exit_stmt = ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(value=ast.Name(id=ctx_name, ctx=ast.Load()),
                                           attr="__exit__", ctx=ast.Load()),
                        args=[ast.Constant(value=0), ast.Constant(value=0), ast.Constant(value=0)],
                        keywords=[],
                    )
                )
                cur_body = [
                    ast.Assign(targets=[ast.Name(id=ctx_name, ctx=ast.Store())],
                               value=item.context_expr),
                    enter_stmt,
                    ast.Try(body=cur_body, handlers=[], orelse=[], finalbody=[exit_stmt]),
                ]
            out.extend(_desugar_stmt_list(cur_body, counter))
            continue

        ext = _CompExtractor(counter)
        if isinstance(s, ast.If):
            s.test = ext.visit(s.test)
            out.extend(_desugar_stmt_list(ext.pre_stmts, counter))
            s.body = _desugar_stmt_list(s.body, counter)
            s.orelse = _desugar_stmt_list(s.orelse, counter)
            out.append(s)
            continue
        if isinstance(s, ast.While):
            s.test = ext.visit(s.test)
            out.extend(_desugar_stmt_list(ext.pre_stmts, counter))
            s.body = _desugar_stmt_list(s.body, counter)
            s.orelse = _desugar_stmt_list(s.orelse, counter)
            out.append(s)
            continue
        if isinstance(s, ast.For):
            s.iter = ext.visit(s.iter)
            out.extend(_desugar_stmt_list(ext.pre_stmts, counter))
            if isinstance(s.target, (ast.Tuple, ast.List)):
                item_var = counter.next("__for_item")
                unpacked = _unpack_assign(
                    s.target, ast.Name(id=item_var, ctx=ast.Load()), counter
                )
                s.target = ast.Name(id=item_var, ctx=ast.Store())
                s.body = unpacked + s.body
            s.body = _desugar_stmt_list(s.body, counter)
            s.orelse = _desugar_stmt_list(s.orelse, counter)
            out.append(s)
            continue
        if isinstance(s, ast.Try):
            s.body = _desugar_stmt_list(s.body, counter)
            s.orelse = _desugar_stmt_list(s.orelse, counter)
            s.finalbody = _desugar_stmt_list(s.finalbody, counter)
            for h in s.handlers:
                h.body = _desugar_stmt_list(h.body, counter)
            out.append(s)
            continue

        s = ext.visit(s)
        if ext.pre_stmts:
            out.extend(_desugar_stmt_list(ext.pre_stmts, counter))

        if isinstance(s, ast.Assign):
            if len(s.targets) == 1 and isinstance(s.targets[0], (ast.Tuple, ast.List)):
                unpacked = _unpack_assign(s.targets[0], s.value, counter)
                out.extend(_desugar_stmt_list(unpacked, counter))
                continue
            if isinstance(s.value, (ast.List, ast.Tuple)) \
                    and any(isinstance(e, ast.Starred) for e in s.value.elts):
                tmp_l = counter.next("__star_list")
                out.append(ast.Assign(
                    targets=[ast.Name(id=tmp_l, ctx=ast.Store())],
                    value=ast.List(elts=[], ctx=ast.Load()),
                ))
                for e in s.value.elts:
                    if isinstance(e, ast.Starred):
                        out.append(ast.Expr(value=ast.Call(
                            func=ast.Attribute(value=ast.Name(id=tmp_l, ctx=ast.Load()),
                                               attr="extend", ctx=ast.Load()),
                            args=[e.value], keywords=[],
                        )))
                    else:
                        out.append(ast.Expr(value=ast.Call(
                            func=ast.Attribute(value=ast.Name(id=tmp_l, ctx=ast.Load()),
                                               attr="append", ctx=ast.Load()),
                            args=[e], keywords=[],
                        )))
                s.value = ast.Name(id=tmp_l, ctx=ast.Load())
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 5. Classes, MRO, super(), descriptors, @property, __slots__, dunder protocols
# ---------------------------------------------------------------------------

@dataclass
class ClassMeta:
    name: str
    class_id: int
    bases: list[str] = field(default_factory=list)
    mro: list[str] = field(default_factory=list)
    slots: set[str] | None = None
    consts: dict[str, ast.Constant] = field(default_factory=dict)
    methods: dict[str, ast.FunctionDef] = field(default_factory=dict)
    prop_get: dict[str, ast.FunctionDef] = field(default_factory=dict)
    prop_set: dict[str, ast.FunctionDef] = field(default_factory=dict)
    descriptors: dict[str, tuple[str, str]] = field(default_factory=dict)  # attr -> (global_var, DescClass)
    field_types: dict[str, str] = field(default_factory=dict)


def _c3_mro(cls_name: str, classes: dict[str, ClassMeta]) -> list[str]:
    meta = classes[cls_name]
    if not meta.bases:
        return [cls_name]
    seqs: list[list[str]] = [list(_c3_mro(b, classes)) for b in meta.bases]
    seqs.append(list(meta.bases))
    res = [cls_name]
    while True:
        seqs = [s for s in seqs if s]
        if not seqs:
            return res
        cand: str | None = None
        for s in seqs:
            head = s[0]
            if all(head not in other[1:] for other in seqs):
                cand = head
                break
        if cand is None:
            raise VelaTypeError(f"inconsistent MRO for class '{cls_name}'")
        res.append(cand)
        for s in seqs:
            if s and s[0] == cand:
                s.pop(0)


def _infer_expr_rough_type(node: ast.expr, env: dict[str, str],
                           classes: dict[str, ClassMeta]) -> str:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return "bool"
        if isinstance(node.value, int):
            return "int"
        if isinstance(node.value, float):
            return "float"
        if isinstance(node.value, str):
            return "str"
        return "int"
    if isinstance(node, ast.Name):
        return env.get(node.id, "int")
    if isinstance(node, ast.List):
        if node.elts and all(_infer_expr_rough_type(e, env, classes) == "str" for e in node.elts):
            return "strlist"
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.Set):
        return "set"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fname = node.func.id
        if fname in classes:
            return f"inst:{fname}"
        if fname == "str":
            return "str"
        if fname == "float":
            return "float"
        if fname == "bool":
            return "bool"
        if fname in ("list", "tuple", "set", "dict"):
            return fname
    if isinstance(node, ast.BinOp):
        lt = _infer_expr_rough_type(node.left, env, classes)
        rt = _infer_expr_rough_type(node.right, env, classes)
        if lt == "str" or rt == "str":
            return "str"
        if lt == "float" or rt == "float" or isinstance(node.op, ast.Div):
            return "float"
    return "int"


def _desugar_classes(tree: ast.Module, counter: _Counter) -> None:
    classes: dict[str, ClassMeta] = {}
    cid = 0
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            cid += 1
            bases = [b.id for b in stmt.bases if isinstance(b, ast.Name) and b.id != "object"]
            classes[stmt.name] = ClassMeta(name=stmt.name, class_id=cid, bases=bases)
    if not classes:
        return

    for cname in classes:
        classes[cname].mro = _c3_mro(cname, classes)

    desc_init_stmts: list[ast.stmt] = []
    desc_slot = 32
    for stmt in tree.body:
        if not isinstance(stmt, ast.ClassDef):
            continue
        meta = classes[stmt.name]
        for item in stmt.body:
            if isinstance(item, ast.Assign) and len(item.targets) == 1 \
                    and isinstance(item.targets[0], ast.Name):
                aname = item.targets[0].id
                if aname == "__slots__":
                    if isinstance(item.value, (ast.Tuple, ast.List)):
                        meta.slots = {
                            e.value for e in item.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        }
                    elif isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                        meta.slots = {item.value.value}
                    continue
                if isinstance(item.value, ast.Constant):
                    meta.consts[aname] = item.value
                    continue
                if isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Name) \
                        and item.value.func.id in classes:
                    dcls = item.value.func.id
                    slot_str = str(desc_slot)
                    desc_slot += 1
                    meta.descriptors[aname] = (slot_str, dcls)
                    desc_init_stmts.append(
                        ast.Expr(
                            value=ast.Call(
                                func=ast.Name(id="__global_obj_set", ctx=ast.Load()),
                                args=[ast.Constant(value=int(slot_str)), item.value],
                                keywords=[],
                            )
                        )
                    )
                    continue
            if isinstance(item, ast.FunctionDef):
                is_prop_get = any(
                    isinstance(d, ast.Name) and d.id == "property"
                    for d in item.decorator_list
                )
                setter_for: str | None = None
                for d in item.decorator_list:
                    if isinstance(d, ast.Attribute) and d.attr == "setter" \
                            and isinstance(d.value, ast.Name):
                        setter_for = d.value.id
                if is_prop_get:
                    fn_copy = copy.deepcopy(item)
                    fn_copy.decorator_list = []
                    meta.prop_get[item.name] = fn_copy
                elif setter_for is not None:
                    fn_copy = copy.deepcopy(item)
                    fn_copy.decorator_list = []
                    meta.prop_set[setter_for] = fn_copy
                else:
                    fn_copy = copy.deepcopy(item)
                    fn_copy.decorator_list = [
                        d for d in fn_copy.decorator_list
                        if not (isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod"))
                    ]
                    meta.methods[item.name] = fn_copy

    # Infer field types across methods of each class (including inherited fields)
    for cname, meta in classes.items():
        for anc in reversed(meta.mro):
            anc_meta = classes[anc]
            for k, cnode in anc_meta.consts.items():
                meta.field_types[k] = _infer_expr_rough_type(cnode, {}, classes)
            for m in anc_meta.methods.values():
                penv: dict[str, str] = {"self": f"inst:{cname}"}
                for a in m.args.args[1:]:
                    if isinstance(a.annotation, ast.Name):
                        penv[a.arg] = (
                            f"inst:{a.annotation.id}"
                            if a.annotation.id in classes
                            else a.annotation.id
                        )
                for sub in ast.walk(m):
                    if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                        t = sub.targets[0]
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                                and t.value.id == "self":
                            if t.attr in ("target", "fn", "callback"):
                                fty = "func"
                            else:
                                fty = _infer_expr_rough_type(sub.value, penv, classes)
                            meta.field_types.setdefault(t.attr, fty)
                        elif isinstance(t, ast.Name):
                            penv[t.id] = _infer_expr_rough_type(sub.value, penv, classes)

    def resolve_method(cname: str, mname: str, start_after: str | None = None) -> tuple[str, ast.FunctionDef] | None:
        mro = classes[cname].mro
        if start_after is not None and start_after in mro:
            mro = mro[mro.index(start_after) + 1:]
        for anc in mro:
            if mname in classes[anc].methods:
                return anc, classes[anc].methods[mname]
        return None

    def resolve_prop_get(cname: str, attr: str) -> tuple[str, ast.FunctionDef] | None:
        for anc in classes[cname].mro:
            if attr in classes[anc].prop_get:
                return anc, classes[anc].prop_get[attr]
        return None

    def resolve_prop_set(cname: str, attr: str) -> tuple[str, ast.FunctionDef] | None:
        for anc in classes[cname].mro:
            if attr in classes[anc].prop_set:
                return anc, classes[anc].prop_set[attr]
        return None

    def resolve_descriptor(cname: str, attr: str) -> tuple[str, str] | None:
        for anc in classes[cname].mro:
            if attr in classes[anc].descriptors:
                return classes[anc].descriptors[attr]
        return None

    def allowed_slots(cname: str) -> set[str] | None:
        acc: set[str] = set()
        for anc in classes[cname].mro:
            s = classes[anc].slots
            if s is None:
                return None
            acc |= s
        return acc

    def resolve_field_type(cname: str, attr: str) -> str:
        for anc in classes[cname].mro:
            if attr in classes[anc].field_types:
                return classes[anc].field_types[attr]
        return "int"

    def resolve_const(cname: str, attr: str) -> ast.Constant | None:
        for anc in classes[cname].mro:
            if attr in classes[anc].consts:
                return classes[anc].consts[attr]
        return None

    def make_attr_get(obj_expr: ast.expr, attr: str, ty: str, default: ast.expr | None = None) -> ast.Call:
        args: list[ast.expr] = [obj_expr, ast.Constant(value=attr)]
        if default is not None:
            args.append(default)
        c = ast.Call(
            func=ast.Name(id="__attr_get", ctx=ast.Load()),
            args=args,
            keywords=[],
        )
        c._vela_attr_ty = ty  # type: ignore[attr-defined]
        return c

    def make_attr_set(obj_expr: ast.expr, attr: str, val_expr: ast.expr) -> ast.Call:
        return ast.Call(
            func=ast.Name(id="__attr_set", ctx=ast.Load()),
            args=[obj_expr, ast.Constant(value=attr), val_expr],
            keywords=[],
        )

    DUNDER_BINOP = {
        ast.Add: "__add__",
        ast.Sub: "__sub__",
        ast.Mult: "__mul__",
        ast.FloorDiv: "__floordiv__",
        ast.Mod: "__mod__",
    }
    DUNDER_CMP = {
        ast.Eq: "__eq__",
        ast.NotEq: "__ne__",
        ast.Lt: "__lt__",
        ast.LtE: "__le__",
        ast.Gt: "__gt__",
        ast.GtE: "__ge__",
    }

    class _ClassExprRewriter(ast.NodeTransformer):
        def __init__(self, current_cls: str | None, defining_cls: str | None,
                     var_classes: dict[str, str]) -> None:
            self.current_cls = current_cls
            self.defining_cls = defining_cls
            self.var_classes = dict(var_classes)

        def _obj_class(self, expr: ast.expr) -> str | None:
            if isinstance(expr, ast.Name):
                if expr.id == "self" and self.current_cls:
                    return self.current_cls
                return self.var_classes.get(expr.id)
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                if expr.func.id in classes:
                    return expr.func.id
                gen_cls = f"__Gen_{expr.func.id}"
                if gen_cls in classes:
                    return gen_cls
                if expr.func.id.startswith("__new_"):
                    c = expr.func.id[6:]
                    if c in classes:
                        return c
            if isinstance(expr, ast.Attribute):
                oc = self._obj_class(expr.value)
                if oc:
                    fty = resolve_field_type(oc, expr.attr)
                    if fty.startswith("inst:"):
                        return fty[5:]
            if isinstance(expr, ast.BinOp):
                oc = self._obj_class(expr.left)
                dunder = DUNDER_BINOP.get(type(expr.op))
                if oc and dunder and resolve_method(oc, dunder) is not None:
                    return oc
            if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub):
                oc = self._obj_class(expr.operand)
                if oc and resolve_method(oc, "__neg__") is not None:
                    return oc
            return None

        def _guess_attr_class(self, attr: str) -> str | None:
            if self.current_cls and (
                attr in classes[self.current_cls].field_types
                or resolve_prop_get(self.current_cls, attr) is not None
                or resolve_descriptor(self.current_cls, attr) is not None
            ):
                return self.current_cls
            for cname, meta in classes.items():
                if (
                    attr in meta.field_types
                    or resolve_prop_get(cname, attr) is not None
                    or resolve_descriptor(cname, attr) is not None
                ):
                    return cname
            return None

        def visit_Assign(self, node: ast.Assign) -> ast.AST:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                oc = self._obj_class(node.value)
                if oc:
                    self.var_classes[node.targets[0].id] = oc
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute):
                attr_t = node.targets[0]
                oc = self._obj_class(attr_t.value) or self._guess_attr_class(attr_t.attr)
                if oc is not None:
                    slots = allowed_slots(oc)
                    if slots is not None and attr_t.attr not in slots \
                            and not attr_t.attr.startswith("_"):
                        raise VelaTypeError(
                            f"'{oc}' object has no attribute '{attr_t.attr}' "
                            f"(restricted by __slots__)",
                            node,
                        )
                    pset = resolve_prop_set(oc, attr_t.attr)
                    if pset is not None:
                        owner, _ = pset
                        return ast.copy_location(
                            ast.Expr(
                                value=ast.Call(
                                    func=ast.Name(id=f"{owner}__propset_{attr_t.attr}", ctx=ast.Load()),
                                    args=[self.visit(attr_t.value), self.visit(node.value)],
                                    keywords=[],
                                )
                            ),
                            node,
                        )
                    if resolve_prop_get(oc, attr_t.attr) is not None:
                        raise VelaTypeError(
                            f"property '{attr_t.attr}' of '{oc}' object has no setter",
                            node,
                        )
                    desc = resolve_descriptor(oc, attr_t.attr)
                    if desc is not None:
                        slot_str, dcls = desc
                        mset = resolve_method(dcls, "__set__")
                        if mset is not None:
                            downer, _ = mset
                            return ast.copy_location(
                                ast.Expr(
                                    value=ast.Call(
                                        func=ast.Name(id=f"{downer}____set__", ctx=ast.Load()),
                                        args=[
                                            ast.Call(
                                                func=ast.Name(id="__global_obj_get", ctx=ast.Load()),
                                                args=[ast.Constant(value=int(slot_str))],
                                                keywords=[],
                                            ),
                                            self.visit(attr_t.value),
                                            self.visit(node.value),
                                        ],
                                        keywords=[],
                                    )
                                ),
                                node,
                            )
                    val_v = self.visit(node.value)
                    val_oc = self._obj_class(node.value)
                    if val_oc:
                        classes[oc].field_types[attr_t.attr] = f"inst:{val_oc}"
                    return ast.copy_location(
                        ast.Expr(value=make_attr_set(self.visit(attr_t.value), attr_t.attr, val_v)),
                        node,
                    )
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
                sub_t = node.targets[0]
                oc = self._obj_class(sub_t.value)
                if oc is not None:
                    m = resolve_method(oc, "__setitem__")
                    if m is not None:
                        owner, _ = m
                        return ast.copy_location(
                            ast.Expr(
                                value=ast.Call(
                                    func=ast.Name(id=f"{owner}____setitem__", ctx=ast.Load()),
                                    args=[
                                        self.visit(sub_t.value),
                                        self.visit(sub_t.slice),
                                        self.visit(node.value),
                                    ],
                                    keywords=[],
                                )
                            ),
                            node,
                        )
            node.value = self.visit(node.value)
            node.targets = [self.visit(t) for t in node.targets]
            return node

        def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
            if isinstance(node.target, ast.Attribute):
                load_attr = ast.Attribute(
                    value=copy.deepcopy(node.target.value),
                    attr=node.target.attr,
                    ctx=ast.Load(),
                )
                assign = ast.Assign(
                    targets=[node.target],
                    value=ast.BinOp(left=load_attr, op=node.op, right=node.value),
                )
                return self.visit_Assign(ast.copy_location(assign, node))
            return self.generic_visit(node)

        def visit_For(self, node: ast.For) -> ast.AST:
            oc = self._obj_class(node.iter)
            if oc is not None and resolve_method(oc, "__iter__") is not None:
                it_var = counter.next("__iter")
                miter = resolve_method(oc, "__iter__")
                assert miter is not None
                iter_owner, _ = miter
                mnext = resolve_method(oc, "__next__")
                next_owner = mnext[0] if mnext is not None else iter_owner
                self.var_classes[it_var] = oc
                init_it = ast.Assign(
                    targets=[ast.Name(id=it_var, ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Name(id=f"{iter_owner}____iter__", ctx=ast.Load()),
                        args=[self.visit(node.iter)],
                        keywords=[],
                    ),
                )
                try_next = ast.Try(
                    body=[
                        ast.Assign(
                            targets=[node.target],
                            value=ast.Call(
                                func=ast.Name(id=f"{next_owner}____next__", ctx=ast.Load()),
                                args=[ast.Name(id=it_var, ctx=ast.Load())],
                                keywords=[],
                            ),
                        )
                    ],
                    handlers=[
                        ast.ExceptHandler(
                            type=ast.Name(id="StopIteration", ctx=ast.Load()),
                            name=None,
                            body=[ast.Break()],
                        )
                    ],
                    orelse=[],
                    finalbody=[],
                )
                body_visited = [self.visit(s) for s in node.body]
                flat_body: list[ast.stmt] = [try_next]
                for b in body_visited:
                    if isinstance(b, list):
                        flat_body.extend(b)
                    elif b is not None:
                        flat_body.append(b)
                loop = ast.While(
                    test=ast.Constant(value=True),
                    body=flat_body,
                    orelse=[],
                )
                return [init_it, loop]  # type: ignore[return-value]
            return self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> ast.AST:
            # super().method(args...)
            if isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Call) \
                    and isinstance(node.func.value.func, ast.Name) \
                    and node.func.value.func.id == "super" \
                    and self.current_cls is not None:
                mname = node.func.attr
                start_cls = self.defining_cls or self.current_cls
                if len(node.func.value.args) >= 1 and isinstance(node.func.value.args[0], ast.Name):
                    start_cls = node.func.value.args[0].id
                resolved = resolve_method(self.current_cls, mname, start_after=start_cls)
                if resolved is None:
                    raise VelaTypeError(
                        f"super() of '{self.current_cls}' has no method '{mname}'", node
                    )
                owner, target_mfn = resolved
                target_fn_name = f"{owner}__{mname}"
                if owner != self.current_cls and any(
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "super"
                    for sub in ast.walk(target_mfn)
                ):
                    target_fn_name = f"{self.current_cls}__super_{owner}__{mname}"
                new_args = [ast.Name(id="self", ctx=ast.Load())] + [
                    self.visit(a) for a in node.args
                ]
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id=target_fn_name, ctx=ast.Load()),
                        args=new_args,
                        keywords=node.keywords,
                    ),
                    node,
                )

            # isinstance(obj, Cls)
            if isinstance(node.func, ast.Name) and node.func.id == "isinstance" \
                    and len(node.args) == 2 and isinstance(node.args[1], ast.Name) \
                    and node.args[1].id in classes:
                target_cls = node.args[1].id
                oc = self._obj_class(node.args[0])
                if oc is not None:
                    is_sub = target_cls in classes[oc].mro
                    return ast.copy_location(ast.Constant(value=is_sub), node)
                valid_ids = [
                    c.class_id for c in classes.values() if target_cls in c.mro
                ]
                cid_expr = make_attr_get(self.visit(node.args[0]), "__class__", "int")
                conds = [
                    ast.Compare(left=copy.deepcopy(cid_expr), ops=[ast.Eq()],
                                comparators=[ast.Constant(value=vid)])
                    for vid in valid_ids
                ]
                if len(conds) == 1:
                    return conds[0]
                return ast.BoolOp(op=ast.Or(), values=conds)

            # len(obj) / str(obj) / iter(obj) / next(obj) / list(obj) / sum(obj)
            if isinstance(node.func, ast.Name) and len(node.args) == 1:
                oc = self._obj_class(node.args[0])
                if oc is not None:
                    if node.func.id == "len" and resolve_method(oc, "__len__") is not None:
                        owner, _ = resolve_method(oc, "__len__")  # type: ignore[misc]
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}____len__", ctx=ast.Load()),
                                args=[self.visit(node.args[0])],
                                keywords=[],
                            ),
                            node,
                        )
                    if node.func.id == "str" and resolve_method(oc, "__str__") is not None:
                        owner, _ = resolve_method(oc, "__str__")  # type: ignore[misc]
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}____str__", ctx=ast.Load()),
                                args=[self.visit(node.args[0])],
                                keywords=[],
                            ),
                            node,
                        )
                    if node.func.id == "iter" and resolve_method(oc, "__iter__") is not None:
                        owner, _ = resolve_method(oc, "__iter__")  # type: ignore[misc]
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}____iter__", ctx=ast.Load()),
                                args=[self.visit(node.args[0])],
                                keywords=[],
                            ),
                            node,
                        )
                    if node.func.id == "next" and resolve_method(oc, "__next__") is not None:
                        owner, _ = resolve_method(oc, "__next__")  # type: ignore[misc]
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}____next__", ctx=ast.Load()),
                                args=[self.visit(node.args[0])],
                                keywords=[],
                            ),
                            node,
                        )
                    if node.func.id == "list" and resolve_method(oc, "__iter__") is not None:
                        owner, _ = resolve_method(oc, "__iter__")  # type: ignore[misc]
                        nowner, _ = resolve_method(oc, "__next__") or (owner, None)  # type: ignore[misc]
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"__iter_to_list_{oc}", ctx=ast.Load()),
                                args=[self.visit(node.args[0])],
                                keywords=[],
                            ),
                            node,
                        )
                    if node.func.id == "sum" and resolve_method(oc, "__iter__") is not None:
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id="sum", ctx=ast.Load()),
                                args=[
                                    ast.Call(
                                        func=ast.Name(id=f"__iter_to_list_{oc}", ctx=ast.Load()),
                                        args=[self.visit(node.args[0])],
                                        keywords=[],
                                    )
                                ],
                                keywords=[],
                            ),
                            node,
                        )

            # print(..., obj, ...) where obj has __str__
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                new_pargs: list[ast.expr] = []
                for a in node.args:
                    oc = self._obj_class(a)
                    if oc is not None and resolve_method(oc, "__str__") is not None:
                        owner, _ = resolve_method(oc, "__str__")  # type: ignore[misc]
                        new_pargs.append(
                            ast.Call(
                                func=ast.Name(id=f"{owner}____str__", ctx=ast.Load()),
                                args=[self.visit(a)],
                                keywords=[],
                            )
                        )
                    else:
                        new_pargs.append(self.visit(a))
                node.args = new_pargs
                return node

            # Class instantiation C(args...)
            if isinstance(node.func, ast.Name) and node.func.id in classes:
                cname = node.func.id
                node.func = ast.Name(id=f"__new_{cname}", ctx=ast.Load())
                node.args = [self.visit(a) for a in node.args]
                node.keywords = [
                    ast.keyword(arg=kw.arg, value=self.visit(kw.value))
                    for kw in node.keywords
                ]
                return node

            # Callable object obj(args...) via __call__
            if isinstance(node.func, ast.Name) and node.func.id in self.var_classes:
                oc = self.var_classes[node.func.id]
                mcall = resolve_method(oc, "__call__")
                if mcall is not None:
                    owner, _ = mcall
                    return ast.copy_location(
                        ast.Call(
                            func=ast.Name(id=f"{owner}____call__", ctx=ast.Load()),
                            args=[ast.Name(id=node.func.id, ctx=ast.Load())]
                            + [self.visit(a) for a in node.args],
                            keywords=node.keywords,
                        ),
                        node,
                    )

            # Method call obj.m(args...) or Cls.m(args...)
            if isinstance(node.func, ast.Attribute):
                mname = node.func.attr
                if isinstance(node.func.value, ast.Name) and node.func.value.id in classes:
                    cname = node.func.value.id
                    if mname == "mro":
                        return ast.copy_location(
                            ast.List(
                                elts=[ast.Constant(value=n) for n in classes[cname].mro],
                                ctx=ast.Load(),
                            ),
                            node,
                        )
                    resolved = resolve_method(cname, mname)
                    if resolved is not None:
                        owner, _ = resolved
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}__{mname}", ctx=ast.Load()),
                                args=[self.visit(a) for a in node.args],
                                keywords=node.keywords,
                            ),
                            node,
                        )
                oc = self._obj_class(node.func.value)
                if oc is not None:
                    resolved = resolve_method(oc, mname)
                    if resolved is not None:
                        owner, _ = resolved
                        recv = self.visit(node.func.value)
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}__{mname}", ctx=ast.Load()),
                                args=[recv] + [self.visit(a) for a in node.args],
                                keywords=node.keywords,
                            ),
                            node,
                        )
                node.func.value = self.visit(node.func.value)
                node.args = [self.visit(a) for a in node.args]
                node.keywords = [
                    ast.keyword(arg=kw.arg, value=self.visit(kw.value))
                    for kw in node.keywords
                ]
                return node
            return self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            if isinstance(node.ctx, ast.Load):
                if isinstance(node.value, ast.Name) and node.value.id in classes:
                    cname = node.value.id
                    cval = resolve_const(cname, node.attr)
                    if cval is not None:
                        return ast.copy_location(copy.deepcopy(cval), node)
                oc = self._obj_class(node.value) or self._guess_attr_class(node.attr)
                if oc is not None:
                    pget = resolve_prop_get(oc, node.attr)
                    if pget is not None:
                        owner, _ = pget
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}__propget_{node.attr}", ctx=ast.Load()),
                                args=[self.visit(node.value)],
                                keywords=[],
                            ),
                            node,
                        )
                    desc = resolve_descriptor(oc, node.attr)
                    if desc is not None:
                        slot_str, dcls = desc
                        mget = resolve_method(dcls, "__get__")
                        if mget is not None:
                            downer, _ = mget
                            return ast.copy_location(
                                ast.Call(
                                    func=ast.Name(id=f"{downer}____get__", ctx=ast.Load()),
                                    args=[
                                        ast.Call(
                                            func=ast.Name(id="__global_obj_get", ctx=ast.Load()),
                                            args=[ast.Constant(value=int(slot_str))],
                                            keywords=[],
                                        ),
                                        self.visit(node.value),
                                        ast.Constant(value=classes[oc].class_id),
                                    ],
                                    keywords=[],
                                ),
                                node,
                            )
                    fty = resolve_field_type(oc, node.attr)
                    cdef = resolve_const(oc, node.attr)
                    return ast.copy_location(
                        make_attr_get(
                            self.visit(node.value),
                            node.attr,
                            "dict" if fty.startswith("inst:") else fty,
                            copy.deepcopy(cdef) if cdef is not None else None,
                        ),
                        node,
                    )
            return self.generic_visit(node)

        def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
            oc = self._obj_class(node.left)
            dunder = DUNDER_BINOP.get(type(node.op))
            if oc is not None and dunder is not None:
                m = resolve_method(oc, dunder)
                if m is not None:
                    owner, _ = m
                    return ast.copy_location(
                        ast.Call(
                            func=ast.Name(id=f"{owner}__{dunder}", ctx=ast.Load()),
                            args=[self.visit(node.left), self.visit(node.right)],
                            keywords=[],
                        ),
                        node,
                    )
            return self.generic_visit(node)

        def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
            if isinstance(node.op, ast.USub):
                oc = self._obj_class(node.operand)
                if oc is not None:
                    m = resolve_method(oc, "__neg__")
                    if m is not None:
                        owner, _ = m
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}____neg__", ctx=ast.Load()),
                                args=[self.visit(node.operand)],
                                keywords=[],
                            ),
                            node,
                        )
            return self.generic_visit(node)

        def visit_Compare(self, node: ast.Compare) -> ast.AST:
            if len(node.ops) == 1:
                op = node.ops[0]
                comp = node.comparators[0]
                if isinstance(op, (ast.In, ast.NotIn)):
                    roc = self._obj_class(comp)
                    if roc is not None:
                        m = resolve_method(roc, "__contains__")
                        if m is not None:
                            owner, _ = m
                            call = ast.Call(
                                func=ast.Name(id=f"{owner}____contains__", ctx=ast.Load()),
                                args=[self.visit(comp), self.visit(node.left)],
                                keywords=[],
                            )
                            if isinstance(op, ast.NotIn):
                                return ast.copy_location(
                                    ast.UnaryOp(op=ast.Not(), operand=call), node
                                )
                            return ast.copy_location(call, node)
                loc = self._obj_class(node.left)
                dunder = DUNDER_CMP.get(type(op))
                if loc is not None and dunder is not None:
                    m = resolve_method(loc, dunder)
                    if m is not None:
                        owner, _ = m
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}__{dunder}", ctx=ast.Load()),
                                args=[self.visit(node.left), self.visit(comp)],
                                keywords=[],
                            ),
                            node,
                        )
                    if isinstance(op, ast.NotEq):
                        meq = resolve_method(loc, "__eq__")
                        if meq is not None:
                            owner, _ = meq
                            return ast.copy_location(
                                ast.UnaryOp(
                                    op=ast.Not(),
                                    operand=ast.Call(
                                        func=ast.Name(id=f"{owner}____eq__", ctx=ast.Load()),
                                        args=[self.visit(node.left), self.visit(comp)],
                                        keywords=[],
                                    ),
                                ),
                                node,
                            )
            return self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
            if isinstance(node.ctx, ast.Load):
                oc = self._obj_class(node.value)
                if oc is not None:
                    m = resolve_method(oc, "__getitem__")
                    if m is not None:
                        owner, _ = m
                        return ast.copy_location(
                            ast.Call(
                                func=ast.Name(id=f"{owner}____getitem__", ctx=ast.Load()),
                                args=[self.visit(node.value), self.visit(node.slice)],
                                keywords=[],
                            ),
                            node,
                        )
            return self.generic_visit(node)

    BIN_DUNDERS = {
        "__add__", "__sub__", "__mul__", "__floordiv__", "__mod__",
        "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
    }
    generated_funcs: list[ast.stmt] = []
    # Emit methods, properties, and __new_<C> for each class
    for cname, meta in classes.items():
        for pname, pfn in meta.prop_get.items():
            fn_out = copy.deepcopy(pfn)
            fn_out.name = f"{cname}__propget_{pname}"
            if fn_out.args.args:
                fn_out.args.args[0]._vela_cls_dict = True  # type: ignore[attr-defined]
            rw = _ClassExprRewriter(cname, cname, {"self": cname})
            fn_out = rw.visit(fn_out)
            generated_funcs.append(fn_out)
        for pname, pfn in meta.prop_set.items():
            fn_out = copy.deepcopy(pfn)
            fn_out.name = f"{cname}__propset_{pname}"
            if fn_out.args.args:
                fn_out.args.args[0]._vela_cls_dict = True  # type: ignore[attr-defined]
            rw = _ClassExprRewriter(cname, cname, {"self": cname})
            fn_out = rw.visit(fn_out)
            generated_funcs.append(fn_out)
        for mname, mfn in meta.methods.items():
            fn_out = copy.deepcopy(mfn)
            fn_out.name = f"{cname}__{mname}"
            if fn_out.args.args:
                fn_out.args.args[0]._vela_cls_dict = True  # type: ignore[attr-defined]
            vmap = {"self": cname}
            if mname in BIN_DUNDERS and len(fn_out.args.args) >= 2:
                vmap[fn_out.args.args[1].arg] = cname
                fn_out.args.args[1]._vela_cls_dict = True  # type: ignore[attr-defined]
            if mname in ("__get__", "__set__") and len(fn_out.args.args) >= 2:
                fn_out.args.args[1]._vela_cls_dict = True  # type: ignore[attr-defined]
            for a in fn_out.args.args[1:]:
                if isinstance(a.annotation, ast.Name) and a.annotation.id in classes:
                    vmap[a.arg] = a.annotation.id
                    a._vela_cls_dict = True  # type: ignore[attr-defined]
                    a.annotation = None
            if isinstance(fn_out.returns, ast.Name) and fn_out.returns.id in classes:
                fn_out.returns = None
            rw = _ClassExprRewriter(cname, cname, vmap)
            fn_out = rw.visit(fn_out)
            generated_funcs.append(fn_out)

        # Also emit specialized ancestor methods that use super() for this receiver class MRO
        for anc in meta.mro[1:]:
            for mname, mfn in list(classes[anc].methods.items()):
                if any(
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "super"
                    for sub in ast.walk(mfn)
                ):
                    fn_spec = copy.deepcopy(mfn)
                    fn_spec.name = f"{cname}__super_{anc}__{mname}"
                    if fn_spec.args.args:
                        fn_spec.args.args[0]._vela_cls_dict = True  # type: ignore[attr-defined]
                    rw = _ClassExprRewriter(cname, anc, {"self": cname})
                    fn_spec = rw.visit(fn_spec)
                    generated_funcs.append(fn_spec)
                    if mname not in meta.methods:
                        fn_inh = copy.deepcopy(mfn)
                        fn_inh.name = f"{cname}__{mname}"
                        if fn_inh.args.args:
                            fn_inh.args.args[0]._vela_cls_dict = True  # type: ignore[attr-defined]
                        rw2 = _ClassExprRewriter(cname, anc, {"self": cname})
                        fn_inh = rw2.visit(fn_inh)
                        meta.methods[mname] = fn_inh
                        generated_funcs.append(fn_inh)

    for cname, meta in classes.items():
        init_res = resolve_method(cname, "__init__")
        if init_res is not None:
            init_owner, init_fn = init_res
            init_args = copy.deepcopy(init_fn.args)
            init_args.args = init_args.args[1:]
            param_names = [a.arg for a in init_args.args]
            new_body: list[ast.stmt] = [
                ast.Assign(
                    targets=[ast.Name(id="self", ctx=ast.Store())],
                    value=ast.Dict(keys=[], values=[]),
                ),
                ast.Expr(
                    value=make_attr_set(
                        ast.Name(id="self", ctx=ast.Load()),
                        "__class__",
                        ast.Constant(value=meta.class_id),
                    )
                ),
                ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id=f"{init_owner}____init__", ctx=ast.Load()),
                        args=[ast.Name(id="self", ctx=ast.Load())]
                        + [ast.Name(id=p, ctx=ast.Load()) for p in param_names],
                        keywords=[],
                    )
                ),
                ast.Return(value=ast.Name(id="self", ctx=ast.Load())),
            ]
        else:
            init_args = ast.arguments(
                posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
            )
            new_body = [
                ast.Assign(
                    targets=[ast.Name(id="self", ctx=ast.Store())],
                    value=ast.Dict(keys=[], values=[]),
                ),
                ast.Expr(
                    value=make_attr_set(
                        ast.Name(id="self", ctx=ast.Load()),
                        "__class__",
                        ast.Constant(value=meta.class_id),
                    )
                ),
                ast.Return(value=ast.Name(id="self", ctx=ast.Load())),
            ]
        del_res = resolve_method(cname, "__del__")
        if del_res is not None:
            del_owner, _ = del_res
            new_body.insert(
                -1,
                ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id="__set_finalizer", ctx=ast.Load()),
                        args=[
                            ast.Name(id="self", ctx=ast.Load()),
                            ast.Constant(value=f"{del_owner}____del__"),
                        ],
                        keywords=[],
                    )
                ),
            )
        new_fn = ast.FunctionDef(
            name=f"__new_{cname}",
            args=init_args,
            body=new_body,
            decorator_list=[],
        )
        generated_funcs.append(new_fn)

        if resolve_method(cname, "__iter__") is not None:
            iowner, _ = resolve_method(cname, "__iter__")  # type: ignore[misc]
            nowner, _ = resolve_method(cname, "__next__") or (iowner, None)  # type: ignore[misc]
            obj_arg = ast.arg(arg="_obj")
            obj_arg._vela_cls_dict = True  # type: ignore[attr-defined]
            helper_fn = ast.FunctionDef(
                name=f"__iter_to_list_{cname}",
                args=ast.arguments(
                    posonlyargs=[], args=[obj_arg],
                    kwonlyargs=[], kw_defaults=[], defaults=[],
                ),
                body=[
                    ast.Assign(
                        targets=[ast.Name(id="_out", ctx=ast.Store())],
                        value=ast.List(elts=[], ctx=ast.Load()),
                    ),
                    ast.Assign(
                        targets=[ast.Name(id="_it", ctx=ast.Store())],
                        value=ast.Call(
                            func=ast.Name(id=f"{iowner}____iter__", ctx=ast.Load()),
                            args=[ast.Name(id="_obj", ctx=ast.Load())],
                            keywords=[],
                        ),
                    ),
                    ast.While(
                        test=ast.Constant(value=True),
                        body=[
                            ast.Try(
                                body=[
                                    ast.Assign(
                                        targets=[ast.Name(id="_v", ctx=ast.Store())],
                                        value=ast.Call(
                                            func=ast.Name(id=f"{nowner}____next__", ctx=ast.Load()),
                                            args=[ast.Name(id="_it", ctx=ast.Load())],
                                            keywords=[],
                                        ),
                                    )
                                ],
                                handlers=[
                                    ast.ExceptHandler(
                                        type=ast.Name(id="StopIteration", ctx=ast.Load()),
                                        name=None,
                                        body=[ast.Break()],
                                    )
                                ],
                                orelse=[],
                                finalbody=[],
                            ),
                            ast.Expr(
                                value=ast.Call(
                                    func=ast.Attribute(
                                        value=ast.Name(id="_out", ctx=ast.Load()),
                                        attr="append",
                                        ctx=ast.Load(),
                                    ),
                                    args=[ast.Name(id="_v", ctx=ast.Load())],
                                    keywords=[],
                                )
                            ),
                        ],
                        orelse=[],
                    ),
                    ast.Return(value=ast.Name(id="_out", ctx=ast.Load())),
                ],
                decorator_list=[],
            )
            generated_funcs.append(helper_fn)

    # Rewrite remaining module statements and functions
    main_rw = _ClassExprRewriter(None, None, {})
    new_tree_body: list[ast.stmt] = list(generated_funcs) + [
        main_rw.visit(s) for s in desc_init_stmts
    ]
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            continue
        if isinstance(stmt, ast.FunctionDef):
            vmap: dict[str, str] = {}
            for a in stmt.args.args:
                if isinstance(a.annotation, ast.Name) and a.annotation.id in classes:
                    vmap[a.arg] = a.annotation.id
                    a.annotation = None
            if isinstance(stmt.returns, ast.Name) and stmt.returns.id in classes:
                stmt.returns = None
            fn_rw = _ClassExprRewriter(None, None, vmap)
            visited = fn_rw.visit(stmt)
            if isinstance(visited, list):
                new_tree_body.extend(visited)
            else:
                new_tree_body.append(visited)
        else:
            visited = main_rw.visit(stmt)
            if isinstance(visited, list):
                new_tree_body.extend(visited)
            elif visited is not None:
                new_tree_body.append(visited)

    for s in new_tree_body:
        ast.fix_missing_locations(s)
    tree.body = new_tree_body


# ---------------------------------------------------------------------------
# 6. Decorators, Default/Keyword/*args/**kwargs
# ---------------------------------------------------------------------------

class _NameReplacer(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, ast.expr]) -> None:
        self.mapping = mapping

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if isinstance(node.ctx, ast.Load) and node.id in self.mapping:
            return copy.deepcopy(self.mapping[node.id])
        return node


def _desugar_decorators(tree: ast.Module, counter: _Counter) -> None:
    funcs_by_name: dict[str, ast.FunctionDef] = {
        s.name: s for s in tree.body if isinstance(s, ast.FunctionDef)
    }
    added_funcs: list[ast.stmt] = []
    init_calls: list[ast.stmt] = []
    consumed_decs: set[str] = set()

    def apply_one_decorator(fn: ast.FunctionDef, dec: ast.expr) -> ast.FunctionDef:
        # Handle @dec(arg1, ...) factory decorator
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) \
                and dec.func.id in funcs_by_name:
            factory = funcs_by_name[dec.func.id]
            consumed_decs.add(dec.func.id)
            fparams = [a.arg for a in factory.args.args]
            fmap = {p: a for p, a in zip(fparams, dec.args)}
            inner_fn = next(
                (s for s in factory.body if isinstance(s, ast.FunctionDef)), None
            )
            if inner_fn is not None:
                specialized = copy.deepcopy(inner_fn)
                specialized.name = counter.next(f"__decfac_{dec.func.id}")
                specialized = _NameReplacer(fmap).visit(specialized)
                funcs_by_name[specialized.name] = specialized
                consumed_decs.add(specialized.name)
                return apply_one_decorator(fn, ast.Name(id=specialized.name, ctx=ast.Load()))

        if isinstance(dec, ast.Name) and dec.id in funcs_by_name:
            dec_fn = funcs_by_name[dec.id]
            dparam = dec_fn.args.args[0].arg if dec_fn.args.args else "fn"
            nested_wrap = next(
                (s for s in dec_fn.body if isinstance(s, ast.FunctionDef)), None
            )
            if nested_wrap is not None:
                consumed_decs.add(dec.id)
                orig_fn = copy.deepcopy(fn)
                orig_fn.name = counter.next(f"__orig_{fn.name}")
                orig_fn.decorator_list = []
                added_funcs.append(orig_fn)
                funcs_by_name[orig_fn.name] = orig_fn

                wrapped = copy.deepcopy(nested_wrap)
                wrapped.name = fn.name
                wrapped = _NameReplacer(
                    {dparam: ast.Name(id=orig_fn.name, ctx=ast.Load())}
                ).visit(wrapped)
                for pre in dec_fn.body:
                    if isinstance(pre, ast.Assign):
                        for t in pre.targets:
                            if isinstance(t, ast.Name):
                                wrapped = _NameReplacer({t.id: pre.value}).visit(wrapped)
                return wrapped
            else:
                # Decorator executes statements and returns fn (or transforms)
                for st in dec_fn.body:
                    if isinstance(st, ast.Return):
                        continue
                    st_copy = _NameReplacer(
                        {dparam: ast.Name(id=fn.name, ctx=ast.Load())}
                    ).visit(copy.deepcopy(st))
                    init_calls.append(st_copy)
                fn.decorator_list = []
                return fn
        raise UnsupportedError("decorators", fn)

    new_body: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.decorator_list:
            decs = list(stmt.decorator_list)
            stmt.decorator_list = []
            cur_fn = stmt
            for d in reversed(decs):
                cur_fn = apply_one_decorator(cur_fn, d)
            funcs_by_name[cur_fn.name] = cur_fn
            new_body.append(cur_fn)
        else:
            new_body.append(stmt)
    tree.body = [
        s for s in (added_funcs + new_body + init_calls)
        if not (isinstance(s, ast.FunctionDef) and s.name in consumed_decs)
    ]


@dataclass
class _SigSpec:
    pos_names: list[str]
    pos_defaults: dict[str, ast.expr]
    vararg: str | None
    kwonly_names: list[str]
    kwonly_defaults: dict[str, ast.expr]
    kwarg: str | None

    @property
    def needs_rewrite(self) -> bool:
        return bool(
            self.pos_defaults
            or self.vararg
            or self.kwonly_names
            or self.kwarg
        )


def _desugar_signatures_and_calls(tree: ast.Module) -> None:
    sigs: dict[str, _SigSpec] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            a = node.args
            pos_args = list(a.posonlyargs) + list(a.args)
            pos_names = [p.arg for p in pos_args]
            pos_defaults: dict[str, ast.expr] = {}
            if a.defaults:
                offset = len(pos_names) - len(a.defaults)
                for p, d in zip(pos_names[offset:], a.defaults):
                    pos_defaults[p] = copy.deepcopy(d)
            kwonly_names = [k.arg for k in a.kwonlyargs]
            kwonly_defaults: dict[str, ast.expr] = {}
            for k, d in zip(kwonly_names, a.kw_defaults):
                if d is not None:
                    kwonly_defaults[k] = copy.deepcopy(d)
            vararg = a.vararg.arg if a.vararg else None
            kwarg = a.kwarg.arg if a.kwarg else None
            spec = _SigSpec(
                pos_names=pos_names,
                pos_defaults=pos_defaults,
                vararg=vararg,
                kwonly_names=kwonly_names,
                kwonly_defaults=kwonly_defaults,
                kwarg=kwarg,
            )
            sigs[node.name] = spec
            if spec.needs_rewrite:
                final_args = list(pos_args)
                if vararg:
                    va_arg = ast.arg(arg=vararg)
                    va_arg._vela_cls_list = True  # type: ignore[attr-defined]
                    final_args.append(va_arg)
                final_args.extend(a.kwonlyargs)
                if kwarg:
                    kw_arg = ast.arg(arg=kwarg)
                    kw_arg._vela_cls_dict = True  # type: ignore[attr-defined]
                    final_args.append(kw_arg)
                a.posonlyargs = []
                a.args = final_args
                a.vararg = None
                a.kwonlyargs = []
                a.kw_defaults = []
                a.kwarg = None
                a.defaults = []

    class _CallNormalizer(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.AST:
            self.generic_visit(node)
            if not isinstance(node.func, ast.Name):
                return node
            fname = node.func.id
            spec = sigs.get(fname)
            if spec is None:
                return node
            if not spec.needs_rewrite and not node.keywords \
                    and not any(isinstance(a, ast.Starred) for a in node.args):
                return node

            pos_supplied: list[ast.expr] = []
            starred_expr: ast.expr | None = None
            for a in node.args:
                if isinstance(a, ast.Starred):
                    if isinstance(a.value, (ast.List, ast.Tuple)):
                        pos_supplied.extend(a.value.elts)
                    else:
                        starred_expr = a.value
                else:
                    pos_supplied.append(a)

            kw_supplied: dict[str, ast.expr] = {}
            extra_kw_keys: list[ast.expr] = []
            extra_kw_vals: list[ast.expr] = []
            dstar_expr: ast.expr | None = None
            all_named = set(spec.pos_names) | set(spec.kwonly_names)

            for kw in node.keywords:
                if kw.arg is None:
                    if isinstance(kw.value, ast.Dict):
                        for k_node, v_node in zip(kw.value.keys, kw.value.values):
                            if isinstance(k_node, ast.Constant) and isinstance(k_node.value, str) \
                                    and k_node.value in all_named:
                                kw_supplied[k_node.value] = v_node
                            else:
                                extra_kw_keys.append(k_node)  # type: ignore[arg-type]
                                extra_kw_vals.append(v_node)
                    else:
                        dstar_expr = kw.value
                elif kw.arg in all_named:
                    if kw.arg in kw_supplied:
                        raise VelaCompileError(f"repeated keyword argument '{kw.arg}'", node)
                    kw_supplied[kw.arg] = kw.value
                elif spec.kwarg is not None:
                    extra_kw_keys.append(ast.Constant(value=kw.arg))
                    extra_kw_vals.append(kw.value)
                else:
                    raise VelaCompileError(
                        f"{fname}() got an unexpected keyword argument '{kw.arg}'", node
                    )

            final_call_args: list[ast.expr] = []
            n_pos = len(spec.pos_names)
            for i, pname in enumerate(spec.pos_names):
                if i < len(pos_supplied):
                    if pname in kw_supplied:
                        raise VelaCompileError(
                            f"{fname}() got multiple values for argument '{pname}'", node
                        )
                    final_call_args.append(pos_supplied[i])
                elif pname in kw_supplied:
                    final_call_args.append(kw_supplied.pop(pname))
                elif pname in spec.pos_defaults:
                    final_call_args.append(copy.deepcopy(spec.pos_defaults[pname]))
                elif starred_expr is not None:
                    final_call_args.append(
                        ast.Subscript(
                            value=copy.deepcopy(starred_expr),
                            slice=ast.Constant(value=i - len(pos_supplied)),
                            ctx=ast.Load(),
                        )
                    )
                else:
                    # Leave as-is so Analyzer reports standard arity error
                    return node

            if spec.vararg is not None:
                if starred_expr is not None and len(pos_supplied) <= n_pos:
                    rem_start = max(0, n_pos - len(pos_supplied))
                    if rem_start == 0:
                        final_call_args.append(starred_expr)
                    else:
                        final_call_args.append(
                            ast.Subscript(
                                value=starred_expr,
                                slice=ast.Slice(lower=ast.Constant(value=rem_start), upper=None, step=None),
                                ctx=ast.Load(),
                            )
                        )
                else:
                    extra_pos = pos_supplied[n_pos:]
                    final_call_args.append(ast.List(elts=extra_pos, ctx=ast.Load()))
            elif len(pos_supplied) > n_pos:
                return node

            for kname in spec.kwonly_names:
                if kname in kw_supplied:
                    final_call_args.append(kw_supplied.pop(kname))
                elif kname in spec.kwonly_defaults:
                    final_call_args.append(copy.deepcopy(spec.kwonly_defaults[kname]))
                else:
                    raise VelaCompileError(
                        f"{fname}() missing required keyword-only argument '{kname}'", node
                    )

            if spec.kwarg is not None:
                if dstar_expr is not None and not extra_kw_keys:
                    final_call_args.append(dstar_expr)
                else:
                    final_call_args.append(ast.Dict(keys=extra_kw_keys, values=extra_kw_vals))

            node.args = final_call_args
            node.keywords = []
            return node

    _CallNormalizer().visit(tree)


# ---------------------------------------------------------------------------
# 7. Modern Syntax (Python 3.10-3.15), @dataclass, eval/exec/compile, unittest
# ---------------------------------------------------------------------------

def _collect_class_pos_fields(tree: ast.Module) -> dict[str, list[str]]:
    fields_map: dict[str, list[str]] = {}
    for s in tree.body:
        if isinstance(s, ast.ClassDef):
            fnames: list[str] = []
            for item in s.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    if item.target.id not in fnames:
                        fnames.append(item.target.id)
                elif isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    for a in item.args.args[1:]:
                        if a.arg not in fnames:
                            fnames.append(a.arg)
            fields_map[s.name] = fnames
    return fields_map


class _ModernSyntaxPreDesugarer(ast.NodeTransformer):
    def __init__(self, known_classes: set[str], counter: _Counter) -> None:
        self.known_classes = known_classes
        self.counter = counter
        self.const_strs: dict[str, str] = {}
        self.generic_funcs: dict[str, ast.FunctionDef] = {}
        self.specialized_funcs: list[ast.FunctionDef] = []

    def _clean_ann(self, ann: ast.AST | None) -> ast.expr | None:
        if ann is None:
            return None
        if isinstance(ann, ast.Name) and (
            ann.id in ("int", "float", "bool", "str", "None")
            or ann.id in self.known_classes
        ):
            return ann
        if isinstance(ann, ast.Constant) and ann.value is None:
            return ann
        return None

    def visit_TemplateStr(self, node: ast.AST) -> ast.AST:
        # PEP 750 t-string (Python 3.14/3.15)
        self.generic_visit(node)
        parts: list[ast.expr] = []
        for v in getattr(node, "values", []):
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                if v.value:
                    parts.append(v)
            else:
                inner = getattr(v, "value", v)
                parts.append(
                    ast.Call(
                        func=ast.Name(id="str", ctx=ast.Load()),
                        args=[inner],
                        keywords=[],
                    )
                )
        if not parts:
            return ast.Constant(value="")
        acc = parts[0]
        for p in parts[1:]:
            acc = ast.BinOp(left=acc, op=ast.Add(), right=p)
        return acc

    def visit_TypeAlias(self, node: ast.AST) -> ast.AST:
        return ast.Pass()

    def visit_Delete(self, node: ast.Delete) -> ast.AST:
        for t in node.targets:
            if isinstance(t, ast.Subscript):
                return ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(value=t.value, attr="pop", ctx=ast.Load()),
                        args=[t.slice],
                        keywords=[],
                    )
                )
            if isinstance(t, ast.Name):
                return ast.Assign(
                    targets=[ast.Name(id=t.id, ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Name(id="__del_var", ctx=ast.Load()),
                        args=[ast.Name(id=t.id, ctx=ast.Load())],
                        keywords=[],
                    ),
                )
        return ast.Pass()

    def visit_Await(self, node: ast.Await) -> ast.AST:
        return self.visit(node.value)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AST:
        f = ast.For(
            target=self.visit(node.target),
            iter=self.visit(node.iter),
            body=[self.visit(s) for s in node.body],
            orelse=[self.visit(s) for s in node.orelse],
        )
        return ast.copy_location(f, node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AST:
        w = ast.With(
            items=[
                ast.withitem(
                    context_expr=self.visit(it.context_expr),
                    optional_vars=self.visit(it.optional_vars) if it.optional_vars else None,
                )
                for it in node.items
            ],
            body=[self.visit(s) for s in node.body],
        )
        return ast.copy_location(w, node)

    def visit_TryStar(self, node: ast.AST) -> ast.AST:
        t = ast.Try(
            body=[self.visit(s) for s in getattr(node, "body", [])],
            handlers=[self.visit(h) for h in getattr(node, "handlers", [])],
            orelse=[self.visit(s) for s in getattr(node, "orelse", [])],
            finalbody=[self.visit(s) for s in getattr(node, "finalbody", [])],
        )
        t._vela_trystar = True  # type: ignore[attr-defined]
        return ast.copy_location(t, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        fn = ast.FunctionDef(
            name=node.name,
            args=node.args,
            body=node.body,
            decorator_list=node.decorator_list,
            returns=node.returns,
        )
        return self.visit_FunctionDef(ast.copy_location(fn, node))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        was_generic = bool(getattr(node, "type_params", None))
        if hasattr(node, "type_params"):
            node.type_params = []  # type: ignore[attr-defined]
        for a in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            a.annotation = self._clean_ann(a.annotation)
        if node.args.vararg:
            node.args.vararg.annotation = None
        if node.args.kwarg:
            node.args.kwarg.annotation = None
        node.returns = self._clean_ann(node.returns)
        if was_generic:
            self.generic_funcs[node.name] = copy.deepcopy(node)
        # Filter out @lru_cache / @cache decorators
        cleaned_decs: list[ast.expr] = []
        for d in node.decorator_list:
            dname = ""
            if isinstance(d, ast.Name):
                dname = d.id
            elif isinstance(d, ast.Attribute):
                dname = d.attr
            elif isinstance(d, ast.Call):
                if isinstance(d.func, ast.Name):
                    dname = d.func.id
                elif isinstance(d.func, ast.Attribute):
                    dname = d.func.attr
            if dname in ("lru_cache", "cache", "functools__lru_cache", "functools__cache"):
                continue
            cleaned_decs.append(self.visit(d))
        node.decorator_list = cleaned_decs
        new_body: list[ast.stmt] = []
        for s in node.body:
            v = self.visit(s)
            if isinstance(v, list):
                new_body.extend(v)
            elif v is not None:
                new_body.append(v)
        node.body = new_body
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        if hasattr(node, "type_params"):
            node.type_params = []  # type: ignore[attr-defined]
        # Strip Generic[T] from bases
        node.bases = [
            b for b in node.bases
            if not (isinstance(b, ast.Subscript) or (isinstance(b, ast.Name) and b.id in ("Generic", "typing__Generic")))
        ]
        is_dc = False
        kept_decs: list[ast.expr] = []
        for d in node.decorator_list:
            dname = ""
            if isinstance(d, ast.Name):
                dname = d.id
            elif isinstance(d, ast.Attribute):
                dname = d.attr
            elif isinstance(d, ast.Call):
                if isinstance(d.func, ast.Name):
                    dname = d.func.id
                elif isinstance(d.func, ast.Attribute):
                    dname = d.func.attr
            if dname in ("dataclass", "dataclasses__dataclass"):
                is_dc = True
            else:
                kept_decs.append(self.visit(d))
        node.decorator_list = kept_decs

        dc_fields: list[tuple[str, ast.expr | None, ast.expr | None]] = []
        new_body: list[ast.stmt] = []
        existing_methods: set[str] = set()
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == "__aenter__":
                    item.name = "__enter__"
                elif item.name == "__aexit__":
                    item.name = "__exit__"
                elif item.name == "__aiter__":
                    item.name = "__iter__"
                elif item.name == "__anext__":
                    item.name = "__next__"
                existing_methods.add(item.name)
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                dc_fields.append((item.target.id, self._clean_ann(item.annotation), item.value))
                if item.value is not None and not is_dc:
                    new_body.append(
                        ast.Assign(targets=[item.target], value=self.visit(item.value))
                    )
                continue
            visited = self.visit(item)
            if isinstance(visited, list):
                new_body.extend(visited)
            elif visited is not None:
                new_body.append(visited)

        if is_dc and dc_fields:
            if "__init__" not in existing_methods:
                init_args = [ast.arg(arg="self")]
                defaults: list[ast.expr] = []
                init_stmts: list[ast.stmt] = []
                for fname, fann, fdef in dc_fields:
                    init_args.append(ast.arg(arg=fname, annotation=fann))
                    if fdef is not None:
                        defaults.append(copy.deepcopy(fdef))
                    init_stmts.append(
                        ast.Assign(
                            targets=[
                                ast.Attribute(
                                    value=ast.Name(id="self", ctx=ast.Load()),
                                    attr=fname,
                                    ctx=ast.Store(),
                                )
                            ],
                            value=ast.Name(id=fname, ctx=ast.Load()),
                        )
                    )
                new_body.insert(
                    0,
                    ast.FunctionDef(
                        name="__init__",
                        args=ast.arguments(
                            posonlyargs=[],
                            args=init_args,
                            kwonlyargs=[],
                            kw_defaults=[],
                            defaults=defaults,
                        ),
                        body=init_stmts,
                        decorator_list=[],
                    ),
                )
            if "__eq__" not in existing_methods:
                cmps: list[ast.expr] = [
                    ast.Compare(
                        left=ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()), attr=fn, ctx=ast.Load()),
                        ops=[ast.Eq()],
                        comparators=[
                            ast.Attribute(value=ast.Name(id="other", ctx=ast.Load()), attr=fn, ctx=ast.Load())
                        ],
                    )
                    for fn, _, _ in dc_fields
                ]
                eq_expr = cmps[0] if len(cmps) == 1 else ast.BoolOp(op=ast.And(), values=cmps)
                new_body.append(
                    ast.FunctionDef(
                        name="__eq__",
                        args=ast.arguments(
                            posonlyargs=[],
                            args=[ast.arg(arg="self"), ast.arg(arg="other")],
                            kwonlyargs=[],
                            kw_defaults=[],
                            defaults=[],
                        ),
                        body=[ast.Return(value=eq_expr)],
                        decorator_list=[],
                    )
                )
        if not new_body:
            new_body.append(ast.Pass())
        node.body = new_body
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        node.value = self.visit(node.value)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                self.const_strs[node.targets[0].id] = node.value.value
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) \
                and node.value.func.id == "exec" and len(node.value.args) >= 1:
            arg0 = self.visit(node.value.args[0])
            code_str: str | None = None
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                code_str = arg0.value
            elif isinstance(arg0, ast.Name) and arg0.id in self.const_strs:
                code_str = self.const_strs[arg0.id]
            if code_str is not None:
                parsed = ast.parse(code_str, mode="exec")
                return [self.visit(s) for s in parsed.body]  # type: ignore[return-value]
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "cast" \
                and len(node.args) == 2:
            return node.args[1]
        if isinstance(node.func, ast.Name):
            if node.func.id in ("cast", "typing__cast") and len(node.args) == 2:
                return node.args[1]
            if node.func.id == "weakref__ref" and len(node.args) == 1:
                node.args[0] = ast.Call(
                    func=ast.Name(id="__ptr_to_int", ctx=ast.Load()),
                    args=[node.args[0]],
                    keywords=[],
                )
                return node
            if node.func.id == "weakref__finalize" and len(node.args) == 2:
                node.func = ast.Name(id="__set_finalizer", ctx=ast.Load())
                return node
            if node.func.id in self.generic_funcs:
                tmpl = copy.deepcopy(self.generic_funcs[node.func.id])
                spec_name = self.counter.next(f"__gspec_{node.func.id}")
                tmpl.name = spec_name
                self.specialized_funcs.append(tmpl)
                node.func = ast.Name(id=spec_name, ctx=ast.Load())
                return node
            if node.func.id == "compile" and len(node.args) >= 1:
                return node.args[0]
            if node.func.id == "eval" and len(node.args) >= 1:
                arg0 = node.args[0]
                code_str: str | None = None
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    code_str = arg0.value
                elif isinstance(arg0, ast.Name) and arg0.id in self.const_strs:
                    code_str = self.const_strs[arg0.id]
                if code_str is not None:
                    parsed = ast.parse(code_str, mode="eval")
                    return self.visit(parsed.body)
        return node


def _expand_unittest_main(tree: ast.Module) -> None:
    test_classes: list[tuple[str, list[str], bool, bool]] = []
    for s in tree.body:
        if isinstance(s, ast.ClassDef):
            is_tc = any(
                (isinstance(b, ast.Name) and b.id in ("TestCase", "unittest__TestCase"))
                or (isinstance(b, ast.Attribute) and b.attr == "TestCase")
                for b in s.bases
            )
            if is_tc:
                mnames = [
                    m.name for m in s.body
                    if isinstance(m, ast.FunctionDef) and m.name.startswith("test_")
                ]
                has_setup = any(
                    isinstance(m, ast.FunctionDef) and m.name == "setUp" for m in s.body
                )
                has_teardown = any(
                    isinstance(m, ast.FunctionDef) and m.name == "tearDown" for m in s.body
                )
                test_classes.append((s.name, mnames, has_setup, has_teardown))
    if not test_classes:
        return

    def is_ut_main_call(stmt: ast.stmt) -> bool:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            return False
        f = stmt.value.func
        if isinstance(f, ast.Attribute) and f.attr == "main":
            return True
        if isinstance(f, ast.Name) and f.id in ("unittest__main", "main"):
            return True
        return False

    runner_stmts: list[ast.stmt] = []
    total_tests = 0
    for cname, mnames, has_setup, has_teardown in test_classes:
        inst_var = f"__ut_{cname}"
        runner_stmts.append(
            ast.Assign(
                targets=[ast.Name(id=inst_var, ctx=ast.Store())],
                value=ast.Call(func=ast.Name(id=cname, ctx=ast.Load()), args=[], keywords=[]),
            )
        )
        for m in mnames:
            total_tests += 1
            if has_setup:
                runner_stmts.append(
                    ast.Expr(
                        value=ast.Call(
                            func=ast.Attribute(
                                value=ast.Name(id=inst_var, ctx=ast.Load()),
                                attr="setUp",
                                ctx=ast.Load(),
                            ),
                            args=[],
                            keywords=[],
                        )
                    )
                )
            runner_stmts.append(
                ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(id=inst_var, ctx=ast.Load()),
                            attr=m,
                            ctx=ast.Load(),
                        ),
                        args=[],
                        keywords=[],
                    )
                )
            )
            if has_teardown:
                runner_stmts.append(
                    ast.Expr(
                        value=ast.Call(
                            func=ast.Attribute(
                                value=ast.Name(id=inst_var, ctx=ast.Load()),
                                attr="tearDown",
                                ctx=ast.Load(),
                            ),
                            args=[],
                            keywords=[],
                        )
                    )
                )
    runner_stmts.append(
        ast.Expr(
            value=ast.Call(
                func=ast.Name(id="print", ctx=ast.Load()),
                args=[ast.Constant(value=f"OK ({total_tests} tests)")],
                keywords=[],
            )
        )
    )

    new_body: list[ast.stmt] = []
    for s in tree.body:
        if is_ut_main_call(s):
            new_body.extend(runner_stmts)
        elif isinstance(s, ast.If) and any(is_ut_main_call(b) for b in s.body):
            new_body.extend(runner_stmts)
        else:
            new_body.append(s)
    tree.body = new_body


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def desugar_module(tree: ast.Module) -> ast.Module:
    counter = _Counter()
    known_cls = {s.name for s in tree.body if isinstance(s, ast.ClassDef)}
    pre = _ModernSyntaxPreDesugarer(known_cls, counter)
    flat_body: list[ast.stmt] = []
    for s in tree.body:
        v = pre.visit(s)
        if isinstance(v, list):
            flat_body.extend(v)
        elif v is not None:
            flat_body.append(v)
    tree.body = list(pre.specialized_funcs) + flat_body
    _expand_unittest_main(tree)
    _FStringDesugarer().visit(tree)
    _LambdaDesugarer(counter).visit(tree)
    _desugar_generators(tree, counter)
    cfields = _collect_class_pos_fields(tree)
    tree.body = _desugar_stmt_list(tree.body, counter, cfields)
    _desugar_classes(tree, counter)
    _desugar_decorators(tree, counter)
    tree.body = _desugar_stmt_list(tree.body, counter, cfields)
    _desugar_signatures_and_calls(tree)
    ast.fix_missing_locations(tree)
    return tree
