"""Shared helpers for the Vela test suite."""
from __future__ import annotations

import atexit
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vela.driver import compile_source  # noqa: E402

TMPDIR = tempfile.mkdtemp(prefix="vela_tests_")
atexit.register(lambda: shutil.rmtree(TMPDIR, ignore_errors=True))
_counter = [0]


def native_exe(source: str, name: str = "case") -> str:
    _counter[0] += 1
    result = compile_source(source, f"{name}{_counter[0]}.py")
    path = os.path.join(TMPDIR, f"{name}{_counter[0]}.exe")
    with open(path, "wb") as f:
        f.write(result.exe)
    return path


def run_native(source: str, timeout: float = 20.0, name: str = "case"):
    path = native_exe(source, name)
    return subprocess.run([path], capture_output=True, timeout=timeout)


def run_python(source: str, timeout: float = 120.0):
    _counter[0] += 1
    path = os.path.join(TMPDIR, f"py{_counter[0]}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)
    return subprocess.run([sys.executable, path], capture_output=True, timeout=timeout)


def out_text(proc) -> str:
    return proc.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def exec_code(asm, restype, argtypes, *args):
    """Assemble `asm` and execute it through VirtualAlloc (unit tests only)."""
    asm.resolve(0, {})
    code = bytes(asm.b)
    k32 = ctypes.windll.kernel32
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_uint32, ctypes.c_uint32]
    addr = k32.VirtualAlloc(None, 4096, 0x3000, 0x40)
    ctypes.memmove(addr, code, len(code))
    fn = ctypes.CFUNCTYPE(restype, *argtypes)(addr)
    return fn(*args)


class VelaTestCase(unittest.TestCase):
    def assertOutput(self, source: str, expected: str, timeout: float = 20.0) -> None:
        proc = run_native(source, timeout=timeout)
        self.assertEqual(proc.returncode, 0,
                         f"exit {proc.returncode}, stderr={proc.stderr!r}")
        self.assertEqual(out_text(proc), expected)
