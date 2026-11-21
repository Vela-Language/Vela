"""Compile-time expansion of simple pure-Python imports.

Only top-level ``import module`` is supported in boxed mode.  The imported
file's ``def`` statements are prefixed with ``module__`` and merged into the
importing module.  Module-level assignments/classes and dynamic imports are
rejected explicitly.
"""
from __future__ import annotations

import ast
from pathlib import Path

from .errors import UnsupportedError


def _module_path(name: str, filename: str, base: Path | None = None) -> Path:
    base = base or Path(filename).resolve().parent
    for suffix in (".vl", ".py"):
        p = base / (name.replace(".", "/") + suffix)
        if p.is_file():
            return p
    raise UnsupportedError(f"cannot find pure Python module '{name}'")


def _rewrite_module(tree: ast.Module, prefix: str) -> tuple[list[ast.stmt], dict[str, object]]:
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    consts: dict[str, object] = {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) or isinstance(n, ast.Import):
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant):
            consts[n.targets[0].id] = n.value.value
            continue
        raise UnsupportedError(
            f"module '{prefix}' contains code other than functions/constants", n)
    names = {f.name for f in funcs}

    class Rewriter(ast.NodeTransformer):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            node.name = f"{prefix}__{node.name}"
            self.generic_visit(node)
            return node

        def visit_Call(self, node: ast.Call) -> ast.AST:
            if isinstance(node.func, ast.Name) and node.func.id in names:
                node.func.id = f"{prefix}__{node.func.id}"
            self.generic_visit(node)
            return node

    for f in funcs:
        Rewriter().visit(f)
    return funcs, consts


def expand_imports(tree: ast.Module, filename: str) -> ast.Module:
    body: list[ast.stmt] = []
    modules: dict[str, tuple[set[str], dict[str, object]]] = {}
    imported: dict[str, str] = {}
    imported_consts: dict[str, object] = {}
    module_names: set[str] = set()
    module_index: dict[str, dict[str, tuple[int, str]]] = {}
    has_sys = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    raise UnsupportedError("import ... as ... is not supported", node)
                if alias.name == "sys":
                    has_sys = True
                    continue
                module_names.add(alias.name)
                path = _module_path(alias.name, filename)
                src = path.read_text(encoding="utf-8-sig")
                sub = ast.parse(src, str(path))
                qual = alias.name.replace(".", "__")
                funcs, consts = _rewrite_module(sub, qual)
                body.extend(funcs)
                modules[alias.name] = ({f.name.rsplit("__", 1)[1] for f in funcs}, consts)
                values: list[tuple[str, object, str]] = []
                for cname, cval in consts.items():
                    if isinstance(cval, bool):
                        cty = "bool"
                    elif isinstance(cval, int):
                        cty = "int" if -(2 ** 63) <= cval < 2 ** 63 else "bigint"
                    elif isinstance(cval, str):
                        cty = "str"
                    elif isinstance(cval, float):
                        cty = "float"
                    else:
                        continue
                    values.append((cname, cval, cty))
                if values:
                    module_index[alias.name] = {
                        n: (i, t) for i, (n, _, t) in enumerate(values)}
                    body.append(ast.Assign(
                        targets=[ast.Name(id=alias.name, ctx=ast.Store())],
                        value=ast.Call(
                            func=ast.Name(id="__module_new", ctx=ast.Load()),
                            args=[ast.Constant(value=v) for _, v, _ in values],
                            keywords=[])))
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                raise UnsupportedError("from . import ... is not supported", node)
            base = Path(filename).resolve().parent
            for _ in range(max(node.level - 1, 0)):
                base = base.parent
            path = _module_path(node.module, filename, base)
            src = path.read_text(encoding="utf-8-sig")
            sub = ast.parse(src, str(path))
            qual = node.module.replace(".", "__")
            funcs, consts = _rewrite_module(sub, qual)
            body.extend(funcs)
            available = {f.name.rsplit("__", 1)[1] for f in funcs}
            for cname, cval in consts.items():
                imported_consts[cname] = cval
            module_names.add(node.module)
            for alias in node.names:
                if alias.asname:
                    raise UnsupportedError("from ... import ... as ... is not supported", node)
                if alias.name not in available:
                    raise UnsupportedError(
                        f"module '{node.module}' has no function '{alias.name}'", node)
                imported[alias.name] = f"{qual}__{alias.name}"
            continue
        body.append(node)

    class MainRewriter(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:
            if isinstance(node.ctx, ast.Load) and node.id in imported_consts:
                return ast.copy_location(ast.Constant(value=imported_consts[node.id]), node)
            return node

        def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
            v = node.value
            if has_sys and isinstance(v, ast.Attribute) \
                    and isinstance(v.value, ast.Name) and v.value.id == "sys" \
                    and v.attr == "modules" \
                    and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str) \
                    and node.slice.value in module_index:
                return ast.copy_location(
                    ast.Name(id=node.slice.value, ctx=ast.Load()), node)
            return self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            v = node.value
            if isinstance(node.ctx, ast.Load) and isinstance(v, ast.Subscript) \
                    and isinstance(v.value, ast.Attribute) \
                    and isinstance(v.value.value, ast.Name) \
                    and v.value.value.id == "sys" and v.value.attr == "modules" \
                    and isinstance(v.slice, ast.Constant) \
                    and isinstance(v.slice.value, str) and v.slice.value in module_index \
                    and node.attr in module_index[v.slice.value]:
                idx, ty = module_index[v.slice.value][node.attr]
                call = ast.Call(
                    func=ast.Name(id="__module_get", ctx=ast.Load()),
                    args=[ast.Name(id=v.slice.value, ctx=ast.Load()),
                          ast.Constant(value=idx)],
                    keywords=[])
                call._vela_mod_ty = ty
                return ast.copy_location(call, node)
            if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name) \
                    and node.value.id == "sys" and node.attr == "modules" and has_sys:
                names = sorted(module_names | {"sys"})
                return ast.copy_location(ast.Dict(
                    keys=[ast.Constant(value=n) for n in names],
                    values=[ast.Constant(value=1) for _ in names]), node)
            if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name) \
                    and node.value.id in modules:
                funcs, consts = modules[node.value.id]
                if node.value.id in module_index and node.attr in module_index[node.value.id]:
                    idx, ty = module_index[node.value.id][node.attr]
                    call = ast.Call(
                        func=ast.Name(id="__module_get", ctx=ast.Load()),
                        args=[ast.Name(id=node.value.id, ctx=ast.Load()),
                              ast.Constant(value=idx)],
                        keywords=[])
                    call._vela_mod_ty = ty
                    return ast.copy_location(call, node)
                if node.attr in consts:
                    return ast.copy_location(ast.Constant(value=consts[node.attr]), node)
            return self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> ast.AST:
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in modules:
                funcs, consts = modules[f.value.id]
                if f.attr in funcs:
                    node.func = ast.Name(id=f"{f.value.id}__{f.attr}", ctx=ast.Load())
                elif f.attr in consts:
                    return ast.copy_location(ast.Constant(value=consts[f.attr]), node)
            elif isinstance(f, ast.Name) and f.id in imported:
                node.func = ast.Name(id=imported[f.id], ctx=ast.Load())
            self.generic_visit(node)
            return node

    tree.body = body
    MainRewriter().visit(tree)
    return tree
