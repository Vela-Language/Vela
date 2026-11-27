"""Bilingual (English / Chinese) CLI, compile-time diagnostic, and runtime strings.

Language selection priority:
  1. ``--lang zh|en`` on the command line (temporary override for current command)
  2. ``VELA_LANG=zh|en`` environment variable
  3. Persisted default language set by ``vela --set-lang zh|en``
  4. Default: ``en`` (deterministic baseline)

Terminology glossary (统一术语表):
  * native profile -> 原生 profile（命令行帮助中保留 `native profile` 标识以便对照）
  * type error     -> 类型错误
  * syntax error   -> 语法错误
  * internal error -> 内部错误
  * runtime error  -> 运行时错误
"""
from __future__ import annotations

import os
import re

SUPPORTED = ("zh", "en")
_LANG: str | None = None


def _config_path() -> str:
    override = os.environ.get("VELA_LANG_CONFIG")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".vela_lang")


def set_lang(lang: str | None) -> None:
    """Temporarily set the active language for the current process."""
    global _LANG
    if lang is None:
        _LANG = None
        return
    lang = lang.lower()
    if lang not in SUPPORTED:
        raise ValueError(lang)
    _LANG = lang


def set_default_lang(lang: str) -> str:
    """Persistently set the default display language (--set-lang)."""
    lang = lang.lower()
    if lang not in SUPPORTED:
        raise ValueError(lang)
    path = _config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(lang + "\n")
    set_lang(lang)
    return path


def get_default_lang() -> str | None:
    """Read the persisted default language from disk, if present."""
    path = _config_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                val = f.read().strip().lower()
            if val in SUPPORTED:
                return val
    except OSError:
        pass
    return None


def get_lang() -> str:
    if _LANG is not None:
        return _LANG
    env = os.environ.get("VELA_LANG", "").lower()
    if env in SUPPORTED:
        return env
    persisted = get_default_lang()
    if persisted in SUPPORTED:
        return persisted
    return "en"


EN: dict[str, str] = {
    "usage": """Vela {version} - Python-syntax native compiler for Windows x86-64

usage:
  vela build <file.vl> [-o out.exe] [--stats] [--emit-ir] [--emit-asm] [--lang zh|en]
  vela run   <file.vl> [--strict] [--stats] [-v] [--lang zh|en] [-- <program args>...]
  vela check <file.vl> [-v] [--lang zh|en]
  vela bench <file.vl>... [--lang zh|en]
  vela --set-lang <zh|en>

  .vl and .py are both accepted; .vl is the recommended Vela source suffix
  (the contents are ordinary Python syntax).
  vela version
  vela --help

commands:
  build   compile to a standalone .exe (native profile, no fallback)
  run     compile and execute; if the file is outside the native profile it
          falls back to CPython (unless --strict)
  check   parse + type-check only, report whether the file is in the profile
  bench   compile and run each source once, reporting compile and run timings

options:
  --lang <zh|en>      temporarily set display language for this invocation
  --set-lang <zh|en>  permanently set default display language

notes:
  The native profile is a statically typed subset of Python (int64, float64,
  bool, str, None); --boxed enables the full pure-Python object runtime.
  See docs/ARCHITECTURE.md and docs/PROFILE.md.
""",
    # CLI status & framing
    "build.one_file": "vela build: expected exactly one source file",
    "build.built": "vela: built {path} ({size} bytes)",
    "build.ir": "vela: IR written to {path}",
    "build.asm": "vela: assembly written to {path}",
    "check.one_file": "vela check: expected exactly one source file",
    "check.ok": "OK: {path} is within the Vela native profile",
    "check.globals": "  module globals: ",
    "run.one_file": "vela run: expected exactly one source file",
    "run.fallback": "vela: falling back to CPython (source is outside the native profile)",
    "run.native": "vela: running native code ({size} bytes)",
    "bench.one_file": "vela bench: expected at least one source file",
    "bench.result": "{src}: compile {compile_ms} ms, run {run_ms} ms, image {size} bytes, exit {code}",
    "cli.stats": (
        "vela: {functions} functions, {blocks} blocks, {instructions} IR "
        "instructions, {vregs} vregs, {code_bytes} bytes of machine code, "
        "{image_bytes} byte image, {ms} ms"
    ),
    "unknown.command": "vela: unknown command '{cmd}'",
    "unknown.option": "vela: unknown option {opt}",
    "output.requires": "vela: -o requires a path",
    "lang.invalid": "vela: unsupported language '{lang}' (use zh or en)",
    "lang.missing": "vela: --lang requires a value (zh or en)",
    "lang.set_missing": "vela: --set-lang requires a value (zh or en)",
    "lang.set_ok": "vela: default language permanently set to '{lang}'",
    "kind.error": "error",
    "kind.type": "type error",
    "kind.internal": "internal error",
    "err.unsupported": "unsupported by the Vela native profile (v0.1): {feature}",
    "err.syntax": "{filename}:{lineno}:{offset}: syntax error: {msg}",
    "err.internal": "{filename}: internal error: {exc_type}: {exc}",
    # Runtime error messages (embedded into .rdata according to compile-time language)
    "rt.divzero": "Vela runtime error: integer division by zero\n",
    "rt.overflow": "Vela runtime error: integer overflow (int64 range exceeded)\n",
    "rt.negexp": "Vela runtime error: negative integer exponent is not supported by the native int profile\n",
    "rt.stepzero": "Vela runtime error: range() step must not be zero\n",
    "rt.alloc": "Vela runtime error: out of memory\n",
    "rt.capacity": "Vela runtime error: list capacity exceeded\n",
    "rt.pop": "Vela runtime error: pop from empty list\n",
    "rt.big": "Vela runtime error: bigint too large for this operation\n",
    "rt.code": "Vela runtime error: code object arity must be 0..3 for dynamic calls\n",
    "rt.bigdiv": "Vela runtime error: bigint division requires non-negative dividend and positive divisor\n",
    "rt.key": "Vela runtime error: missing dict key\n",
    "rt.index": "Vela runtime error: string index out of range\n",
    "rt.unhandled": "Vela runtime error: unhandled exception\n",
    "rt.root_overflow": "Vela runtime error: GC root stack overflow\n",
    "rt.type": "Vela runtime error: unbox type mismatch\n",
    # Compile-time & type diagnostic message IDs
    "diag.cannot_mix": "cannot mix {a} and {b} in one expression",
    "diag.var_reassigned": "variable '{name}' is {old} but is assigned {ty}; the native profile requires one static type per variable",
    "diag.cond_type": "condition must be bool or int, got {ty}",
    "diag.var_before_assign": "variable '{name}' is used before assignment",
    "diag.local_before_assign": "local variable '{name}' is used before assignment",
    "diag.div_zero": "division by zero",
    "diag.range_step_zero": "range() step must not be zero",
    "diag.range_arity": "range() takes 1 to 3 arguments",
    "diag.range_arg_type": "range() arguments must be int, got {t}",
    "diag.int_overflow_lit": "integer literal does not fit in int64",
    "diag.not_type": "'not' requires bool/int, got {t}",
    "diag.unary_type": "unary +/- requires a number, got {t}",
    "diag.boolop_type": "'{op}' requires bool/int, got {t}",
    "diag.cmp_type": "cannot compare {lt} with {rt}",
    "diag.ifexp_mix": "conditional expression mixes {a} and {b}",
    "diag.dict_key_type": "dict key must be int or str",
    "diag.container_elem_type": "container element must be int, got {vt}",
    "diag.set_elem_type": "set element must be int, got {vt}",
    "diag.div_requires_num": "'/' requires numbers, got {a} and {b}",
    "diag.bit_requires_int": "bitwise/shift operators require int, got {a} and {b}",
    "diag.op_requires_num": "operator {op} requires numbers, got {a} and {b}",
    "diag.takes_one_arg": "{fn}() takes one argument",
    "diag.takes_exact_one_arg": "{fn}() takes exactly one argument",
    "diag.takes_no_args": "{fn}() takes no arguments",
    "diag.takes_at_most_one": "{fn}() takes at most one argument",
    "diag.takes_at_most_1": "{fn}() takes at most 1 argument",
    "diag.takes_two_args": "{fn}() takes two arguments",
    "diag.sum_arity": "sum() takes 1 or 2 arguments",
    "diag.append_type": "append() requires int, got {vt}",
    "diag.print_type": "print() cannot print a value of type {t}",
    "diag.len_type": "len() requires str or list, got {t}",
    "diag.abs_type": "abs() requires a number, got {t}",
    "diag.minmax_type": "{name}() requires numbers, got {ta} and {tb}",
    "diag.str_unsupported": "str() does not support {t}",
    "diag.conv_cannot": "{name}() cannot convert {t}",
    "diag.call_arity": "{name}() takes {expected} arguments but {given} were given",
    "diag.call_arg_type": "argument {idx} of {name}() is {got} but the parameter is {want}",
    "diag.dup_func": "duplicate function definition '{name}'",
    "diag.param_inferred": "parameter '{p}' of {name}() is inferred as {old} but used as {new}",
    "diag.ret_mismatch": "{name}() is declared to return {want} but returns {got}",
    "diag.ret_partial": "{name}() returns a value on some paths but falls off the end of the function; add an explicit return",
    "diag.ret_missing": "{name}() is declared to return {want} but may fall off the end without returning a value",
    "diag.assign_global_type": "cannot assign {ty} to global '{name}' of type {want}",
    "diag.assign_ann_type": "cannot assign {ty} to variable '{name}' declared as {declared}",
    "diag.assign_none_global": "cannot assign None to global '{name}'",
    "diag.assign_none_var": "cannot assign None to '{name}'",
    "diag.assign_none_list": "cannot assign None to list '{name}'",
    "diag.assign_none_typed": "cannot assign None to variable '{name}' of type {ty}",
    "diag.convert_type": "cannot convert {src} to {want}",
    "diag.bigint_arith": "cannot use {ty} in bigint arithmetic",
    "diag.use_as_int": "cannot use {ty} as integer",
    "diag.break_outside": "'break' outside a loop",
    "diag.continue_outside": "'continue' outside a loop",
    "diag.inconsistent_mro": "inconsistent MRO for class '{cls_name}'",
    "diag.slots_restricted": "'{oc}' object has no attribute '{attr}' (restricted by __slots__)",
    "diag.prop_no_setter": "property '{attr}' of '{oc}' object has no setter",
    "diag.repeated_kw": "repeated keyword argument '{kw}'",
    "diag.unexpected_kw": "{fname}() got an unexpected keyword argument '{kw}'",
    "diag.multiple_arg_vals": "{fname}() got multiple values for argument '{pname}'",
    "diag.missing_kwonly": "{fname}() missing required keyword-only argument '{kname}'",
    # Parameterized UnsupportedError feature templates
    "feat.import_not_found": "ImportError: cannot find pure Python module '{name}'",
    "feat.import_no_func": "module '{mod}' has no function '{fn}'",
    "feat.type_annotation": "type annotation {ann}",
    "feat.global_init": "global '{name}' needs a constant module initializer",
    "feat.global_var": "global variable '{name}' (the native profile only supports function-local names)",
    "feat.const_type": "constant of type {ty}",
    "feat.operator": "operator {op}",
    "feat.unary_op": "unary operator {op}",
    "feat.aug_op": "augmented operator {op}",
    "feat.comparison": "comparison {op}",
    "feat.unknown_func": "call to unknown function '{name}'",
    "feat.unknown_func_val": "unknown function value '{name}'",
    "feat.unknown_closure": "unknown closure function '{name}'",
    "feat.minmax_arity": "{name}() with other than 2 arguments",
    "feat.float_op": "'{op}' on floats",
}

ZH: dict[str, str] = {
    "usage": """Vela {version} —— 面向 Windows x86-64 的 Python 语法原生编译器

用法:
  vela build <file.vl> [-o out.exe] [--stats] [--emit-ir] [--emit-asm] [--lang zh|en]
  vela run   <file.vl> [--strict] [--stats] [-v] [--lang zh|en] [-- <程序参数>...]
  vela check <file.vl> [-v] [--lang zh|en]
  vela bench <file.vl>... [--lang zh|en]
  vela --set-lang <zh|en>

  同时接受 .vl 与 .py；推荐使用 .vl 作为 Vela 源码后缀（内容为普通 Python 语法）。
  vela version
  vela --help

命令:
  build   编译为独立 .exe（native profile，不回退）
  run     编译并执行；不在 native profile 内时回退 CPython（除非 --strict）
  check   只做解析与类型检查，报告是否在 profile 内
  bench   编译并运行源文件一次，输出编译与运行耗时

选项:
  --lang <zh|en>      临时指定本次命令的显示语言
  --set-lang <zh|en>  永久设置默认显示语言

说明:
  native profile 是 Python 的静态类型子集（int64、float64、bool、str、None）；
  --boxed 启用完整的纯 Python 对象运行时。详见 docs/ARCHITECTURE.md 与 docs/PROFILE.md。
""",
    # CLI status & framing
    "build.one_file": "vela build: 需要且只需要一个源文件",
    "build.built": "vela: 已生成 {path}（{size} 字节）",
    "build.ir": "vela: IR 已写入 {path}",
    "build.asm": "vela: 汇编清单已写入 {path}",
    "check.one_file": "vela check: 需要且只需要一个源文件",
    "check.ok": "OK: {path} 位于 Vela native profile 内",
    "check.globals": "  模块级全局变量: ",
    "run.one_file": "vela run: 需要且只需要一个源文件",
    "run.fallback": "vela: 回退到 CPython（源码超出 native profile）",
    "run.native": "vela: 运行原生代码（{size} 字节）",
    "bench.one_file": "vela bench: 至少需要一个源文件",
    "bench.result": "{src}: 编译 {compile_ms} ms，运行 {run_ms} ms，映像 {size} 字节，退出码 {code}",
    "cli.stats": (
        "vela: {functions} 个函数，{blocks} 个基本块，{instructions} 条 IR 指令，"
        "{vregs} 个虚拟寄存器，{code_bytes} 字节机器码，{image_bytes} 字节映像，{ms} ms"
    ),
    "unknown.command": "vela: 未知命令 '{cmd}'",
    "unknown.option": "vela: 未知选项 {opt}",
    "output.requires": "vela: -o 需要一个路径",
    "lang.invalid": "vela: 不支持的语言 '{lang}'（可用 zh 或 en）",
    "lang.missing": "vela: --lang 需要一个值（zh 或 en）",
    "lang.set_missing": "vela: --set-lang 需要一个值（zh 或 en）",
    "lang.set_ok": "vela: 已将默认显示语言永久设置为 '{lang}'",
    "kind.error": "错误",
    "kind.type": "类型错误",
    "kind.internal": "内部错误",
    "err.unsupported": "Vela 原生 profile（v0.1）不支持：{feature}",
    "err.syntax": "{filename}:{lineno}:{offset}: 语法错误: {msg}",
    "err.internal": "{filename}: 内部错误: {exc_type}: {exc}",
    # Runtime error messages
    "rt.divzero": "Vela 运行时错误：整数除以零\n",
    "rt.overflow": "Vela 运行时错误：整数溢出（超出 int64 范围）\n",
    "rt.negexp": "Vela 运行时错误：原生整数 profile 不支持负整数指数\n",
    "rt.stepzero": "Vela 运行时错误：range() 步长不能为零\n",
    "rt.alloc": "Vela 运行时错误：内存不足\n",
    "rt.capacity": "Vela 运行时错误：列表容量超限\n",
    "rt.pop": "Vela 运行时错误：从空列表弹出元素\n",
    "rt.big": "Vela 运行时错误：大整数超出此操作允许范围\n",
    "rt.code": "Vela 运行时错误：动态调用的代码对象参数个数必须在 0..3 之间\n",
    "rt.bigdiv": "Vela 运行时错误：大整数除法要求非负被除数与正除数\n",
    "rt.key": "Vela 运行时错误：字典键不存在\n",
    "rt.index": "Vela 运行时错误：字符串索引超出范围\n",
    "rt.unhandled": "Vela 运行时错误：未捕获的异常\n",
    "rt.root_overflow": "Vela 运行时错误：GC 根栈溢出\n",
    "rt.type": "Vela 运行时错误：拆箱类型不匹配\n",
    # Compile-time & type diagnostic message IDs
    "diag.cannot_mix": "不能在同一表达式中混用 {a} 与 {b}",
    "diag.var_reassigned": "变量 '{name}' 的类型为 {old}，但被赋值为 {ty}；原生 profile 要求每个变量保持单一静态类型",
    "diag.cond_type": "条件表达式必须为 bool 或 int，实际为 {ty}",
    "diag.var_before_assign": "变量 '{name}' 在赋值前被使用",
    "diag.local_before_assign": "局部变量 '{name}' 在赋值前被使用",
    "diag.div_zero": "除以零",
    "diag.range_step_zero": "range() 步长不能为零",
    "diag.range_arity": "range() 需要 1 到 3 个参数",
    "diag.range_arg_type": "range() 参数必须为 int，实际为 {t}",
    "diag.int_overflow_lit": "整数字面量超出 int64 范围",
    "diag.not_type": "'not' 需要 bool/int，实际为 {t}",
    "diag.unary_type": "一元 +/- 需要数值类型，实际为 {t}",
    "diag.boolop_type": "'{op}' 需要 bool/int，实际为 {t}",
    "diag.cmp_type": "无法比较 {lt} 与 {rt}",
    "diag.ifexp_mix": "条件表达式混用了 {a} 与 {b}",
    "diag.dict_key_type": "字典键必须为 int 或 str",
    "diag.container_elem_type": "容器元素必须为 int，实际为 {vt}",
    "diag.set_elem_type": "集合元素必须为 int，实际为 {vt}",
    "diag.div_requires_num": "'/' 需要数值类型，实际为 {a} 与 {b}",
    "diag.bit_requires_int": "位运算/移位运算符需要 int，实际为 {a} 与 {b}",
    "diag.op_requires_num": "运算符 {op} 需要数值类型，实际为 {a} 与 {b}",
    "diag.takes_one_arg": "{fn}() 需要 1 个参数",
    "diag.takes_exact_one_arg": "{fn}() 需要且仅需要 1 个参数",
    "diag.takes_no_args": "{fn}() 不接受参数",
    "diag.takes_at_most_one": "{fn}() 最多接受 1 个参数",
    "diag.takes_at_most_1": "{fn}() 最多接受 1 个参数",
    "diag.takes_two_args": "{fn}() 需要 2 个参数",
    "diag.sum_arity": "sum() 需要 1 或 2 个参数",
    "diag.append_type": "append() 需要 int，实际为 {vt}",
    "diag.print_type": "print() 无法打印类型为 {t} 的值",
    "diag.len_type": "len() 需要 str 或 list，实际为 {t}",
    "diag.abs_type": "abs() 需要数值类型，实际为 {t}",
    "diag.minmax_type": "{name}() 需要数值类型，实际为 {ta} 与 {tb}",
    "diag.str_unsupported": "str() 不支持类型 {t}",
    "diag.conv_cannot": "{name}() 无法转换类型 {t}",
    "diag.call_arity": "{name}() 需要 {expected} 个参数，但传入了 {given} 个",
    "diag.call_arg_type": "{name}() 的第 {idx} 个参数为 {got}，但形参类型为 {want}",
    "diag.dup_func": "重复的函数定义 '{name}'",
    "diag.param_inferred": "{name}() 的形参 '{p}' 推导为 {old}，但被作为 {new} 使用",
    "diag.ret_mismatch": "{name}() 声明返回 {want}，但实际返回 {got}",
    "diag.ret_partial": "{name}() 在部分路径返回值但可能直接执行到函数末尾；请添加显式 return",
    "diag.ret_missing": "{name}() 声明返回 {want}，但可能未返回值即到达函数末尾",
    "diag.assign_global_type": "无法将 {ty} 赋值给类型为 {want} 的全局变量 '{name}'",
    "diag.assign_ann_type": "无法将 {ty} 赋值给声明为 {declared} 的变量 '{name}'",
    "diag.assign_none_global": "无法将 None 赋值给全局变量 '{name}'",
    "diag.assign_none_var": "无法将 None 赋值给 '{name}'",
    "diag.assign_none_list": "无法将 None 赋值给列表 '{name}'",
    "diag.assign_none_typed": "无法将 None 赋值给类型为 {ty} 的变量 '{name}'",
    "diag.convert_type": "无法将 {src} 转换为 {want}",
    "diag.bigint_arith": "无法在 bigint 算术中使用 {ty}",
    "diag.use_as_int": "无法将 {ty} 作为整数使用",
    "diag.break_outside": "'break' 位于循环外部",
    "diag.continue_outside": "'continue' 位于循环外部",
    "diag.inconsistent_mro": "类 '{cls_name}' 的 MRO 方法解析顺序不一致",
    "diag.slots_restricted": "'{oc}' 对象没有属性 '{attr}'（受 __slots__ 限制）",
    "diag.prop_no_setter": "'{oc}' 对象的属性 '{attr}' 没有设置器（setter）",
    "diag.repeated_kw": "重复的关键字参数 '{kw}'",
    "diag.unexpected_kw": "{fname}() 收到未预期的关键字参数 '{kw}'",
    "diag.multiple_arg_vals": "{fname}() 的参数 '{pname}' 收到多个值",
    "diag.missing_kwonly": "{fname}() 缺少必需的仅关键字参数 '{kname}'",
    # Parameterized UnsupportedError feature templates
    "feat.import_not_found": "ImportError: 找不到纯 Python 模块 '{name}'",
    "feat.import_no_func": "模块 '{mod}' 没有函数 '{fn}'",
    "feat.type_annotation": "类型注解 {ann}",
    "feat.global_init": "全局变量 '{name}' 需要模块级常量初始值",
    "feat.global_var": "全局变量 '{name}'（原生 profile 仅支持函数局部变量）",
    "feat.const_type": "类型为 {ty} 的常量",
    "feat.operator": "运算符 {op}",
    "feat.unary_op": "一元运算符 {op}",
    "feat.aug_op": "复合赋值运算符 {op}",
    "feat.comparison": "比较运算符 {op}",
    "feat.unknown_func": "调用未知函数 '{name}'",
    "feat.unknown_func_val": "未知函数值 '{name}'",
    "feat.unknown_closure": "未知闭包函数 '{name}'",
    "feat.minmax_arity": "{name}() 参数个数不为 2",
    "feat.float_op": "浮点数上的 '{op}' 运算",
}

# Static UnsupportedError feature literals.
FEATURES_ZH: dict[str, str] = {
    "container literals": "容器字面量",
    "attribute access / method calls": "属性访问 / 方法调用",
    "f-strings": "f-string",
    "lambda expressions": "lambda 表达式",
    "comprehensions": "推导式",
    "nested function definitions": "嵌套函数定义",
    "varargs / keyword-only arguments": "可变参数 / 仅关键字参数",
    "default argument values": "默认参数值",
    "decorators": "装饰器",
    "generic type parameters (PEP 695)": "泛型类型参数（PEP 695）",
    "assignment to anything but a single name": "只能赋值给单个名字",
    "for-loop over anything but range()": "for 只能遍历 range()",
    "subscripting (lists, dicts, strings)": "下标访问（list/dict/str）",
    "calling an attribute or expression": "调用属性或表达式",
    "keyword arguments": "关键字参数",
    "starred call arguments": "星号解包调用参数",
    "class definitions": "类定义",
    "try statements": "try 语句",
    "try without except/finally": "没有 except/finally 的 try 语句",
    "except with a non-name type": "except 类型不是标识符",
    "assert with a message": "带消息的 assert 语句",
    "assert statements": "assert 语句",
    "raise with a non-literal exception": "raise 非字面量异常",
    "raise ... from with a non-call cause": "raise ... from 原因不是异常调用",
    "with statements": "with 语句",
    "import statements": "import 语句",
    "from . import ... is not supported": "不支持 from . import ...",
    "yield expressions": "yield 表达式",
    "function object as a value": "函数作为值",
    "float floor division / modulo": "浮点地板除 / 取模",
    "float exponentiation ('**')": "浮点幂运算（**）",
    "more than 4 parameters (Win64 ABI v0.1 limit)": "超过 4 个形参（Win64 ABI v0.1 限制）",
    "return statement at module level": "模块顶层 return 语句",
    "global statement at module level": "模块顶层 global 语句",
    "tuple unpacking target": "元组解包赋值目标",
    "annotation without a value": "不带初始值的类型注解",
    "augmented assignment to anything but a name": "复合赋值目标不是单个变量名",
    "for-loop target other than a name": "for 循环目标不是单个变量名",
    "range() keyword arguments": "range() 关键字参数",
    "range() outside a for-loop": "for 循环之外的 range()",
    "dict entries other than int/str keys and int values": "非 int/str 键或非 int 值的字典项",
    "assignment expressions (:=)": "海象赋值表达式（:=）",
    "while/else": "while/else 语句",
    "for/else": "for/else 语句",
    "bigint operator": "不支持的 bigint 运算符",
    "arithmetic on None": "对 None 进行算术运算",
    "division on None": "对 None 进行除法运算",
    "shift on None": "对 None 进行移位运算",
    "comparison with None": "与 None 进行大小比较",
    "dynamic int comparison": "不支持的动态整数比较",
    "bigint comparison": "不支持的 bigint 比较",
    "str() on native int in this profile": "当前 profile 下对原生 int 调用 str()",
    "non-ASCII bytes literal": "非 ASCII bytes 字面量",
    "AsyncFunctionDef": "异步函数定义（AsyncFunctionDef）",
    "ClassDef": "类定义（ClassDef）",
    "Try": "异常捕获（Try）",
    "With": "上下文管理器（With）",
    "Import": "模块导入（Import）",
    "ImportFrom": "从模块导入（ImportFrom）",
    "Yield": "生成器产出（Yield）",
    "YieldFrom": "委托生成器产出（YieldFrom）",
    "Await": "异步等待（Await）",
    "Match": "结构化模式匹配（Match）",
    "Delete": "删除语句（Delete）",
    "Nonlocal": "非局部声明（Nonlocal）",
}


def _compile_template_patterns(prefix: str) -> list[tuple[re.Pattern[str], str]]:
    patterns: list[tuple[re.Pattern[str], str]] = []
    # Sort longer templates first so more specific templates match before generic ones.
    keys = sorted(
        (k for k in EN if k.startswith(prefix)),
        key=lambda k: len(EN[k]),
        reverse=True,
    )
    for k in keys:
        en_tmpl = EN[k]
        zh_tmpl = ZH[k]
        parts = re.split(r"(\{[a-zA-Z0-9_]+\})", en_tmpl)
        regex_parts: list[str] = ["^"]
        seen_groups: set[str] = set()
        for part in parts:
            if part.startswith("{") and part.endswith("}"):
                gname = part[1:-1]
                if gname not in seen_groups:
                    regex_parts.append(f"(?P<{gname}>.+?)")
                    seen_groups.add(gname)
                else:
                    regex_parts.append(f"(?P={gname})")
            else:
                regex_parts.append(re.escape(part))
        regex_parts.append("$")
        patterns.append((re.compile("".join(regex_parts)), zh_tmpl))
    return patterns


_DIAG_PATTERNS = _compile_template_patterns("diag.")
_FEAT_PATTERNS = _compile_template_patterns("feat.")


def usage(version: str) -> str:
    table = ZH if get_lang() == "zh" else EN
    return table["usage"].format(version=version)


def kind_label(kind: str) -> str:
    key = {"error": "kind.error", "type error": "kind.type",
           "internal error": "kind.internal"}.get(kind, None)
    if key is None:
        return kind
    return t(key)


def feature_text(feature: str) -> str:
    if get_lang() != "zh":
        return feature
    if feature in FEATURES_ZH:
        return FEATURES_ZH[feature]
    for pat, zh_tmpl in _FEAT_PATTERNS:
        m = pat.match(feature)
        if m:
            return zh_tmpl.format(**m.groupdict())
    return feature


def translate_diagnostic(message: str) -> str:
    """Translate an English compile-time/type diagnostic message when lang == 'zh'."""
    if get_lang() != "zh":
        return message
    for pat, zh_tmpl in _DIAG_PATTERNS:
        m = pat.match(message)
        if m:
            return zh_tmpl.format(**m.groupdict())
    return message


def t(key: str, **kwargs) -> str:
    table = ZH if get_lang() == "zh" else EN
    text = table.get(key) or EN.get(key) or key
    return text.format(**kwargs) if kwargs else text
