"""Bilingual (English / Chinese) CLI and diagnostic strings.

Language selection:
  * ``--lang zh|en`` on the command line (highest priority)
  * ``VELA_LANG=zh|en`` environment variable
  * default: English (deterministic for CI and tests)

Only user-facing surfaces are translated: help/usage, CLI status messages, the
diagnostic kind label, and the "unsupported by the native profile" wrapper.
Detailed type/compile messages are still English until they are migrated to
message IDs.
"""
from __future__ import annotations

import os

SUPPORTED = ("zh", "en")
_LANG: str | None = None


def set_lang(lang: str) -> None:
    global _LANG
    lang = lang.lower()
    if lang not in SUPPORTED:
        raise ValueError(lang)
    _LANG = lang


def get_lang() -> str:
    if _LANG is not None:
        return _LANG
    env = os.environ.get("VELA_LANG", "").lower()
    if env in SUPPORTED:
        return env
    return "en"


EN = {
    "usage": """Vela {version} - Python-syntax native compiler for Windows x86-64

usage:
  vela build <file.vl> [-o out.exe] [--stats] [--emit-ir]
  vela run   <file.vl> [--strict] [--stats] [-v] [-- <program args>...]
  vela check <file.vl> [-v] [--lang zh|en]

  .vl and .py are both accepted; .vl is the recommended Vela source suffix
  (the contents are ordinary Python syntax).
  vela version
  vela --help

commands:
  build   compile to a standalone .exe (native profile, no fallback)
  run     compile and execute; if the file is outside the native profile it
          falls back to CPython (unless --strict)
  check   parse + type-check only, report whether the file is in the profile

notes:
  The native profile is a statically typed subset of Python (int64, float64,
  bool, str, None).  See docs/ARCHITECTURE.md and docs/PROFILE.md.
""",
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
    "unknown.command": "vela: unknown command '{cmd}'",
    "unknown.option": "vela: unknown option {opt}",
    "output.requires": "vela: -o requires a path",
    "lang.invalid": "vela: unsupported language '{lang}' (use zh or en)",
    "lang.missing": "vela: --lang requires a value (zh or en)",
    "kind.error": "error",
    "kind.type": "type error",
    "kind.internal": "internal error",
    "err.unsupported": "unsupported by the Vela native profile (v0.1): {feature}",
}

ZH = {
    "usage": """Vela {version} —— 面向 Windows x86-64 的 Python 语法原生编译器

用法:
  vela build <file.vl> [-o out.exe] [--stats] [--emit-ir]
  vela run   <file.vl> [--strict] [--stats] [-v] [-- <程序参数>...]
  vela check <file.vl> [-v] [--lang zh|en]

  同时接受 .vl 与 .py；推荐使用 .vl 作为 Vela 源码后缀（内容为普通 Python 语法）。
  vela version
  vela --help

命令:
  build   编译为独立 .exe（native profile，不回退）
  run     编译并执行；不在 native profile 内时回退 CPython（除非 --strict）
  check   只做解析与类型检查，报告是否在 profile 内

说明:
  native profile 是 Python 的静态类型子集（int64、float64、bool、str、None）。
  详见 docs/ARCHITECTURE.md 与 docs/PROFILE.md。
""",
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
    "unknown.command": "vela: 未知命令 '{cmd}'",
    "unknown.option": "vela: 未知选项 {opt}",
    "output.requires": "vela: -o 需要一个路径",
    "lang.invalid": "vela: 不支持的语言 '{lang}'（可用 zh 或 en）",
    "lang.missing": "vela: --lang 需要一个值（zh 或 en）",
    "kind.error": "错误",
    "kind.type": "类型错误",
    "kind.internal": "内部错误",
    "err.unsupported": "Vela 原生 profile（v0.1）不支持：{feature}",
}

# Common UnsupportedError features.  Unknown features fall back to English.
FEATURES_ZH = {
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
    "class definitions": "类定义",
    "try statements": "try 语句",
    "with statements": "with 语句",
    "import statements": "import 语句",
    "yield expressions": "yield 表达式",
    "function object as a value": "函数作为值",
    "float floor division / modulo": "浮点地板除 / 取模",
    "float exponentiation ('**')": "浮点幂运算（**）",
}


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
    if get_lang() == "zh":
        return FEATURES_ZH.get(feature, feature)
    return feature


def t(key: str, **kwargs) -> str:
    table = ZH if get_lang() == "zh" else EN
    text = table.get(key) or EN.get(key) or key
    return text.format(**kwargs) if kwargs else text
