"""Vela command line interface."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time

from . import __version__, i18n
from .driver import analyze_source, compile_file, compile_source
from .errors import UnsupportedError, VelaError
from .ir_dump import dump_program




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
            "verbose": False, "boxed": False, "args": []}
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
                raise SystemExit(i18n.t("output.requires"))
            opts["output"] = argv[i]
        elif a == "--strict":
            opts["strict"] = True
        elif a == "--boxed":
            opts["boxed"] = True
        elif a == "--stats":
            opts["stats"] = True
        elif a == "--emit-ir":
            opts["emit_ir"] = True
        elif a in ("-v", "--verbose"):
            opts["verbose"] = True
        elif a.startswith("-"):
            raise SystemExit(i18n.t("unknown.option", opt=a))
        else:
            positional.append(a)
        i += 1
    return positional, opts


def cmd_build(argv: list[str]) -> int:
    pos, opts = _parse(argv)
    if len(pos) != 1:
        _err(i18n.t("build.one_file"))
        return 2
    src = pos[0]
    try:
        result = compile_file(src, boxed=opts["boxed"])
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
        _err(i18n.t("build.ir", path=ir_path))
    print(i18n.t("build.built", path=out, size=len(result.exe)))
    return 0


def cmd_check(argv: list[str]) -> int:
    pos, opts = _parse(argv)
    if len(pos) != 1:
        _err(i18n.t("check.one_file"))
        return 2
    src = pos[0]
    with open(src, "r", encoding="utf-8-sig") as f:
        source = f.read()
    try:
        analyzer = analyze_source(source, src)
    except (VelaError, SyntaxError) as exc:
        _err(_format_error(exc, src))
        return 1
    print(i18n.t("check.ok", path=src))
    if opts["verbose"]:
        for name, info in analyzer.funcs.items():
            params = ", ".join(
                f"{p}: {t}" for p, t in zip(info.params, info.param_types))
            print(f"  def {name}({params}) -> {info.ret_type}")
        main_names = sorted(analyzer.main_vars)
        if main_names:
            print(i18n.t("check.globals") + ", ".join(
                f"{n}: {analyzer.main_vars[n]}" for n in main_names))
    return 0


def cmd_run(argv: list[str]) -> int:
    pos, opts = _parse(argv)
    if len(pos) != 1:
        _err(i18n.t("run.one_file"))
        return 2
    src = pos[0]
    program_args = opts["args"]
    try:
        result = compile_file(src, boxed=opts["boxed"])
    except SyntaxError as exc:
        _err(_format_error(exc, src))
        return 1
    except VelaError as exc:
        if opts["strict"]:
            _err(_format_error(exc, src))
            return 1
        _err(f"vela: {_format_error(exc, src)}")
        _err(i18n.t("run.fallback"))
        return subprocess.call([sys.executable, src] + program_args)
    if opts["stats"]:
        _print_stats(result)
    fd, exe = tempfile.mkstemp(prefix="vela_", suffix=".exe")
    os.close(fd)
    try:
        with open(exe, "wb") as f:
            f.write(result.exe)
        if opts["verbose"]:
            _err(i18n.t("run.native", size=len(result.exe)))
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
        _err(i18n.t("bench.one_file"))
        return 2
    for src in pos:
        t0 = time.perf_counter()
        try:
            result = compile_file(src, boxed=opts["boxed"])
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


def _extract_lang(argv: list[str]) -> list[str]:
    out: list[str] = []
    lang: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            out.extend(argv[i:])
            break
        if a == "--lang":
            if i + 1 >= len(argv):
                _err(i18n.t("lang.missing"))
                raise SystemExit(2)
            lang = argv[i + 1]
            i += 2
            continue
        if a.startswith("--lang="):
            lang = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    if lang is not None:
        try:
            i18n.set_lang(lang)
        except ValueError:
            _err(i18n.t("lang.invalid", lang=lang))
            raise SystemExit(2)
    return out


def main(argv: list[str] | None = None) -> int:
    # Bilingual output: keep UTF-8 even when stdout is a pipe or a legacy
    # console code page would otherwise mangle Chinese.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - non-standard streams
            pass
    argv = _extract_lang(list(sys.argv[1:] if argv is None else argv))
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(i18n.usage(__version__))
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
    _err(i18n.t("unknown.command", cmd=cmd))
    print(i18n.usage(__version__), file=sys.stderr)
    return 2
