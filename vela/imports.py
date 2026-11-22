"""Compile-time expansion of pure-Python modules, package directories, and
Vela's built-in A3 standard library modules.
"""
from __future__ import annotations

import ast
from pathlib import Path

from .errors import UnsupportedError
from .stdlib import STDLIB_SOURCES


def _module_source(name: str, filename: str, base: Path | None = None) -> tuple[str, str]:
    base = base or Path(filename).resolve().parent
    rel = name.replace(".", "/")
    for suffix in (".vl", ".py"):
        p = base / (rel + suffix)
        if p.is_file():
            return p.read_text(encoding="utf-8-sig"), str(p)
        init_p = base / rel / ("__init__" + suffix)
        if init_p.is_file():
            return init_p.read_text(encoding="utf-8-sig"), str(init_p)
    if name in STDLIB_SOURCES:
        return STDLIB_SOURCES[name], f"<stdlib:{name}>"
    raise UnsupportedError(f"ImportError: cannot find pure Python module '{name}'")


def _rewrite_module(
    tree: ast.Module, prefix: str
) -> tuple[list[ast.stmt], dict[str, object], set[str], set[str]]:
    funcs: list[ast.stmt] = []
    classes: list[ast.ClassDef] = []
    global_assigns: list[ast.Assign] = []
    consts: dict[str, object] = {}
    mod_globals: set[str] = set()

    for n in tree.body:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Global):
                mod_globals.update(sub.names)

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(n)
            continue
        if isinstance(n, ast.ClassDef):
            classes.append(n)
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant):
            tname = n.targets[0].id
            if tname in mod_globals:
                n.targets[0].id = f"{prefix}__{tname}"
                global_assigns.append(n)
            else:
                consts[tname] = n.value.value
            continue
        raise UnsupportedError(
            f"module '{prefix}' contains code other than functions/classes/constants", n)

    func_names = {f.name for f in funcs}  # type: ignore[attr-defined]
    class_names = {c.name for c in classes}
    all_qual_names = func_names | class_names | mod_globals

    class Rewriter(ast.NodeTransformer):
        def __init__(self, top: bool = True) -> None:
            self.top = top

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            if self.top and node.name in func_names:
                node.name = f"{prefix}__{node.name}"
            inner = Rewriter(top=False)
            node.body = [inner.visit(s) for s in node.body]
            node.decorator_list = [inner.visit(d) for d in node.decorator_list]
            node.args.defaults = [inner.visit(d) for d in node.args.defaults]
            return node

        def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
            if self.top and node.name in class_names:
                node.name = f"{prefix}__{node.name}"
            inner = Rewriter(top=False)
            node.bases = [inner.visit(b) for b in node.bases]
            node.body = [inner.visit(s) for s in node.body]
            node.decorator_list = [inner.visit(d) for d in node.decorator_list]
            return node

        def visit_Global(self, node: ast.Global) -> ast.AST:
            node.names = [
                f"{prefix}__{g}" if g in mod_globals else g for g in node.names
            ]
            return node

        def visit_Name(self, node: ast.Name) -> ast.AST:
            if node.id in all_qual_names:
                node.id = f"{prefix}__{node.id}"
            elif isinstance(node.ctx, ast.Load) and node.id in consts:
                return ast.copy_location(ast.Constant(value=consts[node.id]), node)
            return node

        def visit_Call(self, node: ast.Call) -> ast.AST:
            if isinstance(node.func, ast.Name) and node.func.id in (func_names | class_names):
                node.func.id = f"{prefix}__{node.func.id}"
            self.generic_visit(node)
            return node

    rw = Rewriter(top=True)
    for c in classes:
        rw.visit(c)
    for f in funcs:
        rw.visit(f)
    return global_assigns + classes + funcs, consts, func_names, class_names


def expand_imports(tree: ast.Module, filename: str) -> ast.Module:
    body: list[ast.stmt] = []
    modules: dict[str, tuple[set[str], dict[str, object], set[str]]] = {}
    mod_aliases: dict[str, str] = {}
    imported: dict[str, str] = {}
    imported_consts: dict[str, object] = {}
    module_names: set[str] = set()
    module_index: dict[str, dict[str, tuple[int, str]]] = {}
    has_sys = False
    loaded_quals: set[str] = set()

    def load_one_module(mod_name: str, base: Path | None = None) -> None:
        if mod_name in modules:
            return
        module_names.add(mod_name)
        src, src_path = _module_source(mod_name, filename, base)
        sub = ast.parse(src, src_path)
        qual = mod_name.replace(".", "__")
        stmts, consts, fnames, cnames = _rewrite_module(sub, qual)
        if qual not in loaded_quals:
            loaded_quals.add(qual)
            body.extend(stmts)
        modules[mod_name] = (fnames, consts, cnames)
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
            module_index[mod_name] = {
                n: (i, t) for i, (n, _, t) in enumerate(values)}
            if "." not in mod_name:
                body.insert(0, ast.Assign(
                    targets=[ast.Name(id=mod_name, ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Name(id="__module_new", ctx=ast.Load()),
                        args=[ast.Constant(value=v) for _, v, _ in values],
                        keywords=[])))

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bind_name = alias.asname or alias.name
                if alias.name == "sys":
                    has_sys = True
                    mod_aliases[bind_name] = "sys"
                    continue
                load_one_module(alias.name)
                mod_aliases[bind_name] = alias.name
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                raise UnsupportedError("from . import ... is not supported", node)
            base = Path(filename).resolve().parent
            for _ in range(max(node.level - 1, 0)):
                base = base.parent
            if node.module == "sys":
                has_sys = True
                for alias in node.names:
                    bname = alias.asname or alias.name
                    imported[bname] = f"__sys_{alias.name}"
                continue
            load_one_module(node.module, base)
            qual = node.module.replace(".", "__")
            fnames, consts, cnames = modules[node.module]
            available = fnames | cnames
            for alias in node.names:
                bname = alias.asname or alias.name
                if alias.name in consts:
                    imported_consts[bname] = consts[alias.name]
                    continue
                if alias.name not in available:
                    raise UnsupportedError(
                        f"module '{node.module}' has no function '{alias.name}'", node)
                imported[bname] = f"{qual}__{alias.name}"
            continue
        body.append(node)

    # Also auto-load any module referenced in __import__("m") or importlib.import_module("m")
    for sub in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(sub, ast.Call) and len(sub.args) == 1 \
                and isinstance(sub.args[0], ast.Constant) \
                and isinstance(sub.args[0].value, str):
            m_str = sub.args[0].value
            if isinstance(sub.func, ast.Name) and sub.func.id in ("__import__", "importlib__import_module"):
                if m_str != "sys" and m_str not in modules:
                    load_one_module(m_str)
            elif isinstance(sub.func, ast.Attribute) and sub.func.attr == "import_module":
                if m_str != "sys" and m_str not in modules:
                    load_one_module(m_str)

    class MainRewriter(ast.NodeTransformer):
        def __init__(self) -> None:
            self.dyn_mod_vars: dict[str, str] = {}

        def _resolve_mod(self, name: str) -> str | None:
            if name in self.dyn_mod_vars:
                return self.dyn_mod_vars[name]
            if name in mod_aliases:
                return mod_aliases[name]
            if name in modules:
                return name
            return None

        def visit_Assign(self, node: ast.Assign) -> ast.AST:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Call) and len(node.value.args) == 1 \
                    and isinstance(node.value.args[0], ast.Constant) \
                    and isinstance(node.value.args[0].value, str):
                m_target = None
                f = node.value.func
                if isinstance(f, ast.Name) and f.id in ("__import__", "importlib__import_module"):
                    m_target = node.value.args[0].value
                elif isinstance(f, ast.Attribute) and f.attr == "import_module":
                    m_target = node.value.args[0].value
                if m_target:
                    self.dyn_mod_vars[node.targets[0].id] = m_target
                    node.value = ast.Constant(value=0)
                    return node
            return self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
            new_bases: list[ast.expr] = []
            for b in node.bases:
                if isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
                    m = self._resolve_mod(b.value.id)
                    if m and m in modules and b.attr in modules[m][2]:
                        new_bases.append(
                            ast.copy_location(
                                ast.Name(id=f"{m.replace('.', '__')}__{b.attr}", ctx=ast.Load()),
                                b,
                            )
                        )
                        continue
                elif isinstance(b, ast.Name) and b.id in imported:
                    new_bases.append(
                        ast.copy_location(ast.Name(id=imported[b.id], ctx=ast.Load()), b)
                    )
                    continue
                new_bases.append(self.visit(b))
            node.bases = new_bases
            node.decorator_list = [self.visit(d) for d in node.decorator_list]
            node.body = [self.visit(s) for s in node.body]
            return node

        def visit_Name(self, node: ast.Name) -> ast.AST:
            if isinstance(node.ctx, ast.Load) and node.id in imported_consts:
                return ast.copy_location(ast.Constant(value=imported_consts[node.id]), node)
            if isinstance(node.ctx, ast.Load) and node.id in imported:
                return ast.copy_location(ast.Name(id=imported[node.id], ctx=ast.Load()), node)
            return node

        def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
            v = node.value
            if has_sys and isinstance(v, ast.Attribute) \
                    and isinstance(v.value, ast.Name) and self._resolve_mod(v.value.id) == "sys" \
                    and v.attr == "modules" \
                    and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str) \
                    and node.slice.value in module_index:
                return ast.copy_location(
                    ast.Name(id=node.slice.value, ctx=ast.Load()), node)
            return self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            v = node.value
            if isinstance(node.ctx, ast.Load) and isinstance(v, ast.Call) \
                    and len(v.args) == 1 and isinstance(v.args[0], ast.Constant) \
                    and isinstance(v.args[0].value, str) and v.args[0].value in modules:
                m_target = None
                if isinstance(v.func, ast.Name) and v.func.id in ("__import__", "importlib__import_module"):
                    m_target = v.args[0].value
                elif isinstance(v.func, ast.Attribute) and v.func.attr == "import_module":
                    m_target = v.args[0].value
                if m_target:
                    funcs, consts, cnames = modules[m_target]
                    qual = m_target.replace(".", "__") + "__" + node.attr
                    if node.attr in funcs:
                        call = ast.Call(
                            func=ast.Name(id="__module_func", ctx=ast.Load()),
                            args=[ast.Constant(value=qual)], keywords=[])
                        call._vela_func = qual  # type: ignore[attr-defined]
                        return ast.copy_location(call, node)
                    if node.attr in consts:
                        return ast.copy_location(ast.Constant(value=consts[node.attr]), node)
            if isinstance(node.ctx, ast.Load) and isinstance(v, ast.Subscript) \
                    and isinstance(v.value, ast.Attribute) \
                    and isinstance(v.value.value, ast.Name) \
                    and self._resolve_mod(v.value.value.id) == "sys" and v.value.attr == "modules" \
                    and isinstance(v.slice, ast.Constant) \
                    and isinstance(v.slice.value, str) and v.slice.value in modules \
                    and node.attr in modules[v.slice.value][0]:
                qual = v.slice.value.replace(".", "__") + "__" + node.attr
                call = ast.Call(
                    func=ast.Name(id="__module_func", ctx=ast.Load()),
                    args=[ast.Constant(value=qual)], keywords=[])
                call._vela_func = qual  # type: ignore[attr-defined]
                return ast.copy_location(call, node)
            if isinstance(node.ctx, ast.Load) and isinstance(v, ast.Subscript) \
                    and isinstance(v.value, ast.Attribute) \
                    and isinstance(v.value.value, ast.Name) \
                    and self._resolve_mod(v.value.value.id) == "sys" and v.value.attr == "modules" \
                    and isinstance(v.slice, ast.Constant) \
                    and isinstance(v.slice.value, str) and v.slice.value in module_index \
                    and node.attr in module_index[v.slice.value]:
                idx, ty = module_index[v.slice.value][node.attr]
                call = ast.Call(
                    func=ast.Name(id="__module_get", ctx=ast.Load()),
                    args=[ast.Name(id=v.slice.value, ctx=ast.Load()),
                          ast.Constant(value=idx)],
                    keywords=[])
                call._vela_mod_ty = ty  # type: ignore[attr-defined]
                return ast.copy_location(call, node)
            if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name) \
                    and self._resolve_mod(node.value.id) == "sys" and has_sys:
                if node.attr == "modules":
                    names = sorted(module_names | {"sys"})
                    args: list[ast.expr] = []
                    for n in names:
                        args.append(ast.Constant(value=n))
                        if n in module_index and "." not in n:
                            args.append(ast.Name(id=n, ctx=ast.Load()))
                        else:
                            args.append(ast.Constant(value=None))
                    return ast.copy_location(ast.Call(
                        func=ast.Name(id="__modules_registry", ctx=ast.Load()),
                        args=args, keywords=[]), node)
                if node.attr == "platform":
                    return ast.copy_location(ast.Constant(value="win32"), node)
                if node.attr == "version_info":
                    return ast.copy_location(
                        ast.Tuple(
                            elts=[ast.Constant(value=3), ast.Constant(value=15), ast.Constant(value=2)],
                            ctx=ast.Load(),
                        ),
                        node,
                    )
            if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
                mod_name = self._resolve_mod(node.value.id)
                if mod_name and mod_name in modules:
                    funcs, consts, cnames = modules[mod_name]
                    if node.attr in cnames:
                        return ast.copy_location(
                            ast.Name(id=f"{mod_name.replace('.', '__')}__{node.attr}", ctx=ast.Load()),
                            node,
                        )
                    if node.attr in funcs:
                        qual = mod_name.replace(".", "__") + "__" + node.attr
                        call = ast.Call(
                            func=ast.Name(id="__module_func", ctx=ast.Load()),
                            args=[ast.Constant(value=qual)], keywords=[])
                        call._vela_func = qual  # type: ignore[attr-defined]
                        return ast.copy_location(call, node)
                    if mod_name in module_index and "." not in mod_name and node.attr in module_index[mod_name]:
                        idx, ty = module_index[mod_name][node.attr]
                        call = ast.Call(
                            func=ast.Name(id="__module_get", ctx=ast.Load()),
                            args=[ast.Name(id=mod_name, ctx=ast.Load()),
                                  ast.Constant(value=idx)],
                            keywords=[])
                        call._vela_mod_ty = ty  # type: ignore[attr-defined]
                        return ast.copy_location(call, node)
                    if node.attr in consts:
                        return ast.copy_location(ast.Constant(value=consts[node.attr]), node)
            return self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> ast.AST:
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Call) \
                    and len(f.value.args) == 1 and isinstance(f.value.args[0], ast.Constant) \
                    and isinstance(f.value.args[0].value, str) and f.value.args[0].value in modules:
                m_target = None
                if isinstance(f.value.func, ast.Name) and f.value.func.id in ("__import__", "importlib__import_module"):
                    m_target = f.value.args[0].value
                elif isinstance(f.value.func, ast.Attribute) and f.value.func.attr == "import_module":
                    m_target = f.value.args[0].value
                if m_target:
                    funcs, consts, cnames = modules[m_target]
                    if f.attr in (funcs | cnames):
                        node.func = ast.Name(
                            id=f"{m_target.replace('.', '__')}__{f.attr}",
                            ctx=ast.Load(),
                        )
                        self.generic_visit(node)
                        return node
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Subscript) \
                    and isinstance(f.value.value, ast.Attribute) \
                    and isinstance(f.value.value.value, ast.Name) \
                    and self._resolve_mod(f.value.value.value.id) == "sys" \
                    and f.value.value.attr == "modules" \
                    and isinstance(f.value.slice, ast.Constant) \
                    and isinstance(f.value.slice.value, str) \
                    and f.value.slice.value in modules \
                    and f.attr in (modules[f.value.slice.value][0] | modules[f.value.slice.value][2]):
                mod = f.value.slice.value
                node.func = ast.Name(id=f"{mod.replace('.', '__')}__{f.attr}",
                                     ctx=ast.Load())
                self.generic_visit(node)
                return node
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                if self._resolve_mod(f.value.id) == "sys" and has_sys:
                    if f.attr in ("getrefcount", "settrace", "gettrace", "setprofile", "getprofile"):
                        node.func = ast.Name(id=f"__sys_{f.attr}", ctx=ast.Load())
                        self.generic_visit(node)
                        return node
                mod_name = self._resolve_mod(f.value.id)
                if mod_name and mod_name in modules:
                    funcs, consts, cnames = modules[mod_name]
                    qual_mod = mod_name.replace(".", "__")
                    if f.attr in (funcs | cnames):
                        node.func = ast.Name(id=f"{qual_mod}__{f.attr}", ctx=ast.Load())
                    elif f.attr in consts:
                        return ast.copy_location(ast.Constant(value=consts[f.attr]), node)
            elif isinstance(f, ast.Name) and f.id in imported:
                node.func = ast.Name(id=imported[f.id], ctx=ast.Load())
            self.generic_visit(node)
            return node

    tree.body = body
    MainRewriter().visit(tree)
    return tree
