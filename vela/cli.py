"""Vela command line interface."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time

from . import __version__
from .driver import analyze_source, compile_file, compile_source
from .errors import UnsupportedError, VelaError
from .ir_dump import dump_program

USAGE = """Vela {version} - Python-syntax native compiler for Windows x86-64

usage:
  vela build <file.vl> [-o out.exe] [--stats] [--emit-ir]
  vela run   <file.vl> [--strict] [--stats] [-v] [-- <program args>...]
  vela check <file.vl> [-v]

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
"""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _format_error(exc: Exception, filename: str) -> str:
    if isinstance(exc, VelaError):
        return exc.format(filename)
    if isinstance(exc, SyntaxError):
        return f"{filename}:{exc.lineno}:{exc.offset}: syntax error: {exc.msg}"
    return f"{filename}: internal error: {type(exc).__name__}: {exc}"


def _print_stats(result) -> None:
    s = result.stats
    _err(f"vela: {s.functions} functions, {s.blocks} blocks, {s.instructions} IR "
         f"instructions, {s.vregs} vregs, {s.code_bytes} bytes of machine code, "
         f"{s.image_bytes} byte image, {s.seconds * 1000:.1f} ms")


def _parse(argv: list[str]):
    opts = {"output": None, "strict": False, "stats": False, "emit_ir": False,
            "verbose": False, "args": []}
    i = 0
    positional: list[str] = []
    while i < len(argv):
        a = argv[i]
        if a == "--":
            opts["args"] = argv[i + 1:]
            break
        if a in ("-o", "--output"):
            i += 1
            if i >= len(argv):
                raise SystemExit("vela: -o requires a path")
            opts["output"] = argv[i]
        elif a == "--strict":
            opts["strict"] = True
        elif a == "--stats":
            opts["stats"] = True
        elif a == "--emit-ir":
            opts["emit_ir"] = True
        elif a in ("-v", "--verbose"):
            opts["verbose"] = True
        elif a.startswith("-"):
            raise SystemExit(f"vela: unknown option {a}")
        else:
            positional.append(a)
        i += 1
    return positional, opts


def cmd_build(argv: list[str]) -> int:
    pos, opts = _parse(argv)
    if len(pos) != 1:
        _err("vela build: expected exactly one source file")
        return 2
    src = pos[0]
    try:
        result = compile_file(src)
    except (VelaError, SyntaxError) as exc:
        _err(_format_error(exc, src))
        return 1
    out = opts["output"] or (os.path.splitext(src)[0] + ".exe")
    with open(out, "wb") as f:
        f.write(result.exe)
    if opts["stats"]:
        _print_stats(result)
    if opts["emit_ir"]:
        ir_path = os.path.splitext(out)[0] + ".ir"
        with open(ir_path, "w", encoding="utf-8") as f:
            f.write(dump_program(result.program))
        _err(f"vela: IR written to {ir_path}")
    print(f"vela: built {out} ({len(result.exe)} bytes)")
    return 0


def cmd_check(argv: list[str]) -> int:
    pos, opts = _parse(argv)
    if len(pos) != 1:
        _err("vela check: expected exactly one source file")
        return 2
    src = pos[0]
    with open(src, "r", encoding="utf-8-sig") as f:
        source = f.read()
    try:
        analyzer = analyze_source(source, src)
    except (VelaError, SyntaxError) as exc:
        _err(_format_error(exc, src))
        return 1
    print(f"OK: {src} is within the Vela native profile")
    if opts["verbose"]:
        for name, info in analyzer.funcs.items():
            params = ", ".join(
                f"{p}: {t}" for p, t in zip(info.params, info.param_types))
            print(f"  def {name}({params}) -> {info.ret_type}")
        main_names = sorted(analyzer.main_vars)
        if main_names:
            print("  module globals: " + ", ".join(
                f"{n}: {analyzer.main_vars[n]}" for n in main_names))
    return 0


def cmd_run(argv: list[str]) -> int:
    pos, opts = _parse(argv)
    if len(pos) != 1:
        _err("vela run: expected exactly one source file")
        return 2
    src = pos[0]
    program_args = opts["args"]
    try:
        result = compile_file(src)
    except SyntaxError as exc:
        _err(_format_error(exc, src))
        return 1
    except VelaError as exc:
        if opts["strict"]:
            _err(_format_error(exc, src))
            return 1
        _err(f"vela: {_format_error(exc, src)}")
        _err("vela: falling back to CPython (source is outside the native profile)")
        return subprocess.call([sys.executable, src] + program_args)
    if opts["stats"]:
        _print_stats(result)
    fd, exe = tempfile.mkstemp(prefix="vela_", suffix=".exe")
    os.close(fd)
    try:
        with open(exe, "wb") as f:
            f.write(result.exe)
        if opts["verbose"]:
            _err(f"vela: running native code ({len(result.exe)} bytes)")
        return subprocess.call([exe] + program_args)
    finally:
        try:
            os.unlink(exe)
        except OSError:
            pass


def cmd_bench(argv: list[str]) -> int:
    """Compile *and* run each given source once, printing timings."""
    pos, opts = _parse(argv)
    if not pos:
        _err("vela bench: expected at least one source file")
        return 2
    for src in pos:
        t0 = time.perf_counter()
        try:
            result = compile_file(src)
        except (VelaError, SyntaxError) as exc:
            _err(_format_error(exc, src))
            return 1
        t1 = time.perf_counter()
        fd, exe = tempfile.mkstemp(prefix="vela_bench_", suffix=".exe")
        os.close(fd)
        try:
            with open(exe, "wb") as f:
                f.write(result.exe)
            t2 = time.perf_counter()
            code = subprocess.call([exe])
            t3 = time.perf_counter()
        finally:
            try:
                os.unlink(exe)
            except OSError:
                pass
        print(f"{src}: compile {(t1 - t0) * 1000:.1f} ms, run {(t3 - t2) * 1000:.1f} ms, "
              f"image {len(result.exe)} bytes, exit {code}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.format(version=__version__))
        return 0
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "build":
        return cmd_build(rest)
    if cmd == "run":
        return cmd_run(rest)
    if cmd == "check":
        return cmd_check(rest)
    if cmd == "bench":
        return cmd_bench(rest)
    if cmd in ("version", "--version", "-V"):
        print(f"Vela {__version__} (Windows x86-64 native)")
        return 0
    _err(f"vela: unknown command '{cmd}'")
    print(USAGE.format(version=__version__), file=sys.stderr)
    return 2
