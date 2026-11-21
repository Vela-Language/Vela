"""Compilation pipeline: Python source -> PE32+ executable."""
from __future__ import annotations

import ast
import itertools
import time
from dataclasses import dataclass

from .analyze import Analyzer
from .emit import emit_program
from .errors import VelaError
from .ir import Program
from .lower import Lowerer
from . import inline
from . import opt
from .pe import IMAGE_BASE, DataBuilder, build_exe, layout_image
from .regalloc import allocate
from .runtime import Runtime, make_imports
from .x64 import Asm, XMM_POOL


@dataclass
class CompileStats:
    functions: int = 0
    blocks: int = 0
    instructions: int = 0
    vregs: int = 0
    code_bytes: int = 0
    image_bytes: int = 0
    leaf_functions: int = 0
    seconds: float = 0.0


@dataclass
class CompileResult:
    exe: bytes
    stats: CompileStats
    program: Program


def compile_source(source: str, filename: str = "<input>", boxed: bool = False) -> CompileResult:
    t0 = time.perf_counter()
    tree = ast.parse(source, filename)

    analyzer = Analyzer(tree, filename, boxed=boxed).run()

    rdata = DataBuilder()
    data = DataBuilder()
    imports = make_imports()
    runtime = Runtime(imports, rdata, data)

    lowerer = Lowerer(analyzer, rdata, filename, boxed=boxed)
    program = lowerer.run()
    inline.optimize(program)
    opt.optimize(program)

    asm = Asm()
    runtime.emit(asm)
    allocs = {}
    for fn in program.functions:
        allocs[fn.name] = allocate(fn)
    emit_program(asm, program.functions, allocs, runtime, itertools.count())

    # Entry stub is emitted last so that it stays contiguous in .text; entry
    # code that is split by other routines would fall into them on `ret`.
    asm.bind("_start")
    asm.and_rsp(-16)
    asm.sub_rsp(32)
    asm.call(runtime.RT_INIT)
    asm.call(program.main.name)
    asm.mov_ri("rcx", 0)
    asm.call_iat(("idata", imports.slot("kernel32.dll", "ExitProcess")))

    rdata_bytes = bytes(rdata.buf)
    data_bytes = bytes(data.buf)
    lay = layout_image(bytes(asm.b), rdata_bytes, data_bytes, imports)
    asm.resolve(IMAGE_BASE, lay.rvas)
    exe = build_exe(bytes(asm.b), rdata_bytes, data_bytes, imports,
                    entry_off=asm.labels["_start"])

    stats = CompileStats()
    stats.functions = len(program.functions)
    for fn in program.functions:
        alloc = allocs[fn.name]
        has_calls = any(ins.op in ("call", "print")
                        for block in fn.blocks for ins in block.instrs)
        uses_xmm = any(reg in XMM_POOL for reg in alloc.regs.values())
        if not has_calls and alloc.slot_count == 0 and not uses_xmm:
            stats.leaf_functions += 1
    stats.blocks = sum(len(fn.blocks) for fn in program.functions)
    stats.instructions = sum(len(b.instrs) for fn in program.functions for b in fn.blocks)
    stats.vregs = sum(fn.vreg_count for fn in program.functions)
    stats.code_bytes = len(asm.b)
    stats.image_bytes = len(exe)
    stats.seconds = time.perf_counter() - t0
    return CompileResult(exe=exe, stats=stats, program=program)


def compile_file(path: str, boxed: bool = False) -> CompileResult:
    # utf-8-sig: CPython's tokenizer accepts (and strips) a UTF-8 BOM.
    with open(path, "r", encoding="utf-8-sig") as f:
        source = f.read()
    return compile_source(source, path, boxed=boxed)


def analyze_source(source: str, filename: str = "<input>", boxed: bool = False) -> Analyzer:
    tree = ast.parse(source, filename)
    return Analyzer(tree, filename, boxed=boxed).run()
