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


def _module_path(name: str, filename: str) -> Path:
    base = Path(filename).resolve().parent
    for suffix in (".vl", ".py"):
        p = base / (name.replace(".", "/") + suffix)
        if p.is_file():
            return p
    raise UnsupportedError(f"cannot find pure Python module '{name}'")


def _rewrite_module(tree: ast.Module, prefix: str) -> list[ast.stmt]:
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.Import)):
            raise UnsupportedError(
                f"module '{prefix}' contains code other than function definitions", n)
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
    return funcs


def expand_imports(tree: ast.Module, filename: str) -> ast.Module:
    body: list[ast.stmt] = []
    modules: dict[str, set[str]] = {}
    imported: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    raise UnsupportedError("import ... as ... is not supported", node)
                path = _module_path(alias.name, filename)
                src = path.read_text(encoding="utf-8-sig")
                sub = ast.parse(src, str(path))
                funcs = _rewrite_module(sub, alias.name)
                body.extend(funcs)
                modules[alias.name] = {f.name.split("__", 1)[1] for f in funcs}
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level:
                raise UnsupportedError("relative imports are not supported", node)
            if node.module is None:
                raise UnsupportedError("from . import ... is not supported", node)
            path = _module_path(node.module, filename)
            src = path.read_text(encoding="utf-8-sig")
            sub = ast.parse(src, str(path))
            funcs = _rewrite_module(sub, node.module)
            body.extend(funcs)
            available = {f.name.split("__", 1)[1] for f in funcs}
            for alias in node.names:
                if alias.asname:
                    raise UnsupportedError("from ... import ... as ... is not supported", node)
                if alias.name not in available:
                    raise UnsupportedError(
                        f"module '{node.module}' has no function '{alias.name}'", node)
                imported[alias.name] = f"{node.module}__{alias.name}"
            continue
        body.append(node)

    class MainRewriter(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.AST:
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id in modules and f.attr in modules[f.value.id]:
                node.func = ast.Name(id=f"{f.value.id}__{f.attr}", ctx=ast.Load())
            elif isinstance(f, ast.Name) and f.id in imported:
                node.func = ast.Name(id=imported[f.id], ctx=ast.Load())
            self.generic_visit(node)
            return node

    tree.body = body
    MainRewriter().visit(tree)
    return tree
