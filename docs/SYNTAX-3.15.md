# CPython 3.15 语法逐项对照表

> 更新日期：2026-11-22 · 对标版本：CPython 3.15.2 AST / Vela v0.7.0
> 唯一能力真源见 [CAPABILITIES.md](CAPABILITIES.md)。

Vela 前端直接复用宿主 CPython `ast.parse`，并在 `vela/desugar.py`、`vela/analyze.py` 与 `vela/lower.py` 中处理全部语法节点。下表列出 CPython 3.15.2 全部 AST 语法类别在 Vela 中的支持状态：

## 1. 模块与顶层声明

| 语法 / AST 节点 | 示例 | 状态 | 实现位置 |
| --- | --- | --- | --- |
| `Module` | `.vl` / `.py` 源文件 | 已支持 | `analyze.py` / `lower.py` |
| `Import` / `ImportFrom` | `import m`, `from pkg.m import f` | 已支持（含包 `__init__.py` 与 A3 标准库） | `imports.py` / `stdlib.py` |
| `TypeAlias`（PEP 695） | `type Point[T] = tuple[T, T]` | 已支持 | `desugar.py` |
| `FunctionDef`（含 PEP 695 `[T]`） | `def f[T](a: T, b=1, *args, **kw):` | 已支持 | `desugar.py` / `analyze.py` |
| `AsyncFunctionDef` | `async def fetch(x):` | 已支持 | `desugar.py` |
| `ClassDef`（含 PEP 695 `[T]`） | `class Box[T](Base):` | 已支持 | `desugar.py` |

## 2. 控制流与语句

| 语法 / AST 节点 | 示例 | 状态 | 实现位置 |
| --- | --- | --- | --- |
| `Assign` / `AnnAssign` / `AugAssign` | `a, *b = xs`, `x: int = 1`, `x += 1` | 已支持 | `desugar.py` / `lower.py` |
| `NamedExpr`（海象运算符 `:=`） | `if (n := len(xs)) > 0:` | 已支持 | `desugar.py` |
| `If` / `While` / `For` / `Break` / `Continue` / `Pass` | 循环与分支（含 `while/for ... else`） | 已支持 | `desugar.py` / `lower.py` |
| `AsyncFor` / `AsyncWith` | `async for x in aiter:`, `async with ctx:` | 已支持 | `desugar.py` |
| `With` | `with open_res() as r:` | 已支持 | `desugar.py` |
| `Match` / `match_case`（PEP 634） | `match v: case Point(x, y) if x > 0:` | 已支持（含值/单例/序列/映射/类/或/守卫模式） | `desugar.py` |
| `Try` / `ExceptHandler` / `Raise` / `Assert` | `try ... except ... else ... finally` | 已支持 | `analyze.py` / `lower.py` |
| `TryStar`（PEP 654 `except*`） | `try ... except* ValueError as eg:` | 已支持 | `desugar.py` |
| `Global` / `Nonlocal` | `global G`, `nonlocal count` | 已支持 | `analyze.py` |
| `Return` / `Yield` / `YieldFrom` | `return v`, `yield x`, `yield from g` | 已支持 | `desugar.py` / `lower.py` |
| `Delete`（`del`） | `del x` | 已支持 | `desugar.py` |

## 3. 表达式与字面量

| 语法 / AST 节点 | 示例 | 状态 | 实现位置 |
| --- | --- | --- | --- |
| `Constant` | `int`（含超 64 位 `bigint`）、`float`、`bool`、`str`、`bytes`、`None` | 已支持 | `analyze.py` / `lower.py` |
| `JoinedStr` / `FormattedValue`（f-string） | `f"val={x + 1}"` | 已支持 | `desugar.py` |
| `TemplateStr` / `Interpolation`（PEP 750 t-string） | `t"hello {name}"` / `Template(...)` | 已支持 | `desugar.py` |
| `List` / `Tuple` / `Set` / `Dict` | `[1, *a]`, `(x, y)`, `{1, 2}`, `{"a": 1}` | 已支持 | `analyze.py` / `lower.py` |
| `ListComp` / `SetComp` / `DictComp` / `GeneratorExp` | `[x*x for x in xs if x > 0]` | 已支持 | `desugar.py` |
| `Lambda` | `lambda x, y=1: x + y` | 已支持 | `desugar.py` |
| `Await` | `await coro(x)` | 已支持 | `desugar.py` |
| `BinOp` / `UnaryOp` / `BoolOp` / `Compare` / `IfExp` | 全部算术/位运算/逻辑/链式比较/三元表达式 | 已支持 | `analyze.py` / `lower.py` |
| `Subscript` / `Slice` | `xs[-1]`, `xs[a:b:step]`, `s[::-1]` | 已支持 | `analyze.py` / `lower.py` |
| `Call` / `Attribute` | 位置/关键字/`*args`/`**kwargs`、方法与描述符 | 已支持 | `desugar.py` / `lower.py` |
